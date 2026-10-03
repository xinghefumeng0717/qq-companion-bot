"""FIXES11 真实 API 冒烟验证 (scripts/smoke_fixes11.py)

复用 smoke_fixes8.py 的思路：真实生产库副本 → 沙箱 → 本地 config.toml 的真实 key，
零服务器副作用。跑三段，每段都对应一条生产证据：

A. E1（任务1 主动消息注入近期历史）：
   构造"他昨晚说坐动车回家（并且票已买好）"的历史 + 一条永不更新的旧事实
   "国庆期间打算坐动车回家"，触发一次主动消息生成，
   断言产出**不含**"买票/买好了没"这类把已发生的事当没发生的重复询问。
B. E3（任务2 节假日感知）：
   把今天填进 holidays（[llm.pricing].holidays 的行为层读取路径），
   造一条"国庆放假在家"的近期历史后再触发一次主动消息，
   断言产出**不含**"上课/下课/专业课"这类假期里不成立的说法。
C. E4（任务3 facts 作废通路）：
   再走一轮真实对话（用户说"票早就买好了、我昨天到家了"），
   打印观察者原始 updated_facts 输出与 facts 表前后对比。

报告落 data/smoke_fixes11_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.chat import ChatSession
from companion.config import ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT
from companion.observer import recent_observer_logs
from companion.proactive import ProactiveScheduler
from companion.replier import Replier

logging.basicConfig(level=logging.WARNING)

# 冒烟把主动消息的时钟固定到今天 15:00：真实时间此刻是凌晨，
# 决策层会（合理地）选 B 短路掉生成层，那样就验证不到本次修的东西。
FAKE_NOW = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)


class _FakeDatetime(datetime):
    """只改 now()，strptime 等照常——供 patch('companion.proactive.datetime') 使用"""

    @classmethod
    def now(cls, tz=None):
        return FAKE_NOW if tz is None else FAKE_NOW.astimezone(tz)

REAL_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_real.db"
FALLBACK_DB = "data/companion.db"
SANDBOX_DB = "data/smoke_fixes11_sandbox.db"
REPORT_FILE = "data/smoke_fixes11_report.json"

# A 段：把"已经发生"写成近期历史
E1_USER = "我昨天已经坐动车到家啦，票上周就买好了"
E1_BOT = "那就好，回去路上没堵车吧"
STALE_FACT = "国庆期间打算坐动车回家"

# B 段：假期场景
E3_USER = "国庆放假在家躺尸，图书馆都关门了"
E3_BOT = "那这几天是纯放养了"

# 任务1/2 的负向断言词
E1_FORBIDDEN = ["买票", "票买", "买好了", "买到票", "票买好了没", "买到没", "买好票"]
E3_FORBIDDEN = ["上课", "下课", "专业课", "上午的课", "刚下课"]


async def _seed_turn(db, user_msg: str, bot_msg: str, at: datetime) -> None:
    ts = at.strftime(TIME_FORMAT)
    await db.execute(
        "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES ('user', ?, 0, 0, ?)",
        (user_msg, ts),
    )
    await db.execute(
        "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES ('assistant', ?, 0, 0, ?)",
        (bot_msg, ts),
    )


def _sent_text(chunks) -> str:
    return "\n".join(
        c["content"] if c["type"] == "text" else f"[表情:{c.get('desc', '')}]" for c in chunks
    )


async def run_smoke_test() -> dict:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    print("=" * 70)
    print(f"   FIXES11 真实 API 冒烟验证 (基准库: {prod_db})")
    print("=" * 70)

    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    print("✓ 沙箱初始化完成（真实 key，无服务器副作用）")

    report = {
        "base_db": prod_db,
        "stage": None,
        "clock_note": "A/B 段把 companion.proactive 的时钟挪到今天 15:00（凌晨决策层会以'别打扰'选 B 短路生成层）",
        "A_e1_recent_chat": {},
        "B_e3_holiday": {},
        "C_e4_supersede": {},
        "verdict": "PENDING",
    }

    sent: list = []
    llm_calls: list = []

    async def collect(chunk):
        sent.append(chunk)

    # 决策层原始输出留证（它选 A/B/C 是模型的自由意志，冒烟只如实记录）
    _real_chat = session.gateway.chat

    async def spy_chat(*args, **kwargs):
        resp = await _real_chat(*args, **kwargs)
        llm_calls.append({"purpose": kwargs.get("purpose"), "response": str(resp)[:300]})
        return resp

    session.gateway.chat = spy_chat

    # 零延迟分段发送（只影响发送节奏，不影响生成内容）
    replier = Replier(ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0), session.stickers)
    scheduler = ProactiveScheduler(
        # 只放开免打扰时段闸门：现在是凌晨，生产的 [0,8] 会直接拦掉本次冒烟
        config=ProactiveConfig(enabled=True, quiet_hours=[]),
        persona=session.persona,
        affection=session.affection,
        mood=session.mood,
        memory=session.memory,
        stickers=session.stickers,
        replier=replier,
        gateway=session.gateway,
        db=session.db,
        send_msg_fn=collect,
        assembler=session.assembler,
        holidays_provider=session.config.get_holidays,
    )

    try:
        aff = await session.affection.get_state()
        stage = int(aff.get("stage", 0))
        report["stage"] = stage
        print(f"✓ 当前状态: 阶段 {stage}（{session.persona.get_stage(stage).name}）| 复合分 {aff.get('composite')}")

        # ---------- A 段：E1 近期历史注入 ----------
        print("\n" + "-" * 70)
        print("A 段（任务1 / E1）：注入「他已坐动车到家」近期历史后触发主动消息")
        print(f"  时钟：{FAKE_NOW:%Y-%m-%d %H:%M}（{report['clock_note']}）")
        print("-" * 70)
        llm_calls.clear()
        with patch("companion.proactive.datetime", _FakeDatetime):
            await _seed_turn(session.db, E1_USER, E1_BOT, FAKE_NOW - timedelta(hours=3))
            await session.memory.add_fact(STALE_FACT)  # E4 的"永不更新"旧事实
            facts_before = await session.memory.get_all_facts()
            print(f"  种子历史: {E1_USER} / {E1_BOT}（{FAKE_NOW - timedelta(hours=3):%H:%M}）")
            print(f"  旧事实仍在库: {STALE_FACT}")

            sent.clear()
            await scheduler.trigger_cycle()
        a_text = _sent_text(sent)
        a_found = [w for w in E1_FORBIDDEN if w in a_text]
        a_decision = next((c for c in llm_calls if c["purpose"] == "proactive_decision"), None)
        report["A_e1_recent_chat"] = {
            "clock": FAKE_NOW.strftime(TIME_FORMAT),
            "seed_user": E1_USER,
            "seed_bot": E1_BOT,
            "stale_fact": STALE_FACT,
            "decision_raw": a_decision,
            "sent_text": a_text,
            "sent_chunks": [dict(c) for c in sent],
            "forbidden_found": a_found,
            "mentions_arrived": ("到家" in a_text or "回来" in a_text or "路上" in a_text),
            "pass": bool(a_text) and not a_found,
        }
        print(f"  决策层输出: {a_decision['response'] if a_decision else '（未调用）'}")
        print(f"  主动消息产出: {a_text if a_text else '（未发送）'}")
        print(f"  违禁词命中: {a_found or '无'}")
        print(f"  判定: {'PASS' if report['A_e1_recent_chat']['pass'] else 'FAIL'}")

        # ---------- B 段：E3 节假日感知 ----------
        print("\n" + "-" * 70)
        print("B 段（任务2 / E3）：把今天填进 holidays 后触发主动消息")
        print("-" * 70)
        today = datetime.now().strftime("%Y-%m-%d")
        scheduler._holidays_provider = lambda: [today]
        session.assembler._holidays_provider = lambda: [today]
        llm_calls.clear()
        with patch("companion.proactive.datetime", _FakeDatetime):
            await _seed_turn(
                session.db, E3_USER, E3_BOT, FAKE_NOW - timedelta(hours=1, minutes=30)
            )
            print(f"  holidays = [{today}]（Config.get_holidays 读的就是 [llm.pricing].holidays）")
            print(f"  种子历史: {E3_USER} / {E3_BOT}（{FAKE_NOW - timedelta(hours=1, minutes=30):%H:%M}）")

            await scheduler.reset_unanswered_count()  # 清掉 A 段留下的未回复计数，否则闸门 #4 拦下
            sent.clear()
            await scheduler.trigger_cycle()
        b_text = _sent_text(sent)
        b_found = [w for w in E3_FORBIDDEN if w in b_text]
        b_decision = next((c for c in llm_calls if c["purpose"] == "proactive_decision"), None)
        report["B_e3_holiday"] = {
            "clock": FAKE_NOW.strftime(TIME_FORMAT),
            "holidays": [today],
            "seed_user": E3_USER,
            "decision_raw": b_decision,
            "sent_text": b_text,
            "sent_chunks": [dict(c) for c in sent],
            "forbidden_found": b_found,
            "pass": bool(b_text) and not b_found,
        }
        print(f"  决策层输出: {b_decision['response'] if b_decision else '（未调用）'}")
        print(f"  主动消息产出: {b_text if b_text else '（未发送）'}")
        print(f"  假期违禁词命中: {b_found or '无'}")
        print(f"  判定: {'PASS' if report['B_e3_holiday']['pass'] else 'FAIL'}")

        # ---------- C 段：E4 facts 作废通路 ----------
        print("\n" + "-" * 70)
        print("C 段（任务3 / E4）：真实一轮对话，看观察者是否作废旧事实")
        print("-" * 70)
        facts_pre = await session.memory.get_all_facts()
        c_user = "我昨天已经到家啦，票上周就买好了，不用操心"
        bot_reply = await session.handle_input(c_user)
        facts_post = await session.memory.get_all_facts()
        last_obs = dict(recent_observer_logs[-1]) if recent_observer_logs else {}
        stale_gone = STALE_FACT not in facts_post
        report["C_e4_supersede"] = {
            "user_msg": c_user,
            "bot_reply": bot_reply,
            "facts_before": facts_pre,
            "facts_after": facts_post,
            "observer_updated_facts": last_obs.get("updated_facts"),
            "observer_facts": last_obs.get("facts"),
            "stale_fact_gone": stale_gone,
            "pass": stale_gone,
        }
        print(f"  机主: {c_user}")
        print(f"  青梓: {bot_reply}")
        print(f"  观察者 updated_facts: {last_obs.get('updated_facts')}")
        print(f"  观察者 facts: {last_obs.get('facts')}")
        print(f"  facts 前: {facts_pre}")
        print(f"  facts 后: {facts_post}")
        print(f"  判定: {'PASS（旧事实已被作废）' if stale_gone else 'FAIL（旧事实仍残留）'}")

    finally:
        await session.close()
        print("\n✓ 沙箱资源已清理")

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    passed = all(report[k]["pass"] for k in ("A_e1_recent_chat", "B_e3_holiday", "C_e4_supersede"))
    report["verdict"] = "PASS" if passed else "PARTIAL/FAIL"
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"✓ 完整报告: {REPORT_FILE}")
    print(f"✓ 总判定: {report['verdict']}")
    return report


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
