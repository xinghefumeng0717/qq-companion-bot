"""真实 API 的连贯性小样本（不接触生产库）。"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.config import Config
from companion.gateway import LLMGateway
from companion.reply_check import check_continuity


CASES = [
    ("好-短安慰", "今天累死了", ["先歇会儿，饭吃了没", "我这边新开了个铺子"], [], False),
    ("好-自然收尾", "嗯 晚安", ["嗯，晚安", "今天天气不错"], [], False),
    ("好-承认不知道", "你猜我在哪", ["不知道，猜不着", "到舰桥了"], [], False),
    ("好-自然反问", "给我讲讲昨天那家店", ["哪家？我记性可没你好", "到舰桥了"], [], False),
    ("好-口语调侃", "我最近老忘事", ["谁没点忘事的时候，别老说自己", "到舰桥了"], [], False),
    ("坏-答非所问", "今天累死了", ["我这边新开了个铺子", "先歇会儿，饭吃了没"], [], True),
    ("坏-没有接最新一句", "你吃了吗", ["今天天气不错", "吃了，你呢"], [], True),
    ("坏-把计划说成到达", "昨天我说要去帝江号，现在我到哪了", ["到舰桥了", "还在路上"], [
        {"role": "assistant", "content": "我这边正好要往帝江号送东西", "created_at": "2026-10-24 12:00"}
    ], True),
    ("坏-半截病句", "把这段话接完整：因为下雨了", ["因为下雨了，所以我就", "因为下雨了，所以记得带伞"], [], True),
    ("坏-否认自己说过的话", "我刚说我很忙", ["我什么时候说你忙了", "我只是问你闲不闲"], [
        {"role": "assistant", "content": "在忙？", "created_at": "2026-10-24 12:00"}
    ], True),
]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.toml")
    args = parser.parse_args()
    gateway = LLMGateway(Config.load(args.config).llm)
    good_modified = 0
    bad_caught = 0
    rows = []
    try:
        for name, his, candidates, history, is_bad in CASES:
            out, info = await check_continuity(gateway, his, candidates, 0, history=history)
            changed = out != candidates[0]
            caught = bool(info.get("changed") or info.get("issues") or info.get("mode") != "keep")
            if is_bad:
                bad_caught += int(caught)
            else:
                good_modified += int(changed)
            rows.append({"name": name, "changed": changed, "mode": info.get("mode"), "caught": caught})
            print(f"{name} -> {info.get('mode')} / changed={changed} / caught={caught} / {out[:60]}")
    finally:
        await gateway.close()
    print(f"SUMMARY good_modified={good_modified} bad_caught={bad_caught}/5")
    return 1 if good_modified or bad_caught < 4 else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
