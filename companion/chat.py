"""仿真对话 CLI (companion/chat.py)
用于在终端与伴侣机器人进行全真沙箱对话验证文笔与逻辑，对生产数据零副作用。
用法:
  python -m companion.chat
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
from typing import Callable, List, Optional

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.persona import Persona
from companion.prompts import get_mood_description, get_mood_label
from companion.replier import Replier
from companion.stickers import StickerManager


class ChatSession:
    def __init__(
        self,
        config: Optional[Config] = None,
        prod_db_path: str = "data/companion.db",
        sandbox_db_path: str = "data/chat-sandbox.db",
    ):
        self.prod_db_path = prod_db_path
        self.sandbox_db_path = sandbox_db_path
        self.config = config or Config.load()
        self.db: Optional[Database] = None
        self.gateway: Optional[LLMGateway] = None
        self._initialized = False

    async def initialize(self) -> None:
        if not os.path.exists(self.prod_db_path):
            raise FileNotFoundError(
                f"生产数据库 {self.prod_db_path} 不存在，请先启动系统或进行正常交互后再运行仿真对话。"
            )

        # 复制生产库到沙箱
        sandbox_dir = os.path.dirname(self.sandbox_db_path)
        if sandbox_dir:
            os.makedirs(sandbox_dir, exist_ok=True)
        shutil.copy2(self.prod_db_path, self.sandbox_db_path)

        self.db = Database(self.sandbox_db_path)
        await self.db.init_tables()

        self.persona = Persona.load(self.config.character.path)
        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)
        await self.stickers.sync_initial_stickers()

        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(self.config.llm, self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)
        self.assembler = PromptAssembler(
            self.persona,
            self.affection,
            self.mood,
            self.memory,
            self.stickers,
            self.db,
            holidays_provider=self.config.get_holidays,
        )
        self.replier = Replier(self.config.reply, self.stickers)
        self.observer = Observer(
            self.gateway, self.affection, self.mood, self.memory, self.stickers, self.db
        )
        self._initialized = True

    async def close(self) -> None:
        if self.db:
            await self.db.close()
            self.db = None
        if self.gateway:
            await self.gateway.close()
            self.gateway = None
        if os.path.exists(self.sandbox_db_path):
            try:
                os.remove(self.sandbox_db_path)
            except Exception:
                pass

    async def handle_input(
        self,
        text: str,
        on_piece: Optional[Callable[[str], None]] = None,
    ) -> str:
        """处理一轮输入并落沙箱库（流式调用 -> 切段 -> 落库 -> 加固 -> 结算）"""
        messages, _ = await self.assembler.assemble_messages(text)
        if hasattr(self.config.llm, "active"):
            target_model = self.config.llm.active().chat
        else:
            target_model = getattr(self.config.llm, "chat_model", "deepseek-chat")

        reply_parts: List[str] = []
        try:
            async for piece in self.gateway.stream_chat(
                messages=messages,
                model=target_model,
                purpose="main_chat",
            ):
                if on_piece:
                    on_piece(piece)
                reply_parts.append(piece)
        except Exception as e:
            err_msg = f"（调用出错: {e}）"
            if on_piece:
                on_piece(err_msg)
            reply_parts.append(err_msg)

        full_reply = "".join(reply_parts).strip()
        chunks, clean_record_text = self.replier.parse_reply(full_reply)
        if not clean_record_text:
            clean_record_text = full_reply

        # 本轮对话落沙箱库
        await self.memory.save_turn_pair(
            user_msg=text,
            bot_msg=clean_record_text,
            has_image=False,
        )
        await self.memory.reinforce_memories(text)
        await self.observer.settle_turn(
            user_message=text,
            assistant_reply=clean_record_text,
            user_image_path=None,
        )
        return clean_record_text

    async def get_status_str(self) -> str:
        aff = await self.affection.get_state()
        dims = aff.get("dims", {})
        comp = float(aff.get("composite", 30.0))
        stg = int(aff.get("stage", 0))
        stg_obj = self.persona.get_stage(stg)

        mood = await self.mood.get_state()
        v = float(mood.get("v", 2.0))
        a = float(mood.get("a", 1.0))
        t = float(mood.get("t", 7.0))
        lbl = get_mood_label(v, a)
        desc = get_mood_description(v, a)

        lines = [
            f"【好感度】阶段 {stg} ({stg_obj.name}) | 复合分: {comp:.1f}",
            f"  温暖: {dims.get('warmth', 0):.1f} | 信任: {dims.get('trust', 0):.1f} | 亲密: {dims.get('intimacy', 0):.1f}",
            f"  好奇: {dims.get('intrigue', 0):.1f} | 包容: {dims.get('patience', 0):.1f} | 紧张: {dims.get('tension', 0):.1f}",
            f"【心境 (PAD)】{lbl} ({desc}) | 安心度: {t:.2f} | 愉悦度: {v:.1f} | 唤醒度: {a:.1f}",
        ]
        return "\n".join(lines)


async def run_chat() -> None:
    session = ChatSession()
    try:
        await session.initialize()
    except FileNotFoundError as e:
        print(f"\n❌ {e}\n")
        return

    print("=" * 60)
    print(f"✨ 仿真对话沙箱已启动 · 角色: {session.persona.name} ✨")
    print("内置指令: /prompt 查看提示词 | /status 查看好感度 | /quit 退出")
    print("提示: 所有对话与状态仅在沙箱副本运行，对生产数据零副作用。")
    print("=" * 60)

    try:
        while True:
            try:
                user_input = input(f"\n{session.persona.user_address}> ").strip()
            except (EOFError, KeyboardInterrupt):
                break

            if not user_input:
                continue

            if user_input == "/quit":
                print("退出仿真对话。")
                break
            elif user_input == "/prompt":
                prompt = session.assembler.last_assembled_prompt or await session.assembler.assemble_system_prompt("")
                print("\n--- 最近一次完整 System Prompt ---")
                print(prompt)
                print("----------------------------------\n")
                continue
            elif user_input == "/status":
                status = await session.get_status_str()
                print("\n--- 当前好感与心境状态 ---")
                print(status)
                print("--------------------------\n")
                continue

            print(f"\n{session.persona.name}> ", end="", flush=True)
            await session.handle_input(
                user_input,
                on_piece=lambda piece: print(piece, end="", flush=True),
            )
            print()

    finally:
        await session.close()
        print("沙箱已清理完毕。")


if __name__ == "__main__":
    asyncio.run(run_chat())
