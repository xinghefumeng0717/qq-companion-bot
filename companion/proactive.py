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
from companion.config import ProactiveConfig, TimingConfig
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
from companion.persona import LONG_HOLIDAY_MIN_SPAN, Persona, holiday_span
from companion.prompts import (
    FACE_PROMPT_BLOCK,
    PROACTIVE_DECISION_PROMPT,
    PROACTIVE_GENERATE_PROMPT,
    STAGE_GATING_RESTRICTED,
    get_mood_description,
    get_mood_label,
    get_trust_description,
    holiday_prompt_note,
)
from companion.replier import (
    Replier,
    strip_face_markers,
    typing_text_from_chunks_or_record,
)
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
        set_typing_fn: Optional[Callable[[bool], Coroutine[Any, Any, bool]]] = None,
        timing_config: Optional[TimingConfig] = None,
        arcs: Optional[Any] = None,
        tts: Optional[Any] = None,
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
        # FIXES15：主动消息同样走 typing 展示（D=0，T_typing 照常算），不另搞一套。
        # 两个新参数默认 None = 不演，与改动前一致；生产由 main.py 显式注入。
        self.set_typing_fn = set_typing_fn
        self.timing = timing_config
        # FIXES16 生活主线：可选注入（None = 无生活剧本，行为与改动前逐字节一致）
        self.arcs = arcs
        # FIXES22：语音闸门（与主聊共用同一份 TTSManager 账目：开关/日上限/作息）
        self.tts = tts
        self._typing_on = (
            timing_config is not None
            and timing_config.typing_indicator_enabled
            and set_typing_fn is not None
        )
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

    async def _check_rules_gate(self, skip_unanswered_reply: bool = False) -> Tuple[bool, str]:
        """第一层·规则闸门：零成本，任一命中则拦截返回 (True, 原因)

        `skip_unanswered_reply`（FIXES24）：**只为事件通道开的口子**——豁免第 4 条
        "上一条主动消息机主尚未回复"。理由是事件消息讲的是她自己生活的进展、且有
        24h 时效，"他没回上一条主动消息"不构成掐死它的理由；免打扰、60 分钟闸门、
        连续未回闸门（第 3 条）、晚安、情绪下限一律照常。默认 False，其余调用方
        行为逐字节不变。
        """
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

        # 4. 工作记忆最后一轮是机器人发言且未获回复（事件通道可豁免）
        if not skip_unanswered_reply:
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
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), holidays)

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

        # ③ 作息活动 + 当前时间（短假按周六作息留校；长假直接回绍兴老家）
        current_activity = self.persona.get_current_activity(
            now_dt.hour, now_dt.weekday(), holiday_span=span
        )
        if current_activity:
            # 长假只附短假那句会漏掉"不在学校"的关键信息，但那句文案本身已写明回绍兴老家，
            # 叠上去就是同一件事说两遍，所以长假不再附注
            if 0 < span < LONG_HOLIDAY_MIN_SPAN:
                current_activity += holiday_prompt_note(span)
            return f"现在是 {now_dt.hour}点多，自己此刻正在：{current_activity}"

        # ④ 高强度日记回忆
        diaries = await self.memory.get_active_diaries()
        if diaries:
            return f"忽然回想起了之前的片段：{diaries[0]}"

        # ⑤ 日期感
        weekday_map = {0: "周一，新的一周开始啦", 4: "周五啦，快要周末了", 5: "周六休息日", 6: "周日时光"}
        date_sense = weekday_map.get(now_dt.weekday(), "平常的一天")
        return f"日期感念：今天好像是{date_sense}"

    def _calc_typing_duration(self, text: str) -> float:
        """typing 展示时长：每 10 字 2 秒，钳在 typing_min~typing_max。

        与 turn_handler.calc_typing_duration 同公式，但这里是**故意各留一份**：
        turn_handler 已经 import proactive，反向 import 会成环；且本方法不吃
        "首条/非首条"参数（主动消息恒定 D=0），两者不是同一份代码可合的形状。
        与 _char_jaccard 一样按 FIXES11 负面清单不跨模块合并。
        """
        t = self.timing
        raw = len(text) / 10.0 * t.typing_seconds_per_10chars
        return max(t.typing_min, min(t.typing_max, raw))

    async def _set_typing(self, typing: bool) -> None:
        """调注入的 typing 回调，异常/False 一律静默降级（与 turn_handler 同策略）"""
        if not self._typing_on:
            return
        try:
            await self.set_typing_fn(typing)
        except Exception as e:
            logger.warning(
                f"[Proactive] 正在输入回调异常（{'开' if typing else '关'}），静默降级: {e}"
            )

    async def _play_typing_indicator(self, text: str) -> None:
        """发送前演"正在输入"：开 → 等 T_typing → 关。异常降级为"不演了，照发"。

        FIXES20：与 turn_handler 同款——记录里的 [face:标签] 标记不计入打字时长，
        打字时长只按她真正打出来的字算（一个 3 字短句挂个脸不该把 T_typing 拉长一倍）。
        """
        if not self._typing_on:
            return
        duration = self._calc_typing_duration(strip_face_markers(text))
        if duration <= 0:
            return
        try:
            await self._set_typing(True)
            logger.info(f"[Proactive] 正在输入 {duration:.1f} 秒（主动消息，{len(text)} 字）")
            await asyncio.sleep(duration)
        except Exception as e:
            logger.warning(f"[Proactive] 正在输入展示异常，降级为直接发送: {e}")
        finally:
            await self._set_typing(False)

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

    async def _generate_and_send(self, material: str, current_time_str: str) -> bool:
        """给定话题素材走"生成 → 切段 → typing 表演 → 发送 → 落库"整条管道。

        A 分支（常规决策选中想发）与 FIXES16 事件通道共用这一条，
        保证两条路产出的消息在切段、typing、落库、未回复计数上完全同构。
        返回 True = 消息已发出并落库。
        """
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
            face_block=FACE_PROMPT_BLOCK,
        )

        # FIXES22：主动消息同样走语音闸门（"睡前收到她一条语音"是这张卡的高光场景），
        # 与主聊共用同一份 TTSManager 账目（开关/日上限/作息）。**先问闸门再组装**：
        # 关着时提示词里就不该出现 [voice:] 这个写法。
        voice_ok = await self._voice_gate_ok()
        # 完整人格 system prompt 注入
        system_prompt = await self.assembler.assemble_system_prompt("", voice_ok)
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
            return False

        # 切段与发送（source=proactive：占位符兜底的 INFO 日志据此标注来源）
        # FIXES22：用上面问过闸门的那份结论（不重复查库），语音与主聊共用额度
        chunks, clean_text = self.replier.parse_reply(
            reply_text,
            source="proactive",
            voice_allowed=voice_ok,
            voice_max_chars=getattr(
                getattr(self.tts, "config", None), "max_chars", 60
            ),
        )
        if not chunks:
            return False

        logger.info(f"[Proactive] 正在分段发送主动消息: {clean_text}")
        # FIXES22：打字时长按"她真打出来的字"算（语音段不算，它走自己的发送停顿）
        # DEEP_AUDIT B-6：纯语音轮**不**回退记录文本（语音正文不是她打的字）
        await self._play_typing_indicator(
            typing_text_from_chunks_or_record(chunks, clean_text)
        )
        await self.replier.send_reply_chunks(chunks, self.send_msg_fn)

        # 发送后落库并累加未回复计数
        await self.memory.save_proactive_turn(clean_text)
        await self.increment_unanswered_count()
        return True

    async def _voice_gate_ok(self) -> bool:
        """主动消息的语音闸门（FIXES22 任务3 第3条）

        与 TurnHandler 问的是同一份状态：同一个 TTSManager 实例，
        所以"主聊发了一条语音"会**占掉**主动消息的每日额度（共用账目，别两处各发各的）。
        拿不到 TTSManager / 问出错 → False（默认关）。
        """
        tts = getattr(self, "tts", None)
        if tts is None:
            return False
        try:
            now_dt = datetime.now()
            span = holiday_span(
                now_dt.strftime("%Y-%m-%d"),
                self._holidays_provider() if self._holidays_provider else [],
            )
            activity = self.persona.get_current_activity(
                now_dt.hour, now_dt.weekday(), holiday_span=span
            )
            ok, why = await tts.check_gate(activity)
            logger.debug(f"[Proactive] 语音闸门: {'可用' if ok else '不可用'}（{why}）")
            return ok
        except Exception as e:
            logger.debug(f"[Proactive] 语音闸门查询异常，按不可用处理: {e}")
            return False

    async def _try_event_channel(self) -> bool:
        """FIXES16 事件通道：生活主线刚出结果 → 抢先于 LLM 决策层发一条"脱口而出"。

        返回 True = 本周期已被事件通道处理（无论发没发出去，调用方都不要再走常规决策）。
        事件消息与常规主动消息共用 _generate_and_send，因此同样计入未回复连续闸门。

        免打扰时段不会被这里绕过：调用方在本方法之前就过了规则闸门，
        0~8 点整条 trigger_cycle 提前 return，事件留在库里等下一轮。
        """
        if self.arcs is None:
            return False
        try:
            arc = await self.arcs.claim_event()
        except Exception as e:
            logger.error(f"[Proactive] 事件通道取主线异常: {e}")
            return False
        if not arc:
            return False

        logger.info(f"[Proactive] 命中生活事件通道: 《{arc.get('title')}》")
        now_dt = datetime.now()
        current_time_str = now_dt.strftime(TIME_FORMAT)
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), self.get_holidays())
        if span:
            current_time_str += holiday_prompt_note(span)

        sent = await self._generate_and_send(
            self.arcs.build_event_material(arc), current_time_str
        )
        if sent:
            try:
                await self.arcs.mark_event_sent()
            except Exception as e:
                logger.warning(f"[Proactive] 事件消息已发但日计数记账失败: {e}")
        else:
            # 生成/发送失败：回滚置位，让它下轮还能再试
            try:
                await self.arcs.release_event(arc)
            except Exception as e:
                logger.warning(f"[Proactive] 事件回滚失败: {e}")
        return True

    async def trigger_cycle(self) -> None:
        """执行单次主动消息评估周期"""
        # FIXES16：状态机每次醒来顺手推一次（极便宜，只在 today 且过 18:00 才发 API）
        if self.arcs is not None:
            try:
                await self.arcs.advance_states()
            except Exception as e:
                logger.warning(f"[Proactive] 生活主线状态推进异常: {e}")

        # 第一层·前置闸门。FIXES24：事件通道豁免第 4 条（"上一条主动消息机主尚未回复"），
        # 所以这里先用 skip_unanswered_reply=True 过一遍零成本闸门——
        # 免打扰 / 60 分钟 / 连续未回(第3条) / 晚安 / 情绪下限 仍然照拦，
        # 这样事件检查就发生在 #4 之前，而 #4 对"常规决策路径"一分不减（见下方完整闸门）。
        blocked, reason = await self._check_rules_gate(skip_unanswered_reply=True)
        if blocked:
            logger.debug(f"[Proactive] 规则闸门拦截: {reason}")
            return

        # FIXES16 任务5：事件检查在常规 LLM 决策**之前**。
        # 当天无事件时 claim_event 返回 None，行为与改动前完全一致（回落常规决策层）。
        if await self._try_event_channel():
            return

        # 完整闸门（含第 4 条）：常规决策路径的判定与改动前逐条一致。
        # 无事件时本周期必然走到这里，结果与改动前相同（事件通道那步是无副作用的空查询）。
        blocked, reason = await self._check_rules_gate()
        if blocked:
            logger.debug(f"[Proactive] 规则闸门拦截: {reason}")
            return

        logger.info(f"[Proactive] 规则闸门通过，进入 LLM 潜意识决策层")

        # FIXES16 任务2：活跃主线 < 2 条时即时补（自身带 1 小时节流，连烧不了）
        if self.arcs is not None:
            try:
                await self.arcs.ensure_arcs()
            except Exception as e:
                logger.warning(f"[Proactive] 补充生活主线异常: {e}")

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
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), holidays)
        current_time_str = now_dt.strftime(TIME_FORMAT)
        if span:
            current_time_str += holiday_prompt_note(span)
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
                now_dt.hour, now_dt.weekday(), holiday_span=span
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
        await self._generate_and_send(material, current_time_str)

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
