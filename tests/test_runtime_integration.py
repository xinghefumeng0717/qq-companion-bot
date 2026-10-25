import os
import tempfile
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from companion.config import ReplyConfig
from companion.replier import Replier
from companion.runtime_state import RuntimeEventStore, TurnTracer
from companion.proactive import ProactiveScheduler
from companion.config import ProactiveConfig
from companion.turn_handler import TurnHandler
from helpers import card_path, close_db, make_db, make_engine_stack


class RuntimePromptIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_实验提示词只读且状态块独立(self):
        db = await make_db()
        try:
            stack = make_engine_stack(db, persona_path=card_path())
            await stack.stickers.sync_initial_stickers()
            store = RuntimeEventStore(db)
            await store.init()
            await store.record_voice("sent", "晚安", now=datetime(2026, 10, 24, 12, 0))
            stack.assembler.runtime_state_v1 = True
            stack.assembler.runtime_events = store
            before = dict(await stack.mood.get_state())
            prompt = await stack.assembler.assemble_system_prompt(
                "在干嘛", now=datetime(2026, 10, 24, 12, 30),
                runtime_state_block=await store.render_prompt(),
            )
            after = dict(await stack.mood.get_state())
            self.assertEqual(before, after)
            self.assertIn("【当前对账状态】", prompt)
            self.assertIn("\n【事实】", prompt)
            self.assertIn("不代表对方已经听到", prompt)
        finally:
            await close_db(db)


async def _run_handler(delivery):
    db = await make_db()
    store = RuntimeEventStore(db)
    await store.init()
    gateway = MagicMock()
    gateway.config.observer_model = "deepseek-flash"
    gateway.config.text_model = "deepseek-flash"

    async def stream(**_kwargs):
        yield "刚逛完摊子\n腿有点酸"

    gateway.stream_chat = stream
    assembler = MagicMock()
    assembler.assemble_messages = AsyncMock(return_value=([], "sys"))
    assembler.meetups = None
    assembler.persona.get_current_activity = MagicMock(return_value="闲逛")
    assembler.get_holidays = MagicMock(return_value=[])
    tts = SimpleNamespace(
        config=SimpleNamespace(enabled=True, max_chars=90),
        check_gate=AsyncMock(return_value=(True, "测试放行")),
    )
    reply_config = ReplyConfig(chunk_delay_min=0, chunk_delay_max=0)
    reply_config.runtime_state_v1 = True
    reply_config.continuity_check = False
    reply_config.candidates = 1
    stickers = MagicMock()
    stickers.match_sticker = MagicMock(return_value=None)
    replier = Replier(reply_config, stickers)
    memory = MagicMock()
    memory.save_turn_pair = AsyncMock()
    memory.reinforce_memories = AsyncMock()
    memory.get_recent_turns = AsyncMock(return_value=[])
    memory.get_last_assistant_turn_time = AsyncMock(return_value=None)
    proactive = MagicMock(reset_unanswered_count=AsyncMock())
    sent_chunks = []

    async def send(chunk):
        sent_chunks.append(chunk)
        if chunk.get("type") == "voice":
            if delivery == "sent":
                chunk["_voice_delivery"] = "sent"
            else:
                chunk["type"] = "text"
                chunk["_voice_delivery"] = "recovered"

    config = MagicMock()
    config.reply = reply_config
    config.llm.text_model = "deepseek-flash"
    handler = TurnHandler(
        config=config, gateway=gateway, assembler=assembler, replier=replier,
        memory=memory, observer=MagicMock(settle_turn=AsyncMock()), proactive=proactive,
        send_chunk_fn=send, tts=tts,
    )
    handler.runtime_events = store
    with tempfile.TemporaryDirectory() as directory:
        handler.turn_tracer = TurnTracer(directory)
        await handler.handle_turn("在干嘛", None)
        trace_path = os.path.join(directory, f"{datetime.now():%Y-%m-%d}.jsonl")
        trace_exists = os.path.exists(trace_path)
    return store, sent_chunks, memory, trace_exists


class RuntimeTurnIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_高频语音成功写入已发出(self):
        store, chunks, memory, trace_exists = await _run_handler("sent")
        await close_db(store.db)
        voice = [c for c in chunks if c.get("_runtime_voice")]
        self.assertEqual(len(voice), 1)
        self.assertEqual(voice[0]["content"], "腿有点酸")
        self.assertEqual(voice[0]["_voice_delivery"], "sent")
        self.assertTrue(trace_exists)
        self.assertIn("刚逛完摊子\n（语音消息）腿有点酸", memory.save_turn_pair.call_args.kwargs["bot_msg"])

    async def test_语音降级写入文字兜底而非已发出(self):
        store, chunks, memory, _ = await _run_handler("recovered")
        latest = await store.latest_voice()
        await close_db(store.db)
        self.assertEqual(latest["status"], "text_fallback")
        self.assertTrue(any(c.get("type") == "text" and c.get("_runtime_voice") for c in chunks))



class RuntimeProactiveIntegration(unittest.IsolatedAsyncioTestCase):
    async def test_主动消息也按最终语音投递入账(self):
        db = await make_db()
        store = RuntimeEventStore(db)
        await store.init()
        gateway = MagicMock()
        gateway.config.text_model = "deepseek-flash"
        gateway.chat = AsyncMock(return_value="刚逛完摊子\n腿有点酸")
        assembler = MagicMock()
        assembler.runtime_state_v1 = True
        assembler.voice_frequency = "frequent"
        assembler.assemble_system_prompt = AsyncMock(return_value="sys")
        persona = MagicMock()
        persona.world.face_usage = "normal"
        persona.world.relative_dates = False
        memory = MagicMock()
        memory.get_recent_turns = AsyncMock(return_value=[])
        memory.save_proactive_turn = AsyncMock()
        memory.increment_unanswered_count = AsyncMock()
        reply_config = ReplyConfig(chunk_delay_min=0, chunk_delay_max=0)
        replier = Replier(reply_config, MagicMock())
        sent = []

        async def send(chunk):
            sent.append(chunk)
            if chunk.get("type") == "voice":
                chunk["_voice_delivery"] = "sent"

        scheduler = ProactiveScheduler(
            config=ProactiveConfig(), persona=persona,
            affection=MagicMock(), mood=MagicMock(), memory=memory,
            stickers=MagicMock(), replier=replier, gateway=gateway, db=db,
            send_msg_fn=send, assembler=assembler,
            tts=SimpleNamespace(config=SimpleNamespace(enabled=True, max_chars=90)),
        )
        scheduler.runtime_events = store
        scheduler._voice_gate_ok = AsyncMock(return_value=True)
        scheduler._play_typing_indicator = AsyncMock()
        scheduler.increment_unanswered_count = AsyncMock()
        await scheduler._generate_and_send("随便说说", "2026-10-24 16:00")
        latest = await store.latest_voice()
        await close_db(db)
        self.assertEqual(latest["status"], "sent")
        self.assertTrue(any(c.get("_runtime_voice") for c in sent))
        memory.save_proactive_turn.assert_awaited()


if __name__ == "__main__":
    unittest.main()
