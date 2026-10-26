"""心理状态快照构建器 (PsychologicalSnapshotBuilder)。

重构第一阶段：离线纯文字内核的心理快照模块。
每轮生成不可变 contracts.PsychologicalSnapshot：
1. 保留原始 affection / mood 状态副本与内部 action_context 供内部规划消费。
2. 提取并保留 stage_index。
3. 仲裁当前正在聊天与睡眠作息冲突：
   客观当前聊天优先，绝不出现“睡着（其实醒着）”或自相矛盾的休眠描写。
4. 生成 1~2 句精炼 prompt_hint：
   优先级：客观当前聊天 > 阶段边界 > 心情/作息。
   严禁泄漏动作语法（如 [sticker:...]、[沉默]）与底层原始数值。
5. 通用无硬编码（角色/世界观来自 Persona，不出现任何具体角色名或特定作品名）。
6. 只读取现有引擎，不推进情绪与好感度状态。
7. 节假日：只读 holidays_provider（每轮一次），算出 holiday_span 一并记入 action_context；
   缺省或异常一律按无节假日回退，绝不写配置或状态。
   角色卡 world.follow_public_holidays=False 时，本模块是新内核唯一的节假日闸门：
   provider 根本不会被调用，调用方（脚本/装配层）不得再重复判断同一件事。
"""
from __future__ import annotations

import copy
from datetime import datetime
from types import MappingProxyType
from typing import Any, Callable, List, Mapping, Optional, Protocol

from companion.kernel.contracts import PsychologicalSnapshot
from companion.persona import SLEEP_WORDS, holiday_span, is_sleep_activity
from companion.prompts import (
    get_frustration_description,
    get_mood_description,
    get_neglect_description,
    get_trust_description,
)


def _deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    if isinstance(value, set):
        return frozenset(_deep_freeze(v) for v in value)
    return value


class AffectionReader(Protocol):
    async def get_state(self) -> Mapping[str, Any]:
        ...


class MoodReader(Protocol):
    async def get_state(self) -> Mapping[str, Any]:
        ...

    async def get_hours_since_last_chat(self, now: Optional[datetime] = None) -> float:
        ...


_ADDITIONAL_SLEEP_WORDS = ("睡", "梦乡", "做梦", "歇息", "休息中")


def _is_sleep_like(activity: str) -> bool:
    """全面检测作息文案是否表示处于睡眠/就寝状态。"""
    if not activity:
        return False
    if is_sleep_activity(activity):
        return True
    return any(w in activity for w in _ADDITIONAL_SLEEP_WORDS)


class PsychologicalSnapshotBuilder:
    """构建一轮内唯一的心理快照。"""

    def __init__(
        self,
        affection_engine: Optional[AffectionReader] = None,
        mood_engine: Optional[MoodReader] = None,
        persona: Optional[Any] = None,
        holidays_provider: Optional[Callable[[], List[str]]] = None,
    ) -> None:
        self.affection_engine = affection_engine
        self.mood_engine = mood_engine
        self.persona = persona
        # 节假日只读取值函数（与旧 assembler 的 holidays_provider 同构）：
        # 缺省即"无节假日"，异常一律按无节假日回退，绝不因此炸掉整轮。
        self.holidays_provider = holidays_provider

    def _read_holidays(self) -> List[str]:
        """只读节假日列表；架空世界明确关闭时不得读取或注入现实节假日。

        这是新内核链路唯一的 follow_public_holidays 闸门：
        关闭时 provider 一次都不调用（holiday_span 恒为 0），
        调用方直接把只读取值函数传进来即可，不要在调用处再判断一次。
        """
        world = getattr(self.persona, "world", None)
        if getattr(world, "follow_public_holidays", True) is False:
            return []
        provider = self.holidays_provider
        if provider is None:
            return []
        try:
            return list(provider())
        except Exception:
            return []

    async def build_snapshot(
        self,
        turn_id: str,
        user_message: str = "",
        now: Optional[datetime] = None,
        state_version: int = 1,
    ) -> PsychologicalSnapshot:
        """异步构建 PsychologicalSnapshot。

        参数:
            turn_id: 本轮唯一标识。
            user_message: 用户本轮输入的自然语言（为空时视为无用户对话，例如主动消息）。
            now: 可注入的当前时间（默认为 datetime.now()）。
            state_version: 状态版本号，默认 1。
        """
        now_dt = now or datetime.now()

        # 1. 读取好感度与情绪状态副本（深拷贝，只读不推进）
        raw_affection = (
            await self.affection_engine.get_state()
            if self.affection_engine is not None
            else {}
        )
        raw_mood = (
            await self.mood_engine.get_state()
            if self.mood_engine is not None
            else {}
        )

        affection_copy = copy.deepcopy(dict(raw_affection))
        mood_copy = copy.deepcopy(dict(raw_mood))

        # 2. 提取数值与阶段
        stage_idx = int(affection_copy.get("stage", 0))
        composite_aff = float(affection_copy.get("composite", 0.0))

        v = float(mood_copy.get("v", 0.0))
        a = float(mood_copy.get("a", 0.0))
        t = float(mood_copy.get("t", 7.0))
        frustration = float(mood_copy.get("frustration", 0.0))

        hours_since_chat = 0.0
        if self.mood_engine is not None:
            try:
                hours_since_chat = float(
                    await self.mood_engine.get_hours_since_last_chat(now=now_dt)
                )
            except Exception:
                hours_since_chat = 0.0

        # 3. 作息与当前活动判定及睡眠冲突仲裁
        routine_activity = ""
        is_sleeping_routine = False
        is_chatting = bool(user_message and user_message.strip())

        # 节假日段长：每轮只读一次节假日表，算出当天所在连续假期段（0 = 非假期）。
        holidays = self._read_holidays()
        span = 0
        try:
            span = holiday_span(now_dt.strftime("%Y-%m-%d"), holidays)
        except Exception:
            span = 0

        if self.persona is not None and hasattr(self.persona, "get_current_activity"):
            routine_activity = self.persona.get_current_activity(
                now_dt.hour, now_dt.weekday(), holiday_span=span
            )
            is_sleeping_routine = _is_sleep_like(routine_activity)

        # 仲裁原则：
        # 客观当前聊天优先于作息设定。
        # 当正在与用户聊天时：即使作息表排了睡眠，此刻也是确切醒着在聊天，不能出现“睡着（其实醒着）”之类自相矛盾的怪异修饰。
        effective_activity = ""
        if is_chatting:
            if is_sleeping_routine:
                effective_activity = "刚被消息吵醒，仍然很困，但正在回应他"
            elif routine_activity:
                effective_activity = f"在与他聊天（日常状态：{routine_activity}）"
            else:
                effective_activity = "在和他聊天"
        else:
            effective_activity = routine_activity

        # 4. 生成自然语言 prompt_hint（1~2句精简结论）
        # 优先级：客观当前聊天优先 > 其次阶段边界 > 再心情/作息
        prompt_hint = self._build_prompt_hint(
            is_chatting=is_chatting,
            is_sleeping_routine=is_sleeping_routine,
            stage_idx=stage_idx,
            v=v,
            a=a,
            t=t,
            frustration=frustration,
            hours_since_chat=hours_since_chat,
            composite_aff=composite_aff,
            effective_activity=effective_activity,
        )

        # 5. 内部 action_context 留存完整上下文供内部决策
        action_context: dict[str, Any] = {
            "hours_since_chat": hours_since_chat,
            "composite_affection": composite_aff,
            "stage_index": stage_idx,
            "routine_activity": routine_activity,
            "effective_activity": effective_activity,
            "is_sleeping_routine": is_sleeping_routine,
            "is_chatting": is_chatting,
            "holiday_span": span,
            "valence": v,
            "arousal": a,
            "trust": t,
            "frustration": frustration,
            "frozen_at": now_dt.isoformat(),
        }

        return PsychologicalSnapshot(
            turn_id=turn_id,
            frozen_at=now_dt,
            state_version=state_version,
            stage_index=stage_idx,
            prompt_hint=prompt_hint,
            action_context=_deep_freeze(action_context),
            affection=_deep_freeze(affection_copy),
            mood=_deep_freeze(mood_copy),
        )

    def _build_prompt_hint(
        self,
        is_chatting: bool,
        is_sleeping_routine: bool,
        stage_idx: int,
        v: float,
        a: float,
        t: float,
        frustration: float,
        hours_since_chat: float,
        composite_aff: float,
        effective_activity: str,
    ) -> str:
        """合成精简的 1~2 句自然语言 prompt_hint 给主模型。

        严格约束：
        - 优先级：客观当前聊天优先 > 其次阶段边界 > 再心情/作息。
        - 绝不泄漏动作语法（如 [sticker:...]、[沉默]、[语音]）。
        - 绝不泄漏底层数值（如 composite、v/a/t 数值）。
        - 绝不出现“睡着（其实醒着）”或自相矛盾的睡眠表述。
        """
        # 第一优先级：客观当前聊天与状态句
        # 如果是聊天中且作息是睡觉：客观说明醒着在对话
        chat_state_parts = []
        if is_chatting:
            if is_sleeping_routine:
                chat_state_parts.append("刚被消息吵醒，仍然很困，但正在回应他")
            elif effective_activity:
                chat_state_parts.append(f"此刻你正专注地与他交流")
            else:
                chat_state_parts.append("此刻你正与他交谈")
        elif effective_activity:
            chat_state_parts.append(f"此刻你正处于{effective_activity}")

        # 只保留阶段语气，不拼接可能含动作指令的整段规则。
        stage_tone = ""
        if self.persona is not None and hasattr(self.persona, "get_stage"):
            try:
                stage_tone = str(getattr(self.persona.get_stage(stage_idx), "tone", "") or "")
            except Exception:
                stage_tone = ""
        if stage_tone:
            chat_state_parts.append(f"相处分寸保持{stage_tone}")

        # 情绪/心理体验
        mood_text = get_mood_description(v, a)
        trust_text = get_trust_description(t)
        frust_text = get_frustration_description(frustration)
        neglect_text = get_neglect_description(hours_since_chat, composite_aff)

        emotional_text = f"{mood_text}，{trust_text}{frust_text}{neglect_text}".strip("，。； ")

        # 组织精炼的 1~2 句话
        # 句 1：当前客观交流状态 + 阶段相处分寸
        sentence1_parts = []
        if chat_state_parts:
            sentence1_parts.extend(chat_state_parts)
        sentence1 = "，".join(sentence1_parts) + "。" if sentence1_parts else ""

        # 句 2：内心感受与情绪
        sentence2 = f"内心感受：{emotional_text}。" if emotional_text else ""

        # 如果只有一句或者两句，拼接输出
        result = f"{sentence1}{sentence2}".strip()
        # 再次兜底过滤动作语法和原始数值格式
        for token in ["[sticker", "[沉默", "[吃糖", "[voice", "[label:"]:
            result = result.replace(token, "")
        return result
