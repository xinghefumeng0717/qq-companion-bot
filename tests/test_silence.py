"""沉默权（[沉默]）测试集 (tests/test_silence.py)

覆盖三处：
1. parse_reply：完整输出恰为 [沉默] -> ([], "")；混入其他文字的一律走正常流程（防滥用）
2. send_reply_chunks：空段列表不发送、正常返回
3. TurnHandler：沉默时不发送、不落 assistant 记录（只落用户消息）、跳过 observer 结算
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from companion.config import ReplyConfig
from companion.memory import MemoryManager
from companion.prompts import SYSTEM_PROMPT_TEMPLATE
from companion.replier import SILENCE_TOKEN, Replier, is_silence_output
from companion.turn_handler import TurnHandler
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway


class _NoStickers:
    """假表情包管理器：一律匹配不到（不落盘）"""

    def match_sticker(self, desc: str):
        return None

    def get_prompt_sticker_list(self):
        return []


def _handler(gateway: MagicMock, replier, memory, observer, proactive, send_fn):
    assembler = MagicMock()
    assembler.assemble_messages = AsyncMock(return_value=([], "sys"))

    def _stream(**kwargs):
        async def _gen():
            for piece in gateway._pieces:
                yield piece

        return _gen()

    gateway.stream_chat = _stream
    return TurnHandler(
        config=MagicMock(),
        gateway=gateway,
        assembler=assembler,
        replier=replier,
        memory=memory,
        observer=observer,
        proactive=proactive,
        send_chunk_fn=send_fn,
    )


class TestSilenceParsing(unittest.TestCase):
    def _replier(self):
        return Replier(
            ReplyConfig(max_chunks=5, chunk_delay_min=0.0, chunk_delay_max=0.0),
            _NoStickers(),
        )

    def test_exact_silence_returns_empty(self):
        chunks, record = self._replier().parse_reply(SILENCE_TOKEN)
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_silence_with_surrounding_whitespace(self):
        """模型常带首尾换行/空格，strip 后仍算沉默"""
        chunks, record = self._replier().parse_reply("  \n [沉默] \n ")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_silence_mixed_with_text_not_triggered(self):
        """行内含 [沉默] 但还有别的文字 -> 按正常文本走（防滥用）"""
        chunks, record = self._replier().parse_reply(f"{SILENCE_TOKEN} 哈哈")
        self.assertTrue(chunks)
        self.assertEqual(record, f"{SILENCE_TOKEN} 哈哈")

    def test_silence_appended_after_text_not_triggered(self):
        chunks, record = self._replier().parse_reply("好呀\n[沉默]")
        self.assertTrue(chunks)
        self.assertIn(SILENCE_TOKEN, record)

    def test_normal_reply_unaffected(self):
        chunks, record = self._replier().parse_reply("好呀，晚安啦")
        self.assertEqual(record, "好呀，晚安啦")
        self.assertTrue(chunks)

    def test_is_silence_output_helper(self):
        self.assertTrue(is_silence_output("[沉默]"))
        self.assertTrue(is_silence_output(" [沉默]\n"))
        self.assertFalse(is_silence_output("[沉默]好"))
        self.assertFalse(is_silence_output(""))

    def test_system_prompt_has_silence_clause(self):
        self.assertIn("沉默权：", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("[沉默]", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("除这个场景外严禁使用", SYSTEM_PROMPT_TEMPLATE)


class TestSendChunksEmpty(unittest.TestCase):
    def test_empty_chunks_sends_nothing(self):
        sent = []

        async def _send(chunk):
            sent.append(chunk)

        async def _run():
            replier = Replier(ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0), _NoStickers())
            await replier.send_reply_chunks([], _send)

        asyncio.run(_run())
        self.assertEqual(sent, [])


class TestTurnHandlerSilence(unittest.TestCase):
    def _run_turn(self, pieces):
        """跑一轮 handle_turn，返回 (发送段, turns 表行, observer)"""

        async def _run():
            db = await make_db()
            try:
                gateway = MagicMock()
                gateway._pieces = list(pieces)
                stack = make_engine_stack(
                    db,
                    gateway=make_mock_gateway(),
                    reply_config=ReplyConfig(max_chunks=5, chunk_delay_min=0.0, chunk_delay_max=0.0),
                )
                observer = MagicMock()
                observer.settle_turn = AsyncMock()
                proactive = MagicMock()
                proactive.reset_unanswered_count = AsyncMock()
                sent = []

                async def _send(chunk):
                    sent.append(chunk)

                handler = _handler(
                    gateway, stack.replier, stack.memory, observer, proactive, _send
                )
                await handler.handle_turn("嗯嗯", None)
                await asyncio.sleep(0.02)  # 让 create_task 出去的结算跑完（非沉默分支会用到）
                rows = await db.fetchall("SELECT role, content FROM turns ORDER BY id")
                return sent, [(r["role"], r["content"]) for r in rows], observer
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_silence_skips_send_record_and_observer(self):
        sent, rows, observer = self._run_turn(["[沉默]"])
        self.assertEqual(sent, [])
        self.assertEqual(rows, [("user", "嗯嗯")])
        observer.settle_turn.assert_not_awaited()

    def test_mixed_silence_goes_normal_flow(self):
        sent, rows, observer = self._run_turn(["[沉默] 哈哈"])
        self.assertTrue(sent)
        self.assertEqual(sent[0]["content"], "[沉默] 哈哈")
        self.assertEqual(rows[0][0], "user")
        self.assertEqual(rows[1], ("assistant", "[沉默] 哈哈"))
        observer.settle_turn.assert_awaited()

    def test_normal_reply_records_assistant(self):
        sent, rows, observer = self._run_turn(["好呀，晚安啦"])
        self.assertTrue(sent)
        self.assertEqual([r[0] for r in rows], ["user", "assistant"])
        observer.settle_turn.assert_awaited()


class TestSaveTurnPairSilence(unittest.TestCase):
    def test_bot_msg_none_writes_user_only(self):
        async def _run():
            db = await make_db()
            try:
                memory = MemoryManager(db, gateway=make_mock_gateway())
                await memory.save_turn_pair(user_msg="嗯嗯", bot_msg=None)
                await asyncio.sleep(0.02)  # 让后台日记归档任务跑完，避免它撞上已关闭的库
                rows = await db.fetchall("SELECT role, content FROM turns ORDER BY id")
                return [(r["role"], r["content"]) for r in rows]
            finally:
                await close_db(db)

        self.assertEqual(asyncio.run(_run()), [("user", "嗯嗯")])


if __name__ == "__main__":
    unittest.main()
