"""角色卡加载与渲染 (persona.py)
解析 character.json 并在运行时提供人设、作息、阶段及聊天规则。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 法定节假日按"周末节奏"匹配作息时使用的星期索引（5 = 周六，与 daily_routine 的 days 约定一致）
HOLIDAY_WEEKDAY = 5

# 连续假期段长达到该值即视为"长假"（FIXES14 任务1 / 生产证据 E10）。
# 3 天以内的小长假她留校，校园场景合理，继续走周六作息；长假（国庆/寒暑假级）
# 她不在学校，校园场景一律不出现。故阈值取 4 天。
LONG_HOLIDAY_MIN_SPAN = 4

# 长假当天的活动文案：**角色卡字段 long_holiday_activity 的兜底默认值**（不带任何具体学校名）。
# 卡里填了自己的长假口径就用卡里那句；卡里没填时用这句，任何角色都能用。
# 长假**不匹配任何 daily_routine**：卡里的作息写的是在校生活，长假套上去会自相矛盾。
DEFAULT_LONG_HOLIDAY_ACTIVITY = "放长假中，回老家，不在学校"

# 作息表覆盖不到时的回退文案：她"在度过属于自己的时间"，人是在闲的（FIXES15 忙/闲判定）
FREE_ACTIVITY_FALLBACK = "在度过属于自己的时间"

# calendar_anchors 成员必须写成 MM-DD；写成 YYYY-MM-DD 之类会永远查不中（静默的死锚点）
_MMDD_RE = re.compile(r"^\d{2}-\d{2}$")


def parse_calendar_anchors(raw: Any) -> List[Tuple[str, str, str]]:
    """解析角色卡 calendar_anchors 字段：[["起", "止", "一句话锚点"], ...]。

    锚点语义（FIXES16 生活主线生成器）：每项 (起, 止, 一句话)，止 < 起 表示跨年区间
    （如 12-31~01-06）；生成主线时注入"当前锚点 + 下一个锚点"，让她编出来的事踩在
    真实校历/日历上。**锚点内容属于角色卡，代码只负责结构校验**。

    畸形成员（不是三元组、字段不是字符串、不是 MM-DD、文本为空）逐条跳过并告警：
    某一条写坏了不该让整张卡加载失败，也不该静默变成永不命中。
    """
    out: List[Tuple[str, str, str]] = []
    if raw is None:
        return out
    if not isinstance(raw, (list, tuple)):
        logger.warning(f"[Persona] calendar_anchors 不是列表，按'无锚点'处理: {type(raw).__name__}")
        return out
    for item in raw:
        if not isinstance(item, (list, tuple)) or len(item) != 3:
            logger.warning(f"[Persona] calendar_anchors 成员不是三元组，已跳过: {item!r}")
            continue
        start, end, note = item
        if not all(isinstance(x, str) for x in (start, end, note)):
            logger.warning(f"[Persona] calendar_anchors 成员含非字符串字段，已跳过: {item!r}")
            continue
        if not (_MMDD_RE.match(start) and _MMDD_RE.match(end)) or not note.strip():
            logger.warning(
                f"[Persona] calendar_anchors 成员格式不合法（需 MM-DD / MM-DD / 非空文本），已跳过: {item!r}"
            )
            continue
        out.append((start, end, note))
    return out


def parse_life_arc_seed_pool(raw: Any) -> str:
    """解析角色卡 life_arc_seed_pool 字段：整段字符串，或字符串数组（按行拼接）。

    数组形态只是卡作者写起来方便（JSON 没有多行字符串），落地一律拼成一段文本
    交给提示词。非字符串成员逐条跳过并告警；整体类型不认识时按空池处理
    （空池 = 主线生成整条跳过，见 arcs.ensure_arcs）。
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    if isinstance(raw, (list, tuple)):
        lines = [x for x in raw if isinstance(x, str)]
        if len(lines) != len(raw):
            logger.warning(
                f"[Persona] life_arc_seed_pool 含 {len(raw) - len(lines)} 条非字符串成员，已跳过"
            )
        return "\n".join(lines)
    logger.warning(f"[Persona] life_arc_seed_pool 类型不认识（{type(raw).__name__}），按空池处理")
    return ""


def _mmdd(date_str: str) -> str:
    """取 YYYY-MM-DD 的 MM-DD 部分；**格式不合法返回空串**。

    必须校验：不能只切片。"not-a-date"[5:10] 会切出 "a-date"，而 "a-date" >= "12-31"
    在字符串比较下成立，于是任何乱码都会被跨年锚点（12-31~01-06）静默命中——
    脏数据伪装成了一条真实校历锚点。
    """
    if not isinstance(date_str, str):
        return ""
    m = re.match(r"^\d{4}-(\d{2}-\d{2})", date_str)
    return m.group(1) if m else ""


def _anchor_contains(start: str, end: str, mmdd: str) -> bool:
    """MM-DD 是否落在 [start, end] 内；end < start 视为跨年区间（如 12-31~01-06）"""
    if end < start:
        return mmdd >= start or mmdd <= end
    return start <= mmdd <= end


def calendar_anchor_note(
    date_str: str,
    anchors: Optional[List[Tuple[str, str, str]]],
    lookahead: int = 1,
) -> str:
    """取"当前日期所在锚点 + 后面 lookahead 个锚点"的节奏提示文本。

    anchors 由调用方从角色卡取（Persona.calendar_anchors）；空表 = 这张卡没有锚点功能，
    直接返回空串。查不到当前锚点时（日期格式异常 / 日期不落在任何区间）同样返回空串，
    调用方据此不注入——节奏锚点是氛围加成，不该因为查不到就让主线生成整条链失败。
    """
    mmdd = _mmdd(date_str)
    if not mmdd or not anchors:
        return ""

    ordered = sorted(anchors, key=lambda a: a[0])
    idx = next(
        (i for i, (s, e, _n) in enumerate(ordered) if _anchor_contains(s, e, mmdd)),
        None,
    )
    if idx is None:
        return ""

    parts = [f"眼下：{ordered[idx][2]}"]
    for j in range(idx + 1, min(idx + 1 + max(0, lookahead), len(ordered))):
        parts.append(f"接下来：{ordered[j][2]}")
    return "；".join(parts)


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
    # ---- 以下是角色卡可选字段（卡里没有就用默认值，行为对旧卡 = 功能关掉） ----
    # 长假（连续假期段长 >= LONG_HOLIDAY_MIN_SPAN）当天的活动文案；卡里没填用通用默认。
    long_holiday_activity: str = DEFAULT_LONG_HOLIDAY_ACTIVITY
    # 生活主线生成器的日历/校历锚点：[[起, 止, 一句话], ...]；空表 = 无锚点功能。
    calendar_anchors: List[Tuple[str, str, str]] = field(default_factory=list)
    # 生活主线生成器的取材范围（整段文本）；空串 = 无素材，主线生成整条跳过（见 arcs.py）。
    life_arc_seed_pool: str = ""

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

        # 可选字段：卡里没填就落到"功能关掉"的默认值（旧卡零改动、行为不炸）。
        long_holiday_activity = str(
            data.get("long_holiday_activity", "") or DEFAULT_LONG_HOLIDAY_ACTIVITY
        ).strip() or DEFAULT_LONG_HOLIDAY_ACTIVITY
        calendar_anchors = parse_calendar_anchors(data.get("calendar_anchors"))
        life_arc_seed_pool = parse_life_arc_seed_pool(data.get("life_arc_seed_pool"))

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
            long_holiday_activity=long_holiday_activity,
            calendar_anchors=calendar_anchors,
            life_arc_seed_pool=life_arc_seed_pool,
        )

    def calendar_anchor_note(self, date_str: str, lookahead: int = 1) -> str:
        """本卡 calendar_anchors 的节奏提示（卡里无锚点时返回空串）。"""
        return calendar_anchor_note(date_str, self.calendar_anchors, lookahead)

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
            角色卡的 long_holiday_activity（卡里没填时用 DEFAULT_LONG_HOLIDAY_ACTIVITY）
            （FIXES14 任务1 / 证据 E10）。

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
            return self.long_holiday_activity, False

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
