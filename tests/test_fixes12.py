"""FIXES12 观察期小修测试集 (tests/test_fixes12.py)

按任务书分组：
1. 「不能拍照」规则入三个提示词 + [图片] 占位符发送滤网（E8）
2. 视觉描述补社交语义（E9）

两条生产证据：
E8 = 10-04 09:19 主动消息把字面量 [图片] 当普通文字发到了 QQ 上
E9 = 10-04 10:03 表情包的社交含义（收尾信号）被视觉描述滤成纯画面元素
"""

import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock

from companion.config import ReplyConfig
from companion.prompts import (
    PROACTIVE_DECISION_PROMPT,
    PROACTIVE_GENERATE_PROMPT,
    SYSTEM_PROMPT_TEMPLATE,
    VISION_PERCEPTION_PROMPT,
)
from companion.replier import Replier, drop_image_placeholder_lines


class _RecordingStickers:
    """假表情包管理器：按描述词直接回一个假路径（不落盘）"""

    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫", "狗头"]


# ==========================================
# 任务 1：不能拍照规则 + [图片] 占位符滤网
# ==========================================


class TestTask1PromptGuards(unittest.TestCase):
    """三个提示词模板都要带上"不能拍照/禁止占位符"的硬约束"""

    def test_main_prompt_sticker_line_forbids_placeholder(self):
        self.assertIn(
            "你不能拍照，也无法发送真实照片；[sticker:描述词] 是你唯一的发图方式，"
            "绝对不要输出 [图片]、[照片] 这类占位符。",
            SYSTEM_PROMPT_TEMPLATE,
        )

    def test_main_prompt_keeps_original_sticker_sentence(self):
        """禁令是追加，不得改掉原句（表情包能力本身要留着）"""
        self.assertIn(
            "可发送表情包：在需要时于消息中单独一行输出 [sticker:描述词]",
            SYSTEM_PROMPT_TEMPLATE,
        )
        self.assertIn("{stickers_list}", SYSTEM_PROMPT_TEMPLATE)

    def test_decision_prompt_forbids_unexecutable_photo_topic(self):
        self.assertIn(
            "注意：她不能拍照或发送真实照片，topic_hint 不要包含"
            "\"拍照片/拍张照发给他\"这类不可执行的计划；想分享场景就用文字描述。",
            PROACTIVE_DECISION_PROMPT,
        )

    def test_decision_prompt_keeps_branch_spec(self):
        """追加不得挤掉原有 A/B/C 分支说明"""
        for tag in ("- A:", "- B:", "- C:"):
            self.assertIn(tag, PROACTIVE_DECISION_PROMPT)

    def test_generate_prompt_forbids_placeholder(self):
        self.assertIn(
            "你不能拍照或发真实照片；唯一的发图方式是单独一行 [sticker:描述词]；"
            "绝对禁止输出 [图片]、[照片] 这类占位符。",
            PROACTIVE_GENERATE_PROMPT,
        )

    def test_generate_prompt_keeps_original_requirements(self):
        for old in ("1. 只输出 1~3 条", "2. 绝对禁止动作描写", "5. 发消息前先核对"):
            self.assertIn(old, PROACTIVE_GENERATE_PROMPT)


class TestTask1PlaceholderFilterUnit(unittest.TestCase):
    """滤网函数级：整行占位符丢弃，行内夹杂文字的保留"""

    def test_five_placeholder_forms_dropped(self):
        for placeholder in ("[图片]", "【图片】", "[照片]", "【照片】", "[image]"):
            with self.subTest(placeholder=placeholder):
                self.assertEqual(drop_image_placeholder_lines(placeholder), "")

    def test_case_insensitive_variants_dropped(self):
        for placeholder in ("[IMAGE]", "[Image]", "[iMaGe]", "  [image]  ", "\t[图片]\t"):
            with self.subTest(placeholder=placeholder):
                self.assertEqual(drop_image_placeholder_lines(placeholder), "")

    def test_inline_placeholder_kept(self):
        """行内夹杂其他文字的一律保留（不能误杀正常聊天）"""
        for kept in (
            "今天[图片]里那只猫真好玩",
            "[图片] 刚出琴房",
            "我发了[照片]给你",
            "他说的是【图片】那个梗",
        ):
            with self.subTest(text=kept):
                self.assertEqual(drop_image_placeholder_lines(kept), kept)

    def test_sticker_marker_untouched(self):
        raw = "[sticker:猫猫]"
        self.assertEqual(drop_image_placeholder_lines(raw), raw)

    def test_only_placeholder_lines_dropped_in_multiline(self):
        raw = "临湖今天人少，窗边位不用抢，早午餐给你看下\n[图片]\n你上次不是问哪个窗口人少才肯动身嘛"
        kept = drop_image_placeholder_lines(raw)
        self.assertNotIn("[图片]", kept)
        self.assertIn("临湖今天人少", kept)
        self.assertIn("你上次不是问哪个窗口人少", kept)

    def test_all_lines_placeholder_yields_empty(self):
        self.assertEqual(drop_image_placeholder_lines("[图片]\n[照片]\n[image]"), "")

    def test_logs_info_with_content_and_source(self):
        """丢弃必须记 INFO 日志，且带被丢内容与来源"""
        with self.assertLogs("companion.replier", level="INFO") as captured:
            drop_image_placeholder_lines("早上好\n[图片]", source="proactive")
        joined = "\n".join(captured.output)
        self.assertIn("[图片]", joined, "日志要带被丢内容")
        self.assertIn("proactive", joined, "日志要带来源标注")

    def test_no_log_when_nothing_dropped(self):
        with self.assertNoLogs("companion.replier", level="INFO"):
            self.assertEqual(drop_image_placeholder_lines("今天[图片]里那只猫"), "今天[图片]里那只猫")


class TestTask1PlaceholderFilterPipeline(unittest.TestCase):
    """parse_reply 级：走完整条出口（普通回复与主动消息共用）"""

    def _replier(self, max_chunks=5):
        return Replier(ReplyConfig(max_chunks=max_chunks), _RecordingStickers())

    def test_production_transcript_e8_no_longer_leaks(self):
        """E8 原样：那条把 [图片] 发到 QQ 的主动消息，现在必须拦下"""
        raw = (
            "临湖今天人少，窗边位不用抢，早午餐给你看下\n"
            "[图片]\n"
            "你上次不是问哪个窗口人少才肯动身嘛，这会儿在家早餐吃了吗"
        )
        chunks, record = self._replier().parse_reply(raw, source="proactive")
        sent = "\n".join(c["content"] for c in chunks if c["type"] == "text")
        self.assertNotIn("[图片]", sent)
        self.assertNotIn("[图片]", record, "被滤掉的行本就不该落库")
        self.assertIn("临湖今天人少", sent)
        self.assertIn("在家早餐吃了吗", sent)

    def test_record_built_from_final_chunks_only(self):
        """record 由实发段反推：被滤内容不出现在 record 里"""
        chunks, record = self._replier().parse_reply("第一句\n[照片]\n第二句")
        self.assertNotIn("[照片]", record)
        self.assertEqual(record, "第一句\n第二句")

    def test_whole_reply_is_placeholder_produces_no_chunks(self):
        """整轮只剩占位符：一条都不该发（发出去就是 [图片]）"""
        chunks, record = self._replier().parse_reply("[图片]")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_placeholder_before_and_after_sticker(self):
        """滤网在 sticker 拆分之后生效：占位符夹在表情包两侧也要被丢"""
        raw = "[图片]\n[sticker:猫猫]\n[照片]"
        chunks, record = self._replier().parse_reply(raw)
        self.assertEqual([c["type"] for c in chunks], ["sticker"])
        self.assertEqual(record, "[表情:猫猫]")

    def test_sticker_still_survives_filter(self):
        """不能误伤正常表情包（E8 的修法不该牵连 FIXES11 任务5）"""
        chunks, record = self._replier().parse_reply("在吗\n[sticker:猫猫]\n吃了吗")
        self.assertEqual([c["type"] for c in chunks], ["text", "sticker", "text"])
        self.assertIn("[表情:猫猫]", record)

    def test_literal_newline_placeholder_dropped(self):
        """模型把换行写成字面量 \\n 时，\\n[图片]\\n 也要能还原后被丢"""
        chunks, record = self._replier().parse_reply("第一句\\n[图片]\\n第二句")
        self.assertNotIn("[图片]", record)
        self.assertIn("第一句", record)
        self.assertIn("第二句", record)

    def test_inline_placeholder_survives_pipeline(self):
        chunks, record = self._replier().parse_reply("今天[图片]里那只猫真好玩")
        self.assertEqual(len(chunks), 1)
        self.assertIn("[图片]", record)

    def test_proactive_source_marked_in_log(self):
        with self.assertLogs("companion.replier", level="INFO") as captured:
            self._replier().parse_reply("[图片]", source="proactive")
        self.assertIn("proactive", "\n".join(captured.output))

    def test_default_source_is_reply(self):
        """既有调用方（chat.py / turn_handler.py）不传 source，日志标 reply"""
        with self.assertLogs("companion.replier", level="INFO") as captured:
            self._replier().parse_reply("[图片]")
        joined = "\n".join(captured.output)
        self.assertIn("来源: reply", joined)

    def test_clean_reply_unchanged(self):
        chunks, record = self._replier().parse_reply("行 那你忙")
        self.assertEqual([c["type"] for c in chunks], ["text"])
        self.assertEqual(record, "行 那你忙")

    def test_direction_tag_still_stripped(self):
        """别回归 FIXES9：滤网不能 interfere 触发方向标签剥离"""
        chunks, record = self._replier().parse_reply("【起】刚出琴房")
        self.assertEqual(record, "刚出琴房")


# ==========================================
# 任务 2：视觉描述补社交语义（E9）
# ==========================================


class TestTask2VisionPerceptionPrompt(unittest.TestCase):
    """硬编码字符串已提取为 prompts.VISION_PERCEPTION_PROMPT"""

    def test_constant_lives_in_prompts_module(self):
        from companion import prompts

        self.assertTrue(
            hasattr(prompts, "VISION_PERCEPTION_PROMPT"),
            "VISION_PERCEPTION_PROMPT 必须是 prompts 模块级常量",
        )

    def test_turn_handler_uses_the_prompts_constant(self):
        """turn_handler 导入处引用的常量与 prompts.py 同源（不再各写一份）"""
        from companion import turn_handler

        self.assertIs(
            turn_handler.VISION_PERCEPTION_PROMPT,
            VISION_PERCEPTION_PROMPT,
            "turn_handler 必须直接引用 prompts 的同一个常量对象",
        )

    def test_prompt_asks_for_emotion_and_chat_meaning(self):
        """关键词：表情包 + 情绪或聊天含义（E9 的病灶就是这两处被滤掉）"""
        self.assertIn("表情包", VISION_PERCEPTION_PROMPT)
        self.assertIn("情绪或聊天含义", VISION_PERCEPTION_PROMPT)

    def test_prompt_keeps_plain_photo_branch(self):
        """普通照片仍走客观描述，别把两支都改成情绪分析"""
        self.assertIn("如果是普通照片，客观描述关键内容与场景即可", VISION_PERCEPTION_PROMPT)

    def test_prompt_gives_examples(self):
        for example in ("表示同意", "撒娇", "无语", "调侃"):
            self.assertIn(example, VISION_PERCEPTION_PROMPT)

    def test_old_hardcoded_string_gone(self):
        """旧文案不得残留（否则两处并存，行为不确定）"""
        from companion import turn_handler

        with open(turn_handler.__file__, encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn("客观描述画面即可", source)

    def test_word_limit_raised_to_60(self):
        """旧文案 50 字上限已改为 60 字（要给社交含义留字）"""
        self.assertIn("60字以内", VISION_PERCEPTION_PROMPT)
        self.assertNotIn("50字以内", VISION_PERCEPTION_PROMPT)


class TestTask2VisionPipeline(unittest.TestCase):
    """两段式视觉架构的承重墙不动：截断 120 字 / purpose=vision_perception"""

    def _handler(self, vision_desc: str):
        """全程 mock 的 TurnHandler：只有 vision 调用与 assembler 是靶子"""
        from companion.turn_handler import TurnHandler

        gateway = MagicMock()
        gateway.chat = AsyncMock(return_value=vision_desc)

        async def _stream(**kwargs):
            for piece in ("嗯", "，那先这样"):
                yield piece

        gateway.stream_chat = _stream

        assembler = MagicMock()
        assembler.assemble_messages = AsyncMock(return_value=([], "sys"))

        replier = MagicMock()
        replier.parse_reply = MagicMock(return_value=([{"type": "text", "content": "嗯"}], "嗯"))
        replier.send_reply_chunks = AsyncMock()

        memory = MagicMock()
        memory.save_turn_pair = AsyncMock()
        memory.reinforce_memories = AsyncMock()

        observer = MagicMock()
        observer.settle_turn = AsyncMock()

        proactive = MagicMock()
        proactive.reset_unanswered_count = AsyncMock()

        handler = TurnHandler(
            config=MagicMock(),
            gateway=gateway,
            assembler=assembler,
            replier=replier,
            memory=memory,
            observer=observer,
            proactive=proactive,
            send_chunk_fn=AsyncMock(),
        )
        return handler, gateway

    def _run_with_image(self, handler):
        """喂一张图跑完 handle_turn，返回注入主聊的 user_text"""
        import asyncio
        import os
        import tempfile

        async def _run():
            with tempfile.TemporaryDirectory() as tmp:
                img = os.path.join(tmp, "s.jpg")
                with open(img, "wb") as f:
                    f.write(b"\xff\xd8\xff")
                with mock.patch(
                    "companion.turn_handler.image_to_base64_data_url",
                    return_value=("data:image/jpeg;base64,AAA", None),
                ):
                    await handler.handle_turn("嗯嗯", img)
                    # 让 create_task 出去的观察者结算跑完，避免 loop 关闭时 pending 告警
                    await asyncio.sleep(0.01)
            return handler.assembler.assemble_messages.call_args[0][0]

        return asyncio.run(_run())

    def test_vision_call_sends_new_prompt_and_keeps_contract(self):
        handler, gateway = self._handler("白色卡通小动物，紫底，带腮红，表情乖巧")
        self._run_with_image(handler)

        kwargs = gateway.chat.call_args.kwargs
        self.assertEqual(kwargs["purpose"], "vision_perception", "purpose 契约不变")
        content = kwargs["messages"][0]["content"]
        self.assertEqual(content[0]["text"], VISION_PERCEPTION_PROMPT, "必须发新文案")
        self.assertEqual(content[1]["type"], "image_url", "承重墙：图片仍单独一段")

    def test_vision_description_injected_into_main_chat(self):
        """两段式不变：flash 描述注入主聊 user_text"""
        handler, _ = self._handler("表示同意和收尾，意思是这段对话可以结束了")
        user_text = self._run_with_image(handler)
        self.assertIn("发来一张照片：", user_text)
        self.assertIn("表示同意和收尾", user_text)

    def test_vision_description_still_truncated_to_120(self):
        """截断 120 字的逻辑不动"""
        handler, _ = self._handler("长" * 300)
        user_text = self._run_with_image(handler)
        self.assertIn("长" * 120, user_text)
        self.assertNotIn("长" * 121, user_text)

    def test_image_never_leaks_to_main_model(self):
        """承重墙原样：主 chat 不带图片，image_data_url 被置空"""
        handler, _ = self._handler("一只白猫")
        self._run_with_image(handler)
        # assemble_messages(user_text, image_data_url)：第二个位置参数必须已被置空
        image_data_url = handler.assembler.assemble_messages.call_args[0][1]
        self.assertIsNone(image_data_url, "两段式承重墙：主 chat 不直吃图片")
        self.assertEqual(
            handler.assembler.assemble_messages.return_value,
            mock.ANY,
        )

    def test_vision_failure_falls_back_to_placeholder(self):
        """视觉模型异常时的降级占位符逻辑不动"""
        handler, gateway = self._handler("x")
        gateway.chat = AsyncMock(side_effect=RuntimeError("boom"))
        user_text = self._run_with_image(handler)
        self.assertIn("发来一张图片，但没能看清", user_text)


if __name__ == "__main__":
    unittest.main()


