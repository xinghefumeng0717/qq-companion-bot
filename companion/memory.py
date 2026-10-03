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

from companion.db import (
    Database,
    COUNTER_KEY_TOTAL_TURNS,
    COUNTER_KEY_ARCHIVED_TURNS,
    STATE_KEY_AFFECTION,
    parse_dt,
    now_str,
)
from companion.gateway import LLMGateway, parse_llm_json
from companion.prompts import DIARY_SYSTEM_PROMPT, DIARY_USER_PROMPT

logger = logging.getLogger(__name__)

POSITIVE_SENTIMENTS = {"温暖", "感动", "幸福", "思念", "欢喜"}
NEGATIVE_SENTIMENTS = {"不安", "伤感"}
ALL_SENTIMENTS = POSITIVE_SENTIMENTS | NEGATIVE_SENTIMENTS | {"平静", "释然"}

# 记忆"可见/可加固"强度阈值：低于此值的日记视为已淡出，既不被召回注入，也不被加固复活
RECALL_VISIBLE_THRESHOLD = 0.5


DEFAULT_STAGE_NAMES = {
    0: "初识",
    1: "相识",
    2: "熟络",
    3: "同好",
    4: "知己",
    5: "微酸",
    6: "倾心",
    7: "依恋",
    8: "深情",
    9: "相守",
}


def calc_diary_strength(
    importance: float,
    recall_count: int,
    sentiment: str,
    days: float,
) -> Tuple[float, float]:
    """计算单条日记随时间衰减后的记忆强度 (strength) 与有效时间常数 (tau_effective)。

    返回: (strength, tau_effective)
    标定基准：
      - imp 1 ~ 7d
      - imp 5 ~ 78d
      - imp 8 pos ~ 300d
      - imp 10 pos 8 recalls ~ 3y
    """
    tau_base = max(10.0, importance * 6.8)
    tau_effective = tau_base * (1.0 + 0.15 * recall_count)
    # "正面"/"负面" 别名仅为兼容 score_simulation.py 的情感系数表，生产路径 sentiment 已收敛到 ALL_SENTIMENTS，此分支不可达
    if sentiment in POSITIVE_SENTIMENTS or sentiment == "正面":
        tau_effective *= 2.0
    elif sentiment in NEGATIVE_SENTIMENTS or sentiment == "负面":
        tau_effective *= 1.5

    strength = (
        importance
        * (1.0 + 0.3 * math.log2(recall_count + 1))
        * math.exp(-days / tau_effective)
    )
    return strength, tau_effective


class MemoryManager:
    def __init__(
        self,
        db: Database,
        gateway: Optional[LLMGateway] = None,
        affection: Optional[Any] = None,
        persona: Optional[Any] = None,
    ):
        self.db = db
        self.gateway = gateway
        self.affection = affection
        self.persona = persona
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
            "UPDATE counters SET value = value + 1 WHERE key = ?",
            (COUNTER_KEY_TOTAL_TURNS,),
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
                    "SELECT value FROM counters WHERE key = ?",
                    (COUNTER_KEY_ARCHIVED_TURNS,),
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
        """调用 LLM 将 8 轮对话压缩为第一人称日记，感知当前关系阶段，并在事务中原子落库更新游标"""
        formatted_turns = "\n".join(
            [f"{'机主' if r['role'] == 'user' else '我'}：{r['content']}" for r in turns]
        )

        stage_num = 0
        if self.affection:
            try:
                aff_st = await self.affection.get_state()
                stage_num = aff_st.get("stage", 0)
            except Exception:
                stage_num = 0
        else:
            aff_data = await self.db.get_state_json(STATE_KEY_AFFECTION)
            if aff_data and isinstance(aff_data, dict):
                try:
                    stage_num = int(aff_data.get("stage", 0))
                except Exception:
                    stage_num = 0

        if self.persona:
            stage_name = self.persona.get_stage(stage_num).name
        else:
            stage_name = DEFAULT_STAGE_NAMES.get(stage_num, f"阶段{stage_num}")

        user_prompt = DIARY_USER_PROMPT.format(
            stage_num=stage_num,
            stage_name=stage_name,
            conversation_turns=formatted_turns,
        )
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

        data = parse_llm_json(resp)

        content = str(data.get("content", "")).strip()
        importance = max(1, min(10, int(data.get("importance", 5))))
        sentiment = str(data.get("sentiment", "平静")).strip()
        if sentiment not in ALL_SENTIMENTS:
            sentiment = "平静"
        # 只接受字符串项：LLM 把 facts 写成整串时，list() 会把一句话拆成一条条单字事实
        facts_raw = data.get("facts", [])
        if not isinstance(facts_raw, list):
            logger.warning(f"[Memory] 日记 facts 类型异常 ({type(facts_raw).__name__}: {facts_raw!r})，已忽略")
            facts = []
        else:
            facts = []
            for item in facts_raw:
                if isinstance(item, str) and item.strip():
                    facts.append(item.strip())
                else:
                    logger.warning(f"[Memory] 丢弃畸形 fact ({type(item).__name__}: {item!r})")

        # 事务包裹：日记写入、事实更新与已归档游标推进原子执行
        async with self.db.transaction():
            current_time = now_str()
            await self.db.execute(
                """
                INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)
                VALUES (?, ?, ?, 0, ?, ?)
                """,
                (content, importance, sentiment, current_time, current_time),
            )

            # 重要性 >= 6 写入语义记忆
            if importance >= 6:
                for fact in facts:
                    await self.add_fact(fact)

            # 更新已归档游标为本次处理的最新 turn id
            await self.db.execute(
                "UPDATE counters SET value = ? WHERE key = ?",
                (new_cursor_id, COUNTER_KEY_ARCHIVED_TURNS),
            )

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

        logger.info(f"[Memory] 成功归档日记 (游标推进至 id={new_cursor_id}): 《{content[:20]}...》 importance={importance}, sentiment={sentiment}")

    # ==========================================
    # 3. 回忆加固与遗忘曲线 (§8.3)
    # ==========================================

    @staticmethod
    def calc_row_strength(row: Any, now_dt: datetime, fallback_time: str) -> float:
        """按遗忘曲线计算一行日记的当前强度（last_recall_at 缺失时退化为 created_at）"""
        importance = float(row["importance"] or 5)
        recall_count = int(row["recall_count"] or 0)
        sentiment = str(row["sentiment"] or "平静")
        last_recall_str = row["last_recall_at"] or row["created_at"] or fallback_time
        last_dt = parse_dt(last_recall_str)
        days = max(0.0, (now_dt - last_dt).total_seconds() / 86400.0) if last_dt else 0.0
        strength, _ = calc_diary_strength(importance, recall_count, sentiment, days)
        return strength

    async def reinforce_memories(self, user_message: str) -> None:
        """回忆加固：含‘还记得’/‘想你’时强度 >= 0.5 的日记 +1；与日记有 >= 3 个共同汉字时该条+1（同样只加固强度 >= 0.5 的日记）"""
        if not user_message:
            return

        current_time = now_str()
        now_dt = datetime.now()
        # 1. 触发关键词加固（只加固当前强度 >= 0.5 的日记，与回忆注入可见阈值一致）
        if "还记得" in user_message or "想你" in user_message:
            rows = await self.db.fetchall(
                """
                SELECT id, importance, recall_count, sentiment, created_at, last_recall_at
                FROM diary
                """
            )
            reinforced = 0
            for r in rows:
                if self.calc_row_strength(r, now_dt, current_time) < RECALL_VISIBLE_THRESHOLD:
                    continue
                await self.db.execute(
                    "UPDATE diary SET recall_count = recall_count + 1, last_recall_at = ? WHERE id = ?",
                    (current_time, r["id"]),
                )
                reinforced += 1
            if reinforced:
                logger.info(f"[Memory] 触发关键词回忆加固 {reinforced} 条")
            else:
                logger.info("[Memory] 关键词触发但无可加固日记（强度均低于 0.5）")
            return

        # 2. 汉字共现加固 (≥ 3 个共同汉字)，与关键词分支同语义：已淡出的日记不得被复活
        user_hanzi = set(re.findall(r"[\u4e00-\u9fa5]", user_message))
        if len(user_hanzi) < 3:
            return

        rows = await self.db.fetchall(
            """
            SELECT id, content, importance, recall_count, sentiment, created_at, last_recall_at
            FROM diary
            """
        )
        reinforced = 0
        for r in rows:
            diary_hanzi = set(re.findall(r"[\u4e00-\u9fa5]", r["content"]))
            common = user_hanzi.intersection(diary_hanzi)
            if len(common) < 3:
                continue
            if self.calc_row_strength(r, now_dt, current_time) < RECALL_VISIBLE_THRESHOLD:
                continue
            await self.db.execute(
                "UPDATE diary SET recall_count = recall_count + 1, last_recall_at = ? WHERE id = ?",
                (current_time, r["id"]),
            )
            reinforced += 1
        if reinforced:
            logger.info(f"[Memory] 汉字共现回忆加固 {reinforced} 条")

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

            last_dt = parse_dt(last_recall_str)
            days = max(0.0, (now_dt - last_dt).total_seconds() / 86400.0) if last_dt else 0.0

            strength, tau_effective = calc_diary_strength(importance, recall_count, sentiment, days)

            if strength >= RECALL_VISIBLE_THRESHOLD:
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
        """添加关于机主的语义记忆事实（支持字符 Jaccard 相似度 >= 0.6 近似去重）"""
        content = content.strip()
        if not content:
            return

        new_chars = set(re.findall(r"[\u4e00-\u9fa5]", content))
        if not new_chars:
            new_chars = set(content.lower().split()) or set(content.lower())

        rows = await self.db.fetchall("SELECT id, content FROM facts")
        best_sim = 0.0
        best_fact = ""
        best_id = None

        for r in rows:
            exist_content = r["content"]
            exist_chars = set(re.findall(r"[\u4e00-\u9fa5]", exist_content))
            if not exist_chars:
                exist_chars = set(exist_content.lower().split()) or set(exist_content.lower())
            union = new_chars | exist_chars
            sim = len(new_chars & exist_chars) / len(union) if union else 0.0
            if sim > best_sim:
                best_sim = sim
                best_fact = exist_content
                best_id = r["id"]

        if best_sim >= 0.6 and best_id is not None:
            await self.db.execute(
                "UPDATE facts SET created_at = ? WHERE id = ?",
                (now_str(), best_id),
            )
            logger.info(
                f"[Memory] 事实近义去重命中: 《{content}》 与既有 《{best_fact}》 相似度 {best_sim:.2f}，跳过插入"
            )
            return

        await self.db.execute(
            "INSERT OR IGNORE INTO facts (content, created_at) VALUES (?, ?)",
            (content, now_str()),
        )

    async def get_all_facts(self) -> List[str]:
        """获取所有关于机主的事实"""
        rows = await self.db.fetchall("SELECT content FROM facts ORDER BY id ASC")
        return [r["content"] for r in rows]

    async def supersede_fact(self, old_content: str, new_content: str) -> bool:
        """用新事实作废旧事实（"国庆期间打算坐动车回家" → "国庆已坐动车到家"）。

        匹配算法与 add_fact 同源（字符 Jaccard，此处为模块内第三份拷贝，
        按 FIXES11 负面清单不重构合并），但阈值放宽到 >= 0.4：观察者回传的 old
        常有表述漂移，add_fact 的 0.6 命中不了。
        命中：同事务内删旧行 + 走 add_fact 插新事实，返回 True；
        未命中：只插新事实（等价 add_fact），返回 False。
        """
        new_content = (new_content or "").strip()
        if not new_content:
            logger.warning("[Memory] supersede_fact 收到空的新事实，已忽略")
            return False

        old_content = (old_content or "").strip()
        old_chars = set(re.findall(r"[\u4e00-\u9fa5]", old_content))
        if not old_chars:
            old_chars = set(old_content.lower().split()) or set(old_content.lower())

        rows = await self.db.fetchall("SELECT id, content FROM facts")
        best_sim = 0.0
        best_fact = ""
        best_id = None
        for r in rows:
            exist_content = r["content"]
            exist_chars = set(re.findall(r"[\u4e00-\u9fa5]", exist_content))
            if not exist_chars:
                exist_chars = set(exist_content.lower().split()) or set(exist_content.lower())
            union = old_chars | exist_chars
            sim = len(old_chars & exist_chars) / len(union) if union else 0.0
            if sim > best_sim:
                best_sim = sim
                best_fact = exist_content
                best_id = r["id"]

        if best_id is None or best_sim < 0.4:
            await self.add_fact(new_content)
            logger.info(f"[Memory] 事实作废未命中(相似度 {best_sim:.2f} < 0.4)，仅新增: 《{new_content}》")
            return False

        async with self.db.transaction():
            await self.db.execute("DELETE FROM facts WHERE id = ?", (best_id,))
            await self.add_fact(new_content)
        logger.info(
            f"[Memory] 事实作废命中: 《{best_fact}》(相似度 {best_sim:.2f}) → 《{new_content}》"
        )
        return True
