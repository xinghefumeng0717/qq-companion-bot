"""一张卡：档案内嵌、features 自声明、语音忙闲词、看板主题跟随卡。

优先级统一为三层：
  人物档案：config 显式 user_profile > 卡顶层 user > profiles/default.json
  能力开关：config 显式（含 false）> 卡 features > 代码默认
  看板主题：config 显式（含 journal）> 卡 theme/skin.json > journal
  语音词表：卡 world.voice_busy_words > 内置 BLOCK_KEYWORDS

旧卡（没有 user/features/voice_busy_words/skin.json）全链路行为不变，本文件同时钉住。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from companion.admin import resolve_theme  # noqa: E402
from companion.config import (  # noqa: E402
    AdminConfig,
    CharacterConfig,
    Config,
    ReplyConfig,
    TTSConfig,
)
from companion.persona import Persona  # noqa: E402
from companion.tts import BLOCK_KEYWORDS, TTSManager, activity_allows_voice  # noqa: E402

EXAMPLE_CARD = os.path.join(_REPO_ROOT, "characters", "example")


def _stages() -> list:
    return [{"name": f"阶段{i}", "tone": "", "instructions": []} for i in range(10)]


class _TmpCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="qqc_card_features_")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self._n = 0

    def _next(self, prefix: str) -> str:
        self._n += 1
        path = os.path.join(self.dir, f"{prefix}{self._n}")
        os.makedirs(path, exist_ok=True)
        return path

    def card(self, **extra) -> str:
        d = self._next("card")
        data = {"name": "测试", "user_address": "你", "stages": _stages()}
        data.update(extra)
        with open(os.path.join(d, "character.json"), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return d

    def profile(self, **data) -> str:
        p = os.path.join(self.dir, f"profile{self._n}.json")
        self._n += 1
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        return p

    def config_file(self, body: str) -> str:
        p = os.path.join(self.dir, f"config{self._n}.toml")
        self._n += 1
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        return p


# ==========================================
# 1. 人物档案内嵌（config 显式 > 卡 user > default.json）
# ==========================================


class TestUserProfilePriority(_TmpCase):
    def test_config显式档案赢过卡里的user段(self):
        card = self.card(
            core_description="{user_name}是她同学，她叫他{user_address}",
            user={"name": "卡里的人", "given_name": "卡里", "address": "卡里称呼",
                  "identity": "卡里的身份"},
        )
        profile = self.profile(name="档案里的人", given_name="档", address="档案称呼",
                               identity="档案身份")
        p = Persona.load(card, profile)
        self.assertEqual(p.core_description, "档案里的人是她同学，她叫他档案称呼")
        self.assertEqual(p.user_address, "档案称呼")
        self.assertEqual(p.user.identity, "档案身份")

    def test_config没写时用卡里的user段(self):
        card = self.card(
            core_description="{user_name}是她同学，她叫他{user_address}",
            user={"name": "管理员", "given_name": "管理员", "address": "管理员",
                  "identity": "管理员（她等了十年的那个人）"},
        )
        p = Persona.load(card)
        self.assertEqual(p.core_description, "管理员是她同学，她叫他管理员")
        self.assertEqual(p.user_address, "管理员")
        self.assertEqual((p.user.name, p.user.given_name), ("管理员", "管理员"))
        self.assertEqual(p.user.identity, "管理员（她等了十年的那个人）")

    def test卡里user段的address优先于卡里的user_address(self):
        card = self.card(user_address="你", user={"address": "呆货", "name": "管理员"})
        p = Persona.load(card)
        self.assertEqual(p.user_address, "呆货")

    @unittest.skipUnless(os.path.isfile("profiles/default.json"), "需要私有默认人物档案，公开克隆时明确跳过")
    def test都没有时回落到default_json(self):
        """旧卡：没 user 段、config 没写、卡里有占位符 → profiles/default.json（行为不变）。"""
        card = self.card(core_description="{user_name}是她同学，她叫他{user_address}")
        p = Persona.load(card)
        self.assertEqual(p.core_description, "吴俊儒是她同学，她叫他吴同学")
        self.assertEqual(p.user_address, "吴同学")

    def test旧卡没占位符也不需要档案(self):
        card = self.card(user_address="阿俊", core_description="老写法")
        p = Persona.load(card)
        self.assertEqual((p.user_address, p.core_description), ("阿俊", "老写法"))

    @unittest.skipUnless(os.path.isfile("profiles/default.json"), "需要私有默认人物档案，公开克隆时明确跳过")
    def test卡里user段不是对象时按没写处理(self):
        card = self.card(
            core_description="{user_name}是她同学",
            user="管理员",
        )
        p = Persona.load(card)
        self.assertEqual(p.core_description, "吴俊儒是她同学")


# ==========================================
# 2. features 三层优先
# ==========================================


class TestCardFeatures(_TmpCase):
    def _load(self, card_dir: str, character_body: str = "", reply_body: str = "") -> Config:
        cfg = (
            "[account]\nallowed_user_id = 1\n\n"
            f'[character]\npath = "{card_dir.replace(chr(92), "/")}"\n'
            f"{character_body}\n"
            "[reply]\n"
            f"{reply_body}\n"
        )
        return Config.load(self.config_file(cfg))

    def test无features段时全用代码默认(self):
        cfg = self._load(self.card())
        self.assertFalse(cfg.character.self_memory)
        self.assertFalse(cfg.character.fact_layers)
        self.assertFalse(cfg.character.impressions)
        self.assertFalse(cfg.character.meetup_recap)
        self.assertFalse(cfg.reply.trim_echo)
        self.assertEqual(cfg.reply.candidates, 1)
        self.assertFalse(cfg.reply.vision_direct)

    def testconfig没写时取卡里的features(self):
        card = self.card(features={
            "self_memory": True, "fact_layers": True, "impressions": True,
            "meetup_recap": True, "trim_echo": True, "candidates": 3,
            "vision_direct": True,
        })
        cfg = self._load(card)
        self.assertTrue(cfg.character.self_memory)
        self.assertTrue(cfg.character.fact_layers)
        self.assertTrue(cfg.character.impressions)
        self.assertTrue(cfg.character.meetup_recap)
        self.assertTrue(cfg.reply.trim_echo)
        self.assertEqual(cfg.reply.candidates, 3)
        self.assertTrue(cfg.reply.vision_direct)

    def testconfig显式false赢过卡里的true(self):
        card = self.card(features={"self_memory": True, "trim_echo": True, "candidates": 3})
        cfg = self._load(
            card,
            character_body="self_memory = false",
            reply_body="trim_echo = false\ncandidates = 1",
        )
        self.assertFalse(cfg.character.self_memory)
        self.assertFalse(cfg.reply.trim_echo)
        self.assertEqual(cfg.reply.candidates, 1)

    def testconfig显式值赢过卡里的false(self):
        card = self.card(features={"self_memory": False, "candidates": 2})
        cfg = self._load(card, character_body="self_memory = true")
        self.assertTrue(cfg.character.self_memory)
        self.assertEqual(cfg.reply.candidates, 2)

    def testcandidates按现有上限clamp(self):
        card = self.card(features={"candidates": 9})
        self.assertEqual(self._load(card).reply.candidates, 4)
        card2 = self.card(features={"candidates": 0})
        self.assertEqual(self._load(card2).reply.candidates, 1)

    def testfeatures里的畸形值逐项忽略(self):
        card = self.card(features={
            "self_memory": "true",      # 不是布尔
            "candidates": True,         # bool 不算整数
            "trim_echo": 1,             # 不是布尔
        })
        cfg = self._load(card)
        self.assertFalse(cfg.character.self_memory)
        self.assertEqual(cfg.reply.candidates, 1)
        self.assertFalse(cfg.reply.trim_echo)

    def test卡不存在时不炸(self):
        cfg = self._load(os.path.join(self.dir, "no_such_card"))
        self.assertFalse(cfg.character.self_memory)

    def testtiebreak默认warm且卡可自声明first(self):
        self.assertEqual(self._load(self.card()).reply.candidate_tiebreak, "warm")
        card = self.card(features={"candidate_tiebreak": "first"})
        self.assertEqual(self._load(card).reply.candidate_tiebreak, "first")

    def testconfig显式tiebreak赢过卡(self):
        card = self.card(features={"candidate_tiebreak": "first"})
        cfg = self._load(card, reply_body='candidate_tiebreak = "warm"')
        self.assertEqual(cfg.reply.candidate_tiebreak, "warm")

    def testtiebreak畸形值回退warm(self):
        card = self.card(features={"candidate_tiebreak": "tsundere"})
        self.assertEqual(self._load(card).reply.candidate_tiebreak, "warm")
        cfg = self._load(self.card(), reply_body='candidate_tiebreak = "tsundere"')
        self.assertEqual(cfg.reply.candidate_tiebreak, "warm")


# ==========================================
# 3. 语音忙闲词表：卡驱动 + 回落内置
# ==========================================


class TestVoiceBusyWords(_TmpCase):
    def test卡里词表生效并替换内置词表(self):
        card = self.card(world={"voice_busy_words": ["睡着", "作画", "对账"]})
        p = Persona.load(card)
        self.assertEqual(p.world.voice_busy_words, ("睡着", "作画", "对账"))
        # 卡里的词命中 → 拦
        ok, why = activity_allows_voice("在缘缘庄睡着，雷打不动", p.world.voice_busy_words)
        self.assertFalse(ok)
        self.assertIn("睡着", why)
        # 内置校园词不在卡里 → 不再误拦（旧词表被整表替换）
        ok, _ = activity_allows_voice("在教室上课", p.world.voice_busy_words)
        self.assertTrue(ok)

    def test没写时回落内置词表(self):
        p = Persona.load(self.card(world={"her_identity": "她"}))
        self.assertIsNone(p.world.voice_busy_words)
        ok, why = activity_allows_voice("在教室上课", p.world.voice_busy_words)
        self.assertFalse(ok)
        self.assertIn("上课", why)

    def test旧卡没有world段也是内置词表(self):
        p = Persona.load(self.card())
        self.assertIsNone(p.world.voice_busy_words)
        ok, why = activity_allows_voice("蒙民伟楼大排练厅全团合练", p.world.voice_busy_words)
        self.assertFalse(ok)

    def test空数组表示卡里没有忙闲场景词(self):
        card = self.card(world={"voice_busy_words": []})
        p = Persona.load(card)
        self.assertEqual(p.world.voice_busy_words, ())
        ok, _ = activity_allows_voice("在教室上课", p.world.voice_busy_words)
        self.assertTrue(ok)

    def test畸形成员逐条跳过_整体类型不认识按未声明(self):
        p = Persona.load(self.card(world={"voice_busy_words": ["睡着", "", 3]}))
        self.assertEqual(p.world.voice_busy_words, ("睡着",))
        p2 = Persona.load(self.card(world={"voice_busy_words": "睡着"}))
        self.assertIsNone(p2.world.voice_busy_words)

    def test例外白名单在卡词表下仍然优先(self):
        ok, why = activity_allows_voice(
            "在客栈对账，手机在手边较为空闲", ("对账",)
        )
        self.assertTrue(ok)
        self.assertIn("例外", why)

    def testTTSManager按卡词表过闸门(self):
        card = self.card(world={"voice_busy_words": ["睡着"]})
        p = Persona.load(card)
        tts = TTSManager(TTSConfig(enabled=True), voice_busy_words=p.world.voice_busy_words)
        ok, why = asyncio.run(tts.check_gate("在缘缘庄睡着"))
        self.assertFalse(ok)
        self.assertIn("作息", why)
        ok2, _ = asyncio.run(tts.check_gate("在教室上课"))  # 内置词表已被替换
        self.assertTrue(ok2)

    def testTTSManager没有卡词表时用内置词表(self):
        tts = TTSManager(TTSConfig(enabled=True))
        ok, why = asyncio.run(tts.check_gate("在教室上课"))
        self.assertFalse(ok)
        self.assertIn("上课", why)

    def test内置词表常量未被改动(self):
        for kw in ("上课", "合排", "合练", "排练", "图书馆", "考试", "讲座", "熄灯"):
            self.assertIn(kw, BLOCK_KEYWORDS)


# ==========================================
# 3.5 小黄脸频率：world.face_usage 卡驱动
# ==========================================


class TestFaceUsage(_TmpCase):
    def test默认normal逐字节不变(self):
        from companion.prompts import FACE_PROMPT_BLOCK, face_prompt_block
        p = Persona.load(self.card())
        self.assertEqual(face_prompt_block(p.world), FACE_PROMPT_BLOCK)

    def test_rare换低频纪律且不留示范(self):
        from companion.prompts import FACE_PROMPT_BLOCK, face_prompt_block
        p = Persona.load(self.card(world={"face_usage": "rare"}))
        block = face_prompt_block(p.world)
        self.assertIn("基本不用", block)
        self.assertIn("可用表情清单", block)   # 清单保留，真要用时名字才合法
        self.assertNotIn("示范", block)        # 示范会拉高采纳率，rare 版刻意不带
        self.assertNotEqual(block, FACE_PROMPT_BLOCK)


# ==========================================
# 4. 看板主题三层优先
# ==========================================


class TestAdminThemePriority(_TmpCase):
    def skin_card(self, skin=None) -> str:
        card = self.card()
        theme_dir = os.path.join(card, "theme")
        os.makedirs(theme_dir, exist_ok=True)
        with open(os.path.join(theme_dir, "skin.json"), "w", encoding="utf-8") as f:
            json.dump(skin if isinstance(skin, dict) else {}, f, ensure_ascii=False)
        return card

    def testconfig显式主题赢过皮肤(self):
        card = self.skin_card()
        for theme in ("journal", "plain", "endfield"):
            with self.subTest(theme=theme):
                self.assertEqual(resolve_theme(AdminConfig(theme=theme), card), theme)

    def test没写但有皮肤时跟随皮肤(self):
        card = self.skin_card()
        self.assertEqual(resolve_theme(AdminConfig(host="127.0.0.1", port=8080), card), "endfield")

    def test皮肤可以声明自己的主题(self):
        card = self.skin_card({"theme": "plain"})
        self.assertEqual(resolve_theme(AdminConfig(), card), "plain")

    def test皮肤声明不认识的主题时按endfield(self):
        card = self.skin_card({"theme": "赛博朋克"})
        self.assertEqual(resolve_theme(AdminConfig(), card), "endfield")

    def test没有皮肤回落到journal(self):
        self.assertEqual(resolve_theme(AdminConfig(), self.card()), "journal")
        self.assertEqual(resolve_theme(AdminConfig(), os.path.join(self.dir, "nope")), "journal")

    def test皮肤json坏掉时按没有皮肤处理(self):
        card = self.card()
        os.makedirs(os.path.join(card, "theme"))
        with open(os.path.join(card, "theme", "skin.json"), "w", encoding="utf-8") as f:
            f.write("{坏")
        self.assertEqual(resolve_theme(AdminConfig(), card), "journal")

    def testConfig_load记录显式键(self):
        card = self.skin_card()
        base = "[account]\nallowed_user_id = 1\n\n[character]\npath = \"%s\"\n" % (
            card.replace("\\", "/")
        )
        cfg_no_theme = Config.load(self.config_file(base + "\n[admin]\nport = 8081\n"))
        self.assertFalse(cfg_no_theme.admin.theme_explicit)
        self.assertEqual(resolve_theme(cfg_no_theme.admin, cfg_no_theme.character.path), "endfield")

        cfg_journal = Config.load(
            self.config_file(base + "\n[admin]\nport = 8081\ntheme = \"journal\"\n")
        )
        self.assertTrue(cfg_journal.admin.theme_explicit)
        self.assertEqual(
            resolve_theme(cfg_journal.admin, cfg_journal.character.path), "journal"
        )


if __name__ == "__main__":
    unittest.main()
