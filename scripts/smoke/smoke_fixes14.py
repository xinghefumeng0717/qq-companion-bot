"""FIXES14 真实 API 冒烟验证 (scripts/smoke/smoke_fixes14.py)

沿用 smoke_fixes11/12 的做法：生产库副本 → 沙箱 → 本地 config.toml 真实 key，
零服务器副作用。跑两段，每段对应一条生产证据：

A. E10（任务1 长假作息注入校园地点，V3 大考 J 场景实锤）：
   holidays 只在**内存里**注入连续 8 天段（含今天），不动 config.toml。
   分三层取证，避免"断言自动成立"：
   [0] 零模型对照：1 天短假 vs 8 天长假，【她此刻】/【事实】两行逐字对比
       （这条是确定性证据，不依赖模型这次是否配合；短假那侧必须出现校园词，
        证明长假那侧的"没有校园词"是真的）
   [1] 真实主动消息：长假钟下走完整决策层+生成层，断言实发文本无校园词
   [2] 生成层直调（强制最坏输入）：topic_material 强制填 E10 原话
       "坐校车去玉泉老校区看老建筑"，绕过决策层的自由意志

B. E11（任务2 观察者畸形数据容忍）：
   先打一次真实 API 走完整对话，再把观察者模型返回替换成 E11 生产日志里的
   畸形形态（facts 是 {'内容': ...}，followups 是裸字典），走真实结算，
   断言 fact 与 followup 真的入库（remind_after ≈ now+24h）。

报告落 data/smoke_fixes14_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.chat import ChatSession
from companion.config import ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT, parse_dt
from companion.persona import holiday_span
from companion.proactive import ProactiveScheduler, format_recent_chat
from companion.prompts import FACE_PROMPT_BLOCK, PROACTIVE_GENERATE_PROMPT
from companion.replier import Replier
from companion.turn_handler import TurnHandler

logging.basicConfig(level=logging.WARNING)

# 冒烟把主动消息的时钟固定到今天 15:00：真实时间此刻是上午，决策层可能
# （合理地）选 B 短路掉生成层，那样就验证不到本次修的东西。
FAKE_NOW = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)

REAL_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_real.db"
FALLBACK_DB = "data/companion.db"
SANDBOX_DB = "data/smoke_fixes14_sandbox.db"
REPORT_FILE = "data/smoke_fixes14_report.json"

# 校园场景词黑名单：E10 事故里她真说过的就是"坐校车去玉泉老校区"/"在玉泉转悠"
CAMPUS_WORDS = ["银泉", "临湖", "琴房", "玉泉", "校车", "教学区", "启真湖", "基础馆", "风味"]
# 回家/老家类词（只作对照记录，不作硬判据：她用什么词说"回家了"是她的自由）
HOME_WORDS = ["老家", "绍兴", "回家", "在家", "家里", "高铁"]

# E10 实锤原话（J 场景长假钟产出），用作 [2] 强制最坏输入
E10_PROD_LINE = "坐校车去玉泉老校区看老建筑"
E10_FORCED_MATERIAL = f"想跟他讲讲{E10_PROD_LINE}的事"

# E11 生产日志里的畸形返回形态，逐字照抄
E11_OBSERVER_PAYLOAD = {
    "self_disclosure": 5.0,
    "responsiveness": 5.0,
    "warmth_score": 5.0,
    "resonance": 5.0,
    "moments": [],
    "mood_impact": {"v": 0.0, "a": 0.0, "trust": 0.0},
    "facts": [{"内容": "他不吃香菜"}],
    "followups": {"topic": "问他科创比赛", "remind_after_hours": 24},
    "done_followups": [],
    "collect_sticker": False,
    "sticker_name": "",
}
E11_FACT_TEXT = "他不吃香菜"
E11_FOLLOWUP_TOPIC = "问他科创比赛"
E11_FOLLOWUP_HOURS = 24


class _FakeDatetime(datetime):
    """只改 now()，strptime 等照常——供 patch('companion.proactive.datetime') 使用"""

    @classmethod
    def now(cls, tz=None):
        return FAKE_NOW if tz is None else FAKE_NOW.astimezone(tz)


def _sent_text(chunks) -> str:
    return "\n".join(
        c["content"] if c["type"] == "text" else f"[表情:{c.get('desc', '')}]" for c in chunks
    )


def _hits(text: str, words) -> list:
    return [w for w in words if w in (text or "")]


def _line(prompt: str, prefix: str) -> str:
    for line in (prompt or "").splitlines():
        if line.startswith(prefix):
            return line
    return ""


def _long_holidays() -> list:
    """连续 8 天、含今天的假期段（只在内存里注入）"""
    return [(datetime.now() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(-3, 5)]


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


async def run_smoke_test() -> dict:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    print("=" * 70)
    print(f"   FIXES14 真实 API 冒烟验证 (基准库: {prod_db})")
    print("=" * 70)

    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    print("✓ 沙箱初始化完成（真实 key，无服务器副作用）")

    report = {
        "base_db": prod_db,
        "clock_note": "A 段把 companion.proactive 的时钟挪到今天 15:00（上午决策层可能选 B 短路生成层）",
        "holidays_note": "8 天长假只注入内存里的 llm.pricing.holidays，不写 config.toml",
        "A_e10_long_holiday": {},
        "B_e11_observer_dict": {},
        "verdict": "PENDING",
    }

    sent: list = []
    llm_calls: list = []

    async def collect(chunk):
        sent.append(chunk)

    # 决策层/生成层原始输出留证（模型选什么是它的自由意志，冒烟只如实记录）
    _real_chat = session.gateway.chat

    async def spy_chat(*args, **kwargs):
        resp = await _real_chat(*args, **kwargs)
        llm_calls.append({"purpose": kwargs.get("purpose"), "response": str(resp)[:600]})
        return resp

    session.gateway.chat = spy_chat

    replier = Replier(ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0), session.stickers)
    scheduler = ProactiveScheduler(
        # 只放开免打扰时段闸门：生产的 [0,8] 会直接拦掉本次冒烟
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

    original_holidays = list(session.config.llm.pricing.holidays)

    try:
        aff = await session.affection.get_state()
        stage = int(aff.get("stage", 0))
        print(f"✓ 当前状态: 阶段 {stage}（{session.persona.get_stage(stage).name}）| 复合分 {aff.get('composite')}")

        # ---------- A 段：E10 回归 ----------
        print("\n" + "-" * 70)
        print("A 段（任务1 / E10）：注入连续 8 天长假，断言不吐校园作息")
        print("-" * 70)

        long_hols = _long_holidays()
        today = datetime.now().strftime("%Y-%m-%d")
        span = holiday_span(today, long_hols)
        print(f"  注入 holidays（仅内存）: {long_hols}")
        print(f"  今天 {today} 的连续段长 holiday_span = {span}")

        # [0] 零模型对照：短假 vs 长假，两行提示词逐字对比
        session.config.llm.pricing.holidays = [today]
        prompt_short = await session.assembler.assemble_system_prompt("在吗")
        session.config.llm.pricing.holidays = long_hols
        prompt_long = await session.assembler.assemble_system_prompt("在吗")

        her_short = _line(prompt_short, "【她此刻】")
        her_long = _line(prompt_long, "【她此刻】")
        fact_short = _line(prompt_short, "【事实】")
        fact_long = _line(prompt_long, "【事实】")
        short_hits = _hits(her_short, CAMPUS_WORDS)
        long_hits = _hits(her_long, CAMPUS_WORDS)
        print("  [0] 零模型对照（assembler 组装，不依赖模型）")
        print(f"      短假(1天) 【她此刻】: {her_short}")
        print(f"      长假(8天) 【她此刻】: {her_long}")
        print(f"      短假(1天) 校园词命中: {short_hits or '无'}")
        print(f"      长假(8天) 校园词命中: {long_hits or '无'}")
        print(f"      长假 【事实】行: {fact_long[:80]}")
        # 判据成立的前提：短假那侧真的带校园词，否则"长假没有"是空断言
        control_ok = bool(short_hits) and not long_hits
        print(f"      判定: {'PASS（短假会漏、长假不漏，机制确实被改动）' if control_ok else 'FAIL'}")
        if not short_hits:
            print("      ⚠ 短假侧没带校园词，反向对照失效，本次结论不可采信")

        # [1] 真实主动消息：长假钟下走完整决策层 + 生成层
        attempts = []
        choice_a_seen = False
        for attempt in range(1, 4):
            llm_calls.clear()
            with patch("companion.proactive.datetime", _FakeDatetime):
                if attempt == 1:
                    await _seed_turn(
                        session.db,
                        "国庆快乐，你在干嘛呢",
                        "在呢，刚给你热了杯水",
                        FAKE_NOW - timedelta(hours=2),
                    )
                    print(f"  种子历史已写入，时钟：{FAKE_NOW:%Y-%m-%d %H:%M}")
                await scheduler.reset_unanswered_count()
                sent.clear()
                await scheduler.trigger_cycle()

            attempt_sent = _sent_text(sent)
            decision = next((c for c in llm_calls if c["purpose"] == "proactive_decision"), None)
            generation = next((c for c in llm_calls if c["purpose"] == "proactive_message"), None)
            choice = "?"
            if decision:
                try:
                    choice = json.loads(decision["response"]).get("choice", "?")
                except Exception:
                    choice = "?"
            attempts.append({
                "attempt": attempt,
                "choice": choice,
                "decision_raw": decision["response"] if decision else None,
                "generation_raw": generation["response"] if generation else None,
                "sent_text": attempt_sent,
                "campus_words_found": _hits(attempt_sent, CAMPUS_WORDS),
                "home_words_found": _hits(attempt_sent, HOME_WORDS),
            })
            print(f"  [1] 第 {attempt} 次: choice={choice}, 产出={attempt_sent or '（未发送）'}")
            if str(choice).upper() == "A" and attempt_sent:
                choice_a_seen = True
                break
        real_attempts = [a for a in attempts if a["sent_text"]]
        real_hits = [w for a in real_attempts for w in a["campus_words_found"]]
        print(f"      真实产出条数: {len(real_attempts)}（choice=A 出现过: {choice_a_seen}）")
        print(f"      真实产出里校园词命中: {real_hits or '无'}")
        print(f"      真实产出里回家类词命中: {sorted({w for a in real_attempts for w in a['home_words_found']}) or '无'}")
        if not real_attempts:
            print("      ⚠ 决策层三次都没选 A，一次都没生成：上面那行「无校园词」是自动成立的空断言，")
            print("        不能当作本段通过证据——这正是 FIXES12 记过的坑，真实产出证据改由 [1b] 提供")

        # [1b] 真实产出（不依赖决策层自由意志）：拿生产真实会用的话题材料
        #      （长假下的 _select_topic_material()，即角色卡里的长假文案）
        #      直调生成层。这是本次修的注入链在真实 API 上的产出证据。
        print("  [1b] 真实产出：长假话题材料直调生成层（绕过决策层的自由意志）")
        with patch("companion.proactive.datetime", _FakeDatetime):
            real_material = await scheduler._select_topic_material()
            turns_1b = await session.memory.get_recent_turns(limit=8)
            prompt_1b = PROACTIVE_GENERATE_PROMPT.format(
            typing_habit="手机打字习惯",
                user_address=session.persona.user_address,
                current_time=FAKE_NOW.strftime(TIME_FORMAT),
                topic_material=real_material,
                recent_chat=format_recent_chat(turns_1b, max_chars=60),
                stickers_list="、".join(session.stickers.get_prompt_sticker_list()),
                face_block=FACE_PROMPT_BLOCK,
            )
            system_1b = await session.assembler.assemble_system_prompt("")
            raw_1b = await session.gateway.chat(
                messages=[
                    {"role": "system", "content": system_1b},
                    {"role": "user", "content": prompt_1b},
                ],
                model=session.gateway.config.text_model,
                temperature=0.8,
                purpose="proactive_message",
            )
        chunks_1b, record_1b = replier.parse_reply(raw_1b, source="proactive")
        sent_1b = _sent_text(chunks_1b)
        hits_1b = _hits(sent_1b, CAMPUS_WORDS)
        home_1b = _hits(sent_1b, HOME_WORDS)
        gen_1b_pass = bool(sent_1b) and not hits_1b
        print(f"      生产话题材料: {real_material}")
        print(f"      生成层原始输出: {str(raw_1b)[:300]}")
        print(f"      滤网后实发: {sent_1b or '（空）'}")
        print(f"      实发里校园词命中: {hits_1b or '无'}")
        print(f"      实发里回家类词命中: {home_1b or '无'}")
        print(f"      判定: {'PASS（真实产出无校园场景）' if gen_1b_pass else 'FAIL'}")
        if not sent_1b:
            print("      ⚠ 本次生成层返回空：产出证据缺失，不能算通过")

        # [2] 生成层直调：绕过决策层自由意志，强制喂 E10 原话
        print("  [2] 生成层直调（强制最坏输入：topic_material = E10 实锤原话）")
        with patch("companion.proactive.datetime", _FakeDatetime):
            gen_turns = await session.memory.get_recent_turns(limit=8)
            gen_user_prompt = PROACTIVE_GENERATE_PROMPT.format(
            typing_habit="手机打字习惯",
                user_address=session.persona.user_address,
                current_time=FAKE_NOW.strftime(TIME_FORMAT),
                topic_material=E10_FORCED_MATERIAL,
                recent_chat=format_recent_chat(gen_turns, max_chars=60),
                stickers_list="、".join(session.stickers.get_prompt_sticker_list()),
                face_block=FACE_PROMPT_BLOCK,
            )
            forced_system = await session.assembler.assemble_system_prompt("")
            forced_raw = await session.gateway.chat(
                messages=[
                    {"role": "system", "content": forced_system},
                    {"role": "user", "content": gen_user_prompt},
                ],
                model=session.gateway.config.text_model,
                temperature=0.8,
                purpose="proactive_message",
            )
        forced_chunks, forced_record = replier.parse_reply(forced_raw, source="proactive")
        forced_sent = _sent_text(forced_chunks)
        forced_hits = _hits(forced_sent, CAMPUS_WORDS)
        print(f"      强制话题: {E10_FORCED_MATERIAL}")
        print(f"      生成层原始输出: {str(forced_raw)[:300]}")
        print(f"      滤网后实发: {forced_sent or '（空）'}")
        print(f"      实发里校园词命中: {forced_hits or '无'}")
        print(f"      实发里回家类词命中: {_hits(forced_sent, HOME_WORDS) or '无'}")
        if forced_hits:
            print("      判定: FAIL —— 但这不是本次注入链失效")
            print("        机制说明：本次修的是【作息注入】（她此刻在干嘛），已由 [0] 零模型对照证实。")
            print("        此处是**话题材料本身**由冒烟强灌了 E10 原话，模型顺着话题编了校园场景。")
            print("        属文字层口径问题（提示词要不要加'长假期间任何校园场景都不许说'的叮嘱），")
            print("        任务书负面清单第 5 条明确'文字层/卡的一切内容不碰'，故此处如实记录为残余风险，")
            print("        交所有者拍板，不在本迭代擅改。")
        else:
            print("      判定: PASS（喂了校园话题也没跟着编校园场景）")

        report["A_e10_long_holiday"] = {
            "holidays_injected_in_memory": long_hols,
            "holiday_span_today": span,
            "control_zero_model": {
                "note": "短假(1天) vs 长假(8天) 的【她此刻】/【事实】逐字对照，零模型参与",
                "her_line_short": her_short,
                "her_line_long": her_long,
                "fact_line_short": fact_short,
                "fact_line_long": fact_long,
                "campus_words_short": short_hits,
                "campus_words_long": long_hits,
                "long_activity_placeholder": session.persona.long_holiday_activity,
                "pass": control_ok,
            },
            "attempts": attempts,
            "any_choice_a": choice_a_seen,
            "real_generated_count": len(real_attempts),
            "real_campus_words": real_hits,
            "note_decision_gate": (
                "决策层三次均未选 A，一次都没进入生成层，故 attempts 的'无校园词'不算证据；"
                "真实产出证据由 real_generation(1b) 提供"
            ),
            "real_generation": {
                "note": "长假生产话题材料（_select_topic_material）直调生成层，不依赖决策层",
                "topic_material": real_material,
                "generation_raw": str(raw_1b),
                "sent_text": sent_1b,
                "campus_words_found": hits_1b,
                "home_words_found": home_1b,
                "record": record_1b,
                "pass": gen_1b_pass,
            },
            "forced_worst_case": {
                "note": "强灌 E10 校园话题后的生成层直调；超出本迭代范围，属文字层口径残余风险",
                "topic_material": E10_FORCED_MATERIAL,
                "generation_raw": str(forced_raw),
                "sent_text": forced_sent,
                "campus_words_found": forced_hits,
                "home_words_found": _hits(forced_sent, HOME_WORDS),
                "record": forced_record,
                "pass": not forced_hits,
                "in_scope": False,
            },
            "pass": control_ok and gen_1b_pass and not real_hits,
            "residual_risk": {
                "summary": "话题材料/历史里若含校园词，长假期间她仍可能顺着编校园场景",
                "evidence": forced_sent,
                "owner_decision_needed": "是否在长假提示词里加'任何校园场景都不许说'的叮嘱（文字层口径，任务书划给所有者）",
            },
        }
        print(f"  A 段判定: {'PASS' if report['A_e10_long_holiday']['pass'] else 'FAIL'}")

        # ---------- B 段：E11 回归 ----------
        print("\n" + "-" * 70)
        print("B 段（任务2 / E11）：真实对话 + 观察者回畸形字典，断言事实/待跟进入库")
        print("-" * 70)

        # 1) 先打一次真实 API 走完整对话（真实 key，真实回复）
        sent.clear()
        handler = TurnHandler(
            config=session.config,
            gateway=session.gateway,
            assembler=session.assembler,
            replier=replier,
            memory=session.memory,
            observer=session.observer,
            proactive=scheduler,
            send_chunk_fn=collect,
        )
        turn_user = "对了，我科创比赛的结果今天出了，还挺顺的"
        try:
            await handler.handle_turn(turn_user, None)
            await asyncio.sleep(0.05)
        except Exception as e:  # 真实对话失败也要继续跑结算断言
            print(f"  ⚠ 真实对话异常（不影响 B 段解析层判定）: {e}")
        real_reply = _sent_text(sent)
        print(f"  真实对话：机主「{turn_user}」")
        print(f"  真实回复：{real_reply or '（未发送）'}")

        # 2) 观察者返回注入 E11 生产日志里的畸形形态，走真实结算
        before_facts = await session.memory.get_all_facts()
        before_fu = await session.db.fetchall("SELECT topic FROM followups ORDER BY id")
        llm_calls.clear()
        real_observer_chat = session.gateway.chat

        async def e11_observer_chat(*args, **kwargs):
            if kwargs.get("purpose") == "observer":
                llm_calls.append({"purpose": "observer", "response": json.dumps(E11_OBSERVER_PAYLOAD, ensure_ascii=False)})
                return json.dumps(E11_OBSERVER_PAYLOAD, ensure_ascii=False)
            return await real_observer_chat(*args, **kwargs)

        session.gateway.chat = e11_observer_chat
        try:
            await session.observer.settle_turn(turn_user, real_reply or "嗯嗯，恭喜你")
        finally:
            session.gateway.chat = spy_chat

        after_facts = await session.memory.get_all_facts()
        after_fu = await session.db.fetchall("SELECT topic, remind_after FROM followups ORDER BY id")
        new_facts = [f for f in after_facts if f not in before_facts]
        new_fu = [r for r in after_fu if r["topic"] not in {b["topic"] for b in before_fu}]

        fact_ok = E11_FACT_TEXT in new_facts
        fu_ok = any(r["topic"] == E11_FOLLOWUP_TOPIC for r in new_fu)
        fu_drift = None
        for r in new_fu:
            if r["topic"] == E11_FOLLOWUP_TOPIC:
                expected = datetime.now() + timedelta(hours=E11_FOLLOWUP_HOURS)
                fu_drift = abs((parse_dt(r["remind_after"]) - expected).total_seconds())

        print(f"  观察者返回: facts={json.dumps(E11_OBSERVER_PAYLOAD['facts'], ensure_ascii=False)} "
              f"followups={json.dumps(E11_OBSERVER_PAYLOAD['followups'], ensure_ascii=False)}")
        print(f"  结算后新增事实: {new_facts or '无'}")
        print(f"  结算后新增待跟进: {[(r['topic'], r['remind_after']) for r in new_fu] or '无'}")
        print(f"  fact 入库: {'PASS' if fact_ok else 'FAIL'}")
        print(f"  followup 入库: {'PASS' if fu_ok else 'FAIL'}"
              + (f"（remind_after 与 now+{E11_FOLLOWUP_HOURS}h 偏差 {fu_drift:.0f} 秒）" if fu_drift is not None else ""))
        print(f"  判定: {'PASS' if (fact_ok and fu_ok) else 'FAIL'}")

        report["B_e11_observer_dict"] = {
            "real_turn": {"user": turn_user, "reply": real_reply},
            "observer_payload": E11_OBSERVER_PAYLOAD,
            "new_facts": new_facts,
            "new_followups": [dict(r) for r in new_fu],
            "fact_pass": fact_ok,
            "followup_pass": fu_ok,
            "followup_remind_drift_seconds": fu_drift,
            "pass": fact_ok and fu_ok,
        }
    finally:
        session.config.llm.pricing.holidays = original_holidays
        await session.close()
        print("\n✓ 沙箱资源已清理")

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    passed = all(report[k].get("pass") for k in ("A_e10_long_holiday", "B_e11_observer_dict"))
    report["verdict"] = "PASS" if passed else "PARTIAL/FAIL"
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"✓ 完整报告: {REPORT_FILE}")
    print(f"✓ 总判定: {report['verdict']}")
    return report


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
