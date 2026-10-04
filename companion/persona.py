"""角色卡加载与渲染 (persona.py)
解析 character.json 并在运行时提供人设、作息、阶段及聊天规则。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 法定节假日按"周末节奏"匹配作息时使用的星期索引（5 = 周六，与 daily_routine 的 days 约定一致）
HOLIDAY_WEEKDAY = 5

# 连续假期段长达到该值即视为"长假"（FIXES14 任务1 / 生产证据 E10）。
# 语义来源是 V3 角色卡 core_description 的锚点："国庆、寒暑假长假她回绍兴老家，
# 长假期间银泉、临湖、琴房等校园场景一律不出现"。反过来，3 天以内的小长假她留校，
# 校园场景合理，继续走周六作息即可。故阈值取 4 天。
LONG_HOLIDAY_MIN_SPAN = 4

# 长假当天的固定作息文案（与 V3 卡锚点逐字一致）。
# 长假**不匹配任何 daily_routine**，直接返回这句——按周六作息塞"在琴房练琴"会与角色卡打架。
LONG_HOLIDAY_ACTIVITY = "放长假中，回绍兴老家陪父母，不在学校"

# 作息表覆盖不到时的回退文案：她"在度过属于自己的时间"，人是在闲的（FIXES15 忙/闲判定）
FREE_ACTIVITY_FALLBACK = "在度过属于自己的时间"


def is_holiday_date(date_str: str, holidays: Optional[List[str]]) -> bool:
    """当天（YYYY-MM-DD）是否命中法定节假日列表。holidays 为空时一律 False。

    节假日列表的唯一数据源是 Config.get_holidays()（转发自 [llm.pricing].holidays），
    这里不做任何日期推算：填了什么就是什么。
    """
    if not holidays:
        return False
    return date_str in holidays


def holiday_span(date_str: str, holidays: Optional[List[str]]) -> int:
    """当天所在**连续法定节假日段**的长度（含当天）。不在 holidays 里返回 0。

    与 is_holiday_date 同一数据源，同样不做节假日推算：只认配置里逐条填好的日期。
    段长靠日期 ±1 天进位判断，因此 09-30 与 10-01 这类跨月相连也算连成一段
    （纯字符串比较会跨月断链，日历上却是连续的）。
    """
    if not holidays or not date_str:
        return 0
    holiday_set = set(holidays)
    if date_str not in holiday_set:
        return 0
    try:
        cur = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        # 配置里日期格式不对就无从算段长：退回最短假（1 天），行为回到 FIXES11 的短假路径，
        # 并告警让"长假没生效"这件事在日志里看得见，而不是静默按短假算。
        logger.warning(
            f"[Persona] holidays 里日期格式异常，无法计算连续假期段长: {date_str!r}，按 1 天短假处理"
        )
        return 1

    span = 1
    probe = cur - timedelta(days=1)
    while probe.strftime("%Y-%m-%d") in holiday_set:
        span += 1
        probe -= timedelta(days=1)
    probe = cur + timedelta(days=1)
    while probe.strftime("%Y-%m-%d") in holiday_set:
        span += 1
        probe += timedelta(days=1)
    return span


def _hour_in_item(item: "RoutineItem", hour: int) -> bool:
    """小时是否落在作息区间内（支持 23:00~07:00 这类跨午夜区间）"""
    if item.start <= item.end:
        return item.start <= hour < item.end
    return hour >= item.start or hour < item.end


@dataclass
class ChatStyle:
    rules: List[str] = field(default_factory=list)
    good_examples: List[str] = field(default_factory=list)
    bad_examples: List[str] = field(default_factory=list)
    # 日常废话流基线示例（可选字段；旧角色卡没有该字段时为空列表）
    plain_examples: List[str] = field(default_factory=list)


@dataclass
class Stage:
    name: str
    tone: str
    instructions: List[str] = field(default_factory=list)
    examples: List[str] = field(default_factory=list)  # 该阶段的对话示范（"机主：…→ 她：…"）


@dataclass
class RoutineItem:
    start: int
    end: int
    activity: str
    days: Optional[List[int]] = None


@dataclass
class PersonalMemory:
    title: str
    content: str
    emotion: str = ""


@dataclass
class Persona:
    name: str
    user_address: str
    core_description: str
    chat_style: ChatStyle
    initial_dims: Dict[str, float]
    stages: List[Stage]
    daily_routine: List[RoutineItem]
    personal_memories: List[PersonalMemory]
    habits: List[str]
    stickers_dir: str
    base_dir: str

    @classmethod
    def load(cls, char_dir: str) -> Persona:
        json_path = os.path.join(char_dir, "character.json")
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"角色卡文件不存在: {json_path}")

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        chat_style_data = data.get("chat_style", {})
        chat_style = ChatStyle(
            rules=list(chat_style_data.get("rules", [])),
            good_examples=list(chat_style_data.get("good_examples", [])),
            bad_examples=list(chat_style_data.get("bad_examples", [])),
            plain_examples=list(chat_style_data.get("plain_examples", [])),
        )

        stages_raw = data.get("stages", [])
        if len(stages_raw) != 10:
            raise ValueError(f"角色卡 stages 数量必须恰好为 10，当前为 {len(stages_raw)}")

        stages = [
            Stage(
                name=s.get("name", f"阶段{i}"),
                tone=s.get("tone", ""),
                instructions=list(s.get("instructions", [])),
                examples=list(s.get("examples", [])),
            )
            for i, s in enumerate(stages_raw)
        ]

        routine_raw = data.get("daily_routine", [])
        daily_routine = [
            RoutineItem(
                start=int(r["start"]),
                end=int(r["end"]),
                activity=str(r["activity"]),
                days=r.get("days"),
            )
            for r in routine_raw
        ]

        memories_raw = data.get("personal_memories", [])
        personal_memories = [
            PersonalMemory(
                title=str(m.get("title", "")),
                content=str(m.get("content", "")),
                emotion=str(m.get("emotion", "")),
            )
            for m in memories_raw
        ]

        habits = list(data.get("habits", []))
        stickers_dir = str(data.get("stickers_dir", "stickers"))

        initial_dims = {
            "warmth": float(data.get("initial_dims", {}).get("warmth", 40.0)),
            "trust": float(data.get("initial_dims", {}).get("trust", 50.0)),
            "intimacy": float(data.get("initial_dims", {}).get("intimacy", 35.0)),
            "intrigue": float(data.get("initial_dims", {}).get("intrigue", 30.0)),
            "patience": float(data.get("initial_dims", {}).get("patience", 50.0)),
            "tension": float(data.get("initial_dims", {}).get("tension", 3.0)),
        }

        return cls(
            name=str(data.get("name", "伴侣")),
            user_address=str(data.get("user_address", "你")),
            core_description=str(data.get("core_description", "")),
            chat_style=chat_style,
            initial_dims=initial_dims,
            stages=stages,
            daily_routine=daily_routine,
            personal_memories=personal_memories,
            habits=habits,
            stickers_dir=stickers_dir,
            base_dir=char_dir,
        )

    def get_stage(self, stage_idx: int) -> Stage:
        """获取对应关系阶段（0~9），超出范围 clamp"""
        clamped = max(0, min(len(self.stages) - 1, stage_idx))
        return self.stages[clamped]

    def get_current_activity(
        self,
        hour: int,
        weekday: Optional[int] = None,
        holiday_span: int = 0,
        is_holiday: bool = False,
    ) -> str:
        """根据当前小时查找作息表当前活动。若提供 weekday (0=周一..6=周日)，优先匹配指定星期的作息。

        holiday_span 是当天所在的**连续法定节假日段长**（holiday_span() 计算）：
          - 0：非节假日，按 weekday 走日常作息；
          - 1~3（短假，3 天以内）：先按周六作息匹配——假期不上课，节奏≈周末，留校的
            校园场景合理；周六作息也覆盖不到时再回落到 weekday 的现有逻辑（FIXES11 行为，保持不变）；
          - >=4（长假，国庆/春节/寒暑假级）：**不匹配任何 daily_routine**，直接返回
            LONG_HOLIDAY_ACTIVITY（FIXES14 任务1 / 证据 E10）。

        is_holiday 是 FIXES11 旧调用方式的兼容垫片：True 等价 holiday_span=1（短假），
        已有调用方（benchmark_v4.py）与既有测试不需要改，行为与改动前逐格一致。

        只取文案；需要"忙/闲"这一结构化信息时用 get_current_activity_detail。
        """
        return self.get_current_activity_detail(
            hour, weekday, holiday_span=holiday_span, is_holiday=is_holiday
        )[0]

    def get_current_activity_detail(
        self,
        hour: int,
        weekday: Optional[int] = None,
        holiday_span: int = 0,
        is_holiday: bool = False,
    ) -> Tuple[str, bool]:
        """同 get_current_activity，但多返回一位"是否命中结构化作息条目"。

        返回 (活动文案, is_structured)：
          - is_structured=True：命中 daily_routine 里明确的作息条目（上课/练琴/合练/睡觉…），
            她人在忙——FIXES15 用它选"首条延迟 1~10 分钟"；
          - is_structured=False：回退文案（在度过属于自己的时间）或长假文案，人在闲——
            FIXES15 用它选"首条延迟 5~30 秒"。长假虽然文案固定，但它不来自作息表，
            按任务书要求同样归为回退类（闲）。

        匹配顺序与文案逐格保持 get_current_activity 不变，此处只是把结果与判定一起返回。
        """
        span = holiday_span if holiday_span and holiday_span > 0 else 0
        if is_holiday and not span:
            span = 1

        if span >= LONG_HOLIDAY_MIN_SPAN:
            return LONG_HOLIDAY_ACTIVITY, False

        if span > 0:
            for item in self.daily_routine:
                if item.days is not None and HOLIDAY_WEEKDAY in item.days:
                    if _hour_in_item(item, hour):
                        return item.activity, True

        if weekday is not None:
            for item in self.daily_routine:
                if item.days is not None and weekday in item.days:
                    if _hour_in_item(item, hour):
                        return item.activity, True

        for item in self.daily_routine:
            if item.days is None or (weekday is not None and weekday in item.days):
                if _hour_in_item(item, hour):
                    return item.activity, True
        return FREE_ACTIVITY_FALLBACK, False
