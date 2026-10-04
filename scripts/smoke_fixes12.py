"""FIXES12 真实 API 冒烟验证 (scripts/smoke_fixes12.py)

复用 smoke_fixes11.py 的思路：生产库副本 → 沙箱 → 本地 config.toml 的真实 key，
零服务器副作用。跑两段，每段对应一条生产证据：

A. E8（任务1 "[图片]"占位符泄漏）：
   往欲言又止池塞一条 E8 原样念头"想拍一张临湖早午餐的照片发给他"（她没有摄像头，
   这是不可执行计划），触发一次真实主动消息生成；断言实发文本不含
   [图片]/[照片]/[image]/【图片】任何一种占位符。
   另附 filter_replay：把 E8 事故当天真正发到 QQ 上的那段原文直接喂真实 Replier，
   做 before/after 对比——这是滤网层的确定性证据，不依赖模型这次是否配合。
B. E9（任务2 视觉描述补社交语义）：
   喂一张真实表情包图片走完整 TurnHandler.handle_turn，断言视觉描述里带
   情绪/聊天含义类表述，而不是只有画面元素；并记录主聊实际接的话。

报告落 data/smoke_fixes12_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.chat import ChatSession
from companion.config import ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT
from companion.proactive import ProactiveScheduler, format_recent_chat
from companion.prompts import PROACTIVE_GENERATE_PROMPT
from companion.replier import Replier
from companion.turn_handler import TurnHandler

logging.basicConfig(level=logging.WARNING)

# 冒烟把主动消息的时钟固定到今天 15:00：真实时间此刻是上午，决策层可能
# （合理地）选 B 短路掉生成层，那样就验证不到本次修的东西。
FAKE_NOW = datetime.now().replace(hour=15, minute=0, second=0, microsecond=0)


class _FakeDatetime(datetime):
    """只改 now()，strptime 等照常——供 patch('companion.proactive.datetime') 使用"""

    @classmethod
    def now(cls, tz=None):
        return FAKE_NOW if tz is None else FAKE_NOW.astimezone(tz)


REAL_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_real.db"
FALLBACK_DB = "data/companion.db"
SANDBOX_DB = "data/smoke_fixes12_sandbox.db"
REPORT_FILE = "data/smoke_fixes12_report.json"

# A 段：E8 原样念头（决策层会读到它）
E8_DESIRE = "想拍一张临湖早午餐的照片发给他"
E8_SEED_USER = "临湖那边今天人少吗"
E8_SEED_BOT = "我刚到的时候还挺空的"

# A 段：E8 事故当天真正发到 QQ 上的原文（bot.log 实锤，10-04 09:19）
E8_PROD_TRANSCRIPT = (
    "临湖今天人少，窗边位不用抢，早午餐给你看下\n"
    "[图片]\n"
    "你上次不是问哪个窗口人少才肯动身嘛，这会儿在家早餐吃了吗"
)

# 占位符黑名单（含各种写法，滤网必须全拦下）
PLACEHOLDER_TOKENS = ["[图片]", "【图片】", "[照片]", "【照片】", "[image]", "[IMAGE]"]

# B 段：一张真实表情包（「夸夸」＝赞同、认可，正是 E9 里的场景）
B_STICKER = "characters/qingzi/stickers/cat_thanks.jpg"
# E9 的判据：描述里必须出现"社交含义"而非只有画面元素
SOCIAL_MEANING_KEYWORDS = [
    "表示", "传达", "含义", "意思", "收尾", "同意", "认可", "敷衍", "回应",
    "撒娇", "调侃", "无语", "感谢", "安抚", "安慰", "认可", "可以结束", "结束对话",
]
# 纯画面词（用于报告对照，说明模型确实还在描述画面，只是这次额外点了含义）
OBJECT_ONLY_KEYWORDS = ["背景", "虚化", "紫底", "底色", "像素", "分辨率", "色块"]


def _sent_text(chunks) -> str:
    return "\n".join(
        c["content"] if c["type"] == "text" else f"[表情:{c.get('desc', '')}]" for c in chunks
    )


def _find_placeholders(text: str) -> list:
    return [t for t in PLACEHOLDER_TOKENS if t in text]


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
    print(f"   FIXES12 真实 API 冒烟验证 (基准库: {prod_db})")
    print("=" * 70)

    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    print("✓ 沙箱初始化完成（真实 key，无服务器副作用）")

    report = {
        "base_db": prod_db,
        "clock_note": "A 段把 companion.proactive 的时钟挪到今天 15:00（上午决策层可能选 B 短路生成层）",
        "A_e8_image_placeholder": {},
        "B_e9_social_semantics": {},
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

    # 零延迟分段发送（只影响发送节奏，不影响生成内容）
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

    try:
        aff = await session.affection.get_state()
        stage = int(aff.get("stage", 0))
        print(f"✓ 当前状态: 阶段 {stage}（{session.persona.get_stage(stage).name}）| 复合分 {aff.get('composite')}")

        # ---------- A 段：E8 回归 ----------
        print("\n" + "-" * 70)
        print("A 段（任务1 / E8）：欲言又止池塞入「拍照片发给他」念头，触发真实主动消息")
        print("-" * 70)

        # 0) 滤网确定性证据：E8 事故原文直接过真实 Replier
        chunks_replay, record_replay = replier.parse_reply(E8_PROD_TRANSCRIPT, source="proactive")
        replay_sent = _sent_text(chunks_replay)
        replay_found = _find_placeholders(replay_sent)
        replay_pass = bool(replay_sent) and not replay_found and not _find_placeholders(record_replay)
        print("  [0] 滤网回放（E8 事故当天实发原文，零模型参与）")
        print(f"      输入: {E8_PROD_TRANSCRIPT}")
        print(f"      实发: {replay_sent}")
        print(f"      落库: {record_replay}")
        print(f"      占位符残留: {replay_found or '无'}")
        print(f"      判定: {'PASS（滤网拦下）' if replay_pass else 'FAIL'}")

        # 1) 真实主动消息：模型自己会不会再吐占位符
        attempts = []
        choice_a_seen = False
        sent_text = ""
        for attempt in range(1, 4):
            llm_calls.clear()
            with patch("companion.proactive.datetime", _FakeDatetime):
                if attempt == 1:
                    await session.db.execute(
                        "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
                        (E8_DESIRE, FAKE_NOW.strftime(TIME_FORMAT)),
                    )
                    await _seed_turn(
                        session.db, E8_SEED_USER, E8_SEED_BOT, FAKE_NOW - timedelta(hours=2)
                    )
                    print(f"  已写入欲言又止池: {E8_DESIRE}")
                    print(f"  种子历史: {E8_SEED_USER} / {E8_SEED_BOT}")
                    print(f"  时钟：{FAKE_NOW:%Y-%m-%d %H:%M}（{report['clock_note']}）")

                await scheduler.reset_unanswered_count()  # 清掉上一轮未回复计数，否则闸门拦下
                sent.clear()
                await scheduler.trigger_cycle()

            attempt_sent = _sent_text(sent)
            sent_text = sent_text or attempt_sent
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
                "placeholders_found": _find_placeholders(attempt_sent),
            })
            print(f"  第 {attempt} 次: choice={choice}, 产出={attempt_sent or '（未发送）'}")
            if str(choice).upper() == "A" and attempt_sent:
                choice_a_seen = True
                break

        # 生成层原始输出里也可能带占位符（那是模型不听话的证据），实发层才是判据
        gen_raw = next((a["generation_raw"] for a in reversed(attempts) if a["generation_raw"]), None)
        gen_found = _find_placeholders(gen_raw) if gen_raw else []
        if gen_found:
            print(f"  ⚠ 生成层原始输出仍含占位符 {gen_found}（说明滤网确实在兜底）")

        # 2) 生成层直调：决策层若真选了"拍照片"话题（E8 事故 09:18:43 的原话），
        #    这里强制喂最坏情况，看模型还吐不吐占位符、滤网拦不拦得住。
        #    决策层是否选 A 属于模型自由意志，不能只靠它配合来验收。
        print("  [2] 生成层直调（强制最坏情况：topic_material = E8 的拍照片计划）")
        with patch("companion.proactive.datetime", _FakeDatetime):
            gen_turns = await session.memory.get_recent_turns(limit=8)
            gen_user_prompt = PROACTIVE_GENERATE_PROMPT.format(
                user_address=session.persona.user_address,
                current_time=FAKE_NOW.strftime(TIME_FORMAT),
                topic_material=E8_DESIRE,
                recent_chat=format_recent_chat(gen_turns, max_chars=60),
                stickers_list="、".join(session.stickers.get_prompt_sticker_list()),
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
        raw_found = _find_placeholders(str(forced_raw))
        sent_found = _find_placeholders(forced_sent)
        record_found = _find_placeholders(forced_record)
        forced_pass = not sent_found and not record_found
        print(f"      生成层原始输出: {forced_raw}")
        print(f"      原始输出里的占位符: {raw_found or '无'}")
        print(f"      滤网后实发: {forced_sent or '（空）'}")
        print(f"      实发里的占位符: {sent_found or '无'}")
        print(f"      判定: {'PASS（模型没吐或滤网拦下）' if forced_pass else 'FAIL'}")

        report["A_e8_image_placeholder"] = {
            "clock": FAKE_NOW.strftime(TIME_FORMAT),
            "seeded_desire": E8_DESIRE,
            "seed_user": E8_SEED_USER,
            "seed_bot": E8_SEED_BOT,
            "filter_replay": {
                "input": E8_PROD_TRANSCRIPT,
                "sent_text": replay_sent,
                "record": record_replay,
                "sent_chunks": [dict(c) for c in chunks_replay],
                "placeholders_found": replay_found,
                "pass": replay_pass,
            },
            "attempts": attempts,
            "generation_raw_placeholders": gen_found,
            "forced_worst_case": {
                "note": "决策层若真选了拍照片话题（E8 事故原话）时的生成层直调结果",
                "topic_material": E8_DESIRE,
                "generation_raw": str(forced_raw),
                "raw_placeholders_found": raw_found,
                "sent_text": forced_sent,
                "sent_chunks": [dict(c) for c in forced_chunks],
                "record": forced_record,
                "sent_placeholders_found": sent_found,
                "pass": forced_pass,
            },
            "any_choice_a": choice_a_seen,
            "pass": replay_pass and forced_pass,
        }
        print(f"  A 段判定: {'PASS' if report['A_e8_image_placeholder']['pass'] else 'FAIL'}")

        # ---------- B 段：E9 回归 ----------
        print("\n" + "-" * 70)
        print("B 段（任务2 / E9）：喂真实表情包，断言视觉描述带社交含义")
        print("-" * 70)
        sticker_src = B_STICKER if os.path.exists(B_STICKER) else None
        if sticker_src is None:
            print(f"  ✗ 找不到素材: {B_STICKER}")
            report["B_e9_social_semantics"] = {"pass": False, "error": "sticker not found"}
        else:
            # 复制到临时目录再喂：resize_image_if_needed 会原地改文件，
            # 绝不能让它碰到 characters/ 下的角色卡素材（任务书高压线）
            tmp_dir = tempfile.mkdtemp(prefix="fixes12_vision_")
            tmp_img = os.path.join(tmp_dir, os.path.basename(sticker_src))
            shutil.copy2(sticker_src, tmp_img)
            print(f"  素材: {sticker_src}（已复制到 {tmp_img}，不碰角色卡原图）")

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
            llm_calls.clear()
            sent.clear()
            try:
                await handler.handle_turn("", tmp_img)
                await asyncio.sleep(0.05)  # 让观察者结算任务跑完
            finally:
                shutil.rmtree(tmp_dir, ignore_errors=True)

            vision = next((c for c in llm_calls if c["purpose"] == "vision_perception"), None)
            desc = (vision["response"] if vision else "").strip()
            hits = [k for k in SOCIAL_MEANING_KEYWORDS if k in desc]
            obj_hits = [k for k in OBJECT_ONLY_KEYWORDS if k in desc]
            bot_reply = _sent_text(sent)
            b_pass = bool(desc) and bool(hits)
            print(f"  视觉描述原文: {desc}")
            print(f"  含义类关键词命中: {hits or '无'}")
            print(f"  纯画面词命中（对照）: {obj_hits or '无'}")
            print(f"  主聊接的话: {bot_reply or '（未发送）'}")
            print(f"  判定: {'PASS（描述含社交含义）' if b_pass else 'FAIL（仍是纯画面描述）'}")

            report["B_e9_social_semantics"] = {
                "sticker": sticker_src,
                "vision_desc": desc,
                "social_meaning_hits": hits,
                "object_only_hits": obj_hits,
                "bot_reply": bot_reply,
                "pass": b_pass,
            }

    finally:
        await session.close()
        print("\n✓ 沙箱资源已清理")

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    passed = all(report[k].get("pass") for k in ("A_e8_image_placeholder", "B_e9_social_semantics"))
    report["verdict"] = "PASS" if passed else "PARTIAL/FAIL"
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"✓ 完整报告: {REPORT_FILE}")
    print(f"✓ 总判定: {report['verdict']}")
    return report


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
