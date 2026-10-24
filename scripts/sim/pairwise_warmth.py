"""成对盲评：同一道题、同一阶段，功能关 vs 开，哪条更亲近、哪条更像真人 (scripts/sim/pairwise_warmth.py)

规则指标（字数、语气词）说不清"冷不冷"，这里让评审模型逐对盲判：
两条回复随机排成 A/B，评审不知道哪条开了功能。按 (题号, 第几次) 配对两份 benchmark_v5 的 raw.json。

用法：
  venv/bin/python scripts/sim/pairwise_warmth.py --off <关的产物目录> --on <开的产物目录> --stage-name 依恋
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import tempfile
from collections import Counter, defaultdict
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.config import Config  # noqa: E402
from companion.db import Database  # noqa: E402
from companion.gateway import LLMGateway, parse_llm_json  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROBES_FILE = os.path.join(ROOT, "data", "benchmark_v5", "probes.json")

JUDGE = """你在比较两条 QQ 私聊回复。聊天的是一对大学生（男生发消息，女生回复），两人现在的关系是「{stage}」。
只看女生的两条候选回复 A 和 B，回答两个问题：
1. warmer：哪条对他更亲近、更有温度？（"A" / "B" / "差不多"）
2. human：放在这个关系里，哪条更像真人女生会打出来的？（"A" / "B" / "差不多"）
标记说明：[face:X] 是 QQ 小表情，[表情:X] 是表情包，[沉默] 是不回。
输出 JSON：{{"warmer": "...", "human": "...", "why": "一句话"}}"""


def _norm(v: Any) -> str:
    v = str(v or "").strip().upper()
    return v if v in ("A", "B") else "="


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--off", required=True)
    ap.add_argument("--on", required=True)
    ap.add_argument("--stage-name", default="相识")
    ap.add_argument("--judge-model", default="deepseek-v4-pro")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    os.chdir(ROOT)

    probes = {p["id"]: p for p in json.load(open(PROBES_FILE, encoding="utf-8"))}
    groups: Dict[str, Dict[Any, List[str]]] = {}
    for tag, d in (("off", args.off), ("on", args.on)):
        g: Dict[Any, List[str]] = defaultdict(list)
        for c in json.load(open(os.path.join(d, "raw.json"), encoding="utf-8"))["cells"]:
            if "reply" in c:
                g[c["probe"]].append(c["reply"])
        groups[tag] = g
    pairs = [(pid, k, groups["off"][pid][k], groups["on"][pid][k])
             for pid in groups["off"] for k in range(min(len(groups["off"][pid]), len(groups["on"].get(pid, []))))]

    cfg = Config.load()
    tmp = tempfile.mkdtemp(prefix="pairwise_")
    db = Database(os.path.join(tmp, "judge.db"))
    await db.init_tables()
    gw = LLMGateway(cfg.llm, db)
    sem = asyncio.Semaphore(args.concurrency)
    rng = random.Random(0)
    results: List[Dict[str, Any]] = []

    async def judge(pid, k, off, on):
        swap = rng.random() < 0.5
        a, b = (on, off) if swap else (off, on)
        p = probes.get(pid, {})
        ctx = "\n".join(f"{t['who']}：{t['text']}" for t in p.get("context", [])[-4:])
        user = (f"【前面的聊天】\n{ctx}\n\n" if ctx else "") + f"【他刚发】{p.get('his', '')}\n\n【A】{a}\n\n【B】{b}"
        async with sem:
            try:
                resp = await gw.chat(
                    messages=[{"role": "system", "content": JUDGE.format(stage=args.stage_name)},
                              {"role": "user", "content": user}],
                    model=args.judge_model, temperature=0.0, json_mode=True, purpose="pairwise_judge",
                )
                d = parse_llm_json(resp)
            except Exception as e:
                d = {"error": str(e)}
        back = {"A": "on", "B": "off"} if swap else {"A": "off", "B": "on"}
        results.append({
            "probe": pid, "k": k, "off": off, "on": on,
            "warmer": back.get(_norm(d.get("warmer")), "="),
            "human": back.get(_norm(d.get("human")), "="),
            "why": d.get("why", d.get("error", "")),
        })

    await asyncio.gather(*[judge(*x) for x in pairs])
    row = await db.fetchone("SELECT SUM(cost_estimate) c FROM llm_calls")
    await gw.close()
    await db.close()

    warm = Counter(r["warmer"] for r in results)
    human = Counter(r["human"] for r in results)
    n = len(results) or 1
    summary = {
        "stage": args.stage_name, "pairs": len(results),
        "更亲近": {"关": warm["off"], "开": warm["on"], "差不多": warm["="]},
        "更像真人": {"关": human["off"], "开": human["on"], "差不多": human["="]},
        "开更冷的比例%": round(100 * warm["off"] / n, 1),
        "cost": float(row["c"] or 0.0) if row else 0.0,
    }
    out = args.out or os.path.join(args.on, "pairwise.json")
    json.dump({"summary": summary, "results": sorted(results, key=lambda r: (str(r["probe"]), r["k"]))},
              open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
