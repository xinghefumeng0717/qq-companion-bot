"""观察者引擎 (observer.py)
每轮对话落库后异步结算，评估好感度评分、情绪冲击量、提取事实、待跟进事项与表情包收藏。
"""

from __future__ import annotations

import collections
import json
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple

from companion.affection import AffectionEngine
from companion.db import Database, TIME_FORMAT, now_str
from companion.gateway import LLMGateway, parse_llm_json
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.prompts import OBSERVER_SYSTEM_PROMPT, OBSERVER_USER_PROMPT
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

ALLOWED_MOMENTS = {"深度共情", "分享脆弱", "轻浮表白", "伤害行为"}

# 观察者打分中性默认（单维 4.0）与待跟进默认提醒时长（小时）
NEUTRAL_SCORE = 4.0
DEFAULT_REMIND_HOURS = 24.0
MAX_REMIND_HOURS = 24.0 * 365

# 内存环形缓冲，保留最近 5 次观察者结算结果供 /debug 查看
recent_observer_logs: Deque[Dict[str, Any]] = collections.deque(maxlen=5)


def _coerce_float(value: Any, default: float, field: str) -> float:
    """数值字段容错：None 或无法转成 float 时落默认值，并记 warning 让坏输出可见"""
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        logger.warning(f"[Observer] 字段 {field} 类型异常 ({type(value).__name__}: {value!r})，落默认 {default}")
        return default


class Observer:
    def __init__(
        self,
        gateway: LLMGateway,
        affection: AffectionEngine,
        mood: MoodEngine,
        memory: MemoryManager,
        stickers: StickerManager,
        db: Database,
    ):
        self.gateway = gateway
        self.affection = affection
        self.mood = mood
        self.memory = memory
        self.stickers = stickers
        self.db = db

    async def settle_turn(
        self,
        user_message: str,
        assistant_reply: str,
        user_image_path: Optional[str] = None,
    ) -> Dict[str, Any]:
        """每轮对话后异步执行结算"""
        user_trunc = user_message[:500]
        bot_trunc = assistant_reply[:500]
        image_notice = "【机主本轮发送了一张图片】" if user_image_path else ""

        # 获取未完成待跟进事项
        pending_rows = await self.db.fetchall(
            "SELECT topic FROM followups WHERE done = 0 ORDER BY id ASC LIMIT 5"
        )
        pending_text = (
            "\n".join([f"- {r['topic']}" for r in pending_rows])
            if pending_rows
            else "（当前无未完成事项）"
        )

        user_prompt = OBSERVER_USER_PROMPT.format(
            user_message=user_trunc,
            assistant_reply=bot_trunc,
            image_notice=image_notice,
            pending_followups=pending_text,
        )

        messages = [
            {"role": "system", "content": OBSERVER_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        data, llm_failed = await self._call_observer_llm(messages)
        recent_observer_logs.append(data)

        # 1. 好感度引擎结算（逐字段强制 float；类型错误落中性默认，不中断整轮结算）
        s_disclosure = _coerce_float(data.get("self_disclosure"), NEUTRAL_SCORE, "self_disclosure")
        resp_score = _coerce_float(data.get("responsiveness"), NEUTRAL_SCORE, "responsiveness")
        w_score = _coerce_float(data.get("warmth_score"), NEUTRAL_SCORE, "warmth_score")
        reso_score = _coerce_float(data.get("resonance"), NEUTRAL_SCORE, "resonance")

        raw_moments = data.get("moments")
        if raw_moments is not None and not isinstance(raw_moments, list):
            logger.warning(f"[Observer] 字段 moments 类型异常 ({type(raw_moments).__name__}: {raw_moments!r})，已忽略")
        moments: List[str] = []
        for m in raw_moments if isinstance(raw_moments, list) else []:
            if isinstance(m, str) and m in ALLOWED_MOMENTS:
                moments.append(m)
            elif not isinstance(m, str):
                logger.warning(f"[Observer] 丢弃畸形 moment ({type(m).__name__}: {m!r})")

        # 打分持久化（仪表盘分布分析用，失败不影响结算）。
        # 失败兜底路径写的是固定 4.0，落库会污染锚点校准的分布，故只在成功解析时入表。
        if not llm_failed:
            try:
                await self.db.execute(
                    """
                    INSERT INTO observer_scores (self_disclosure, responsiveness, warmth_score, resonance, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (s_disclosure, resp_score, w_score, reso_score, now_str()),
                )
            except Exception as e:
                logger.warning(f"[Observer] 记录打分失败: {e}")

        new_aff_state, pulse = await self.affection.update(
            self_disclosure=s_disclosure,
            responsiveness=resp_score,
            warmth_score=w_score,
            resonance=reso_score,
            moments=moments,
        )

        # 2. 情绪冲击结算 (clamp: v,a in [-2,2], trust in [-0.3, 0.15])
        impact = data.get("mood_impact")
        if not isinstance(impact, dict):
            if impact is not None:
                logger.warning(f"[Observer] 字段 mood_impact 类型异常 ({type(impact).__name__}: {impact!r})，按零冲击处理")
            impact = {}
        raw_v = _coerce_float(impact.get("v"), 0.0, "mood_impact.v")
        raw_a = _coerce_float(impact.get("a"), 0.0, "mood_impact.a")
        raw_t = _coerce_float(impact.get("trust"), 0.0, "mood_impact.trust")

        clamped_v = max(-2.0, min(2.0, raw_v))
        clamped_a = max(-2.0, min(2.0, raw_a))
        clamped_t = max(-0.30, min(0.15, raw_t))

        # 若好感度大涨触发脉冲
        if pulse:
            clamped_v += 1.0
            clamped_a += 0.3
            clamped_t += 0.05

        await self.mood.update_mood(
            composite_affection=new_aff_state["composite"],
            conv_v=clamped_v,
            conv_a=clamped_a,
            conv_trust=clamped_t,
        )

        # 3. 语义事实入库（只接受字符串；LLM 偶尔返回字典，提取 value/content 字段，提取不到则丢弃）
        facts = data.get("facts")
        if facts is not None and not isinstance(facts, list):
            logger.warning(f"[Observer] 字段 facts 类型异常 ({type(facts).__name__}: {facts!r})，已忽略")
        if isinstance(facts, list):
            for fact in facts:
                if isinstance(fact, str):
                    fact_str = fact.strip()
                elif isinstance(fact, dict):
                    fact_str = str(fact.get("value") or fact.get("content") or "").strip()
                else:
                    fact_str = ""
                if fact_str:
                    await self.memory.add_fact(fact_str)
                elif fact:
                    logger.warning(f"[Observer] 丢弃畸形 fact ({type(fact).__name__}: {fact!r})")

        # 4. 待跟进事项入库（逐项校验：坏项丢单项，不丢整轮）
        followups = data.get("followups")
        if followups is not None and not isinstance(followups, list):
            logger.warning(f"[Observer] 字段 followups 类型异常 ({type(followups).__name__}: {followups!r})，已忽略")
        if isinstance(followups, list):
            now_dt = datetime.now()
            for fu in followups:
                if not isinstance(fu, dict):
                    logger.warning(f"[Observer] 丢弃畸形 followup ({type(fu).__name__}: {fu!r})")
                    continue
                raw_topic = fu.get("topic")
                topic = raw_topic.strip() if isinstance(raw_topic, str) else ""
                if not topic:
                    if raw_topic:
                        logger.warning(f"[Observer] 丢弃畸形 followup topic ({type(raw_topic).__name__}: {raw_topic!r})")
                    continue
                remind_hours = _coerce_float(
                    fu.get("remind_after_hours"), DEFAULT_REMIND_HOURS, "followups[].remind_after_hours"
                )
                if not math.isfinite(remind_hours) or not (0.0 <= remind_hours <= MAX_REMIND_HOURS):
                    logger.warning(f"[Observer] followups[].remind_after_hours 越界({remind_hours})，回退 {DEFAULT_REMIND_HOURS} 小时")
                    remind_hours = DEFAULT_REMIND_HOURS
                remind_dt = now_dt + timedelta(hours=remind_hours)
                remind_str = remind_dt.strftime(TIME_FORMAT)
                await self.db.execute(
                    """
                    INSERT INTO followups (topic, remind_after, done, created_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (topic, remind_str, now_str()),
                )

        # 5. 标记完成的待跟进事项（精确匹配，不匹配则记日志；非字符串项跳过）
        done_topics = data.get("done_followups")
        if done_topics is not None and not isinstance(done_topics, list):
            logger.warning(f"[Observer] 字段 done_followups 类型异常 ({type(done_topics).__name__}: {done_topics!r})，已忽略")
        if isinstance(done_topics, list):
            for dt in done_topics:
                if not isinstance(dt, str):
                    logger.warning(f"[Observer] 丢弃畸形 done_followup ({type(dt).__name__}: {dt!r})")
                    continue
                dt_str = dt.strip()
                if dt_str:
                    cur = await self.db.execute(
                        "UPDATE followups SET done = 1 WHERE topic = ? AND done = 0",
                        (dt_str,),
                    )
                    if cur.rowcount == 0:
                        logger.info(f"[Observer] done_followups 未精确匹配到待处理事项: '{dt_str}'")

        # 6. 收藏表情包回路 (§7.4)
        if user_image_path and data.get("collect_sticker") is True:
            raw_sticker_name = data.get("sticker_name")
            sticker_name = raw_sticker_name.strip() if isinstance(raw_sticker_name, str) else ""
            if not sticker_name:
                sticker_name = f"表情_{now_str()[:10]}"
            await self.stickers.collect_sticker(
                image_path=user_image_path,
                sticker_name=sticker_name,
                gateway=self.gateway,
            )

        return data

    async def _call_observer_llm(self, messages: List[Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        """调用模型并容错解析 JSON。返回 (结算数据, 是否走了失败兜底)。

        失败兜底（调用异常 / 解析异常 / 顶层非 dict）返回中性默认，第二个元素为 True，
        调用方据此跳过 observer_scores 落库。
        """
        default_res = {
            "self_disclosure": 4.0,
            "responsiveness": 4.0,
            "warmth_score": 4.0,
            "resonance": 4.0,
            "moments": [],
            "mood_impact": {"v": 0.0, "a": 0.0, "trust": 0.0},
            "facts": [],
            "followups": [],
            "done_followups": [],
            "collect_sticker": False,
            "sticker_name": "",
            "user_state": "平静",
        }

        try:
            resp = await self.gateway.chat(
                messages=messages,
                model=self.gateway.config.observer_model,
                temperature=0.3,
                json_mode=True,
                purpose="observer",
            )
            parsed = parse_llm_json(resp)
        except Exception as e:
            logger.warning(f"[Observer] 观察者调用或解析失败，使用中性默认: {e}")
            return default_res, True

        if not isinstance(parsed, dict):
            logger.warning(f"[Observer] 观察者返回顶层非 dict ({type(parsed).__name__}: {parsed!r})，使用中性默认")
            return default_res, True
        return parsed, False
