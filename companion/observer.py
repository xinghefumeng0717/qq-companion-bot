"""观察者引擎 (observer.py)
每轮对话落库后异步结算，评估好感度评分、情绪冲击量、提取事实、待跟进事项与表情包收藏。
"""

from __future__ import annotations

import collections
import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional

from companion.affection import AffectionEngine
from companion.db import Database, TIME_FORMAT, now_str
from companion.gateway import LLMGateway, parse_llm_json
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.prompts import OBSERVER_SYSTEM_PROMPT, OBSERVER_USER_PROMPT
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

ALLOWED_MOMENTS = {"深度共情", "分享脆弱", "轻浮表白", "伤害行为"}

# 内存环形缓冲，保留最近 5 次观察者结算结果供 /debug 查看
recent_observer_logs: Deque[Dict[str, Any]] = collections.deque(maxlen=5)


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

        data = await self._call_observer_llm(messages)
        recent_observer_logs.append(data)

        # 1. 好感度引擎结算
        s_disclosure = float(data.get("self_disclosure") if data.get("self_disclosure") is not None else 4.0)
        resp_score = float(data.get("responsiveness") if data.get("responsiveness") is not None else 4.0)
        w_score = float(data.get("warmth_score") if data.get("warmth_score") is not None else 4.0)
        reso_score = float(data.get("resonance") if data.get("resonance") is not None else 4.0)

        raw_moments = data.get("moments")
        if not isinstance(raw_moments, list):
            raw_moments = []
        moments = [m for m in raw_moments if m in ALLOWED_MOMENTS]

        # 打分持久化（仪表盘分布分析用，失败不影响结算）
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
            impact = {}
        raw_v = float(impact.get("v") if impact.get("v") is not None else 0.0)
        raw_a = float(impact.get("a") if impact.get("a") is not None else 0.0)
        raw_t = float(impact.get("trust") if impact.get("trust") is not None else 0.0)

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
                    logger.warning(f"[Observer] 丢弃畸形 fact: {fact!r}")

        # 4. 待跟进事项入库
        followups = data.get("followups")
        if isinstance(followups, list):
            now_dt = datetime.now()
            for fu in followups:
                if isinstance(fu, dict):
                    topic = str(fu.get("topic", "")).strip()
                    remind_hours = int(fu.get("remind_after_hours", 24))
                    if topic:
                        remind_dt = now_dt + timedelta(hours=remind_hours)
                        remind_str = remind_dt.strftime(TIME_FORMAT)
                        await self.db.execute(
                            """
                            INSERT INTO followups (topic, remind_after, done, created_at)
                            VALUES (?, ?, 0, ?)
                            """,
                            (topic, remind_str, now_str()),
                        )

        # 5. 标记完成的待跟进事项（精确匹配，不匹配则记日志）
        done_topics = data.get("done_followups")
        if isinstance(done_topics, list):
            for dt in done_topics:
                dt_str = str(dt).strip()
                if dt_str:
                    cur = await self.db.execute(
                        "UPDATE followups SET done = 1 WHERE topic = ? AND done = 0",
                        (dt_str,),
                    )
                    if cur.rowcount == 0:
                        logger.info(f"[Observer] done_followups 未精确匹配到待处理事项: '{dt_str}'")

        # 6. 收藏表情包回路 (§7.4)
        if user_image_path and data.get("collect_sticker") is True:
            sticker_name = str(data.get("sticker_name", "")).strip()
            if not sticker_name:
                sticker_name = f"表情_{now_str()[:10]}"
            await self.stickers.collect_sticker(
                image_path=user_image_path,
                sticker_name=sticker_name,
                gateway=self.gateway,
            )

        return data

    async def _call_observer_llm(self, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
        """调用模型并容错解析 JSON，失败返回中性默认"""
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
            return parse_llm_json(resp)
        except Exception as e:
            logger.warning(f"[Observer] 观察者调用或解析失败，使用中性默认: {e}")
            return default_res
