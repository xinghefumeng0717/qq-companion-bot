"""FIXES19 内心旁白滤网真实 API 冒烟 (scripts/smoke/smoke_fixes19.py)

单测全用手造文本，**证明不了两件事**：
1. 真模型在 S1 那种剧情下到底会不会吐内心旁白（不吐的话，滤网在生产里就是死代码）；
2. 滤网接进真实发送管道后，最终发出去的气泡里到底还有没有旁白（parse_reply 之后还有
   切句、表情包硬上限、fit_chunks 三步，中间任何一步都可能把滤网的结果搅乱）。

所以分两段验：
A 段（复现局）：用 FIXES18 的仿真器真跑一局 S1，交付 transcript，并断言
    **滤网拦截日志出现** 或 **全程无旁白行**——两种结果都算数：
    前者证明滤网接住了（真实病灶 + 真实兜底），后者证明这一局模型没犯病
    （卡内规则可能已经拦住了）。**不许只报好看的那种。**
B 段（确定性验证）：拿 S1 的真实翻车原句走完整条发送管道到 mock OneBot，
    验证最终发出去的气泡里没有旁白行。这一段是确定性的，不依赖模型是否犯病。

零服务器副作用、零生产库读写（本地临时库 + 本地 config.toml 的真实 key）。
报告落 data/smoke_fixes19_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.config import Config, ReplyConfig
from companion.db import TIME_FORMAT, Database
from companion.replier import Replier, is_inner_narration_line

REPORT_FILE = "data/smoke_fixes19_report.json"
SANDBOX_DB = "data/smoke_fixes19_sandbox.db"
DUO_SIM = os.path.join("scripts", "duo_sim.py")

# S1 局第 9 轮的真实翻车原句（整行形态），B 段直接拿它当必含旁白的输入
REAL_NARRATION = "这人嘴硬，我折回去看看。"
# 同一轮里紧挨着的两条正常气泡，B 段要验证它们逐字保留
NORMAL_LINES = [
    "行吧，饿着躺。",
    "我刚到楼门口，摸兜里还压着半块黑巧。",
]


class _MockStickers:
    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫"]


class _CapturingLogHandler(logging.Handler):
    """收集 companion.replier 的日志，用来证明滤网真的动手了。"""

    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self.records: List[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def narration_lines(self) -> List[str]:
        out = []
        for r in self.records:
            if "内心旁白兜底" in r.getMessage():
                out.append(r.getMessage())
        return out


# ==========================================
# B 段：确定性验证（不依赖模型是否犯病）
# ==========================================


async def section_b() -> Dict[str, Any]:
    """把 S1 真实翻车输出走完整条发送管道，验证最终气泡里没有旁白。"""
    if os.path.exists(SANDBOX_DB):
        os.remove(SANDBOX_DB)
    db = Database(SANDBOX_DB)
    await db.init_tables()

    replier = Replier(ReplyConfig(), _MockStickers())
    handler = _CapturingLogHandler()
    rlog = logging.getLogger("companion.replier")
    old_level, old_prop = rlog.level, rlog.propagate
    rlog.addHandler(handler)
    rlog.setLevel(logging.INFO)
    rlog.propagate = False

    # S1 那一轮的真实模型输出形态：两条正常气泡 + 一行内心旁白
    raw = "\n".join(NORMAL_LINES + [REAL_NARRATION])

    sent: List[Dict[str, Any]] = []

    async def fake_send(chunk: Dict[str, Any]) -> None:
        sent.append(dict(chunk))

    try:
        chunks, record = replier.parse_reply(raw, source="reply")
        # 再走一次真实发送管道（段间延迟用 no-op，避免冒烟干等几秒）
        import companion.replier as replier_mod

        real_asyncio = replier_mod.asyncio

        class _NoSleep:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            async def sleep(self, delay, *a, **kw):
                return await self._real.sleep(0)

        replier_mod.asyncio = _NoSleep(real_asyncio)
        try:
            await replier.send_reply_chunks(chunks, fake_send)
        finally:
            replier_mod.asyncio = real_asyncio
    finally:
        rlog.removeHandler(handler)
        rlog.setLevel(old_level)
        rlog.propagate = old_prop
        await db.close()

    sent_texts = [c.get("content", "") for c in sent if c.get("type") == "text"]
    joined = "\n".join(sent_texts)
    narration_log = handler.narration_lines()

    result = {
        "input": raw,
        "sent_bubbles": sent_texts,
        "record_text": record,
        "narration_log": narration_log,
        "filter_fired": bool(narration_log),
        "narration_reached_qq": any(
            is_inner_narration_line(t) for t in sent_texts
        ) or REAL_NARRATION in joined,
        "normals_preserved": all(n in joined for n in NORMAL_LINES),
        "empty_bubbles": [t for t in sent_texts if not t.strip()],
    }
    result["pass"] = (
        not result["narration_reached_qq"]
        and result["filter_fired"]
        and result["normals_preserved"]
        and not result["empty_bubbles"]
    )
    return result


# ==========================================
# A 段：复现局（真跑一局 S1）
# ==========================================


async def section_a() -> Dict[str, Any]:
    """用 duo_sim 真跑一局 S1，然后从 transcript 里逐行找旁白。"""
    run_id = f"S1-fixes19-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    cmd = [
        sys.executable, "-u", DUO_SIM,
        "--scene", "S1", "--turns", "15", "--run-id", run_id,
    ]
    print(f"  起仿真：{' '.join(cmd)}", flush=True)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        cwd=os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")),
    )
    out, _ = await proc.communicate()
    log_text = out.decode("utf-8", errors="replace")

    run_dir = os.path.join("data", "duo_sim", run_id)
    transcript = os.path.join(run_dir, "transcript.md")
    if not os.path.exists(transcript):
        return {
            "run_id": run_id, "ok": False,
            "error": f"仿真没产出 transcript（returncode={proc.returncode}）",
            "log_tail": log_text.splitlines()[-15:],
        }

    # 滤网是否在仿真里动过手
    fired = [ln for ln in log_text.splitlines() if "内心旁白兜底" in ln]

    # 逐行扫 transcript 的"她"那一列
    with open(transcript, "r", encoding="utf-8") as f:
        md = f.read()
    hits: List[Dict[str, Any]] = []
    her_turns = 0
    for line in md.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        idx, her = cells[0], cells[2] if cells[2] else cells[3]
        if len(cells) >= 4:
            her = cells[3]
        for seg in her.replace("<br>", "\n").split("\n"):
            seg = seg.strip()
            if not seg:
                continue
            her_turns += 1
            if is_inner_narration_line(seg):
                hits.append({"turn": int(idx), "line": seg})

    metrics_path = os.path.join(run_dir, "metrics.json")
    metrics = {}
    if os.path.exists(metrics_path):
        with open(metrics_path, "r", encoding="utf-8") as f:
            metrics = json.load(f)

    outcome = "滤网拦截过" if fired else ("模型本局没犯病" if not hits else "有旁白漏到台面！")
    return {
        "run_id": run_id,
        "ok": not hits,
        "outcome": outcome,
        "her_segments_scanned": her_turns,
        "narration_hits": hits,
        "filter_log_lines": fired,
        "turns": metrics.get("turns"),
        "cost_cny": metrics.get("cost_cny"),
        "transcript": transcript,
    }


# ==========================================


async def main() -> int:
    print("=" * 66)
    print("FIXES19 冒烟 · B 段：确定性验证（真实翻车原句走完整发送管道）")
    print("=" * 66, flush=True)
    b = await section_b()
    print(f"  输入：{b['input']!r}")
    print(f"  实发气泡：{b['sent_bubbles']}")
    print(f"  落库记录：{b['record_text']!r}")
    print(f"  滤网日志：{b['narration_log']}")
    print(f"  {'✓' if b['filter_fired'] else '✗'} 滤网动手了：{b['filter_fired']}")
    print(f"  {'✓' if not b['narration_reached_qq'] else '✗'} 旁白没到机主眼前：{not b['narration_reached_qq']}")
    print(f"  {'✓' if b['normals_preserved'] else '✗'} 正常气泡逐字保留：{b['normals_preserved']}")
    print(f"  {'✓' if not b['empty_bubbles'] else '✗'} 无空气泡：{not b['empty_bubbles']}")
    print(f"  B 段判定：{'PASS' if b['pass'] else 'FAIL'}")

    print()
    print("=" * 66)
    print("FIXES19 冒烟 · A 段：真跑一局 S1（复现局）")
    print("=" * 66, flush=True)
    a = await section_a()
    if not a.get("ok") and "error" in a:
        print(f"  [X] {a['error']}")
        for ln in a.get("log_tail", []):
            print("      " + ln)
    else:
        print(f"  产物：{a['transcript']}")
        print(f"  扫了她 {a['her_segments_scanned']} 条气泡")
        print(f"  本局结果：**{a['outcome']}**")
        if a["filter_log_lines"]:
            print("  滤网日志原文：")
            for ln in a["filter_log_lines"]:
                print("      " + ln)
        if a["narration_hits"]:
            print("  [X] 仍有旁白行上了台面：")
            for h in a["narration_hits"]:
                print(f"      第{h['turn']}轮 {h['line']}")
        else:
            print("  ✓ 最终 transcript 里没有任何旁白行")
        print(f"  成本 ¥{a.get('cost_cny')}")

    report = {
        "generated_at": datetime.now().strftime(TIME_FORMAT),
        "A_reproduction_run": a,
        "B_deterministic": b,
        "pass": bool(b["pass"] and a.get("ok")),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 66)
    print(f"A 段（复现局）：{'PASS' if a.get('ok') else 'FAIL'} — {a.get('outcome', a.get('error'))}")
    print(f"B 段（确定性）：{'PASS' if b['pass'] else 'FAIL'}")
    print(f"报告：{REPORT_FILE}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
