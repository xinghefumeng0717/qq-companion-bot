"""测试辅助模块 (tests/helpers.py)

提取公共样板：
1. make_db / close_db: 测试数据库初始化与清理
2. make_engine_stack: 角色卡与引擎组装
3. make_mock_gateway: Mock LLM Gateway
"""

import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import AdminConfig, ProactiveConfig, ReplyConfig
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager


async def make_db(db_path: str = ":memory:") -> Database:
    """初始化并返回已建表的测试数据库。
    如果是非内存库且文件已存在，先删除再新建。
    """
    if db_path != ":memory:" and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass
    db = Database(db_path)
    await db.init_tables()
    return db


async def close_db(db: Optional[Database], db_path: Optional[str] = None) -> None:
    """关闭数据库连接并在需要时清理磁盘文件。"""
    if db is not None:
        try:
            await db.close()
        except Exception:
            pass
    if db_path and db_path != ":memory:" and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


@dataclass
class EngineStack:
    """引擎堆栈容器，支持属性访问。"""
    persona: Persona
    affection: AffectionEngine
    mood: MoodEngine
    memory: MemoryManager
    stickers: StickerManager
    replier: Replier
    assembler: PromptAssembler
    proactive: Optional[ProactiveScheduler] = None
    admin: Optional[AdminServer] = None


def make_engine_stack(
    db: Database,
    persona_path: str = "characters/example",
    gateway: Optional[Any] = None,
    reply_config: Optional[ReplyConfig] = None,
    proactive_config: Optional[ProactiveConfig] = None,
    admin_config: Optional[AdminConfig] = None,
    include_proactive: bool = False,
    include_admin: bool = False,
    send_msg_fn: Optional[Callable] = None,
    onebot: Optional[Any] = None,
    db_path: Optional[str] = None,
    backup_dir: Optional[str] = None,
) -> EngineStack:
    """组装 Persona + 六引擎（+ 可选 Proactive / Admin）。"""
    persona = Persona.load(persona_path)
    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db, gateway=gateway, affection=affection)
    stickers_dir = os.path.join(persona.base_dir, persona.stickers_dir)
    stickers = StickerManager(stickers_dir, db)
    replier = Replier(reply_config or ReplyConfig(), stickers)
    assembler = PromptAssembler(persona, affection, mood, memory, stickers, db)

    proactive = None
    if include_proactive:
        proactive = ProactiveScheduler(
            config=proactive_config or ProactiveConfig(),
            persona=persona,
            affection=affection,
            mood=mood,
            memory=memory,
            stickers=stickers,
            replier=replier,
            gateway=gateway,
            db=db,
            send_msg_fn=send_msg_fn,
        )

    admin = None
    if include_admin:
        admin = AdminServer(
            config=admin_config or AdminConfig(),
            persona=persona,
            affection=affection,
            mood=mood,
            memory=memory,
            stickers=stickers,
            proactive=proactive,
            assembler=assembler,
            db=db,
            onebot=onebot,
            db_path=db_path or "data/companion.db",
            backup_dir=backup_dir or "data/backup/daily",
        )

    return EngineStack(
        persona=persona,
        affection=affection,
        mood=mood,
        memory=memory,
        stickers=stickers,
        replier=replier,
        assembler=assembler,
        proactive=proactive,
        admin=admin,
    )


def make_mock_gateway(
    chat_return_value: Optional[str] = None,
    observer_model: str = "deepseek-chat",
    chat_side_effect: Optional[Any] = None,
) -> MagicMock:
    """创建并返回配置好 chat/stream_chat 的 Mock LLMGateway。"""
    mock_gw = MagicMock()
    mock_gw.config.observer_model = observer_model
    if chat_side_effect is not None:
        mock_gw.chat = AsyncMock(side_effect=chat_side_effect)
    elif chat_return_value is not None:
        mock_gw.chat = AsyncMock(return_value=chat_return_value)
    else:
        mock_gw.chat = AsyncMock()
    mock_gw.stream_chat = AsyncMock()
    return mock_gw


def card_path() -> str:
    """解析测试要用的角色卡目录，让公开 clone 不再依赖私有卡。

    优先级：
    1. 环境变量 QQC_TEST_CARD（显式指定，外人环境可设 characters/example）；
    2. characters/qingzi 存在则用它（所有者本机，保持原有覆盖）；
    3. 回落 characters/example（仓库自带的公开模板卡）。
    """
    env_card = os.environ.get("QQC_TEST_CARD", "").strip()
    if env_card:
        return env_card
    private_card = os.path.join("characters", "qingzi")
    if os.path.isdir(private_card):
        return private_card
    return os.path.join("characters", "example")


# 夹具卡的默认 10 阶段：名称覆盖 test_fixes18 断言的第 4/5 格（知己/微酸）。
FIXTURE_STAGE_NAMES = [
    "初识", "认识", "熟悉", "朋友", "知己", "微酸", "倾心", "深情", "挚爱", "相伴",
]

# 夹具卡的默认作息：周六（weekday=5）午后带校园场景词，
# 供 test_fixes14 验证长假路径不泄漏校园词。
FIXTURE_DAILY_ROUTINE = [
    {"start": 0, "end": 8, "activity": "在宿舍睡觉休息"},
    {"start": 8, "end": 12, "activity": "去琴房练琴"},
    {"start": 12, "end": 14, "activity": "在食堂吃午饭"},
    {"start": 14, "end": 18, "activity": "周末外出：坐校车去玉泉老校区看老建筑", "days": [5, 6]},
    {"start": 18, "end": 24, "activity": "在宿舍看书休息"},
]

# 夹具卡的日历锚点：两段就该覆盖全年（12-31~01-06 是跨年区间），
# 供 test_fixes16 验证"当前锚点 + 下一锚点"与全年无缺口；内容纯测试虚构。
FIXTURE_CALENDAR_ANCHORS = [
    ["01-07", "12-30", "测试锚点甲：学期里的平常节奏"],
    ["12-31", "01-06", "测试锚点乙：跨年假期"],
]

# 夹具卡的素材池：内容纯测试虚构，只用于断言"素材池真的进了提示词"。
FIXTURE_LIFE_ARC_SEED_POOL = [
    "【测试素材】",
    "1. 测试素材甲：只用于单元测试的虚构条目",
    "2. 测试素材乙：同样是虚构条目",
]

# 夹具卡的长假文案：故意带一个夹具自己的标记词，用来钉住"长假文案取自卡里"。
FIXTURE_LONG_HOLIDAY_ACTIVITY = "放长假中，在测试老家，不在学校"


def make_fixture_card(
    base_dir: str,
    stage_names: Optional[List[str]] = None,
    daily_routine: Optional[List[Dict[str, Any]]] = None,
    long_holiday_activity: Optional[str] = None,
    calendar_anchors: Optional[List[List[str]]] = None,
    life_arc_seed_pool: Optional[Any] = None,
) -> str:
    """在 base_dir 里写一张最小可用的测试夹具卡（结构化虚构内容，零私有影子）。

    只填被测内容需要的字段：恰好 10 个阶段（默认含「知己」「微酸」）、
    覆盖全天的 daily_routine（默认含一段周六校园作息）。返回 base_dir。
    需要独立临时目录时由调用方自备（如 tempfile.mkdtemp + addCleanup）。

    三个可选字段（long_holiday_activity / calendar_anchors / life_arc_seed_pool）
    **传了才写进卡**：不传 = 卡里没有该字段，走的正是"旧卡零改动"的那条路。
    """
    os.makedirs(base_dir, exist_ok=True)

    names = list(stage_names) if stage_names else list(FIXTURE_STAGE_NAMES)
    if len(names) != 10:
        raise ValueError(f"夹具卡 stages 必须恰好 10 个，当前 {len(names)}")
    stages = [
        {"name": name, "tone": "测试语气", "instructions": [f"阶段 {i} 的测试指令"]}
        for i, name in enumerate(names)
    ]
    routine = (
        [dict(item) for item in daily_routine]
        if daily_routine is not None
        else [dict(item) for item in FIXTURE_DAILY_ROUTINE]
    )

    card = {
        "name": "测试角色",
        "user_address": "你",
        "core_description": "测试夹具卡：只提供最小合法字段，不含任何真实角色设定。",
        "chat_style": {"rules": ["你在用手机QQ聊天，只输出聊天文字本身"]},
        "initial_dims": {
            "warmth": 40.0,
            "trust": 50.0,
            "intimacy": 35.0,
            "intrigue": 30.0,
            "patience": 50.0,
            "tension": 3.0,
        },
        "stages": stages,
        "daily_routine": routine,
        "personal_memories": [],
        "habits": [],
        "stickers_dir": "stickers",
    }
    if long_holiday_activity is not None:
        card["long_holiday_activity"] = long_holiday_activity
    if calendar_anchors is not None:
        card["calendar_anchors"] = [list(a) for a in calendar_anchors]
    if life_arc_seed_pool is not None:
        card["life_arc_seed_pool"] = life_arc_seed_pool

    with open(os.path.join(base_dir, "character.json"), "w", encoding="utf-8") as f:
        json.dump(card, f, ensure_ascii=False, indent=2)

    stickers_dir = os.path.join(base_dir, "stickers")
    os.makedirs(stickers_dir, exist_ok=True)
    with open(os.path.join(stickers_dir, "index.json"), "w", encoding="utf-8") as f:
        json.dump({}, f)

    return base_dir
