"""边界修复回归测试 (tests/test_boundary_rebuild.py)

验证两个独立边界修复：
1. 全角显式表情［face:doge］、［face：doge］与半角一样形成 face/combo；
   保持裸［doge］；普通全角括号不误改；normalize_face_markers、face budget、记录文本整条链一致。
2. third_person 只改观察者角色称谓，不得把咖啡伴侣/灵魂伴侣改成“咖啡她/灵魂她”；
   原有“机主最近想学做锅底”仍正确转换为“他最近想学做锅底”。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from companion.config import ReplyConfig
from companion.faces import face_id_by_name
from companion.memory import third_person
from companion.replier import (
    FACE_PATTERN,
    Replier,
    enforce_face_daily_budget,
    normalize_bare_face_tags,
    normalize_face_markers,
    record_text_from_chunks,
    strip_face_markers,
)


class _DummyStickers:
    def match_sticker(self, desc):
        return None


class TestFullWidthFaceTags(unittest.TestCase):
    def setUp(self):
        self.replier = Replier(ReplyConfig(), stickers=_DummyStickers())

    def test_face_pattern_regex(self):
        self.assertTrue(FACE_PATTERN.search("[face:doge]"))
        self.assertTrue(FACE_PATTERN.search("[face：doge]"))
        self.assertTrue(FACE_PATTERN.search("［face:doge］"))
        self.assertTrue(FACE_PATTERN.search("［face：doge］"))
        self.assertTrue(FACE_PATTERN.search("［FACE:doge］"))
        self.assertFalse(FACE_PATTERN.search("［普通括号］"))

    def test_full_width_face_parse_and_combo(self):
        # ［face:doge］和［face：doge］形成 combo
        chunks1, rec1 = self.replier.parse_reply("真棒［face:doge］")
        self.assertEqual(len(chunks1), 1)
        self.assertEqual(chunks1[0]["type"], "combo")
        self.assertEqual(chunks1[0]["parts"][0]["content"], "真棒")
        self.assertEqual(chunks1[0]["parts"][1]["tag"], "doge")
        self.assertIn("[face:doge]", rec1)

        chunks2, rec2 = self.replier.parse_reply("厉害［face：doge］")
        self.assertEqual(len(chunks2), 1)
        self.assertEqual(chunks2[0]["type"], "combo")
        self.assertEqual(chunks2[0]["parts"][0]["content"], "厉害")
        self.assertEqual(chunks2[0]["parts"][1]["tag"], "doge")
        self.assertIn("[face:doge]", rec2)

        # 纯表情
        chunks3, rec3 = self.replier.parse_reply("［face:doge］")
        self.assertEqual(len(chunks3), 1)
        self.assertEqual(chunks3[0]["type"], "face")
        self.assertEqual(chunks3[0]["tag"], "doge")
        self.assertEqual(rec3, "[face:doge]")

    def test_bare_doge_and_normal_brackets(self):
        # 裸［doge］改写并形成表情
        chunks, rec = self.replier.parse_reply("真好［doge］")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "combo")
        self.assertEqual(chunks[0]["parts"][1]["tag"], "doge")

        # 普通全角括号不得被误改
        normal_text = "今天的安排［重要说明］记得看"
        chunks_normal, rec_normal = self.replier.parse_reply(normal_text)
        self.assertEqual(len(chunks_normal), 1)
        self.assertEqual(chunks_normal[0]["type"], "text")
        self.assertEqual(chunks_normal[0]["content"], normal_text)
        self.assertEqual(rec_normal, normal_text)

    def test_normalize_face_markers_and_strip(self):
        # 非法表情剥除标记保留正文
        res = normalize_face_markers("你好［face:不存在的表情12345］呀")
        self.assertEqual(res, "你好呀")

        # 合法表情不动
        res_valid = normalize_face_markers("你好［face:doge］呀")
        self.assertEqual(res_valid, "你好［face:doge］呀")

        # strip_face_markers
        stripped = strip_face_markers("你好［face:doge］！[face:吃瓜]")
        self.assertEqual(stripped, "你好！")

    def test_full_width_budget_enforcement(self):
        t = "第一句［face:撇嘴］\n第二句［face：呲牙］\n第三句［face:doge］"
        out = enforce_face_daily_budget(t, 1)
        self.assertIn("［face:撇嘴］", out)
        self.assertNotIn("呲牙", out)
        self.assertNotIn("doge", out)
        self.assertIn("第二句", out)
        self.assertIn("第三句", out)

    def test_record_text_chain(self):
        chunks, rec = self.replier.parse_reply("好［face：doge］")
        rec_from_chunks = record_text_from_chunks(chunks)
        self.assertEqual(rec_from_chunks, "好[face:doge]")
        self.assertEqual(rec, "好[face:doge]")


class TestMalformedQuoteBoundary(unittest.TestCase):
    def test_bare_quote_marker_is_not_sent(self):
        replier = Replier(ReplyConfig(), stickers=_DummyStickers())
        chunks, rec = replier.parse_reply("[quote]发条语音来听听")
        self.assertEqual([c["content"] for c in chunks], ["发条语音来听听"])
        self.assertNotIn("quote", rec)

    def test_numbered_quote_is_unchanged(self):
        replier = Replier(ReplyConfig(), stickers=_DummyStickers())
        targets = [{"index": 2, "message_id": 123, "text": "原话"}]
        chunks, _ = replier.parse_reply("[quote:2]收到", quote_targets=targets)
        self.assertEqual(chunks[0]["_quote"]["message_id"], 123)
        self.assertEqual(chunks[0]["content"], "收到")


class TestThirdPersonBoundary(unittest.TestCase):
    def test_owner_replacement(self):
        text = "机主最近想学做锅底"
        self.assertEqual(third_person(text), "他最近想学做锅底")

    def test_companion_role_replacement(self):
        text = "伴侣今天心情很好，给伴侣买了花"
        self.assertEqual(third_person(text), "她今天心情很好，给她买了花")

    def test_compound_words_preserved(self):
        # 咖啡伴侣 / 灵魂伴侣 / 生活伴侣 / 终身伴侣
        self.assertEqual(third_person("买了一盒咖啡伴侣"), "买了一盒咖啡伴侣")
        self.assertEqual(third_person("希望找到灵魂伴侣"), "希望找到灵魂伴侣")
        self.assertEqual(third_person("他是她的生活伴侣"), "他是她的生活伴侣")
        self.assertEqual(third_person("寻找终身伴侣"), "寻找终身伴侣")
        self.assertEqual(third_person("机主喝咖啡喜欢加咖啡伴侣"), "他喝咖啡喜欢加咖啡伴侣")
        self.assertEqual(third_person("伴侣说咖啡伴侣用完了"), "她说咖啡伴侣用完了")

    def test_markdown_prefix_and_tell_companion(self):
        self.assertEqual(third_person("- 机主最近想学做锅底"), "- 他最近想学做锅底")
        self.assertEqual(third_person("1. 机主常去基础馆"), "1. 他常去基础馆")
        self.assertEqual(third_person("告诉伴侣今晚合练"), "告诉她今晚合练")

    def test_empty_or_none(self):
        self.assertEqual(third_person(""), "")
        self.assertEqual(third_person(None), "")


if __name__ == "__main__":
    unittest.main()
