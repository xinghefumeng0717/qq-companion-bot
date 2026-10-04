"""暧昧度实测冒烟 (scripts/smoke/smoke_tone_test.py)
拉取服务器真实数据库副本，在沙箱中用真实 API 分两阶段测青梓的语气：
  阶段 1「相识」原样 8 轮 → 归档日记
  人为提到阶段 2「熟络」再 8 轮 → 再归档日记
核心问题：机主不主动暧昧时，她会不会自己往上贴？
对生产零副作用（操作临时副本），成本约 34 次调用（flash 为主）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.chat import ChatSession

logging.basicConfig(level=logging.WARNING)

REAL_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_real.db"
SANDBOX_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_tone_sandbox.db"

PHASE1_MSGS = [
    "下课了，今天实验课站了一下午，腿酸",
    "晚饭吃了拌面，味道一般般",
    "你今天下午在干嘛呀",
    "图书馆人好多，差点没抢到座",
    "报告写了一半卡住了，明天再弄",
    "路上看到有人在喂黑天鹅，挺可爱的",
    "哈哈是吗，那我下次也带点面包去",
    "不早了，我先去洗漱了",
]

PHASE2_MSGS = [
    "早上好，今天满课，痛苦",
    "上午的课困死我了，全靠咖啡续命",
    "中午还是吃的拌面，这次加了辣",
    "下午要在自习室待一下午了",
    "你今天练琴了吗",
    "羡慕，我五音不全，只会听",
    "明天终于没课了，打算睡到自然醒",
    "有点困了，准备睡了，晚安",
]


async def run_phase(session: ChatSession, msgs: list[str], phase_name: str) -> dict:
    print("\n" + "=" * 70)
    print(f"【{phase_name}】")
    print("=" * 70)
    replies = []
    for i, msg in enumerate(msgs, 1):
        reply = await session.handle_input(msg)
        replies.append((msg, reply))
        print(f"\n[{i}/8] 机主: {msg}")
        print(f"      青梓: {reply}")

    # 触发日记归档
    await session.memory.check_and_trigger_diary_archive()
    diaries = await session.db.fetchall(
        "SELECT content, importance, sentiment, created_at FROM diary ORDER BY id DESC LIMIT 1"
    )
    diary = dict(diaries[0]) if diaries else None
    print(f"\n  >> 归档日记: {diary}")
    return {"replies": replies, "diary": diary}


async def main() -> None:
    print("=" * 70)
    print("   青梓暧昧度实测 · 真实数据库副本 + 真实 API")
    print("=" * 70)

    session = ChatSession(prod_db_path=REAL_DB, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    print("✓ 沙箱已基于服务器真实库初始化")

    try:
        aff = await session.affection.get_state()
        print(f"✓ 当前状态: 复合分 {aff['composite']} 阶段 {aff['stage']} ({session.persona.get_stage(aff['stage']).name})")

        # 阶段 1：相识（原样）
        r1 = await run_phase(session, PHASE1_MSGS, "阶段一：相识（现状原样）")

        # 人为抬到阶段 2 熟络（复合分 ≥31）：温和地上调各维
        st = await session.affection.get_state()
        dims = st["dims"]
        dims.update({"warmth": 32.0, "trust": 30.0, "intimacy": 28.0, "intrigue": 32.0, "patience": 40.0})
        st["composite"] = None  # 让引擎下轮自算；这里直接手写正确值更稳
        from companion.affection import calc_composite_score, determine_stage
        st["composite"] = calc_composite_score(dims)
        st["stage"] = determine_stage(st["composite"])
        await session.affection.save_state(st)
        print(f"\n>>> 已人为调整至阶段 {st['stage']} (复合分 {st['composite']:.1f}) <<<")

        # 阶段 2：熟络
        r2 = await run_phase(session, PHASE2_MSGS, "阶段二：熟络（人为抬阶）")

        # 汇总
        print("\n" + "=" * 70)
        print("【汇总】")
        print("=" * 70)
        print(f"相识阶段日记: {r1['diary']}")
        print(f"熟络阶段日记: {r2['diary']}")
    finally:
        await session.close()
        print("\n✓ 沙箱已清理")


if __name__ == "__main__":
    asyncio.run(main())
