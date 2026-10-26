"""新卡（写了 world 段）组装出来的提示词里不能冒出校园设定：拿祀的卡实测主聊、主动消息与生活主线

2026-10-22 扩大扫描面：除了主聊与主动消息，还把观察者、日记、引用/语音示范、生活主线、
见面纪要、核心记忆合并这些**写给模型看的常量提示词**整块扫一遍——这些是示范与默认值，
不该绑定任何一张卡的世界观。表情说明块按平台命名（face_word），不计入校园词扫描。
"""

import asyncio
import os
import re
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import NEUTRAL_WORLD, Persona, World
from companion.proactive import NEUTRAL_DATE_SENSE, ProactiveScheduler, weekday_date_sense
from companion.prompts import (
    CORE_CONSOLIDATE_PROMPT,
    DIARY_SYSTEM_PROMPT,
    DIARY_USER_PROMPT,
    FACE_TAG_READ_NOTICE,
    IMPRESSIONS_PROMPT,
    IMPRESSIONS_USER,
    LIFE_ARC_GENERATE_PROMPT,
    LIFE_ARC_RESOLUTION_PROMPT,
    MEETUP_RECAP_PROMPT,
    OBSERVER_FACT_CORE_RULES,
    OBSERVER_MEETUP_RULES,
    OBSERVER_SELF_FACTS_RULES,
    OBSERVER_SYSTEM_PROMPT,
    PROACTIVE_DECISION_PROMPT,
    PROACTIVE_GENERATE_PROMPT,
    QUOTE_USAGE_EXAMPLES,
    QUOTE_USAGE_RULES,
    REPLY_CHECK_PROMPT,
    VOICE_PROMPT_BLOCK,
    diary_system_prompt,
    holiday_prompt_note,
)
from companion.stickers import StickerManager

SI = os.path.join("characters", "si")
# 「科学院」不算校园残留：祀的设定里"宏山科学院"是世界观内的机构名。
CAMPUS = re.compile(
    "学校|校园|学期|宿舍|上课|同学|老师|期末|考试|作业|专业|大学|学生|社团|乐团|校历|开学|"
    "食堂|图书馆|室友|课程|论文|练琴|手机|QQ|实验|假期|放假|寒假|暑假|(?<!科)学院|讲座|自习|教室|签到"
)

# 观察者/日记提示词末尾拼的是表情读侧说明块（按平台命名，face_word 决定叫什么），
# 它不属于这两段自己的措辞，扫校园词时先摘掉。
_OBSERVER_OWN = OBSERVER_SYSTEM_PROMPT.replace(FACE_TAG_READ_NOTICE, "")
_DIARY_OWN = DIARY_SYSTEM_PROMPT.replace(FACE_TAG_READ_NOTICE, "")

PROMPT_CONSTANTS = {
    "observer": _OBSERVER_OWN + OBSERVER_SELF_FACTS_RULES + OBSERVER_FACT_CORE_RULES + OBSERVER_MEETUP_RULES,
    "diary": _DIARY_OWN + DIARY_USER_PROMPT,
    "proactive_decision": PROACTIVE_DECISION_PROMPT,
    "quote_examples": QUOTE_USAGE_RULES + QUOTE_USAGE_EXAMPLES,
    "voice_examples": VOICE_PROMPT_BLOCK,
    "life_arc": LIFE_ARC_GENERATE_PROMPT + LIFE_ARC_RESOLUTION_PROMPT,
    "meetup_recap": MEETUP_RECAP_PROMPT,
    "core_consolidate": CORE_CONSOLIDATE_PROMPT,
    "impressions": IMPRESSIONS_PROMPT + IMPRESSIONS_USER,
    "reply_check": REPLY_CHECK_PROMPT,
}


class TestNoCampusLeak(unittest.TestCase):
    def test_neutral_defaults_are_clean(self):
        for k, v in NEUTRAL_WORLD.items():
            self.assertIsNone(CAMPUS.search(v), k)

    def test_常量提示词不含校园词(self):
        """示范/默认值不绑世界观：常量里不许出现任何一张卡才有的场景词。"""
        for name, text in PROMPT_CONSTANTS.items():
            m = CAMPUS.search(text)
            with self.subTest(block=name):
                self.assertIsNone(
                    m,
                    None if m is None else f"{name} 泄漏校园词「{m.group()}」：{text[max(0, m.start() - 40):][:100]}",
                )

    @unittest.skipUnless(os.path.isdir(SI), "祀的卡只在私人仓库")
    def test_si_main_and_proactive_prompts(self):
        async def go():
            with tempfile.TemporaryDirectory() as d:
                db = Database(os.path.join(d, "t.db"))
                await db.init_tables()
                p = Persona.load(SI, os.path.join("profiles", "endministrator.json"))
                aff = AffectionEngine.from_persona(db, p)
                mood = MoodEngine(db)
                mem = MemoryManager(db, gateway=None, affection=aff, persona=p)
                st = StickerManager(os.path.join(p.base_dir, p.stickers_dir), db)
                asm = PromptAssembler(p, aff, mood, mem, st, db)
                msgs, _ = await asm.assemble_messages("在吗", None, [], True)
                await db.close()
                return p, "\n".join(m["content"] for m in msgs if isinstance(m["content"], str))

        p, text = asyncio.run(go())
        self.assertIsNone(CAMPUS.search(text), CAMPUS.search(text) and text[max(0, CAMPUS.search(text).start() - 40):][:100])
        gen = PROACTIVE_GENERATE_PROMPT.format(
            user_address="管理员", chat_medium=p.world.chat_medium, typing_habit=p.world.typing_habit,
            current_time="", topic_material="", recent_chat="", stickers_list="", face_block="",
        )
        self.assertNotIn("手机", gen)
        self.assertIsNone(CAMPUS.search(holiday_prompt_note(5, p.world)))


class TestCardDrivenPromptText(unittest.TestCase):
    """常量里按旧卡写死的口癖/标签，要能被卡的世界设定接管。"""

    def test_星期口癖只给跟现实工作周的卡(self):
        self.assertEqual(weekday_date_sense(0, World()), "周一，新的一周开始啦")
        self.assertEqual(weekday_date_sense(4, World()), "周五啦，快要周末了")
        self.assertEqual(weekday_date_sense(5, World()), "周六休息日")
        # 不跟现实节假日的卡（祀）世界里没有现实工作周
        not_following = World(follow_public_holidays=False)
        for weekday in range(7):
            self.assertEqual(weekday_date_sense(weekday, not_following), NEUTRAL_DATE_SENSE)
        self.assertEqual(weekday_date_sense(0, None), "周一，新的一周开始啦")

    def test_主动消息日期感不跟现实工作周的卡不用星期口癖(self):
        class _StubMemory:
            async def get_open_followups(self, **kw):
                return []

            async def get_live_desires(self, limit=1):
                return []

            async def get_active_diaries(self):
                return []

        class _StubPersona:
            def __init__(self, world):
                self.world = world

            def get_current_activity(self, *a, **kw):
                return ""

        class _Stub:
            def __init__(self, world):
                self.persona = _StubPersona(world)
                self.memory = _StubMemory()

            def get_holidays(self):
                return []

        class _MondayDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 10, 19, 15, 0)

        # 固定成周一：旧卡这里会给"周一，新的一周开始啦"
        with patch("companion.proactive.datetime", _MondayDatetime):
            material = asyncio.run(ProactiveScheduler._select_topic_material(_Stub(World())))
            self.assertIn("周一，新的一周开始啦", material)
            material = asyncio.run(
                ProactiveScheduler._select_topic_material(_Stub(World(follow_public_holidays=False)))
            )
        self.assertIn(NEUTRAL_DATE_SENSE, material)
        self.assertNotIn("周一", material)

    def test_日记阶段标签跟着卡走(self):
        class _Stage:
            def __init__(self, name):
                self.name = name

        bands = ["初识", "相识", "熟络", "同好", "旧识", "伙伴", "偏爱", "依恋", "深情", "相守"]
        text = diary_system_prompt([_Stage(n) for n in bands])
        self.assertIn("- 阶段 0~2 (初识、相识、熟络):", text)
        self.assertIn("- 阶段 3~5 (同好、旧识、伙伴):", text)
        self.assertIn("- 阶段 6+ (偏爱、依恋、深情、相守):", text)
        self.assertNotIn("微酸", text)
        # 不传卡：中性档位说法，不写死任何一张卡的阶段名
        self.assertIn("- 阶段 0~2 (关系还浅的几档):", DIARY_SYSTEM_PROMPT)
        self.assertIn("- 阶段 3~5 (熟悉起来的几档):", DIARY_SYSTEM_PROMPT)
        self.assertIn("- 阶段 6+ (亲近之后的几档):", DIARY_SYSTEM_PROMPT)
        # 只查阶段名特有的词（"依恋/深情"本身是正常形容词，不能一刀切）
        for name in ("同好", "知己", "微酸", "倾心", "相守"):
            self.assertNotIn(name, DIARY_SYSTEM_PROMPT)

    def test_日记阶段标签缺名字时退回中性说法(self):
        class _Stage:
            name = ""

        text = diary_system_prompt([_Stage() for _ in range(10)])
        self.assertIn("- 阶段 0~2 (关系还浅的几档):", text)


if __name__ == "__main__":
    unittest.main()
