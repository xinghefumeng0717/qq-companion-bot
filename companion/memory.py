"""记忆系统 (memory.py)
三层记忆结构：工作记忆 (turns)、情景记忆/日记 (diary + 遗忘曲线 + 回忆加固 + 归档) 与语义事实 (facts)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from companion.db import Database, now_str
from companion.gateway import LLMGateway
from companion.prompts import DIARY_SYSTEM_PROMPT, DIARY_USER_PROMPT

logger = logging.getLogger(__name__)

POSITIVE_SENTIMENTS = {"温暖", "感动", "幸福", "思念", "欢喜"}
NEGATIVE_SENTIMENTS = {"不安", "伤感"}
ALL_SENTIMENTS = POSITIVE_SENTIMENTS | NEGATIVE_SENTIMENTS | {"平静", "释然"}


class MemoryManager:
    def __init__(self, db: Database, gateway: Optional[LLMGateway] = None):
        self.db = db
        self.gateway = gateway
        self._archive_lock = asyncio.Lock()
        self._archive_task: Optional[asyncio.Task] = None

    # ==========================================
    # 1. 工作记忆 (turns)
    # ==========================================

    async def get_recent_turns(self, limit: int = 10) -> List[Dict[str, Any]]:
        """获取最近的工作记忆（最近 limit 轮，即最多 limit*2 条消息，按时间升序）
        截取后若首条为 assistant 则裁掉到以 user 开头为止（保持严格的 user/assistant 对话结构）
        """
        rows = await self.db.fetchall(
            """
            SELECT id, role, content, proactive, has_image, created_at
            FROM turns
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit * 2,),
        )
        turns = [dict(r) for r in reversed(rows)]
        while turns and turns[0]["role"] != "user":
            turns.pop(0)
        return turns

    async def save_turn_pair(
        self,
        user_msg: str,
        bot_msg: str,
        has_image: bool = False,
    ) -> None:
        """保存一轮对话（用户消息 + 机器人回复），递增总轮数，并在后台非阻塞触发日记归档"""
        current_time = now_str()
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('user', ?, 0, ?, ?)
            """,
            (user_msg, 1 if has_image else 0, current_time),
        )
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('assistant', ?, 0, 0, ?)
            """,
            (bot_msg, current_time),
        )

        # 更新计数器
        await self.db.execute(
            "UPDATE counters SET value = value + 1 WHERE key = 'total_turns'"
        )

        # 异步非阻塞执行日记归档检查（加锁防并发，PLAN §1.1）
        asyncio.create_task(self.check_and_trigger_diary_archive())

    async def save_proactive_turn(self, bot_msg: str) -> None:
        """保存一条机器人主动发送的消息"""
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('assistant', ?, 1, 0, ?)
            """,
            (bot_msg, now_str()),
        )

    # ==========================================
    # 2. 情景记忆与日记归档 (diary)
    # ==========================================

    async def check_and_trigger_diary_archive(self) -> None:
        """检查并执行日记归档（后台异步执行，基于 turn id 游标区间与加锁防并发）"""
        if not self.gateway:
            return

        async with self._archive_lock:
            try:
                # 获取上次归档到的 turn id 游标 (archived_turns)
                arch_row = await self.db.fetchone(
                    "SELECT value FROM counters WHERE key = 'archived_turns'"
                )
                last_archived_id = arch_row["value"] if arch_row else 0

                # 查询 id > last_archived_id 的所有消息
                rows = await self.db.fetchall(
                    """
                    SELECT id, role, content, proactive, created_at
                    FROM turns
                    WHERE id > ?
                    ORDER BY id ASC
                    """,
                    (last_archived_id,),
                )
                if not rows:
                    return

                # 检查未归档的 user 轮数是否 >= 8
                user_turns = [r for r in rows if r["role"] == "user"]
                if len(user_turns) < 8:
                    return

                # 截取前 8 轮对应的消息段（直到第 8 个 user 及其对应的 assistant 回复）
                turns_to_archive = []
                user_count = 0
                new_cursor_id = last_archived_id

                for r in rows:
                    turns_to_archive.append(r)
                    if r["role"] == "user":
                        user_count += 1
                    if user_count == 8 and r["role"] == "assistant":
                        new_cursor_id = r["id"]
                        break
                else:
                    new_cursor_id = turns_to_archive[-1]["id"]

                await self.archive_diary(turns_to_archive, new_cursor_id)
            except Exception as e:
                logger.error(f"[Memory] 日记归档异常: {e}", exc_info=True)

    async def archive_diary(self, turns: List[Any], new_cursor_id: int) -> None:
        """调用 LLM 将 8 轮对话压缩为第一人称日记，并更新 turn id 游标"""
        formatted_turns = "\n".join(
            [f"{'机主' if r['role'] == 'user' else '我'}：{r['content']}" for r in turns]
        )

        user_prompt = DIARY_USER_PROMPT.format(conversation_turns=formatted_turns)
        messages = [
            {"role": "system", "content": DIARY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        resp = await self.gateway.chat(
            messages=messages,
            model=self.gateway.config.observer_model,
            temperature=0.7,
            json_mode=True,
            purpose="diary_archive",
        )

        try:
            data = json.loads(resp)
        except Exception:
            clean = re.sub(r"^```json\s*|\s*```$", "", resp.strip(), flags=re.MULTILINE)
            data = json.loads(clean)

        content = str(data.get("content", "")).strip()
        importance = max(1, min(10, int(data.get("importance", 5))))
        sentiment = str(data.get("sentiment", "平静")).strip()
        if sentiment not in ALL_SENTIMENTS:
            sentiment = "平静"
        facts = list(data.get("facts", []))

        current_time = now_str()
        await self.db.execute(
            """
            INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (content, importance, sentiment, current_time, current_time),
        )

        # 重要性 >= 6 写入语义记忆
        if importance >= 6 and facts:
            for fact in facts:
                fact_str = str(fact).strip()
                if fact_str:
                    await self.add_fact(fact_str)

        # 更新已归档游标为本次处理的最新 turn id
        await self.db.execute(
            "UPDATE counters SET value = ? WHERE key = 'archived_turns'",
            (new_cursor_id,),
        )
        logger.info(f"[Memory] 成功归档日记 (游标推进至 id={new_cursor_id}): 《{content[:20]}...》 importance={importance}, sentiment={sentiment}")

        # 检查日记总数是否 > 500
        count_row = await self.db.fetchone("SELECT COUNT(*) as cnt FROM diary")
        if count_row and count_row["cnt"] > 500:
            half = count_row["cnt"] // 2
            old_rows = await self.db.fetchall(
                "SELECT id, content, importance, sentiment, recall_count, created_at, last_recall_at FROM diary ORDER BY id ASC LIMIT ?",
                (half,),
            )
            for r in old_rows:
                await self.db.execute(
                    """
                    INSERT INTO diary_archive (content, importance, sentiment, recall_count, created_at, last_recall_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (r["content"], r["importance"], r["sentiment"], r["recall_count"], r["created_at"], r["last_recall_at"]),
                )
                await self.db.execute("DELETE FROM diary WHERE id = ?", (r["id"],))
            logger.info(f"[Memory] 已将最旧的 {half} 条日记转存入 diary_archive")

    # ==========================================
    # 3. 回忆加固与遗忘曲线 (§8.3)
    # ==========================================

    async def reinforce_memories(self, user_message: str) -> None:
        """回忆加固：含‘还记得’/‘想你’全部+1；与日记有 >= 3 个共同汉字时该条+1"""
        if not user_message:
            return

        current_time = now_str()
        # 1. 触发关键词全部加固
        if "还记得" in user_message or "想你" in user_message:
            await self.db.execute(
                "UPDATE diary SET recall_count = recall_count + 1, last_recall_at = ?",
                (current_time,),
            )
            logger.info("[Memory] 触发关键词全部回忆加固")
            return

        # 2. 汉字共现加固 (≥ 3 个共同汉字)
        user_hanzi = set(re.findall(r"[\u4e00-\u9fa5]", user_message))
        if len(user_hanzi) < 3:
            return

        rows = await self.db.fetchall("SELECT id, content FROM diary")
        for r in rows:
            diary_hanzi = set(re.findall(r"[\u4e00-\u9fa5]", r["content"]))
            common = user_hanzi.intersection(diary_hanzi)
            if len(common) >= 3:
                await self.db.execute(
                    "UPDATE diary SET recall_count = recall_count + 1, last_recall_at = ? WHERE id = ?",
                    (current_time, r["id"]),
                )

    async def get_active_diaries(self, current_valence: float = 2.0) -> List[str]:
        """按遗忘曲线计算记忆强度，返回按强度降序且分级前缀格式化的日记列表（最多 15 条）"""
        rows = await self.db.fetchall(
            """
            SELECT id, content, importance, sentiment, recall_count, created_at, last_recall_at
            FROM diary
            """
        )
        if not rows:
            return []

        now_dt = datetime.now()
        scored_diaries: List[Tuple[float, float, str]] = []

        for r in rows:
            importance = float(r["importance"] or 5)
            recall_count = int(r["recall_count"] or 0)
            sentiment = str(r["sentiment"] or "平静")
            last_recall_str = r["last_recall_at"] or r["created_at"] or now_str()

            try:
                last_dt = datetime.strptime(last_recall_str, "%Y-%m-%d %H:%M")
                days = max(0.0, (now_dt - last_dt).total_seconds() / 86400.0)
            except Exception:
                days = 0.0

            # 遗忘曲线公式
            tau_base = max(20.0, importance * 20.0)
            tau_effective = tau_base * (1.0 + 0.15 * recall_count)
            if sentiment in POSITIVE_SENTIMENTS:
                tau_effective *= 2.0
            elif sentiment in NEGATIVE_SENTIMENTS:
                tau_effective *= 1.5

            strength = importance * (1.0 + 0.3 * math.log2(recall_count + 1)) * math.exp(-days / tau_effective)

            if strength >= 0.5:
                # 情绪一致性加权排序
                sort_weight = strength
                if current_valence < 0 and sentiment in NEGATIVE_SENTIMENTS:
                    sort_weight *= 1.5
                scored_diaries.append((sort_weight, strength, r["content"]))

        # 按加权排序降序，取最多 15 条
        scored_diaries.sort(key=lambda x: x[0], reverse=True)
        top_diaries = scored_diaries[:15]

        # 前缀分级：>=5 "清晰地记得"、>=2 "记得"、>=0.5 "隐约记得"
        result = []
        for _, strength, content in top_diaries:
            if strength >= 5.0:
                prefix = "清晰地记得: "
            elif strength >= 2.0:
                prefix = "记得: "
            else:
                prefix = "隐约记得: "
            result.append(f"{prefix}{content}")

        return result

    # ==========================================
    # 4. 语义事实 (facts)
    # ==========================================

    async def add_fact(self, content: str) -> None:
        """添加关于机主的语义记忆事实（精确去重）"""
        content = content.strip()
        if not content:
            return
        await self.db.execute(
            "INSERT OR IGNORE INTO facts (content, created_at) VALUES (?, ?)",
            (content, now_str()),
        )

    async def get_all_facts(self) -> List[str]:
        """获取所有关于机主的事实"""
        rows = await self.db.fetchall("SELECT content FROM facts ORDER BY id ASC")
        return [r["content"] for r in rows]
