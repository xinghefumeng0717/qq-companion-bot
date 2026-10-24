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

from companion.lorebook import Lorebook

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
    # 全局正确示范只在阶段 ≤ 这个值时显示（可选；不填 = 所有阶段都显示）。
    # 全局示范多取自刚认识时的真实聊天，熟了以后还挂着会把语气拉回"相识"，
    # 这时改由阶段块自己的示范定语气（v5 阶段盲猜：依恋阶段的回复多数被判成相识）
    good_examples_max_stage: Optional[int] = None


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
class AffectionSettings:
    """角色卡可选字段 affection：这个角色的关系节奏。不填的项用代码默认标定（affection.py）。"""

    start_stage: Optional[int] = None                    # 开局阶段（0~9），覆盖 initial_dims
    stage_thresholds: Optional[List[float]] = None       # 10 个递增门槛，第一个必须是 0
    decay_per_day: Optional[Dict[str, float]] = None     # 各维每天衰减点数（只覆盖写了的维）


_DIMS = ("warmth", "trust", "intimacy", "intrigue", "patience", "tension")


def parse_affection_settings(raw: Any) -> AffectionSettings:
    """解析卡里的 affection 段；单项不合法就丢掉那一项（打警告），其余照用。"""
    if not isinstance(raw, dict):
        return AffectionSettings()
    out = AffectionSettings()
    st = raw.get("start_stage")
    if isinstance(st, int) and not isinstance(st, bool) and 0 <= st <= 9:
        out.start_stage = st
    elif st is not None:
        logger.warning(f"角色卡 affection.start_stage 必须是 0~9 的整数，已忽略：{st!r}")
    th = raw.get("stage_thresholds")
    if th is not None:
        ok = (
            isinstance(th, list) and len(th) == 10
            and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in th)
            and th[0] == 0 and all(a < b for a, b in zip(th, th[1:])) and th[-1] <= 100
        )
        if ok:
            out.stage_thresholds = [float(x) for x in th]
        else:
            logger.warning(f"角色卡 affection.stage_thresholds 必须是 10 个从 0 开始严格递增、不超过 100 的数，已忽略：{th!r}")
    dec = raw.get("decay_per_day")
    if isinstance(dec, dict):
        good = {k: float(v) for k, v in dec.items()
                if k in _DIMS and isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
        if len(good) != len(dec):
            logger.warning(f"角色卡 affection.decay_per_day 里有不认识的维度或负数，已忽略这些项：{dec!r}")
        out.decay_per_day = good or None
    return out


def parse_stage_max_drop(raw: Any) -> Optional[int]:
    """角色卡 stage_max_drop：非负整数（0~9）；缺省/null/非法值 = None（不设地板）。"""
    if raw is None:
        return None
    if isinstance(raw, int) and not isinstance(raw, bool) and 0 <= raw <= 9:
        return raw
    logger.warning(f"角色卡 stage_max_drop 必须是 0~9 的整数，已忽略：{raw!r}")
    return None


@dataclass
class World:
    """她所在世界的几句话设定（角色卡可选字段 world）。

    代码里的通用提示词只从这里取"她是谁、在哪聊、过不过现实节假日"，
    不再写死"女大学生/学期/手机QQ"。每项默认值就是改造前写死的原文，
    所以不填 world 的旧卡，生成的提示词逐字节不变。
    """

    her_identity: str = "一个女大学生"          # 质检/记忆合并等后台提示词里怎么称呼她
    life_identity: str = "一个真实的大学生"      # 生活主线生成器里她的身份
    chat_medium: str = "手机QQ"                 # 你们用什么聊天
    calendar_label: str = "学期节奏锚点"         # 生活主线里日历锚点的叫法
    calendar_kind: str = "校历"
    seed_pool_label: str = "课程、场所、烦恼与乐团规矩"
    small_things: str = "读书报告、pre、体测预约、琴房抢位、DDL、乐跑打卡、食堂排队、演出走台"
    title_example: str = "乐团节目审查"
    stake_example: str = "紧张，低音部那段还没合齐"
    follow_public_holidays: bool = True          # 是否跟着现实的法定节假日放假/回家
    date_style: str = "absolute"                 # "relative" = 给她看的日期一律换成"今天/前天/3 天前"（见 datefmt.py）
    chat_tail: str = "手机聊天"                   # 主聊收尾句"现在像平常{chat_tail}一样"
    typing_habit: str = "手机打字习惯"            # 主动消息"符合{typing_habit}"
    holiday_note_short: str = "，今天是法定节假日（学校放假，不上课）"
    holiday_note_long: str = "，今天是法定节假日（放长假，她不在学校）"
    long_holiday_activity: str = "放长假中，回老家，不在学校"
    face_word: str = "QQ"                        # 表情说明里"QQ系统表情"的 QQ
    face_usage: str = "normal"                   # "rare" = 基本不用小黄脸（一天顶天一两次），默认 normal 保持原样

    # "不适合发语音"的场景词（可选；卡里写了用卡里的，没写 = 代码内置词表）
    # None 与空元组语义不同：None = 卡没声明（走 tts.BLOCK_KEYWORDS 旧词表），
    # () = 卡明确声明"没有忙闲场景词"（闸门只保留例外白名单）。
    voice_busy_words: Optional[Tuple[str, ...]] = None
    # 「轻忙」场景词（FIXES27，可选）：手上有事但嘴闲着的活动（对账/逛市集/吃火锅…）。
    # 与 voice_busy_words 的区别：他正在跟她聊的轮次里（awake_override）这些词放行，
    # 没人聊时（主动消息）照样拦。None = 卡没声明（没有轻忙词，全部照硬拦）。
    voice_light_busy_words: Optional[Tuple[str, ...]] = None

    @property
    def relative_dates(self) -> bool:
        return self.date_style == "relative"


# 上面 World 的默认值是改造前写死的原文（校园设定），只留给**没有 world 段的旧卡**用，
# 保证旧卡提示词逐字节不变。卡里写了 world 段的（新卡），没写到的项一律用下面这套
# 和角色无关的中性说法，不会再冒出"女大学生/校历/乐团/手机QQ"。
NEUTRAL_WORLD: Dict[str, Any] = {
    "her_identity": "她",
    "life_identity": "一个有自己日子要过的人",
    "chat_medium": "聊天软件",
    "calendar_label": "近期安排",
    "calendar_kind": "日程",
    "seed_pool_label": "她日常的事务、常去的地方和小烦恼",
    "small_things": "手头的小差事、约好的事、日常的小麻烦",
    "title_example": "月底要交的一件差事",
    "stake_example": "有点烦，还没弄完",
    "chat_tail": "聊天",
    "typing_habit": "打字聊天的习惯",
    "holiday_note_short": "，今天是法定节假日",
    "holiday_note_long": "，今天是法定节假日（放长假）",
    "long_holiday_activity": "放长假中",
    "face_word": "",
    "face_usage": "normal",
}


# 睡眠类作息词：人在睡觉是不可能回消息的——所以他正在跟她聊的轮次里，
# 作息表命中这些词说明"该睡的点上她其实醒着"（熬夜/被吵醒），而不是真睡着。
# 用途：①语音闸门的作息腿在对话中轮次对睡眠词放行（tts.activity_allows_voice
# 的 awake_override）；②提示词里的作息文案加"你其实醒着"注记（assembler），
# 修掉"她说没睡、系统当她睡了"的自相矛盾（生产实测 2026-10-23：她 00:20 说
# "我压根没睡"，但语音闸门整夜因"睡着"关闭）。
SLEEP_WORDS: Tuple[str, ...] = ("睡着", "赖床", "睡下", "就寝", "入睡", "午睡", "打呼噜")


def is_sleep_activity(activity: str) -> bool:
    """作息文案是否命中睡眠类词（与 SLEEP_WORDS 同一份口径）。"""
    text = (activity or "").strip()
    return any(w in text for w in SLEEP_WORDS)


def parse_voice_busy_words(raw: Any) -> Optional[Tuple[str, ...]]:
    """解析卡里 world.voice_busy_words："此刻不方便说话"的活动词。

    None = 卡里没写（调用方回落到 tts.BLOCK_KEYWORDS 内置词表）；
    非空元组 = 用卡里的词；空元组 = 卡明确声明没有这类词。
    数组里非字符串/空白成员逐条跳过并告警；整体类型不认识按"没写"处理，
    不让一个手滑把语音闸门整条弄失效。
    """
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        logger.warning(
            f"[Persona] world.voice_busy_words 不是数组（{type(raw).__name__}），按未声明处理"
        )
        return None
    words = tuple(str(x).strip() for x in raw if isinstance(x, str) and x.strip())
    if len(words) != len(raw):
        logger.warning(
            f"[Persona] world.voice_busy_words 含 {len(raw) - len(words)} 条非字符串/空白成员，已跳过"
        )
    return words


def build_world(world_data: Any) -> World:
    """卡里没有 world 段 → 旧卡默认（原文）；有 world 段 → 中性默认 + 卡里写的。"""
    if not isinstance(world_data, dict) or not world_data:
        return World()
    fields = World.__dataclass_fields__
    merged = {
        **NEUTRAL_WORLD,
        **{
            k: v
            for k, v in world_data.items()
            if k in fields and k not in ("voice_busy_words", "voice_light_busy_words")
        },
    }
    world = World(**merged)
    world.voice_busy_words = parse_voice_busy_words(world_data.get("voice_busy_words"))
    # FIXES27：轻忙词表与忙闲词表同一份解析纪律（非数组按未声明、非字符串成员跳过）
    world.voice_light_busy_words = parse_voice_busy_words(
        world_data.get("voice_light_busy_words")
    )
    return world


@dataclass
class UserProfile:
    """跟她聊天的那个人（人物档案）。角色卡里用 {user_name} {user_given_name}
    {user_address} 占位，加载时用这里的值填进去；换人只改档案，换角色只换卡。"""

    name: str = ""
    given_name: str = ""
    address: str = ""            # 她平时怎么叫他；空 = 用卡里的 user_address
    identity: str = "一个男生（同校朋友）"   # 后台提示词里怎么称呼他（老档案没写时的原文）

    @classmethod
    def load(cls, path: str) -> "UserProfile":
        with open(path, "r", encoding="utf-8") as f:
            d = json.load(f)
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: Any) -> "UserProfile":
        """从 JSON 对象建档案：独立档案文件与卡里的 user 段共用同一结构。"""
        if not isinstance(d, dict):
            logger.warning(f"[Persona] 人物档案不是 JSON 对象（{type(d).__name__}），按空档案处理")
            return cls()
        return cls(
            name=str(d.get("name", "")),
            given_name=str(d.get("given_name", "")),
            address=str(d.get("address", "")),
            identity=str(d.get("identity", "") or cls.identity),
        )


PROFILE_PLACEHOLDERS = ("{user_name}", "{user_given_name}", "{user_address}")


def fill_profile(obj: Any, profile: UserProfile, address: str) -> Any:
    """把卡里所有字符串中的人物占位符换成档案里的值（只做这三个替换，不碰别的花括号）。"""
    if isinstance(obj, str):
        return (
            obj.replace("{user_name}", profile.name or address)
            .replace("{user_given_name}", profile.given_name or profile.name or address)
            .replace("{user_address}", address)
        )
    if isinstance(obj, list):
        return [fill_profile(x, profile, address) for x in obj]
    if isinstance(obj, dict):
        return {k: fill_profile(v, profile, address) for k, v in obj.items()}
    return obj


def find_default_profile(char_dir: str) -> str:
    """没指定人物档案时的默认位置：项目根目录 profiles/default.json（不存在返回空串）。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    path = os.path.join(root, "profiles", "default.json")
    return path if os.path.exists(path) else ""


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
    world: World = field(default_factory=World)
    user: UserProfile = field(default_factory=UserProfile)
    # 阶段地板：关系阶段最多比"历史最高阶段"低几阶（如 1 = 老朋友一个月不聊最多退一阶，
    # 不会退回陌生人）。None = 不设地板（旧行为：阶段完全跟着复合分走）。见 affection.resolve_stage。
    stage_max_drop: Optional[int] = None
    affection: AffectionSettings = field(default_factory=AffectionSettings)
    lorebook: Lorebook = field(default_factory=Lorebook)

    @classmethod
    def load(cls, char_dir: str, user_profile: str = "") -> Persona:
        """user_profile：人物档案路径，优先级 config 显式指定 > 卡里的 user 段 > default.json。

        · 非空 = config 显式写了 [character].user_profile，照用（旧行为）；
        · 空但卡顶层有 user 对象 = 档案内嵌在卡里，换角色只交一张卡；
        · 都没有 = 卡里用了占位符时找 profiles/default.json（旧卡行为不变）。
        """
        json_path = os.path.join(char_dir, "character.json")
        if not os.path.exists(json_path):
            raise FileNotFoundError(f"角色卡文件不存在: {json_path}")

        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if user_profile:
            user = UserProfile.load(user_profile)
        elif isinstance(data.get("user"), dict):
            user = UserProfile.from_dict(data["user"])
        else:
            raw_text = json.dumps(data, ensure_ascii=False)
            profile_path = (
                find_default_profile(char_dir)
                if any(ph in raw_text for ph in PROFILE_PLACEHOLDERS)
                else ""
            )
            user = UserProfile.load(profile_path) if profile_path else UserProfile()
        address = user.address or str(data.get("user_address", "") or "你")
        data = fill_profile(data, user, address)
        data["user_address"] = address
        world = build_world(data.get("world"))

        chat_style_data = data.get("chat_style", {})
        chat_style = ChatStyle(
            rules=list(chat_style_data.get("rules", [])),
            good_examples=list(chat_style_data.get("good_examples", [])),
            bad_examples=list(chat_style_data.get("bad_examples", [])),
            plain_examples=list(chat_style_data.get("plain_examples", [])),
            good_examples_max_stage=(
                int(chat_style_data["good_examples_max_stage"])
                if chat_style_data.get("good_examples_max_stage") is not None
                else None
            ),
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
            data.get("long_holiday_activity", "") or world.long_holiday_activity
        ).strip() or world.long_holiday_activity
        calendar_anchors = parse_calendar_anchors(data.get("calendar_anchors"))
        life_arc_seed_pool = parse_life_arc_seed_pool(data.get("life_arc_seed_pool"))

        aff_raw = data.get("affection") if isinstance(data.get("affection"), dict) else {}
        affection = parse_affection_settings(aff_raw)
        # stage_max_drop 可写在 affection 段里，也兼容早先的顶层写法
        stage_max_drop = parse_stage_max_drop(aff_raw.get("stage_max_drop", data.get("stage_max_drop")))

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
            world=world,
            user=user,
            stage_max_drop=stage_max_drop,
            affection=affection,
            lorebook=Lorebook.load(char_dir, data),
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
