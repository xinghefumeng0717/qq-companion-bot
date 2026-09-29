"""伴侣机器人主入口 (main.py)
装配全部系统模块，启动 OneBot 客户端、主动消息调度器、消息聚合器与状态仪表盘。
支持通过 --status 命令行参数打印当前状态（好感度、PAD、日记、事实）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any, Dict, List, Optional

from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.aggregator import MessageAggregator
from companion.assembler import PromptAssembler
from companion.backup import DailyBackupScheduler
from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.onebot import OneBotClient, build_image_segment, build_text_segment
from companion.observer import Observer
from companion.persona import Persona
from companion.prompts import get_mood_description, get_mood_label, get_trust_description
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager
from companion.turn_handler import TurnHandler
from companion.voice import VoiceProcessor

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("companion")


def format_status_text(
    persona: Persona,
    aff_state: Dict[str, Any],
    mood_state: Dict[str, Any],
    facts: Optional[List[str]] = None,
    diaries: Optional[List[str]] = None,
) -> str:
    """统一格式化伴侣机器人的实时状态（好感度、PAD心境、事实与日记）"""
    dims = aff_state.get("dims", {})
    comp = float(aff_state.get("composite", 30.0))
    stage_idx = int(aff_state.get("stage", 0))
    stage_obj = persona.get_stage(stage_idx)

    v = float(mood_state.get("v", 2.0))
    a = float(mood_state.get("a", 1.0))
    t = float(mood_state.get("t", 7.0))
    frustration = float(mood_state.get("frustration", 0.0))

    lines = [
        "=" * 50,
        f"伴侣状态报告：{persona.name}",
        "=" * 50,
        f"【好感度】复合分: {comp:.1f} | 阶段 {stage_idx} ({stage_obj.name}: {stage_obj.tone})",
        f"  - 温暖 (warmth):   {dims.get('warmth', 0.0):.1f}",
        f"  - 信任 (trust):    {dims.get('trust', 0.0):.1f}",
        f"  - 亲密 (intimacy): {dims.get('intimacy', 0.0):.1f}",
        f"  - 好奇 (intrigue): {dims.get('intrigue', 0.0):.1f}",
        f"  - 包容 (patience): {dims.get('patience', 0.0):.1f}",
        f"  - 紧张 (tension):  {dims.get('tension', 0.0):.1f}",
        "-" * 50,
        f"【情绪 (PAD)】{get_mood_label(v, a)} ({get_mood_description(v, a)})",
        f"  - 愉悦度 (Valence):  {v:.1f}",
        f"  - 唤醒度 (Arousal):  {a:.1f}",
        f"  - 安心度 (Trust):    {t:.2f} ({get_trust_description(t)})",
        f"  - 冷落驱力 (Frust):  {frustration:.2f}",
        "-" * 50,
    ]
    if facts is not None:
        lines.append(f"【语义事实 (Facts)】共 {len(facts)} 条:")
        for f in facts:
            lines.append(f"  * {f}")
    if diaries is not None:
        lines.append(f"【记忆日记 (Active Diaries)】共 {len(diaries)} 条:")
        for d in diaries[:5]:
            lines.append(f"  * {d}")
    lines.append("=" * 50)
    return "\n".join(lines)


async def print_status(config: Config) -> None:
    """CLI 打印当前机器人状态 (--status)"""
    db = Database()
    await db.init_tables()
    persona = Persona.load(config.character.path)
    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db)

    aff_state = await affection.get_state()
    mood_state = await mood.get_state()
    v = float(mood_state.get("v", 2.0))
    facts = await memory.get_all_facts()
    diaries = await memory.get_active_diaries(current_valence=v)

    report = format_status_text(persona, aff_state, mood_state, facts, diaries)
    print("\n" + report + "\n")
    await db.close()


class CompanionBot:
    def __init__(self, config: Config):
        self.config = config
        self.db = Database("data/companion.db")
        self.persona = Persona.load(config.character.path)

        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)

        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(config.llm, self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)

        self.assembler = PromptAssembler(
            self.persona, self.affection, self.mood, self.memory, self.stickers, self.db
        )
        self.replier = Replier(config.reply, self.stickers)
        self.observer = Observer(
            self.gateway, self.affection, self.mood, self.memory, self.stickers, self.db
        )

        self.voice_processor = VoiceProcessor(config.voice)

        self.proactive = ProactiveScheduler(
            config=config.proactive,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=self.gateway,
            db=self.db,
            send_msg_fn=self._send_chunk_to_onebot,
            assembler=self.assembler,
        )

        self.turn_handler = TurnHandler(
            config=config,
            gateway=self.gateway,
            assembler=self.assembler,
            replier=self.replier,
            memory=self.memory,
            observer=self.observer,
            proactive=self.proactive,
            send_chunk_fn=self._send_chunk_to_onebot,
        )
        self.aggregator = MessageAggregator(turn_handler=self.turn_handler.handle_turn)

        self.backup_scheduler = DailyBackupScheduler(
            db_path="data/companion.db",
            backup_dir="data/backup/daily",
        )

        self._stopping = False
        self._closed = False

        self.onebot = OneBotClient(
            config.onebot,
            allowed_user_id=config.account.allowed_user_id,
            on_message_callback=self._on_raw_message,
            voice_processor=self.voice_processor,
        )

        self.admin = AdminServer(
            config=config.admin,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            proactive=self.proactive,
            assembler=self.assembler,
            db=self.db,
            onebot=self.onebot,
            db_path="data/companion.db",
            backup_dir="data/backup/daily",
        )

    async def _send_chunk_to_onebot(self, chunk: Dict[str, Any]) -> None:
        """分段发送底层调用"""
        if chunk["type"] == "text":
            segs = [build_text_segment(chunk["content"])]
        elif chunk["type"] == "sticker":
            segs = [build_image_segment(chunk["file"])]
        else:
            return
        await self.onebot.send_private_msg(self.config.account.allowed_user_id, segs)

    async def _on_raw_message(self, text: str, image_path: Optional[str]) -> None:
        """OneBot 收到机主私聊时交由聚合器"""
        await self.aggregator.push_message(text, image_path)

    async def _handle_turn(self, user_text: str, image_path: Optional[str]) -> None:
        """兼容保留：委托给 TurnHandler 处理单轮对话"""
        await self.turn_handler.handle_turn(user_text, image_path)

    async def run(self) -> None:
        """主运行循环"""
        await self.db.init_tables()
        await self.stickers.sync_initial_stickers()
        self.aggregator.start()
        self.proactive.start()
        self.backup_scheduler.start()
        await self.admin.start()

        # 启动 OneBot WebSocket 客户端
        try:
            await self.onebot.start()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.info("[Bot] 正在关闭伴侣机器人...")

        self.backup_scheduler.stop()
        self.aggregator.stop()
        self.proactive.stop()
        await self.admin.stop()
        await self.onebot.stop()
        await self.gateway.close()
        await self.db.close()

        # 等待后台任务彻底取消，杜绝 pending task 警告
        bg_tasks = []
        for t in [
            getattr(self.aggregator, "_debounce_task", None),
            getattr(self.aggregator, "_consumer_task", None),
            getattr(self.proactive, "_task", None),
            getattr(self.backup_scheduler, "_task", None),
        ]:
            if t and not t.done():
                t.cancel()
                bg_tasks.append(t)
        if bg_tasks:
            await asyncio.gather(*bg_tasks, return_exceptions=True)

        logger.info("[Bot] 关闭完成")

    async def stop_gracefully(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        await self.close()

        # 取消所有尚未结束的后台任务并彻底等待其退出
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ Companion Bot")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    parser.add_argument("--status", action="store_true", help="打印当前伴侣状态并退出")
    args = parser.parse_args()

    config = Config.load(args.config)

    if args.status:
        asyncio.run(print_status(config))
        return

    bot = CompanionBot(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler() -> None:
        logger.info("[Bot] 接收到退出信号")
        loop.create_task(bot.stop_gracefully())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, AttributeError):
            pass

    try:
        loop.run_until_complete(bot.run())
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("[Bot] 进程已中断退出")
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    main()
