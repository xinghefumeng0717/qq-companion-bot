"""角色卡加载与渲染 (persona.py)
解析 character.json 并在运行时提供人设、作息、阶段及聊天规则。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 法定节假日按"周末节奏"匹配作息时使用的星期索引（5 = 周六，与 daily_routine 的 days 约定一致）
HOLIDAY_WEEKDAY = 5


def is_holiday_date(date_str: str, holidays: Optional[List[str]]) -> bool:
    """当天（YYYY-MM-DD）是否命中法定节假日列表。holidays 为空时一律 False。

    节假日列表的唯一数据源是 Config.get_holidays()（转发自 [llm.pricing].holidays），
    这里不做任何日期推算：填了什么就是什么。
    """
    if not holidays:
        return False
    return date_str in holidays


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
        is_holiday: bool = False,
    ) -> str:
        """根据当前小时查找作息表当前活动。若提供 weekday (0=周一..6=周日)，优先匹配指定星期的作息。

        is_holiday=True（当天日期命中 Config.get_holidays()，即法定节假日）时先按周六
        作息匹配——假期不上课，节奏≈周末；周六作息也覆盖不到时再回落到 weekday 的现有逻辑。
        """
        if is_holiday:
            for item in self.daily_routine:
                if item.days is not None and HOLIDAY_WEEKDAY in item.days:
                    if _hour_in_item(item, hour):
                        return item.activity

        if weekday is not None:
            for item in self.daily_routine:
                if item.days is not None and weekday in item.days:
                    if _hour_in_item(item, hour):
                        return item.activity

        for item in self.daily_routine:
            if item.days is None or (weekday is not None and weekday in item.days):
                if _hour_in_item(item, hour):
                    return item.activity
        return "在度过属于自己的时间"
