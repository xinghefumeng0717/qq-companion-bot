"""测试辅助模块 (tests/helpers.py)

提取公共样板：
1. make_db / close_db: 测试数据库初始化与清理
2. make_engine_stack: 角色卡与引擎组装
3. make_mock_gateway: Mock LLM Gateway
"""

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional
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
