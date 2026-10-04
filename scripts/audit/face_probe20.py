"""FIXES20 附加取证：发侧直调探针（scripts/audit/face_probe20.py）

现象：两局对聊仿真（S1 喊累 15 轮、S2 分享开心事 15 轮）她**一次都没发过 [face:]**，
但 A 段直调里出现过 2 次（挂 [流汗]）。到底是"机制没生效"、"提示词太弱"，
还是"被上下文带偏"，必须分层取证，不能靠猜——否则下一轮就会去改错的东西。

本探针绕过仿真器，**直接对生成层**打四条探针（真实 API，¥0.05 以内）：
  P1 他带脸 + 情绪      "我今天被老师骂了[捂脸]"
  P2 同样的话但他不带脸  "我今天被老师骂了"
  P3 他调侃她（最容易接脸的一条）
  P4 纯开心事（最容易用 [庆祝]/[耶] 的一条）
P1 vs P2 回答"她是不是跟着他的用法走"；P3/P4 回答"她到底会不会主动用"。

只统计 `[face:...]` 原文出现次数与形态，**不判语气好坏**（那是所有者的活）。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.chat import ChatSession

SANDBOX_DB = "data/face_probe20_sandbox.db"
REAL_DB = "data/companion.db"
FALLBACK_DB = "data/companion_example.db"
REPORT_FILE = "data/face_probe20_report.json"
FACE_TOKEN = re.compile(r"\[face[:：]([^\]]+)\]")

PROBES: List[Dict[str, Any]] = [
    {"key": "P1", "name": "他带脸+情绪", "text": "我今天被老师骂了[捂脸]"},
    {"key": "P2", "name": "同样的话但他不带脸", "text": "我今天被老师骂了"},
    {"key": "P3", "name": "他调侃她（最容易接脸）", "text": "你今天说话怎么阴阳怪气的[doge]"},
    {"key": "P4", "name": "纯开心事（最容易庆祝）", "text": "我那个实验终于跑出来了，数据比预期好"},
]


async def main() -> int:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    results: List[Dict[str, Any]] = []
    try:
        for probe in PROBES:
            messages, _ = await session.assembler.assemble_messages(probe["text"])
            model = (
                session.config.llm.active().chat
                if hasattr(session.config.llm, "active")
                else getattr(session.config.llm, "chat_model", "deepseek-chat")
            )
            parts: List[str] = []
            async for piece in session.gateway.stream_chat(
                messages=messages, model=model, purpose="main_chat"
            ):
                parts.append(piece)
            raw = "".join(parts).strip()
            chunks, record = session.replier.parse_reply(raw)
            tags = FACE_TOKEN.findall(raw)
            results.append({
                "key": probe["key"],
                "name": probe["name"],
                "he_said": probe["text"],
                "her_raw": raw,
                "face_tags": tags,
                "face_n": len(tags),
                "chunk_types": [c["type"] for c in chunks],
                "record": record,
            })
            r = results[-1]
            print(f"  · {r['key']} {r['name']}：他说 {r['he_said']!r}")
            print(f"      她回 {r['her_raw']!r}")
            print(f"      [face: 数]={r['face_n']} {r['face_tags']} | 段型 {r['chunk_types']}")
    finally:
        await session.close()

    with_face = [r for r in results if r["face_n"] > 0]
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
        "probes_with_face": len(with_face),
        "total_probes": len(results),
        "conclusion_hint": (
            f"{len(with_face)}/{len(results)} 条探针用上了脸；"
            "P1>P2 说明她跟着他的用法走，P3/P4 为 0 说明她不会主动用"
        ),
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print()
    print(f"  用上脸的探针：{len(with_face)}/{len(results)}")
    print(f"  报告：{REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
