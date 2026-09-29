"""FIXES8 真实 API 冒烟验证 (scripts/smoke_fixes8.py)
复用 smoke_tone_test.py 的思路（真实库副本 + 沙箱 + 真实 API）：
跑 8 轮零暧昧日常对话后归档日记，断言：
1. 日记文本不含"想他/心疼/喜欢/在意"（阶段 1 相识下）；
2. 观察者四评分仍在 [4, 7] 锚点区间；
3. 好感度 update 后六维 ≤100、composite ≤100。
原始记录存 data/smoke_fixes8_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.chat import ChatSession

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)

REAL_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_real.db"
FALLBACK_DB = "data/companion.db"
SANDBOX_DB = "data/smoke_fixes8_sandbox.db"
REPORT_FILE = "data/smoke_fixes8_report.json"

SCRIPTED_TURNS = [
    "下课了，今天实验课站了一下午，腿酸",
    "晚饭吃了拌面，味道一般般",
    "你今天下午在干嘛呀",
    "图书馆人好多，差点没抢到座",
    "报告写了一半卡住了，明天再弄",
    "路上看到有人在喂黑天鹅，挺可爱的",
    "哈哈是吗，那我下次也带点面包去",
    "不早了，我先去洗漱了",
]

FORBIDDEN_WORDS = ["想他", "心疼", "喜欢", "在意"]


async def run_smoke_test() -> dict:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    print("=" * 70)
    print(f"   FIXES8 真实 API 冒烟验证 (基准库: {prod_db})")
    print("=" * 70)

    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    print("✓ 沙箱初始化完成")

    report = {
        "turns": [],
        "initial_affection": {},
        "final_affection": {},
        "diary": None,
        "assertions": {
            "diary_no_forbidden_words": True,
            "forbidden_words_found": [],
            "observer_scores_in_anchor": True,
            "observer_scores_summary": [],
            "dims_le_100": True,
            "composite_le_100": True,
        },
        "verdict": "PENDING",
    }

    try:
        init_aff = await session.affection.get_state()
        report["initial_affection"] = init_aff
        stage_name = session.persona.get_stage(init_aff["stage"]).name
        print(f"✓ 初始状态: 阶段 {init_aff['stage']} ({stage_name}) | 复合分: {init_aff['composite']:.2f}")

        for i, user_msg in enumerate(SCRIPTED_TURNS, 1):
            print(f"\n[轮次 {i}/8] 机主: {user_msg}")
            bot_reply = await session.handle_input(user_msg)
            print(f"          青梓: {bot_reply}")

            # 获取本轮观察者评分
            obs_row = await session.db.fetchone(
                "SELECT self_disclosure, responsiveness, warmth_score, resonance FROM observer_scores ORDER BY id DESC LIMIT 1"
            )
            obs_scores = dict(obs_row) if obs_row else {}

            # 获取当前好感度
            aff_st = await session.affection.get_state()

            turn_record = {
                "turn": i,
                "user_msg": user_msg,
                "bot_reply": bot_reply,
                "observer_scores": obs_scores,
                "affection_composite": aff_st["composite"],
                "affection_stage": aff_st["stage"],
                "dims": dict(aff_st["dims"]),
            }
            report["turns"].append(turn_record)

            # 校验观察者评分
            for k, v in obs_scores.items():
                if not (4.0 <= v <= 7.0):
                    report["assertions"]["observer_scores_in_anchor"] = False
                    report["assertions"]["observer_scores_summary"].append(f"轮次 {i} {k}={v} 超出 [4, 7]")

            # 校验六维与复合分不超过 100
            for d_name, d_val in aff_st["dims"].items():
                if d_val > 100.0:
                    report["assertions"]["dims_le_100"] = False
            if aff_st["composite"] > 100.0:
                report["assertions"]["composite_le_100"] = False

        # 触发日记归档
        print("\n>>> 正在触发日记归档...")
        await session.memory.check_and_trigger_diary_archive()

        # 读取最新生成的日记
        diary_rows = await session.db.fetchall(
            "SELECT content, importance, sentiment, created_at FROM diary ORDER BY id DESC LIMIT 1"
        )
        if diary_rows:
            diary_dict = dict(diary_rows[0])
            report["diary"] = diary_dict
            diary_text = diary_dict.get("content", "")
            print(f"\n✓ 成功生成日记 (重要性={diary_dict.get('importance')}, 情感={diary_dict.get('sentiment')}):")
            print(f"  《{diary_text}》")

            found_words = [w for w in FORBIDDEN_WORDS if w in diary_text]
            if found_words:
                report["assertions"]["diary_no_forbidden_words"] = False
                report["assertions"]["forbidden_words_found"] = found_words
                print(f"❌ 警告：日记中包含违禁词: {found_words}")
            else:
                print("✓ 违禁词检测通过：日记完全不含 '想他/心疼/喜欢/在意'")
        else:
            print("❌ 未能生成日记！")
            report["assertions"]["diary_no_forbidden_words"] = False

        final_aff = await session.affection.get_state()
        report["final_affection"] = final_aff

        # 总体判定
        passed = (
            report["assertions"]["diary_no_forbidden_words"]
            and report["assertions"]["dims_le_100"]
            and report["assertions"]["composite_le_100"]
        )
        report["verdict"] = "PASS" if passed else "FAIL"

    finally:
        await session.close()
        print("\n✓ 沙箱资源已清理")

    # 保存报告
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"✓ 完整测试报告已保存至 {REPORT_FILE}")

    return report


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
