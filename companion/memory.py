"""记忆系统 (memory.py)
三层记忆结构：工作记忆 (turns)、情景记忆/日记 (diary + 遗忘曲线 + 回忆加固 + 归档) 与语义事实 (facts)。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from companion.db import (
    Database,
    COUNTER_KEY_TOTAL_TURNS,
    COUNTER_KEY_ARCHIVED_TURNS,
    STATE_KEY_AFFECTION,
    TIME_FORMAT,
    parse_dt,
    now_str,
)
from companion.gateway import LLMGateway, parse_llm_json
from companion.prompts import (
    DIARY_USER_PROMPT,
    diary_system_prompt,
    IMPRESSIONS_PROMPT,
    IMPRESSIONS_USER,
    CORE_CONSOLIDATE_PROMPT,
)

logger = logging.getLogger(__name__)

POSITIVE_SENTIMENTS = {"温暖", "感动", "幸福", "思念", "欢喜"}
NEGATIVE_SENTIMENTS = {"不安", "伤感"}
ALL_SENTIMENTS = POSITIVE_SENTIMENTS | NEGATIVE_SENTIMENTS | {"平静", "释然"}

# 记忆"可见/可加固"强度阈值：低于此值的日记视为已淡出，既不被召回注入，也不被加固复活
RECALL_VISIBLE_THRESHOLD = 0.5

# 待跟进过期：提醒时间过去这么久还没被勾掉，就当这件事已经过去了（done=2）。
# 生产实锤（2026-10-10~18）：他 10-09 后没再回消息，observer 永远没机会勾掉
# "明天七点起床"，这条在【待跟进】与主动消息素材里挂了 9 天，她天天追问"七点计划还算不算数"。
FOLLOWUP_EXPIRE_HOURS = 48
FOLLOWUP_DONE = 1
FOLLOWUP_EXPIRED = 2

# 欲言又止念头的寿命（与 assembler 旧口径一致）
DESIRE_TTL_HOURS = 48

# 自我记忆分层：核心常驻上限 / 合并后目标条数；非核心每轮带最近几条 + 相关几条
SELF_CORE_LIMIT = 30
SELF_CORE_TARGET = 24
SELF_RECENT_N = 5
SELF_RELATED_N = 5
# 关于他的事分层：核心常驻上限 / 合并后目标；非核心每轮带最近几条 + 相关几条
FACT_CORE_LIMIT = 30
FACT_CORE_TARGET = 24
FACT_RECENT_N = 8
FACT_RELATED_N = 5
# 对他的看法最多几条
IMPRESSIONS_MAX = 8
# 相关检索时不算数的虚字（问句里到处都是，命中了也说明不了相关）
SELF_STOP_CHARS = set("你我她他它的了是啥来着说上次在有个这那也就都还吗呢吧啊么什么怎样一不没很好诶哎嗯哦")


def mmdd_of(time_str: Optional[str]) -> str:
    """'2026-10-08 17:03' -> '10-08'；解析不了返回空串。"""
    dt = parse_dt(time_str)
    return dt.strftime("%m-%d") if dt else ""


# 事实正文里的日期（10-22 / 10/22 / 10月22日，带年份的取月日部分）。
# 见面纪要形如"10-22 见面：……"，两次见面的字面几乎一样，只比汉字会把它们判成同一条。
_FACT_DATE_RE = re.compile(r"(?<!\d)(\d{1,2})\s*[-/.月]\s*(\d{1,2})(?!\d)")


def fact_dates(text: str) -> set:
    """抽出正文里的月日集合；没有合法日期返回空集。"""
    dates = set()
    for month, day in _FACT_DATE_RE.findall(text or ""):
        m, d = int(month), int(day)
        if 1 <= m <= 12 and 1 <= d <= 31:
            dates.add((m, d))
    return dates


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
        # 自我记忆开关（[character].self_memory）：开着时日记归档后顺带合并核心自我记忆
        self.self_memory = False
        # 对他的看法开关（[character].impressions）：开着时日记归档后顺带更新看法
        self.impressions = False
        # 关于他的事分层（[character].fact_layers）：开着时提示词只取核心+最近+相关，并定期合并核心
        self.fact_layers = False

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
        bot_msg: Optional[str],
        has_image: bool = False,
    ) -> None:
        """保存一轮对话（用户消息 + 机器人回复），递增总轮数，并在后台非阻塞触发日记归档。

        bot_msg 传 None 表示她本轮选择沉默（[沉默]）：只落用户消息，不落 assistant 记录，
        避免 turns 里出现空回复污染后续工作记忆。
        """
        current_time = now_str()
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('user', ?, 0, ?, ?)
            """,
            (user_msg, 1 if has_image else 0, current_time),
        )
        if bot_msg is not None:
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

    async def get_last_assistant_turn_time(self) -> Optional[datetime]:
        """最近一条 assistant 消息的落库时间；她从没回过话（或时间戳损坏）返回 None。

        FIXES15 首条判定专用：对话激活窗口 = 距这条记录 < active_conversation_window。
        主动消息（proactive=1）同样算"她回过话"，所以不过滤 proactive。
        """
        row = await self.db.fetchone(
            "SELECT created_at FROM turns WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        )
        if not row:
            return None
        return parse_dt(row["created_at"])

    async def get_last_user_turn_time(self) -> Optional[datetime]:
        """他最近一次说话的落库时间；从没说过话返回 None。"""
        row = await self.db.fetchone(
            "SELECT created_at FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1"
        )
        if not row:
            return None
        return parse_dt(row["created_at"])

    # ==========================================
    # 1.5 待跟进 / 欲言又止的时效（提示词与主动消息的唯一取数口）
    # ==========================================

    async def expire_stale_followups(self, now: Optional[datetime] = None) -> int:
        """提醒时间已过 FOLLOWUP_EXPIRE_HOURS 仍未完成的待跟进，置为过期（done=2）。"""
        cutoff = ((now or datetime.now()) - timedelta(hours=FOLLOWUP_EXPIRE_HOURS)).strftime(TIME_FORMAT)
        cur = await self.db.execute(
            "UPDATE followups SET done = ? WHERE done = 0 AND remind_after < ?",
            (FOLLOWUP_EXPIRED, cutoff),
        )
        n = cur.rowcount or 0
        if n > 0:
            logger.info(f"[Memory] {n} 条待跟进超过 {FOLLOWUP_EXPIRE_HOURS} 小时没下文，已按过期处理")
        return n

    async def get_open_followups_readonly(self, due_only: bool = False, limit: int = 5, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """只读取待跟进，不执行过期写回；筛选口径与旧接口一致。"""
        now_dt = now or datetime.now()
        cutoff = (now_dt - timedelta(hours=FOLLOWUP_EXPIRE_HOURS)).strftime(TIME_FORMAT)
        if due_only:
            rows = await self.db.fetchall(
                "SELECT id, topic, remind_after, created_at FROM followups "
                "WHERE done = 0 AND remind_after <= ? AND remind_after >= ? "
                "ORDER BY id ASC LIMIT ?",
                (now_dt.strftime(TIME_FORMAT), cutoff, limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT id, topic, remind_after, created_at FROM followups "
                "WHERE done = 0 AND remind_after >= ? ORDER BY id ASC LIMIT ?",
                (cutoff, limit),
            )
        return [dict(r) for r in rows]

    async def get_open_followups(self, due_only: bool = False, limit: int = 5, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """未完成且未过期的待跟进（先顺手清掉过期的）。due_only=True 只要提醒时间已到的。"""
        await self.expire_stale_followups(now=now)
        if due_only:
            rows = await self.db.fetchall(
                "SELECT id, topic, remind_after, created_at FROM followups "
                "WHERE done = 0 AND remind_after <= ? ORDER BY id ASC LIMIT ?",
                ((now or datetime.now()).strftime(TIME_FORMAT), limit),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT id, topic, remind_after, created_at FROM followups "
                "WHERE done = 0 ORDER BY id ASC LIMIT ?",
                (limit,),
            )
        return [dict(r) for r in rows]

    async def mark_followup_done(self, followup_id: int) -> None:
        await self.db.execute(
            "UPDATE followups SET done = ? WHERE id = ?", (FOLLOWUP_DONE, followup_id)
        )

    @staticmethod
    def format_followup(row: Dict[str, Any]) -> str:
        """带上记录日期；旧数据没有来源字段，不猜测是用户还是她的计划。"""
        day = mmdd_of(row.get("created_at"))
        return f"{row['topic']}（{day} 记下）" if day else str(row["topic"])

    async def get_live_desires_readonly(self, limit: int = 5, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """只筛选仍作数的念头，不删除过期行，供只读心理快照使用。"""
        rows = await self.db.fetchall(
            "SELECT id, content, created_at FROM suppressed_desires ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        now_dt = now or datetime.now()
        last_user = await self.get_last_user_turn_time()
        live: List[Dict[str, Any]] = []
        for r in rows:
            c_dt = parse_dt(r["created_at"])
            if c_dt and (now_dt - c_dt).total_seconds() > DESIRE_TTL_HOURS * 3600:
                continue
            if c_dt and last_user and last_user > c_dt:
                continue
            live.append(dict(r))
        return live

    async def get_live_desires(self, limit: int = 5, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """还作数的欲言又止念头（新到旧）。

        两条作废规则：
          1. 超过 DESIRE_TTL_HOURS（顺手删掉）；
          2. 念头产生之后他又跟她聊过天——当时忍住没说的话，聊过之后场景已经变了
             （生产实锤：10-07 存的"明天背着报告回学校"，10-08 下午她已到校，
             当晚和次日的主动消息又拿它说"明天还得回学校"）。这类只是不再取用，行保留。
        """
        rows = await self.db.fetchall(
            "SELECT id, content, created_at FROM suppressed_desires ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        now_dt = now or datetime.now()
        last_user = await self.get_last_user_turn_time()
        live: List[Dict[str, Any]] = []
        for r in rows:
            c_dt = parse_dt(r["created_at"])
            if c_dt and (now_dt - c_dt).total_seconds() > DESIRE_TTL_HOURS * 3600:
                await self.db.execute("DELETE FROM suppressed_desires WHERE id = ?", (r["id"],))
                continue
            if c_dt and last_user and last_user > c_dt:
                continue
            live.append(dict(r))
        return live

    # ==========================================
    # 2. 情景记忆与日记归档 (diary)
    # ==========================================

    async def check_and_trigger_diary_archive(self) -> None:
        """检查并执行日记归档（后台异步执行，基于 turn id 游标区间与加锁防并发）"""
        if not self.gateway:
            return

        async with self._archive_lock:
            turns_to_archive: List[Any] = []
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
            # 日记失败不连累合并（合并自己失败也只记日志，保持原样）
            if self.self_memory is True:
                try:
                    await self.consolidate_self_core()
                except Exception as e:
                    logger.error(f"[Memory] 核心自我记忆合并异常: {e}")
            if self.fact_layers is True:
                try:
                    await self.consolidate_fact_core()
                except Exception as e:
                    logger.error(f"[Memory] 关于他的核心记忆合并异常: {e}")
            if self.impressions is True and turns_to_archive:
                try:
                    await self.update_impressions(turns_to_archive)
                except Exception as e:
                    logger.error(f"[Memory] 更新对他的看法异常: {e}")

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
            {"role": "system", "content": diary_system_prompt(self.persona.stages if self.persona else None)},
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

    async def get_active_diaries(self, current_valence: float = 2.0, now: Optional[datetime] = None) -> List[str]:
        """按遗忘曲线计算记忆强度，返回按强度降序且分级前缀格式化的日记列表（最多 15 条）"""
        rows = await self.db.fetchall(
            """
            SELECT id, content, importance, sentiment, recall_count, created_at, last_recall_at
            FROM diary
            """
        )
        if not rows:
            return []

        now_dt = now or datetime.now()
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

    async def add_fact(self, content: str, core: bool = False) -> None:
        """添加关于机主的语义记忆事实（字符 Jaccard 相似度 >= 0.6 近似去重；核心标记只升不降）。

        去重比的是"同一件事"：两边都带日期且日期不同（没有一天重合）时不算近义。
        否则两次"和他在武陵城吃了火锅"的见面纪要字面相似度 0.7+，第二次会被当成
        旧事实的更新只改时间，纪要就没了。
        """
        content = content.strip()
        if not content:
            return

        new_chars = set(re.findall(r"[\u4e00-\u9fa5]", content))
        if not new_chars:
            new_chars = set(content.lower().split()) or set(content.lower())
        new_dates = fact_dates(content)

        rows = await self.db.fetchall("SELECT id, content, core FROM facts")
        best_sim = 0.0
        best_fact = ""
        best_id = None
        best_core = 0

        for r in rows:
            exist_content = r["content"]
            if new_dates:
                exist_dates = fact_dates(exist_content)
                if exist_dates and not (new_dates & exist_dates):
                    continue
            exist_chars = set(re.findall(r"[\u4e00-\u9fa5]", exist_content))
            if not exist_chars:
                exist_chars = set(exist_content.lower().split()) or set(exist_content.lower())
            union = new_chars | exist_chars
            sim = len(new_chars & exist_chars) / len(union) if union else 0.0
            if sim > best_sim:
                best_sim = sim
                best_fact = exist_content
                best_id = r["id"]
                best_core = int(r["core"] or 0)

        if best_sim >= 0.6 and best_id is not None:
            await self.db.execute(
                "UPDATE facts SET created_at = ?, core = ? WHERE id = ?",
                (now_str(), int(core or best_core), best_id),
            )
            logger.info(
                f"[Memory] 事实近义去重命中: 《{content}》 与既有 《{best_fact}》 相似度 {best_sim:.2f}，跳过插入"
            )
            return

        await self.db.execute(
            "INSERT OR IGNORE INTO facts (content, core, created_at) VALUES (?, ?, ?)",
            (content, int(core), now_str()),
        )

    async def get_all_facts(self) -> List[str]:
        """获取所有关于机主的事实"""
        rows = await self.db.fetchall("SELECT content FROM facts ORDER BY id ASC")
        return [r["content"] for r in rows]

    async def get_facts_for_prompt(self, query: str = "") -> List[str]:
        """提示词用的事实：每条带上记下的日期（"打算明天开始每天跑步（10-08 记下）"）。

        生产实锤：事实里的"明天"没有日期锚点，19 号她读到的还是"明天"。
        库里存的仍是原文（去重/作废按原文比对），日期只在注入时拼上。
        fact_layers 开着时只取：核心全部 + 最近 FACT_RECENT_N 条 + 与 query 最相关的 FACT_RELATED_N 条。
        """
        rows = await self.db.fetchall("SELECT id, content, core, created_at FROM facts ORDER BY id ASC")
        if self.fact_layers is True:
            by_time = sorted(rows, key=lambda r: (r["created_at"] or "", r["id"]))
            keep = self._pick_layered(by_time, query, FACT_CORE_LIMIT, FACT_RECENT_N, FACT_RELATED_N)
            rows = [r for r in rows if r["id"] in keep]
        out = []
        for r in rows:
            day = mmdd_of(r["created_at"])
            out.append(f"{r['content']}（{day} 记下）" if day else r["content"])
        return out

    # ==========================================
    # 4.5 自我记忆 (self_facts)：她说过的关于自己的事，分三层
    #   核心（身份/家/长期喜好）一直带着，超过 SELF_CORE_LIMIT 条就在日记归档时合并；
    #   其余的只带最近说的几条 + 和这句话相关的几条，别的留在库里，她"想不太起来"
    # ==========================================

    @staticmethod
    def _char_set(text: str) -> set:
        chars = set(re.findall(r"[\u4e00-\u9fa5]", text))
        return chars or set(text.lower().split()) or set(text.lower())

    @staticmethod
    def _bigram_set(text: str) -> set:
        hz = "".join(re.findall(r"[\u4e00-\u9fa5]", text or ""))
        return {hz[i:i + 2] for i in range(len(hz) - 1)}

    @staticmethod
    def _content_chars(text: str) -> set:
        return set(re.findall(r"[\u4e00-\u9fa5]", text or "")) - SELF_STOP_CHARS

    def _relevance(self, query_grams: set, query_chars: set, fact: str) -> float:
        """字面相关度：两字词命中算 2 分、实字命中算 1 分，按事实长度开方归一（短句不吃亏也不占便宜）。"""
        hits = 2 * len(query_grams & self._bigram_set(fact)) + len(query_chars & self._content_chars(fact))
        return hits / math.sqrt(max(len(fact), 1))

    async def _closest_self_fact(self, text: str) -> Tuple[Optional[int], str, float, int]:
        target = self._char_set(text)
        best: Tuple[Optional[int], str, float, int] = (None, "", 0.0, 0)
        for r in await self.db.fetchall("SELECT id, content, core FROM self_facts"):
            other = self._char_set(r["content"])
            union = target | other
            sim = len(target & other) / len(union) if union else 0.0
            if sim > best[2]:
                best = (r["id"], r["content"], sim, int(r["core"] or 0))
        return best

    @staticmethod
    def _is_ephemeral_self_fact(content: str) -> bool:
        """是否属于提示词明确禁止长期保存的当天状态/行程安排。

        长期偏好、习惯和身份可能也会用“现在”表达，先排除这些稳定语义；
        只对“时间词 + 行踪/当下动作”命中，避免误伤真正的长期记忆。
        """
        text = (content or "").strip()
        if not text or re.search(r"喜欢|讨厌|偏好|习惯|老家|家人|身份|爱吃|不吃|学会", text):
            return False
        if not re.search(r"今天|今日|刚才|刚刚|现在|正在|今晚|今早|这顿|这会儿|目前|当下|眼前", text):
            return False
        return bool(re.search(r"在|去|逛|吃|忙|赶|睡|做|买|看|走|玩|回|到|待|呆|出门|出发|抵达|打算|计划|安排", text))

    async def add_self_fact(self, content: str, core: bool = False) -> None:
        """记一条她说过的关于自己的事；近义（字符 Jaccard ≥ 0.6）只刷新时间、核心标记取或。"""
        content = (content or "").strip()
        if not content:
            return
        if not core and self._is_ephemeral_self_fact(content):
            logger.info(f"[Memory] 跳过当天状态类自我记忆（不进入长期注入）: {content}")
            return
        best_id, best_text, sim, best_core = await self._closest_self_fact(content)
        if best_id is not None and sim >= 0.6:
            await self.db.execute(
                "UPDATE self_facts SET created_at = ?, core = ? WHERE id = ?",
                (now_str(), int(core or best_core), best_id),
            )
            logger.info(f"[Memory] 自我记忆近义命中: 《{content}》≈《{best_text}》({sim:.2f})")
            return
        await self.db.execute(
            "INSERT OR IGNORE INTO self_facts (content, core, created_at) VALUES (?, ?, ?)",
            (content, int(core), now_str()),
        )

    async def supersede_self_fact(self, old_content: str, new_content: str) -> bool:
        """她改口了：删掉旧说法，新说法带上 prev（人会变，但要知道自己变过）。"""
        new_content = (new_content or "").strip()
        if not new_content:
            return False
        if self._is_ephemeral_self_fact(new_content):
            logger.info(f"[Memory] 改口内容属于当天状态，不写入长期自我记忆: {new_content}")
            return False
        best_id, best_text, sim, best_core = await self._closest_self_fact((old_content or "").strip())
        if best_id is None or sim < 0.4:
            await self.add_self_fact(new_content)
            return False
        async with self.db.transaction():
            await self.db.execute("DELETE FROM self_facts WHERE id = ?", (best_id,))
            await self.db.execute(
                "INSERT OR REPLACE INTO self_facts (content, prev, core, created_at) VALUES (?, ?, ?, ?)",
                (new_content, best_text, best_core, now_str()),
            )
        logger.info(f"[Memory] 自我记忆改口: 《{best_text}》→《{new_content}》")
        return True

    @staticmethod
    def _format_self_fact(r: Any) -> str:
        day = mmdd_of(r["created_at"])
        note = f"{day} 说的" if day else ""
        if r["prev"]:
            note = f"{note}；之前说过：{r['prev']}" if note else f"之前说过：{r['prev']}"
        return f"{r['content']}（{note}）" if note else r["content"]

    async def get_self_facts_for_prompt(
        self, query: str = "", recent: int = SELF_RECENT_N, related: int = SELF_RELATED_N
    ) -> List[str]:
        """核心全部 + 非核心里最近说的 recent 条 + 和 query 字面最相关的 related 条，按时间排。"""
        rows = await self.db.fetchall(
            "SELECT id, content, prev, core, created_at FROM self_facts ORDER BY created_at, id"
        )
        picked = self._pick_layered(rows, query, SELF_CORE_LIMIT, recent, related)
        return [self._format_self_fact(r) for r in rows if r["id"] in picked]

    def _pick_layered(
        self, rows: List[Any], query: str, core_limit: int, recent: int, related: int
    ) -> set:
        """分层取记忆（rows 按时间先后排好）：核心最近 core_limit 条 + 非核心最近 recent 条
        + 非核心里和 query 字面最相关的 related 条。返回要带上的 id 集合。"""
        core = [r for r in rows if r["core"]][-core_limit:]
        others = [r for r in rows if not r["core"]]
        picked = {r["id"] for r in core}
        if recent:
            picked |= {r["id"] for r in others[-recent:]}
        q_grams, q_chars = self._bigram_set(query), self._content_chars(query)
        if (q_grams or q_chars) and related:
            scored = []
            for r in others:
                if r["id"] in picked:
                    continue
                score = self._relevance(q_grams, q_chars, r["content"])
                if score > 0:
                    scored.append((score, r["id"]))
            picked |= {i for _, i in sorted(scored, reverse=True)[:related]}
        return picked

    async def consolidate_self_core(self) -> bool:
        """她的核心自我记忆超过上限时合并（见 _consolidate_core）。"""
        return await self._consolidate_core(
            "self_facts", SELF_CORE_LIMIT, SELF_CORE_TARGET,
            f"{getattr(getattr(self.persona, 'world', None), 'her_identity', '一个女生')}说过的关于自己", "她"
        )

    async def consolidate_fact_core(self) -> bool:
        """关于他的核心事实超过上限时合并（见 _consolidate_core）。"""
        return await self._consolidate_core(
            "facts", FACT_CORE_LIMIT, FACT_CORE_TARGET, "她记下的关于一个男生（他）", "他"
        )

    async def _consolidate_core(self, table: str, limit: int, target: int, who: str, pron: str) -> bool:
        """核心条数超过 limit 时请模型合并重复/相近的、去掉被取代的，压回 target 条以内。
        模型失败或结果不合格（空、超条数）保持原样。"""
        if not self.gateway:
            return False
        rows = await self.db.fetchall(
            f"SELECT id, content FROM {table} WHERE core = 1 ORDER BY created_at, id"
        )
        if len(rows) <= limit:
            return False
        listing = "\n".join(f"- {r['content']}" for r in rows)
        try:
            resp = await self.gateway.chat(
                messages=[
                    {"role": "system", "content": CORE_CONSOLIDATE_PROMPT.format(n=target, who=who, pron=pron)},
                    {"role": "user", "content": listing},
                ],
                model=self.gateway.config.observer_model,
                temperature=0.2,
                json_mode=True,
                purpose=f"{table}_consolidate",
            )
            data = parse_llm_json(resp)
            items = data.get("facts") if isinstance(data, dict) else data
            items = [x.strip() for x in items if isinstance(x, str) and x.strip()]
        except Exception as e:
            logger.warning(f"[Memory] 核心记忆合并失败（{table}），保持原样: {e}")
            return False
        if not items or len(items) > target:
            logger.warning(f"[Memory] 核心记忆合并结果不合格（{table}，{len(items)} 条），保持原样")
            return False
        async with self.db.transaction():
            await self.db.execute(f"DELETE FROM {table} WHERE core = 1")
            for text in items:
                await self.db.execute(
                    f"INSERT OR REPLACE INTO {table} (content, core, created_at) VALUES (?, 1, ?)",
                    (text, now_str()),
                )
        logger.info(f"[Memory] 核心记忆合并（{table}）: {len(rows)} → {len(items)} 条")
        return True

    # ==========================================
    # 4.6 对他的看法 (impressions)：可能看错、会被修正的判断，只影响她怎么接话
    # ==========================================

    async def update_impressions(self, turns: List[Any]) -> bool:
        """拿最近一段聊天修订看法（整表替换）；模型失败或结果为空保持原样。"""
        if not self.gateway or not turns:
            return False
        current = await self.get_impressions()
        current_text = "\n".join(
            f"- {r['content']}（把握 {r['confidence']}；依据：{r['evidence'] or '无'}）" for r in current
        ) or "（还没有）"
        dialogue = "\n".join(
            f"{'他' if t['role'] == 'user' else '你'}：{t['content']}" for t in turns
        )
        resp = await self.gateway.chat(
            messages=[
                {"role": "system", "content": IMPRESSIONS_PROMPT.format(n=IMPRESSIONS_MAX)},
                {"role": "user", "content": IMPRESSIONS_USER.format(current=current_text, dialogue=dialogue)},
            ],
            model=self.gateway.config.observer_model,
            temperature=0.3,
            json_mode=True,
            purpose="impressions",
        )
        data = parse_llm_json(resp)
        items = data.get("impressions") if isinstance(data, dict) else None
        rows = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict):
                continue
            text = it.get("看法") or it.get("content")
            if not isinstance(text, str) or not text.strip():
                continue
            try:
                conf = min(max(int(it.get("把握", it.get("confidence", 1))), 1), 3)
            except (TypeError, ValueError):
                conf = 1
            ev = it.get("依据") or it.get("evidence") or ""
            rows.append((text.strip(), conf, ev.strip() if isinstance(ev, str) else ""))
        if not rows:
            logger.warning("[Memory] 看法更新结果为空或畸形，保持原样")
            return False
        async with self.db.transaction():
            await self.db.execute("DELETE FROM impressions")
            for text, conf, ev in rows[:IMPRESSIONS_MAX]:
                await self.db.execute(
                    "INSERT INTO impressions (content, confidence, evidence, updated_at) VALUES (?, ?, ?, ?)",
                    (text, conf, ev, now_str()),
                )
        logger.info(f"[Memory] 对他的看法已更新：{len(rows[:IMPRESSIONS_MAX])} 条")
        return True

    async def get_impressions(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT content, confidence, evidence FROM impressions ORDER BY confidence DESC, id"
        )
        return [dict(r) for r in rows]

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

        rows = await self.db.fetchall("SELECT id, content, core FROM facts")
        best_sim = 0.0
        best_fact = ""
        best_id = None
        best_core = 0
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
                best_core = int(r["core"] or 0)

        if best_id is None or best_sim < 0.4:
            await self.add_fact(new_content)
            logger.info(f"[Memory] 事实作废未命中(相似度 {best_sim:.2f} < 0.4)，仅新增: 《{new_content}》")
            return False

        async with self.db.transaction():
            await self.db.execute("DELETE FROM facts WHERE id = ?", (best_id,))
            await self.add_fact(new_content, core=bool(best_core))
        logger.info(
            f"[Memory] 事实作废命中: 《{best_fact}》(相似度 {best_sim:.2f}) → 《{new_content}》"
        )
        return True
