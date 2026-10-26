"""一步看图（[reply].vision_direct）：主聊模型直接看图，回复末尾的【看到：…】剥掉存进记录"""

import asyncio
import os
import tempfile
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from companion.config import ReplyConfig
from companion.prompts import IMAGE_SEEN_INSTRUCTION
from companion.turn_handler import TurnHandler, split_seen_note


class TestSplitSeenNote(unittest.TestCase):
    def test_strips_last_line(self):
        reply, seen = split_seen_note("哈哈这锅底看着就辣\n你吃得完吗\n【看到：一锅红油火锅，冒着热气】")
        self.assertEqual(reply, "哈哈这锅底看着就辣\n你吃得完吗")
        self.assertEqual(seen, "一锅红油火锅，冒着热气")

    def test_half_width_and_unclosed(self):
        self.assertEqual(split_seen_note("好看\n[看到: 夕阳下的海")[1], "夕阳下的海")
        self.assertEqual(split_seen_note("好看\n[看到: 夕阳下的海")[0], "好看")

    def test_no_note_returns_text_unchanged(self):
        self.assertEqual(split_seen_note("就这？"), ("就这？", ""))

    def test_note_in_middle_removed_and_lines_rejoined(self):
        reply, seen = split_seen_note("嗯\n【看到：一只猫】\n挺可爱")
        self.assertEqual(reply, "嗯\n挺可爱")
        self.assertEqual(seen, "一只猫")

    def test_truncated_to_120(self):
        self.assertEqual(len(split_seen_note("嗯\n【看到：" + "长" * 300 + "】")[1]), 120)

    def test_other_bracket_variants(self):
        """模型换括号写法：全角圆括号 / 半角圆括号 / 『』 / 「」"""
        for note in ("（看到：窗外在下雨）", "(看到: 窗外在下雨)", "『看到：窗外在下雨』", "「看到：窗外在下雨」"):
            with self.subTest(note=note):
                self.assertEqual(split_seen_note("好看\n" + note), ("好看", "窗外在下雨"))

    def test_mismatched_brackets(self):
        """括号写反（【看到：…)）也要剥干净，不能留残尾"""
        self.assertEqual(split_seen_note("好看\n【看到：一碗面)"), ("好看", "一碗面"))
        self.assertEqual(split_seen_note("好看\n（看到：一碗面】"), ("好看", "一碗面"))

    def test_unclosed_cross_line_eats_to_end(self):
        """漏闭合 + 描述跨行：一直剥到文本末尾，第二行不许上屏"""
        reply, seen = split_seen_note("好看\n【看到：第一行\n第二行")
        self.assertEqual(reply, "好看")
        self.assertEqual(seen, "第一行\n第二行")

    def test_closed_cross_line(self):
        """闭合符不在同一行也认，跨行描述整段剥掉"""
        self.assertEqual(split_seen_note("好看\n【看到：第一行\n第二行】"), ("好看", "第一行\n第二行"))

    def test_multiple_notes_take_last(self):
        reply, seen = split_seen_note("好看\n【看到：一只猫】\n【看到：一只狗）")
        self.assertEqual(reply, "好看")
        self.assertEqual(seen, "一只狗")

    def test_inline_note_at_line_end(self):
        self.assertEqual(split_seen_note("哈哈这锅真辣【看到：一锅红油】"), ("哈哈这锅真辣", "一锅红油"))

    def test_nested_brackets_in_description(self):
        self.assertEqual(
            split_seen_note("好看\n【看到：红油火锅（微辣），冒着热气】"),
            ("好看", "红油火锅（微辣），冒着热气"),
        )

    def test_prose_mentioning_seen_is_kept(self):
        """正文里的"看到"不是描述行：不带括号、或括号里不带冒号，都不剥"""
        for text in ("我刚看到你说的那个视频了，笑死", "（看到你来了）", "他看到了吗"):
            with self.subTest(text=text):
                self.assertEqual(split_seen_note(text), (text, ""))

    def test_note_only_leaves_empty_reply(self):
        self.assertEqual(split_seen_note("【看到：一只猫】"), ("", "一只猫"))

    def test_leftover_blank_and_indented_lines_cleaned(self):
        self.assertEqual(split_seen_note("嗯\n   \n【看到：一只猫】\n  挺可爱"), ("嗯\n挺可爱", "一只猫"))


class TestConfig(unittest.TestCase):
    def test_default_off(self):
        self.assertFalse(ReplyConfig().vision_direct)


def _handler(stream_pieces, candidates=3):
    gateway = MagicMock()
    gateway.chat = AsyncMock(return_value="不该被调用")
    calls = []

    async def _stream(**kwargs):
        calls.append(kwargs)
        for piece in stream_pieces:
            yield piece

    gateway.stream_chat = _stream

    assembler = MagicMock()
    assembler.assemble_messages = AsyncMock(return_value=([], "sys"))
    assembler.meetups = None

    replier = MagicMock()
    replier.parse_reply = MagicMock(return_value=([{"type": "text", "content": "嗯"}], "嗯"))
    replier.send_reply_chunks = AsyncMock()
    replier.is_silence_decision = MagicMock(return_value=False)

    memory = MagicMock()
    memory.save_turn_pair = AsyncMock()
    memory.reinforce_memories = AsyncMock()
    memory.get_recent_turns = AsyncMock(return_value=[])

    observer = MagicMock()
    observer.settle_turn = AsyncMock()

    config = MagicMock()
    config.reply.vision_direct = True
    config.reply.candidates = candidates
    config.reply.trim_echo = False
    config.reply.self_check = False

    handler = TurnHandler(
        config=config,
        gateway=gateway,
        assembler=assembler,
        replier=replier,
        memory=memory,
        observer=observer,
        proactive=MagicMock(reset_unanswered_count=AsyncMock()),
        send_chunk_fn=AsyncMock(),
    )
    return handler, gateway, calls


def _run(handler):
    async def _go():
        with tempfile.TemporaryDirectory() as tmp:
            img = os.path.join(tmp, "s.jpg")
            with open(img, "wb") as f:
                f.write(b"\xff\xd8\xff")
            with mock.patch(
                "companion.turn_handler.image_to_base64_data_url",
                return_value=("data:image/jpeg;base64,AAA", None),
            ):
                await handler.handle_turn("你看", img)
                await asyncio.sleep(0.01)

    asyncio.run(_go())


class TestDirectPipeline(unittest.TestCase):
    def test_image_goes_to_main_model_without_vision_call(self):
        handler, gateway, calls = _handler(["好家伙\n", "【看到：一碗清汤锅底】"])
        _run(handler)
        gateway.chat.assert_not_called()
        args = handler.assembler.assemble_messages.call_args[0]
        self.assertEqual(args[1], "data:image/jpeg;base64,AAA", "图直接交给主聊组装")

    def test_only_one_candidate_on_image_turn(self):
        handler, _, calls = _handler(["好家伙\n【看到：一碗清汤锅底】"], candidates=3)
        _run(handler)
        self.assertEqual(len(calls), 1, "发图那轮不并行多条")

    def test_note_stripped_and_saved_with_his_message(self):
        handler, _, _ = _handler(["好家伙\n", "【看到：一碗清汤锅底】"])
        _run(handler)
        sent = handler.replier.parse_reply.call_args[0][0]
        self.assertNotIn("看到", sent, "描述行不能发给他")
        saved = handler.memory.save_turn_pair.call_args.kwargs["user_msg"]
        self.assertIn("[发来一张照片：一碗清汤锅底]", saved)
        settled = handler.observer.settle_turn.call_args.kwargs["user_message"]
        self.assertIn("一碗清汤锅底", settled, "观察者也要知道图里是什么")

    def test_continuity_cannot_reintroduce_seen_note(self):
        raw = "好家伙\n【看到：一碗清汤锅底】"
        handler, _, _ = _handler([raw])
        handler.config.reply.continuity_check = True
        with mock.patch(
            "companion.turn_handler.check_continuity",
            AsyncMock(return_value=(raw, {"changed": True})),
        ):
            _run(handler)
        sent = handler.replier.parse_reply.call_args[0][0]
        self.assertEqual(sent, "好家伙")
        saved = handler.memory.save_turn_pair.call_args.kwargs["user_msg"]
        self.assertIn("[发来一张照片：一碗清汤锅底]", saved)

    def test_missing_note_falls_back_to_plain_marker(self):
        handler, _, _ = _handler(["好家伙"])
        _run(handler)
        saved = handler.memory.save_turn_pair.call_args.kwargs["user_msg"]
        self.assertIn("[发来一张图片]", saved)

    def test_bracket_variant_not_sent(self):
        """全角括号变体同样不上屏，描述照旧进记录"""
        handler, _, _ = _handler(["好家伙\n", "（看到：一碗清汤锅底）"])
        _run(handler)
        self.assertEqual(handler.replier.parse_reply.call_args[0][0], "好家伙")
        saved = handler.memory.save_turn_pair.call_args.kwargs["user_msg"]
        self.assertIn("[发来一张照片：一碗清汤锅底]", saved)

    def test_unclosed_cross_line_variant_not_sent(self):
        """漏闭合且描述跨行：残尾一行都不许上屏"""
        handler, _, _ = _handler(["好家伙\n【看到：一锅红油\n还冒着热气"])
        _run(handler)
        sent = handler.replier.parse_reply.call_args[0][0]
        self.assertEqual(sent, "好家伙")
        saved = handler.memory.save_turn_pair.call_args.kwargs["user_msg"]
        self.assertIn("一锅红油\n还冒着热气", saved)


class TestAssemblerInstruction(unittest.TestCase):
    def test_instruction_only_rides_with_image(self):
        from companion import assembler as asm

        with open(asm.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('f"{user_text}\\n{IMAGE_SEEN_INSTRUCTION}"', src)
        self.assertIn("【看到：", IMAGE_SEEN_INSTRUCTION)


if __name__ == "__main__":
    unittest.main()
