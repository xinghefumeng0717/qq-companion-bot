"""角色卡 / 人物档案 / 代码 三分：占位符填充、world 设置、默认值与改造前逐字一致"""

import json
import os
import shutil
import tempfile
import unittest

from companion.persona import Persona, World
from companion.prompts import LIFE_ARC_GENERATE_PROMPT, REPLY_CHECK_PROMPT, SYSTEM_PROMPT_TEMPLATE

ROOT = os.path.join(os.path.dirname(__file__), "..")
LEAN = os.path.join(ROOT, "characters", "qingzi_lean")


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def card(self, extra):
        d = os.path.join(self.dir, "card")
        os.makedirs(d, exist_ok=True)
        base = {"name": "测试", "core_description": "{user_name}是她同学，她叫他{user_address}",
                "stages": [{"name": "相识", "tone": "客气", "instructions": ["偶称'{user_given_name}'"]}]
                + [{"name": f"阶段{i}", "tone": "", "instructions": []} for i in range(1, 10)]}
        base.update(extra)
        with open(os.path.join(d, "character.json"), "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False)
        return d

    def profile(self, **kw):
        p = os.path.join(self.dir, "me.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(kw, f, ensure_ascii=False)
        return p


class TestProfile(_Tmp):
    def test_占位符按档案填充_称呼以档案为准(self):
        p = Persona.load(self.card({"user_address": "你"}),
                         self.profile(name="张三", given_name="三", address="张同学", identity="管理员"))
        self.assertEqual(p.core_description, "张三是她同学，她叫他张同学")
        self.assertEqual(p.stages[0].instructions[0], "偶称'三'")
        self.assertEqual(p.user_address, "张同学")
        self.assertEqual(p.user.identity, "管理员")

    def test_没有占位符的旧卡不需要档案(self):
        d = os.path.join(self.dir, "old")
        os.makedirs(d)
        with open(os.path.join(d, "character.json"), "w", encoding="utf-8") as f:
            json.dump({"name": "旧卡", "user_address": "阿俊", "core_description": "老写法",
                       "stages": [{"name": f"阶段{i}", "tone": "", "instructions": []} for i in range(10)]},
                      f, ensure_ascii=False)
        p = Persona.load(d)
        self.assertEqual((p.user_address, p.core_description), ("阿俊", "老写法"))

    @unittest.skipUnless(os.path.isfile(LEAN), "需要私有精简角色卡与档案，公开克隆时明确跳过")
    def test_精简卡里不再出现他的真名_由默认档案填回(self):
        with open(os.path.join(LEAN, "character.json"), encoding="utf-8") as f:
            self.assertNotIn("俊儒", f.read())
        p = Persona.load(LEAN)
        self.assertIn("吴俊儒", p.core_description)
        self.assertEqual(p.user_address, "吴同学")


class TestWorld(_Tmp):
    def test_默认值就是改造前写死的原文(self):
        w = World()
        self.assertIn("你在用手机QQ和", SYSTEM_PROMPT_TEMPLATE.replace("{chat_medium}", w.chat_medium))
        arc = LIFE_ARC_GENERATE_PROMPT.replace("{life_identity}", w.life_identity) \
            .replace("{calendar_label}", w.calendar_label).replace("{calendar_kind}", w.calendar_kind)
        self.assertIn("她是一个真实的大学生", arc)
        self.assertIn("【学期节奏锚点】（校历硬事实", arc)
        chk = REPLY_CHECK_PROMPT.replace("{her_identity}", w.her_identity) \
            .replace("{his_identity}", "一个男生（同校朋友）")
        self.assertIn("写草稿的是一个女大学生，她在跟一个男生（同校朋友）聊天", chk)

    def test_卡里可以改世界设置_未知字段忽略(self):
        p = Persona.load(self.card({"world": {"her_identity": "岁的第十三代理人", "chat_medium": "终端",
                                              "follow_public_holidays": False, "瞎填": 1}}), self.profile(name="管理员"))
        self.assertEqual((p.world.her_identity, p.world.chat_medium), ("岁的第十三代理人", "终端"))
        self.assertFalse(p.world.follow_public_holidays)
        # 写了 world 段的新卡，没写到的项用中性说法，不会补成校园设定
        self.assertEqual(p.world.calendar_kind, "日程")
        self.assertEqual(p.world.chat_tail, "聊天")

    def test_没写world的旧卡_仍是原文(self):
        p = Persona.load(self.card({}), self.profile(name="管理员"))
        self.assertEqual((p.world.her_identity, p.world.chat_tail, p.world.calendar_kind),
                         ("一个女大学生", "手机聊天", "校历"))


if __name__ == "__main__":
    unittest.main()
