"""观察者引擎 (observer.py)
每轮对话落库后异步结算，评估好感度评分、情绪冲击量、提取事实、待跟进事项与表情包收藏。
"""

from __future__ import annotations

import collections
import logging
import math
import re
from datetime import datetime, timedelta
from typing import Any, Deque, Dict, List, Optional, Tuple

from companion.affection import AffectionEngine
from companion.db import Database, TIME_FORMAT, now_str, parse_dt
from companion.gateway import LLMGateway, parse_llm_json
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.prompts import (
    OBSERVER_FACT_CORE_RULES,
    OBSERVER_MEETUP_RULES,
    OBSERVER_MEETUP_USER,
    OBSERVER_SELF_FACTS_RULES,
    OBSERVER_SELF_FACTS_USER,
    OBSERVER_SYSTEM_PROMPT,
    OBSERVER_USER_PROMPT,
    OBSERVER_RUNTIME_EVENT_RULES,
)
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

ALLOWED_MOMENTS = {"深度共情", "分享脆弱", "轻浮表白", "伤害行为"}

# 观察者打分中性默认（单维 4.0）与待跟进默认提醒时长（小时）
NEUTRAL_SCORE = 4.0
DEFAULT_REMIND_HOURS = 24.0
MAX_REMIND_HOURS = 24.0 * 365

# 观察者模型偶尔把 facts 项回成字典而不是字符串（生产证据 E11：日志持续刷
# "丢弃畸形 fact (dict: {'内容': ...})"，facts 是长期记忆主输入，等于记忆在漏）。
# 字典项按这些键依次尝试，取第一个非空字符串；都取不到才算畸形丢弃。
# 前四个键是 FIXES14 任务书点名的形态，顺序即优先级；"value" 排最后是 FIXES10
# 就支持的老形态，留在链尾只为"只有它"的旧输出不被回退（生产实测的 {'内容': ...}
# 不带 value，两键同存属臆造形态，按任务书顺序取前者）。
FACT_DICT_KEYS = ("内容", "content", "fact", "text", "topic", "value")
# 2026-10-19 跑分实测：flash-1017 常把 fact 回成 {"topic": ..., "time"/"detail": ...}，
# 旧键表不认 topic → 整条丢弃（"室友在学尤克里里"等都漏了）。附加键的内容拼在括号里。
FACT_DICT_EXTRA_KEYS = ("time", "date", "detail", "时间", "日期")

# 线下见面约定识别：约定常常跨轮说完（她说"武陵城新开了家锅子店，你看着办"，他下一轮才回
# "行，明天六点"），只给本轮两句，观察者看不到地点和事，按规则不会记。识别时附最近几轮
# 原文（只给原文，不给主聊提示词），条数和字数都封顶控制成本。
MEETUP_HISTORY_TURNS = 4          # 取最近几轮（含本轮，拼块时会把本轮那两条裁掉）
MEETUP_HISTORY_MSG_CHARS = 120    # 每条消息截断
MEETUP_HISTORY_MAX_CHARS = 700    # 整块上限，超了从最老的开始丢
# 模型有时把"没说地点"回成占位词，这类视同没有地点（去掉中英文括号后再比）
MEETUP_PLACE_PLACEHOLDERS = {"", "没说", "没提", "未说", "未知", "不详", "不确定", "未定", "待定", "无"}


def _meetup_history_block(
    turns: List[Dict[str, Any]], user_message: str, assistant_reply: str
) -> str:
    """把近期轮次原文拼成"他：… 她：…"的块；先把与当前轮重复的尾部记录裁掉。"""
    items = []
    for t in turns:
        if isinstance(t, dict):
            items.append(t)
            continue
        try:  # sqlite3.Row 之类的行对象也认
            items.append(dict(t))
        except Exception:
            continue
    if items and items[-1].get("role") == "assistant" and str(items[-1].get("content") or "").strip() == assistant_reply.strip():
        items.pop()
    if items and items[-1].get("role") == "user" and str(items[-1].get("content") or "").strip() == user_message.strip():
        items.pop()
    lines = []
    for t in items:
        content = " ".join(str(t.get("content") or "").split())
        if not content:
            continue
        who = "他" if t.get("role") == "user" else "她"
        lines.append(f"{who}：{content[:MEETUP_HISTORY_MSG_CHARS]}")
    while lines and sum(len(x) + 1 for x in lines) > MEETUP_HISTORY_MAX_CHARS:
        lines.pop(0)
    return "\n".join(lines)


def _followup_similarity(a: str, b: str) -> float:
    """待跟进标题的字符 Jaccard，用于合并同一件事的近义写法。"""
    def chars(text: str) -> set:
        value = set(re.findall(r"[\u4e00-\u9fa5]", text or ""))
        return value or set((text or "").lower())
    aa, bb = chars(a), chars(b)
    union = aa | bb
    return len(aa & bb) / len(union) if union else 0.0


def fact_to_str(fact: Any) -> str:
    """观察者回的一条事实转成字符串：字符串直接用；字典按 FACT_DICT_KEYS 取正文，
    附加键拼在括号里；取不到返回空串（调用方按畸形处理）。"""
    if isinstance(fact, str):
        return fact.strip()
    if not isinstance(fact, dict):
        return ""
    for key in FACT_DICT_KEYS:
        raw_val = fact.get(key)
        if isinstance(raw_val, str) and raw_val.strip():
            fact_str = raw_val.strip()
            extras = [
                fact[k].strip() for k in FACT_DICT_EXTRA_KEYS
                if isinstance(fact.get(k), str) and fact[k].strip()
                and fact[k].strip() not in fact_str
            ]
            if extras:
                fact_str = f"{fact_str}（{'；'.join(extras)}）"
            logger.info(f"[Observer] 已从字典形态提取 fact (键={key!r}): {fact_str}")
            return fact_str
    return ""


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
        # 自我记忆开关（[character].self_memory），由 main 按配置置 True；关着时提示词逐字节不变
        self.self_memory = False
        # 线下见面（[character].meetup_recap）：由 main 挂上 MeetupManager；None = 关
        self.meetups = None
        # runtime_state_v1：None = 不接收事件字段，旧观察者输出保持不变
        self.runtime_events = None

    async def settle_turn(
        self,
        user_message: str,
        assistant_reply: str,
        user_image_path: Optional[str] = None,
        recent_turns: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """每轮对话后异步执行结算。

        recent_turns：最近几轮原文（含本轮，按 id 升序）。只有见面识别用得上——约定常跨轮，
        缺省时（比如 chat.py 的沙箱直调）在开着 meetup_recap 的前提下由本方法自己读。
        """
        user_trunc = user_message[:500]
        bot_trunc = assistant_reply[:500]
        image_notice = "【机主本轮发送了一张图片】" if user_image_path else ""

        # 获取未完成待跟进事项
        # 过期的待跟进由 memory 统一清掉，这里只给还作数的（带记下日期）
        try:
            pending_rows = await self.memory.get_open_followups(limit=5)
        except Exception as e:
            logger.warning(f"[Observer] 读取待跟进失败，按空处理: {e}")
            pending_rows = []
        pending_text = (
            "\n".join([f"- {r['topic']}" for r in pending_rows])
            if pending_rows
            else "（当前无未完成事项）"
        )

        # 既有事实：让观察者知道"什么已经有定论"，updated_facts 才有参照物（FIXES11 任务3）
        try:
            known = await self.memory.get_facts_for_prompt(query=f"{user_message}\n{assistant_reply}")
        except Exception as e:
            logger.warning(f"[Observer] 读取既有事实失败，按空处理: {e}")
            known = []
        known_facts_text = "\n".join([f"- {f}" for f in known]) if known else "（当前无已确立事实）"

        now_dt = datetime.now()
        user_prompt = OBSERVER_USER_PROMPT.format(
            current_time=f"{now_dt.strftime(TIME_FORMAT)} 星期{'一二三四五六日'[now_dt.weekday()]}",
            user_message=user_trunc,
            assistant_reply=bot_trunc,
            image_notice=image_notice,
            pending_followups=pending_text,
            known_facts=known_facts_text,
        )

        system_prompt = OBSERVER_SYSTEM_PROMPT
        if self.runtime_events is not None:
            system_prompt += OBSERVER_RUNTIME_EVENT_RULES
        if getattr(self.memory, "fact_layers", False) is True:
            system_prompt += OBSERVER_FACT_CORE_RULES
        if self.self_memory is True:
            system_prompt += OBSERVER_SELF_FACTS_RULES
            try:
                known_self = await self.memory.get_self_facts_for_prompt(
                    query=f"{user_message}\n{assistant_reply}"
                )
            except Exception as e:
                logger.warning(f"[Observer] 读取自我记忆失败，按空处理: {e}")
                known_self = []
            self_text = "\n".join(f"- {f}" for f in known_self) if known_self else "（还没有）"
            user_prompt = OBSERVER_SELF_FACTS_USER.format(self_facts=self_text) + user_prompt

        if self.meetups is not None:
            system_prompt += OBSERVER_MEETUP_RULES
            try:
                rows = await self.meetups.pending()
            except Exception as e:
                logger.warning(f"[Observer] 读取约定失败，按空处理: {e}")
                rows = []
            listing = "\n".join(f"- {self.meetups.describe(r)}" for r in rows) or "（没有）"
            history = await self._meetup_history_text(recent_turns, user_message, assistant_reply)
            user_prompt = OBSERVER_MEETUP_USER.format(meetups=listing) + history + user_prompt

        messages = [
            {"role": "system", "content": system_prompt},
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

        if not isinstance(new_aff_state, dict):
            new_aff_state = {"composite": 0.0, "stage": None}
        await self.mood.update_mood(
            composite_affection=new_aff_state["composite"],
            stage=new_aff_state.get("stage") if isinstance(new_aff_state, dict) else None,
            conv_v=clamped_v,
            conv_a=clamped_a,
            conv_trust=clamped_t,
        )

        # 3. 语义事实入库（字符串直接入库；字典形态按 FACT_DICT_KEYS 依次提取，
        #    提取不到才丢弃——LLM 偶尔返回字典，事实是长期记忆主输入，丢不得）
        facts = data.get("facts")
        if facts is not None and not isinstance(facts, list):
            logger.warning(f"[Observer] 字段 facts 类型异常 ({type(facts).__name__}: {facts!r})，已忽略")
        if isinstance(facts, list):
            for fact in facts:
                fact_str = fact_to_str(fact)
                if fact_str:
                    await self.memory.add_fact(fact_str)
                elif fact:
                    logger.warning(f"[Observer] 丢弃畸形 fact ({type(fact).__name__}: {fact!r})")

        # 3.5 关于他的核心事实（fact_layers 开着时观察者才会给这个字段）
        if getattr(self.memory, "fact_layers", False) is True and isinstance(data.get("facts_core"), list):
            for fact in data["facts_core"]:
                try:
                    text = fact_to_str(fact)
                    if text:
                        await self.memory.add_fact(text, core=True)
                except Exception as e:
                    logger.warning(f"[Observer] 核心事实入库失败，已跳过该项: {e}")

        # 3.6 线下见面约定（开关开着才处理；单项坏了丢单项）
        if self.meetups is not None:
            await self._apply_meetups(data.get("meetups"), data.get("meetup_cancel"))

        # 4. 事实作废/更新通路（FIXES11 任务3）：字段可选，缺省或畸形一律跳过，
        #    绝不允许因此炸掉整轮结算
        await self._apply_updated_facts(data.get("updated_facts"))

        # 4.5 自我记忆：只在开关打开时处理，单项坏了丢单项
        if self.self_memory is True:
            await self._apply_self_facts(
                data.get("self_facts"), data.get("updated_self_facts"), data.get("self_facts_core")
            )

        # 5. 待跟进事项入库（逐项校验：坏项丢单项，不丢整轮）
        followups = data.get("followups")
        if isinstance(followups, dict):
            # 观察者偶尔把单条 followup 回成裸字典而不是列表（生产证据 E11 同款形态），
            # 整批忽略等于丢掉一条待跟进，这里降级成单项处理
            logger.info("[Observer] followups 是单个字典而非列表，已按单项处理")
            followups = [followups]
        if followups is not None and not isinstance(followups, list):
            logger.warning(f"[Observer] 字段 followups 类型异常 ({type(followups).__name__}: {followups!r})，已忽略")
        if isinstance(followups, list):
            now_dt = datetime.now()
            try:
                existing_rows = await self.memory.get_open_followups(limit=50)
            except Exception as e:
                logger.warning(f"[Observer] 读取既有待跟进失败，按无重复处理: {e}")
                existing_rows = []
            pending_topics = [str(r.get("topic") or "") for r in existing_rows]
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
                if any(_followup_similarity(topic, old) >= 0.60 for old in pending_topics):
                    logger.info(f"[Observer] 待跟进近义去重，跳过重复事项: {topic}")
                    continue
                remind_dt = now_dt + timedelta(hours=remind_hours)
                remind_str = remind_dt.strftime(TIME_FORMAT)
                await self.db.execute(
                    """
                    INSERT INTO followups (topic, remind_after, done, created_at)
                    VALUES (?, ?, 0, ?)
                    """,
                    (topic, remind_str, now_str()),
                )
                pending_topics.append(topic)

        # 6. 标记完成的待跟进事项（精确匹配，不匹配则记日志；非字符串项跳过）
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

        # 7. 收藏表情包回路 (§7.4)
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

        if self.runtime_events is not None:
            try:
                if recent_turns:
                    last_turn = recent_turns[-1]
                    try:
                        evidence_turn_id = last_turn.get("id")
                    except Exception:
                        evidence_turn_id = dict(last_turn).get("id")
                else:
                    evidence_turn_id = None
                applied = await self.runtime_events.apply_observation(
                    data.get("state_events"),
                    user_text=user_message,
                    assistant_text=assistant_reply,
                    now=now_dt,
                    evidence_turn_id=evidence_turn_id,
                )
                if applied:
                    logger.info(f"[Observer] 已写入 {applied} 条有证据的计划状态变化")
            except Exception as e:
                logger.warning(f"[Observer] 计划状态写入失败，已跳过: {e}")
        return data

    async def _meetup_history_text(
        self,
        recent_turns: Optional[List[Dict[str, Any]]],
        user_message: str,
        assistant_reply: str,
    ) -> str:
        """见面识别用的近期轮次原文块；没有内容时返回空串（提示词保持原样）。"""
        turns = recent_turns
        if turns is None:
            try:
                turns = await self.memory.get_recent_turns(limit=MEETUP_HISTORY_TURNS)
            except Exception as e:
                logger.warning(f"[Observer] 读取近期轮次失败，按无处理: {e}")
                turns = []
        if not isinstance(turns, list):
            logger.warning(f"[Observer] recent_turns 类型异常 ({type(turns).__name__})，已忽略")
            return ""
        body = _meetup_history_block(turns, user_message, assistant_reply)
        if not body:
            return ""
        return f"【刚才的几轮】（约定常常跨轮说完，结合这里看）\n{body}\n"

    @staticmethod
    def _meetup_field(item: Dict[str, Any], key: str) -> str:
        raw = item.get(key)
        return raw.strip() if isinstance(raw, str) else ""

    @staticmethod
    def _is_explicit_meetup(when: str, where: str) -> bool:
        """二次校验：模型判过之后，再按"有明确时间并且有明确地点"过滤一道。

        提示词规则是"双方都同意，有时间，并且有地点或要做的事"，观察者懂一点人情时
        会记下只有时间、没有地点的含糊提议；owner 要求见面降频（太频繁出戏），这类不记。
        时间要能解析成具体日期时间，地点不能是"没说/待定"这类占位。
        """
        if parse_dt(when) is None:
            return False
        return where.strip("（）()[]【】 ") not in MEETUP_PLACE_PLACEHOLDERS

    async def _apply_meetups(self, raw_new: Any, raw_cancel: Any) -> None:
        if isinstance(raw_cancel, list):
            for item in raw_cancel:
                try:
                    if isinstance(item, str) and item.strip():
                        await self.meetups.cancel(item)
                except Exception as e:
                    logger.warning(f"[Observer] 取消约定失败，已跳过: {e}")
        if isinstance(raw_new, dict):
            raw_new = [raw_new]
        if isinstance(raw_new, list):
            for item in raw_new:
                try:
                    if not isinstance(item, dict):
                        continue
                    when = self._meetup_field(item, "when")
                    where = self._meetup_field(item, "where")
                    what = self._meetup_field(item, "what")
                    if not self._is_explicit_meetup(when, where):
                        logger.info(
                            f"[Observer] 约定缺明确时间或地点，已跳过：when={when!r} where={where!r} what={what!r}"
                        )
                        continue
                    await self.meetups.add(when, where, what)
                except Exception as e:
                    logger.warning(f"[Observer] 记约定失败，已跳过: {e}")

    async def _apply_self_facts(self, raw_new: Any, raw_updated: Any, raw_core: Any = None) -> None:
        """她说过的关于自己的事入库（self_facts_core 标核心）；改口的走 supersede（保留之前的说法）。"""
        if isinstance(raw_core, list):
            for item in raw_core:
                try:
                    text = fact_to_str(item)
                    if text:
                        await self.memory.add_self_fact(text, core=True)
                except Exception as e:
                    logger.warning(f"[Observer] 核心自我记忆入库失败，已跳过该项: {e}")
        if isinstance(raw_new, list):
            for item in raw_new:
                try:
                    text = fact_to_str(item)
                    core = isinstance(item, dict) and (item.get("核心") is True or item.get("core") is True)
                    if text:
                        await self.memory.add_self_fact(text, core=core)
                except Exception as e:
                    logger.warning(f"[Observer] 自我记忆入库失败，已跳过该项: {e}")
        elif raw_new is not None:
            logger.warning(f"[Observer] 字段 self_facts 类型异常 ({type(raw_new).__name__})，已忽略")
        if isinstance(raw_updated, list):
            for item in raw_updated:
                try:
                    if not isinstance(item, dict):
                        continue
                    old = item.get("old") if isinstance(item.get("old"), str) else ""
                    new = item.get("new") if isinstance(item.get("new"), str) else ""
                    if old.strip() and new.strip():
                        await self.memory.supersede_self_fact(old, new)
                except Exception as e:
                    logger.warning(f"[Observer] 自我记忆改口失败，已跳过该项: {e}")

    async def _apply_updated_facts(self, raw_updated: Any) -> None:
        """应用 updated_facts（观察者回传的"某条既有事实已过时"）。

        整个方法被 try/except 包死：字段缺失、非 list、单项畸形、supersede_fact 抛错，
        都只丢这一项/整段，绝不牵连整轮观察结算（沿用 FIXES10 的 observer 容错原则）。
        """
        if raw_updated is None:
            return
        if not isinstance(raw_updated, list):
            logger.warning(
                f"[Observer] 字段 updated_facts 类型异常 ({type(raw_updated).__name__}: {raw_updated!r})，已忽略"
            )
            return

        for item in raw_updated:
            try:
                if not isinstance(item, dict):
                    logger.warning(f"[Observer] 丢弃畸形 updated_fact ({type(item).__name__}: {item!r})")
                    continue
                raw_old = item.get("old")
                raw_new = item.get("new")
                old_str = raw_old.strip() if isinstance(raw_old, str) else ""
                new_str = raw_new.strip() if isinstance(raw_new, str) else ""
                if not old_str or not new_str:
                    logger.warning(
                        f"[Observer] 丢弃残缺 updated_fact (old={raw_old!r}, new={raw_new!r})"
                    )
                    continue
                await self.memory.supersede_fact(old_str, new_str)
            except Exception as e:
                logger.warning(f"[Observer] 应用 updated_fact 失败，已跳过该项: {e}")

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
            "updated_facts": [],
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
