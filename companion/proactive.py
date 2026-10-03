"""主动消息调度器 (proactive.py)
后台定时协程，执行三层决策：
1. 零成本规则闸门 (quiet_hours, 60min间隔, 晚安, 情绪下限)
2. LLM 潜意识决策 (A想发 / B不发 / C克制入池)
3. 话题素材五级优选与分段发送
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional

from companion.affection import AffectionEngine
from companion.config import ProactiveConfig
from companion.db import (
    Database,
    STATE_KEY_UNANSWERED_PROACTIVE,
    TIME_FORMAT,
    parse_dt,
    now_str,
)
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona, is_holiday_date
from companion.prompts import (
    HOLIDAY_PROMPT_NOTE,
    PROACTIVE_DECISION_PROMPT,
    PROACTIVE_GENERATE_PROMPT,
    STAGE_GATING_RESTRICTED,
    get_mood_description,
    get_mood_label,
    get_trust_description,
)
from companion.replier import Replier
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

# 念头题材去重阈值：与 memory.add_fact 的字符 Jaccard 同源，阈值一致
TOPIC_DEDUP_THRESHOLD = 0.4
# 阶段 0~2（含）禁止"想他/喜欢/心疼"类依恋念头，与日记门控同源
RESTRICTED_STAGE_MAX = 2


def _char_jaccard(a: str, b: str) -> float:
    """字符级 Jaccard 相似度（汉字集合，无汉字时退化为小写词/字符集合）。

    与 memory.add_fact / memory.supersede_fact 的实现同源。
    按 FIXES11 负面清单，本次不跨模块合并这几份拷贝，仅在此保留一份局部实现。
    """
    a_chars = set(re.findall(r"[\u4e00-\u9fa5]", a))
    if not a_chars:
        a_chars = set(a.lower().split()) or set(a.lower())
    b_chars = set(re.findall(r"[\u4e00-\u9fa5]", b))
    if not b_chars:
        b_chars = set(b.lower().split()) or set(b.lower())
    union = a_chars | b_chars
    return len(a_chars & b_chars) / len(union) if union else 0.0


def format_recent_chat(turns: List[Dict[str, Any]], max_chars: int = 60) -> str:
    """把工作记忆格式化成提示词里的"最近的聊天记录"块。

    每行 `MM-DD HH:MM 他/你：内容`，内容截断 max_chars 字；
    turns 为空（今天还没聊过 / 第一次聊天）时给出兜底说明。
    """
    if not turns:
        return "（今天是你们第一次聊天）"
    lines = []
    for t in turns:
        content = str(t.get("content") or "").strip().replace("\n", " ")
        if len(content) > max_chars:
            content = content[:max_chars] + "…"
        who = "他" if t.get("role") == "user" else "你"
        created = str(t.get("created_at") or "")
        try:
            ts = datetime.strptime(created, TIME_FORMAT).strftime("%m-%d %H:%M")
        except (TypeError, ValueError):
            ts = created[:16] or "??"
        lines.append(f"{ts} {who}：{content}")
    return "\n".join(lines)


class ProactiveScheduler:
    def __init__(
        self,
        config: ProactiveConfig,
        persona: Persona,
        affection: AffectionEngine,
        mood: MoodEngine,
        memory: MemoryManager,
        stickers: StickerManager,
        replier: Replier,
        gateway: LLMGateway,
        db: Database,
        send_msg_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        assembler: Optional[Any] = None,
        holidays_provider: Optional[Callable[[], List[str]]] = None,
    ):
        self.config = config
        self.persona = persona
        self.affection = affection
        self.mood = mood
        self.memory = memory
        self.stickers = stickers
        self.replier = replier
        self.gateway = gateway
        self.db = db
        self.send_msg_fn = send_msg_fn
        self.assembler = assembler
        # 节假日唯一数据源 Config.get_holidays()；不传即视为无节假日
        self._holidays_provider = holidays_provider
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def get_holidays(self) -> List[str]:
        """取节假日列表（provider 缺失或抛错时退回空列表）"""
        if self._holidays_provider is None:
            return []
        try:
            return list(self._holidays_provider())
        except Exception as e:
            logger.warning(f"[Proactive] 读取节假日列表失败，按无节假日处理: {e}")
            return []

    def _build_stage_gating(self, stage_idx: int) -> str:
        """阶段 0~2 注入克制条款（C 分支念头与日记同门控），3+ 注入空串"""
        try:
            stage = int(stage_idx)
        except (TypeError, ValueError):
            stage = 0
        return STAGE_GATING_RESTRICTED if stage <= RESTRICTED_STAGE_MAX else ""

    def start(self) -> None:
        if not self.config.enabled:
            logger.info("[Proactive] 主动消息功能已在配置中禁用")
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("[Proactive] 主动消息调度器已启动")

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()

    async def get_unanswered_count(self) -> int:
        """获取当天连续未回复的主动消息数"""
        today = datetime.now().strftime("%Y-%m-%d")
        data = await self.db.get_state_json(STATE_KEY_UNANSWERED_PROACTIVE)
        if isinstance(data, dict) and data.get("date") == today:
            try:
                return int(data.get("count", 0))
            except (TypeError, ValueError):
                return 0
        return 0

    async def increment_unanswered_count(self) -> int:
        today = datetime.now().strftime("%Y-%m-%d")
        cnt = await self.get_unanswered_count() + 1
        await self.db.set_state_json(STATE_KEY_UNANSWERED_PROACTIVE, {"date": today, "count": cnt})
        return cnt

    async def reset_unanswered_count(self) -> None:
        """用户回复时清零未回复计数"""
        today = datetime.now().strftime("%Y-%m-%d")
        await self.db.set_state_json(STATE_KEY_UNANSWERED_PROACTIVE, {"date": today, "count": 0})

    def _is_in_quiet_hours(self, hour: int) -> bool:
        qh = self.config.quiet_hours
        if len(qh) == 2:
            start, end = qh[0], qh[1]
            if start <= end:
                return start <= hour < end
            else:
                return hour >= start or hour < end
        return hour in qh

    async def _check_rules_gate(self) -> Tuple[bool, str]:
        """第一层·规则闸门：零成本，任一命中则拦截返回 (True, 原因)"""
        now_dt = datetime.now()

        # 1. 当前小时 in quiet_hours
        if self._is_in_quiet_hours(now_dt.hour):
            return True, f"处于免打扰时段 ({now_dt.hour}:00)"

        # 2. 距机器人上次发言 < 60 分钟
        last_bot_row = await self.db.fetchone(
            "SELECT created_at FROM turns WHERE role = 'assistant' ORDER BY id DESC LIMIT 1"
        )
        if last_bot_row and last_bot_row["created_at"]:
            b_dt = parse_dt(last_bot_row["created_at"])
            if b_dt and (now_dt - b_dt).total_seconds() < 3600:
                return True, "距机器人上次发言不足 60 分钟"

        # 3. 连续未回主动消息 >= max_unanswered 当天停止
        unanswered = await self.get_unanswered_count()
        if unanswered >= self.config.max_unanswered:
            return True, f"连续 {unanswered} 条主动消息未回复，当天停止主动发消息"

        # 4. 工作记忆最后一轮是机器人发言且未获回复
        last_turn = await self.db.fetchone("SELECT role FROM turns ORDER BY id DESC LIMIT 1")
        if last_turn and last_turn["role"] == "assistant":
            # 如果最后一轮是机器人且 unanswered 已经有记录
            if unanswered > 0:
                return True, "上一条主动消息机主尚未回复"

        # 5. 用户最后一条消息含“晚安”且距今 < 6 小时
        last_user_row = await self.db.fetchone(
            "SELECT content, created_at FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1"
        )
        if last_user_row and last_user_row["created_at"] and last_user_row["content"]:
            if "晚安" in last_user_row["content"]:
                u_dt = parse_dt(last_user_row["created_at"])
                if u_dt and (now_dt - u_dt).total_seconds() < 6 * 3600:
                    return True, "机主已道晚安且不足 6 小时"

        # 6. 当前 valence < -6（心情太差不装没事）
        mood_state = await self.mood.get_state()
        if float(mood_state.get("v", 2.0)) < -6.0:
            return True, f"心情太差 (valence={mood_state.get('v')})，不装没事"

        return False, "通过规则闸门"

    async def _select_topic_material(self) -> str:
        """第三层：按优先级优选话题素材
        ① 到期未完成待跟进 -> ② 欲言又止池 -> ③ 作息活动+时间 -> ④ 高强度日记回忆 -> ⑤ 日期感
        """
        now_dt = datetime.now()
        current_time_str = now_dt.strftime(TIME_FORMAT)
        holidays = self.get_holidays()
        is_holiday = is_holiday_date(now_dt.strftime("%Y-%m-%d"), holidays)

        # ① 到期未完成的待跟进事项
        fu_row = await self.db.fetchone(
            "SELECT id, topic FROM followups WHERE done = 0 AND remind_after <= ? ORDER BY id ASC LIMIT 1",
            (current_time_str,),
        )
        if fu_row:
            return f"之前答应过要跟进的事情：{fu_row['topic']}"

        # ② 欲言又止池
        sup_row = await self.db.fetchone(
            "SELECT id, content FROM suppressed_desires ORDER BY id DESC LIMIT 1"
        )
        if sup_row:
            # 用掉后清除
            await self.db.execute("DELETE FROM suppressed_desires WHERE id = ?", (sup_row["id"],))
            return f"之前想对他说但忍住的话题：{sup_row['content']}"

        # ③ 作息活动 + 当前时间（法定节假日按周六作息，学校放假）
        current_activity = self.persona.get_current_activity(
            now_dt.hour, now_dt.weekday(), is_holiday=is_holiday
        )
        if current_activity:
            if is_holiday:
                current_activity += f"{HOLIDAY_PROMPT_NOTE}"
            return f"现在是 {now_dt.hour}点多，自己此刻正在：{current_activity}"

        # ④ 高强度日记回忆
        diaries = await self.memory.get_active_diaries()
        if diaries:
            return f"忽然回想起了之前的片段：{diaries[0]}"

        # ⑤ 日期感
        weekday_map = {0: "周一，新的一周开始啦", 4: "周五啦，快要周末了", 5: "周六休息日", 6: "周日时光"}
        date_sense = weekday_map.get(now_dt.weekday(), "平常的一天")
        return f"日期感念：今天好像是{date_sense}"

    async def _is_duplicate_desire(self, topic_hint: str) -> bool:
        """C 分支念头是否与既有题材重复。

        比对对象：现存 suppressed_desires 全部行 + 最近 3 条已发主动消息
        （turns 表 proactive=1，即"这个念头她其实已经说出口过了"也算重复）。
        字符 Jaccard >= 0.4 判为同题材。
        """
        candidates: List[str] = []
        try:
            rows = await self.db.fetchall("SELECT content FROM suppressed_desires")
            candidates.extend([str(r["content"]) for r in rows if r["content"]])
            turns = await self.db.fetchall(
                "SELECT content FROM turns WHERE proactive = 1 ORDER BY id DESC LIMIT 3"
            )
            candidates.extend([str(t["content"]) for t in turns if t["content"]])
        except Exception as e:
            # 查不出来就按"不重复"放行：去重是防复读的加分项，不能反过来吞掉念头
            logger.warning(f"[Proactive] 念头去重比对失败，跳过去重: {e}")
            return False

        for existing in candidates:
            sim = _char_jaccard(topic_hint, existing)
            if sim >= TOPIC_DEDUP_THRESHOLD:
                logger.info(
                    f"[Proactive] 念头题材重复命中: 《{topic_hint}》 vs 《{existing}》 相似度 {sim:.2f}"
                )
                return True
        return False

    async def trigger_cycle(self) -> None:
        """执行单次主动消息评估周期"""
        # 第一层：规则闸门
        blocked, reason = await self._check_rules_gate()
        if blocked:
            logger.debug(f"[Proactive] 规则闸门拦截: {reason}")
            return

        logger.info(f"[Proactive] 规则闸门通过，进入 LLM 潜意识决策层")

        # 第二层：LLM 决策
        aff_state = await self.affection.get_state()
        mood_state = await self.mood.get_state()
        now_dt = datetime.now()
        v = float(mood_state.get("v", 2.0))
        a = float(mood_state.get("a", 1.0))
        t = float(mood_state.get("t", 7.0))

        fu_rows = await self.db.fetchall("SELECT topic FROM followups WHERE done = 0 LIMIT 3")
        fu_str = "、".join([r["topic"] for r in fu_rows]) if fu_rows else "无"
        diaries = await self.memory.get_active_diaries()
        recent_diary_str = diaries[0] if diaries else "暂无特别回忆"

        # 一次读取配置，决策/生成/C 分支共用（任务2：别重复读配置；now_dt 沿用上方闸门后那次）
        holidays = self.get_holidays()
        is_holiday = is_holiday_date(now_dt.strftime("%Y-%m-%d"), holidays)
        current_time_str = now_dt.strftime(TIME_FORMAT)
        if is_holiday:
            current_time_str += HOLIDAY_PROMPT_NOTE
        stage_idx = aff_state.get("stage", 0)

        # 任务1：决策层也要看得见最近聊过什么 / 已有定论 / 已忍住的念头，
        # 否则它会选一个"他上轮刚说过"的话题（E1/E2）或复读同一念头（E5）
        recent_turns = await self.memory.get_recent_turns(limit=5)
        recent_chat_brief = format_recent_chat(recent_turns, max_chars=30)
        facts_all = await self.memory.get_all_facts()
        known_facts_str = "、".join(facts_all) if facts_all else "无"
        desire_rows = await self.db.fetchall(
            "SELECT content FROM suppressed_desires ORDER BY id DESC LIMIT 5"
        )
        pending_desires_str = (
            "、".join([str(r["content"]) for r in desire_rows if r["content"]])
            if desire_rows
            else "无"
        )

        decision_user_prompt = PROACTIVE_DECISION_PROMPT.format(
            current_time=current_time_str,
            stage_name=self.persona.get_stage(stage_idx).name,
            composite_affection=float(aff_state.get("composite", 30.0)),
            mood_label=get_mood_label(v, a),
            mood_desc=get_mood_description(v, a),
            trust_desc=get_trust_description(t),
            current_activity=self.persona.get_current_activity(
                now_dt.hour, now_dt.weekday(), is_holiday=is_holiday
            ),
            pending_followups=fu_str,
            recent_diary=recent_diary_str,
            recent_chat_brief=recent_chat_brief,
            known_facts=known_facts_str,
            pending_desires=pending_desires_str,
            stage_gating=self._build_stage_gating(stage_idx),
        )

        try:
            resp = await self.gateway.chat(
                messages=[{"role": "user", "content": decision_user_prompt}],
                model=self.gateway.config.observer_model,
                temperature=0.7,
                json_mode=True,
                purpose="proactive_decision",
            )
            data = json.loads(resp)
        except Exception as e:
            logger.warning(f"[Proactive] LLM 决策解析失败: {e}")
            return

        choice = str(data.get("choice", "B")).upper().strip()
        topic_hint = str(data.get("topic_hint", "")).strip()
        decision_reason = str(data.get("reason", ""))
        logger.info(f"[Proactive] 决策结果: choice={choice}, reason={decision_reason}, topic_hint={topic_hint}")

        # C 分支：写入欲言又止池（同题材不落库，FIXES11 任务4/E5）
        if choice == "C":
            if topic_hint:
                if await self._is_duplicate_desire(topic_hint):
                    logger.info(
                        f"[Proactive] 欲言又止念头与既有题材重复，跳过入库（念头仍算发生过）: {topic_hint}"
                    )
                    return
                await self.db.execute(
                    "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
                    (topic_hint, now_str()),
                )
                # 保留最近 5 条
                await self.db.execute(
                    """
                    DELETE FROM suppressed_desires
                    WHERE id NOT IN (SELECT id FROM suppressed_desires ORDER BY id DESC LIMIT 5)
                    """
                )
            return

        # B 分支：不发
        if choice != "A":
            return

        # 第三层：A 分支生成与发送
        material = topic_hint or await self._select_topic_material()
        stickers_list = "、".join(self.stickers.get_prompt_sticker_list())

        # 任务1：生成层注入最近 8 条对话历史。E1 的根因就是这里 0 条历史，
        # 她对"他已经坐动车到家"完全无知，只能拿永不更新的 facts 硬编
        gen_turns = await self.memory.get_recent_turns(limit=8)
        recent_chat_block = format_recent_chat(gen_turns, max_chars=60)

        gen_user_prompt = PROACTIVE_GENERATE_PROMPT.format(
            user_address=self.persona.user_address,
            current_time=current_time_str,
            topic_material=material,
            recent_chat=recent_chat_block,
            stickers_list=stickers_list,
        )

        # 完整人格 system prompt 注入
        system_prompt = await self.assembler.assemble_system_prompt("")
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": gen_user_prompt},
        ]

        try:
            reply_text = await self.gateway.chat(
                messages=messages,
                model=self.gateway.config.text_model,
                temperature=0.8,
                purpose="proactive_message",
            )
        except Exception as e:
            logger.error(f"[Proactive] 生成主动消息失败: {e}")
            return

        # 切段与发送
        chunks, clean_text = self.replier.parse_reply(reply_text)
        if not chunks:
            return

        logger.info(f"[Proactive] 正在分段发送主动消息: {clean_text}")
        await self.replier.send_reply_chunks(chunks, self.send_msg_fn)

        # 发送后落库并累加未回复计数
        await self.memory.save_proactive_turn(clean_text)
        await self.increment_unanswered_count()

    async def _run_loop(self) -> None:
        """后台轮询主循环"""
        while self._running:
            # 随机休眠 [min, max] 分钟
            interval_min = random.uniform(
                self.config.wake_interval_min, self.config.wake_interval_max
            )
            sleep_sec = interval_min * 60.0
            logger.debug(f"[Proactive] 主动调度器休眠 {interval_min:.1f} 分钟")
            try:
                await asyncio.sleep(sleep_sec)
                await self.trigger_cycle()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Proactive] 调度循环异常: {e}", exc_info=True)
                await asyncio.sleep(60)
