"""主聊候选生成 → 连贯性闸门 → parse_reply 的接线测试。"""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from companion.turn_handler import TurnHandler


def _entry(i, answers=True, coherent=True, consistent=True):
    return {"i": i, "answers": answers, "coherent": coherent, "consistent": consistent, "why": ""}


def _handler(judge, stream_texts, continuity=True):
    gateway = MagicMock()
    gateway.config.observer_model = "deepseek-flash"
    gateway.chat = AsyncMock(return_value=judge)

    stream_calls = 0

    async def stream(**_kwargs):
        nonlocal stream_calls
        index = min(stream_calls, len(stream_texts) - 1)
        stream_calls += 1
        if index:
            await asyncio.sleep(0.02 * index)
        yield stream_texts[index]

    gateway.stream_chat = stream
    assembler = MagicMock()
    assembler.assemble_messages = AsyncMock(return_value=([], "sys"))
    assembler.meetups = None

    replier = MagicMock()
    replier.parse_reply = MagicMock(
        side_effect=lambda text, **_kwargs: ([{"type": "text", "content": text}], text)
    )
    replier.send_reply_chunks = AsyncMock()
    replier.is_silence_decision = MagicMock(return_value=False)

    memory = MagicMock()
    memory.save_turn_pair = AsyncMock()
    memory.reinforce_memories = AsyncMock()
    memory.get_recent_turns = AsyncMock(return_value=[])
    memory.get_last_assistant_turn_time = AsyncMock(return_value=None)

    config = MagicMock()
    config.reply.candidates = len(stream_texts)
    config.reply.continuity_check = continuity
    config.reply.continuity_timeout = 8.0
    config.reply.candidate_tiebreak = "first"
    config.reply.trim_echo = False
    config.reply.self_check = False

    handler = TurnHandler(
        config=config,
        gateway=gateway,
        assembler=assembler,
        replier=replier,
        memory=memory,
        observer=MagicMock(settle_turn=AsyncMock()),
        proactive=MagicMock(reset_unanswered_count=AsyncMock()),
        send_chunk_fn=AsyncMock(),
    )
    return handler, gateway


class TestContinuityGatePipeline(unittest.IsolatedAsyncioTestCase):
    async def test_通过候选真正进入解析(self):
        judge = json.dumps({"candidates": [
            _entry(1, answers=False), _entry(2), _entry(3)
        ], "fixed": ""}, ensure_ascii=False)
        handler, gateway = _handler(judge, ["答非所问", "答得好", "另一个好"])
        # 生产里 gather_candidates 按完成先后返回，顺序不定；这里固定顺序，
        # 才能让判定里的 i=1/2/3 稳定对应同一段文本。
        with patch(
            "companion.turn_handler.pick_candidate",
            side_effect=lambda _his, candidates, tiebreak="warm": (
                candidates[0],
                {"picked": 0, "flaws": [0] * len(candidates), "warmth": [0] * len(candidates)},
            ),
        ), patch(
            "companion.turn_handler.gather_candidates",
            new=AsyncMock(return_value=["答非所问", "答得好", "另一个好"]),
        ):
            await handler.handle_turn("你吃了吗", None)
        self.assertEqual(handler.replier.parse_reply.call_args[0][0], "答得好")
        self.assertEqual(gateway.chat.call_count, 1)

    async def test_关闭闸门不调用判定(self):
        handler, gateway = _handler("{}", ["原稿"])
        handler.config.reply.continuity_check = False
        await handler.handle_turn("你吃了吗", None)
        gateway.chat.assert_not_called()
        self.assertEqual(handler.replier.parse_reply.call_args[0][0], "原稿")

    async def test_判定异常保留预选(self):
        handler, gateway = _handler(RuntimeError("offline"), ["原稿"])
        gateway.chat = AsyncMock(side_effect=RuntimeError("offline"))
        await handler.handle_turn("你吃了吗", None)
        self.assertEqual(handler.replier.parse_reply.call_args[0][0], "原稿")


if __name__ == "__main__":
    unittest.main()
