"""阶段区分度：把几个阶段的回复混在一起，让评审盲猜"这条像哪个关系阶段说的" (scripts/sim/stage_guess.py)

猜对率高 = 阶段在语气里有体现；接近乱猜（1/阶段数）= 阶段规则没体现出来。
用法：venv/bin/python scripts/sim/stage_guess.py 相识=<目录> 依恋=<目录> [...]
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import sys
import tempfile
from collections import Counter, defaultdict

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.config import Config  # noqa: E402
from companion.db import Database  # noqa: E402
from companion.gateway import LLMGateway, parse_llm_json  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
STAGE_DESC = {
    "初识": "刚加上好友，礼貌客气",
    "相识": "认识不久，客气温和，有分寸",
    "熟络": "混熟了，随意轻松，会开玩笑",
    "同好": "有共同爱好的好朋友",
    "知己": "很信任的知心朋友，会说心事",
    "微酸": "互有好感但没挑明，有点在意、偶尔吃醋",
    "倾心": "喜欢上了对方，会害羞、会想他",
    "依恋": "很亲密，会撒娇、示弱、主动要陪伴",
}


async def main() -> None:
    pairs = [a.split("=", 1) for a in sys.argv[1:]]
    stages = [name for name, _ in pairs]
    os.chdir(ROOT)
    probes = {p["id"]: p for p in json.load(open("data/benchmark_v5/probes.json", encoding="utf-8"))}
    items = []
    for name, d in pairs:
        for c in json.load(open(os.path.join(d, "raw.json"), encoding="utf-8"))["cells"]:
            if "reply" in c and c["reply"] != "[沉默]":
                items.append((name, c["probe"], c["reply"]))
    options = "\n".join(f"- {s}：{STAGE_DESC.get(s, '')}" for s in stages)
    system = (
        "下面是一个大学女生在 QQ 上回一个男生的消息。只根据她这条回复的语气，"
        f"判断两人现在最可能是哪种关系：\n{options}\n"
        f'输出 JSON：{{"stage": "{stages[0]}/…其中之一"}}'
    )
    cfg = Config.load()
    tmp = tempfile.mkdtemp(prefix="stage_guess_")
    db = Database(os.path.join(tmp, "j.db"))
    await db.init_tables()
    gw = LLMGateway(cfg.llm, db)
    sem = asyncio.Semaphore(8)
    conf = defaultdict(Counter)

    async def one(truth, pid, reply):
        async with sem:
            try:
                d = parse_llm_json(await gw.chat(
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": f"他：{probes[pid]['his']}\n她：{reply}"}],
                    model="deepseek-v4-pro", temperature=0.0, json_mode=True, purpose="stage_guess"))
                guess = str(d.get("stage", "")).strip()
            except Exception:
                guess = "?"
        conf[truth][guess if guess in stages else "?"] += 1

    random.shuffle(items)
    await asyncio.gather(*[one(*x) for x in items])
    row = await db.fetchone("SELECT SUM(cost_estimate) c FROM llm_calls")
    await gw.close()
    await db.close()
    total = sum(sum(c.values()) for c in conf.values())
    right = sum(conf[s][s] for s in stages)
    print(f"猜对 {right}/{total} = {100 * right / max(total, 1):.1f}%（乱猜 {100 / len(stages):.0f}%）；费用 ¥{float(row['c'] or 0):.3f}")
    for s in stages:
        print(f"  真={s}: " + "  ".join(f"{g}{conf[s][g]}" for g in stages + ["?"] if conf[s][g]))


if __name__ == "__main__":
    asyncio.run(main())
