"""FIXES14 迭代测试集 (tests/test_fixes14.py)

按任务书分组：
1. 长假期判定与长假作息注入（任务1 / 生产证据 E10）
   - holiday_span 连续段长边界（含跨月、跨年、间隔段）
   - 长假不匹配任何 daily_routine，短假维持周六作息
   - is_holiday=True 旧调用方式行为逐格不变
   - assembler / proactive 按段长选附注
2. 观察者 facts/followups 字典形态容忍（任务2 / 生产证据 E11）
   - facts 四键提取、无有效键丢弃、既有 value 键回归
   - followups hours 形态换算、裸字典容器降级、混合形态、畸形项不炸整轮
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from companion.db import parse_dt
from companion.observer import FACT_DICT_KEYS, Observer
from companion.persona import (
    DEFAULT_LONG_HOLIDAY_ACTIVITY,
    LONG_HOLIDAY_MIN_SPAN,
    ChatStyle,
    Persona,
    RoutineItem,
    holiday_span,
)
from companion.prompts import (
    HOLIDAY_PROMPT_NOTE,
    LONG_HOLIDAY_PROMPT_NOTE,
    SHORT_HOLIDAY_PROMPT_NOTE,
    holiday_prompt_note,
)
from companion.config import ProactiveConfig, ReplyConfig
from helpers import (
    FIXTURE_LONG_HOLIDAY_ACTIVITY,
    card_path,
    close_db,
    make_db,
    make_engine_stack,
    make_fixture_card,
    make_mock_gateway,
)

# 校园场景词：来自夹具卡的周六作息（见下方 TestLongHolidayNoCampusRoutine）
CAMPUS_WORDS = ("银泉", "临湖", "琴房", "玉泉", "校车")


def _holiday_run(center_offset: int, length: int) -> list:
    """造一段以"今天 + center_offset"为起点的连续 length 天假期"""
    base = datetime.now() + timedelta(days=center_offset)
    return [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(length)]


def _persona(routine) -> Persona:
    return Persona(
        name="测试",
        user_address="你",
        core_description="",
        chat_style=ChatStyle(),
        initial_dims={},
        stages=[],
        daily_routine=routine,
        personal_memories=[],
        habits=[],
        stickers_dir="",
        base_dir="",
    )


class _RoutineSpy(list):
    """记录被遍历过几次的作息表：用于断言"长假根本不碰 daily_routine" """

    def __init__(self, *args):
        super().__init__(*args)
        self.iterations = 0

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


# ==========================================
# 任务 1-a：holiday_span 连续段长
# ==========================================


class TestHolidaySpan(unittest.TestCase):
    """holiday_span：只数配置里逐条填好的连续段，不做任何节假日推算"""

    def test_not_in_list_returns_zero(self):
        self.assertEqual(holiday_span("2026-10-01", ["2026-10-02", "2026-10-03"]), 0)
        self.assertEqual(holiday_span("2026-10-01", []), 0)
        self.assertEqual(holiday_span("2026-10-01", None), 0)
        self.assertEqual(holiday_span("", ["2026-10-01"]), 0)

    def test_single_day_is_one(self):
        self.assertEqual(holiday_span("2026-10-01", ["2026-10-01"]), 1)

    def test_three_day_span_middle(self):
        hol = ["2026-10-01", "2026-10-02", "2026-10-03"]
        for day in hol:
            self.assertEqual(holiday_span(day, hol), 3, f"{day} 应数出 3 天段")

    def test_eight_day_span(self):
        hol = _holiday_run(0, 8)
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(holiday_span(today, hol), 8)

    def test_boundary_min_span_is_four(self):
        """3 天段是短假，4 天段是长假——角色卡写明"三天以内的小长假她留校" """
        today = datetime.now().strftime("%Y-%m-%d")
        self.assertEqual(holiday_span(today, _holiday_run(0, 3)), 3)
        self.assertEqual(holiday_span(today, _holiday_run(0, 4)), 4)
        self.assertEqual(LONG_HOLIDAY_MIN_SPAN, 4)

    def test_cross_month_counts_as_connected(self):
        """09-30 与 10-01 日历上连续，必须算连成一段（字符串比较会跨月断链）"""
        hol = ["2026-09-30", "2026-10-01"]
        self.assertEqual(holiday_span("2026-09-30", hol), 2)
        self.assertEqual(holiday_span("2026-10-01", hol), 2)

    def test_cross_year_counts_as_connected(self):
        hol = ["2026-12-31", "2027-01-01"]
        self.assertEqual(holiday_span("2026-12-31", hol), 2)
        self.assertEqual(holiday_span("2027-01-01", hol), 2)

    def test_separate_segments_not_joined(self):
        """中间隔一天的两段不许被连起来（否则无假的 6 天长假）"""
        hol = ["2026-10-01", "2026-10-02", "2026-10-04", "2026-10-05"]
        self.assertEqual(holiday_span("2026-10-01", hol), 2)
        self.assertEqual(holiday_span("2026-10-04", hol), 2)

    def test_extra_dates_outside_range_not_counted(self):
        """段外日期不参与段长计算"""
        hol = ["2026-09-01", "2026-10-01", "2026-10-02", "2026-10-03", "2026-11-01"]
        self.assertEqual(holiday_span("2026-10-01", hol), 3)

    def test_malformed_date_falls_back_to_one(self):
        """配置里日期写坏了要退回短假（1 天），不能炸也不能当成长假"""
        self.assertEqual(holiday_span("10月1日", ["10月1日"]), 1)


# ==========================================
# 任务 1-b：长假作息 vs 短假作息
# ==========================================


class TestGetCurrentActivityHolidaySpan(unittest.TestCase):
    def setUp(self):
        self.persona = _persona(
            [
                RoutineItem(start=9, end=12, activity="专业课", days=[0, 1, 2, 3, 4]),
                RoutineItem(start=10, end=12, activity="周末在宿舍睡懒觉", days=[5, 6]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )

    def test_long_holiday_returns_fixed_activity(self):
        for span in (LONG_HOLIDAY_MIN_SPAN, 5, 8):
            self.assertEqual(
                self.persona.get_current_activity(10, 3, holiday_span=span),
                DEFAULT_LONG_HOLIDAY_ACTIVITY,
                f"段长 {span} 属长假，必须直接回固定文案",
            )

    def test_long_holiday_never_touches_routine(self):
        """E10 的要害：长假根本不该去遍历 daily_routine（周六作息里有校园场景）"""
        spy = _RoutineSpy(
            [
                RoutineItem(start=10, end=12, activity="周末外出：坐校车去玉泉看老建筑", days=[5, 6]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )
        persona = _persona(spy)
        self.assertEqual(
            persona.get_current_activity(10, 3, holiday_span=8), DEFAULT_LONG_HOLIDAY_ACTIVITY
        )
        self.assertEqual(spy.iterations, 0, "长假路径一次都不许遍历作息表")

    def test_short_holiday_keeps_saturday_routine(self):
        """1~3 天短假维持 FIXES11 行为：按周六作息匹配（留校，校园场景合理）"""
        for span in (1, 2, 3):
            self.assertEqual(
                self.persona.get_current_activity(10, 3, holiday_span=span),
                "周末在宿舍睡懒觉",
                f"段长 {span} 属短假，应命中周六作息",
            )

    def test_short_holiday_falls_back_when_no_saturday_routine(self):
        persona = _persona(
            [
                RoutineItem(start=9, end=12, activity="专业课", days=[0, 1, 2, 3, 4]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )
        for span in (1, 2, 3):
            self.assertEqual(persona.get_current_activity(10, 3, holiday_span=span), "专业课")

    def test_is_holiday_shim_equals_span_one(self):
        """兼容垫片：is_holiday=True 必须逐格等价 holiday_span=1，行为零变化"""
        for hour in range(24):
            for wd in range(7):
                self.assertEqual(
                    self.persona.get_current_activity(hour, wd, is_holiday=True),
                    self.persona.get_current_activity(hour, wd, holiday_span=1),
                    f"垫片必须一致：wd={wd} hour={hour}",
                )
                self.assertEqual(
                    self.persona.get_current_activity(hour, wd, is_holiday=False),
                    self.persona.get_current_activity(hour, wd, holiday_span=0),
                    f"非节假日路径必须一致：wd={wd} hour={hour}",
                )

    def test_non_holiday_path_untouched(self):
        """非节假日逐格回归：不能用假期改动影响日常作息"""
        for wd in range(7):
            for hour in (8, 10, 13, 23):
                self.assertEqual(
                    self.persona.get_current_activity(hour, wd),
                    self.persona.get_current_activity(hour, wd, is_holiday=False),
                )


class TestLongHolidayNoCampusRoutine(unittest.TestCase):
    """夹具卡回归：夹具的周六作息里带"坐校车去玉泉"（E10 同类场景）。

    不依赖任何真实角色卡——本类断言的是"作息文案里有校园词"这一结构，
    夹具卡自己提供该结构即可，公开 clone 无需私有卡也能跑到同一条路径。
    长假文案同样由夹具卡自带的 FIXTURE_LONG_HOLIDAY_ACTIVITY 提供，
    钉住"长假那句取自卡里、代码里没有卡内容"。
    """

    def setUp(self):
        self._card_dir = tempfile.mkdtemp(prefix="qqc_fixture_card_")
        self.addCleanup(shutil.rmtree, self._card_dir, True)
        make_fixture_card(self._card_dir, long_holiday_activity=FIXTURE_LONG_HOLIDAY_ACTIVITY)
        self.persona = Persona.load(self._card_dir)

    def test_short_holiday_would_leak_campus_words(self):
        """反向对照：短假路径确实会把校园作息词带出来。

        这条断言是长假期望断言的"有效性证明"——没有它，
        "长假提示词里没有校园词"可能只是因为那条路径压根没跑到。
        """
        activity = self.persona.get_current_activity(14, 3, holiday_span=1)
        self.assertTrue(
            any(w in activity for w in CAMPUS_WORDS),
            f"短假路径本就该命中周六校园作息，实际拿到: {activity}",
        )

    def test_long_holiday_has_no_campus_words(self):
        for hour in range(24):
            for span in (4, 8):
                activity = self.persona.get_current_activity(hour, 3, holiday_span=span)
                self.assertEqual(activity, FIXTURE_LONG_HOLIDAY_ACTIVITY)
                for word in CAMPUS_WORDS:
                    self.assertNotIn(
                        word, activity, f"长假 {span} 天 {hour} 点泄漏校园词 {word}: {activity}"
                    )


# ==========================================
# 任务 1-c：assembler / proactive 按段长选附注
# ==========================================


class TestHolidayPromptNote(unittest.TestCase):
    def test_alias_kept_for_compat(self):
        self.assertEqual(HOLIDAY_PROMPT_NOTE, SHORT_HOLIDAY_PROMPT_NOTE)
        self.assertEqual(
            SHORT_HOLIDAY_PROMPT_NOTE, "，今天是法定节假日（学校放假，不上课）"
        )
        self.assertEqual(LONG_HOLIDAY_PROMPT_NOTE, "，今天是法定节假日（放长假，她不在学校）")

    def test_selector_by_span(self):
        self.assertEqual(holiday_prompt_note(0), "")
        self.assertEqual(holiday_prompt_note(1), SHORT_HOLIDAY_PROMPT_NOTE)
        self.assertEqual(holiday_prompt_note(3), SHORT_HOLIDAY_PROMPT_NOTE)
        self.assertEqual(holiday_prompt_note(4), LONG_HOLIDAY_PROMPT_NOTE)
        self.assertEqual(holiday_prompt_note(8), LONG_HOLIDAY_PROMPT_NOTE)


class TestTask1AssemblerAndProactiveLongHoliday(unittest.IsolatedAsyncioTestCase):
    """assembler / 主动消息的长假附注与长假作息注入"""

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.gateway = make_mock_gateway()
        self.stack = make_engine_stack(
            self.db,
            card_path(),
            gateway=self.gateway,
            reply_config=ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0),
            proactive_config=ProactiveConfig(quiet_hours=[]),
            include_proactive=True,
        )
        self.assembler = self.stack.assembler
        self.proactive = self.stack.proactive
        self.proactive.assembler = self.assembler

    async def asyncTearDown(self):
        await close_db(self.db)

    def _long_holiday_text(self) -> str:
        """长假文案的唯一事实源：当前这张卡自己的 long_holiday_activity。

        本类用 card_path()（所有者本机是私有卡，公开 clone 回落 example 卡），
        断言一律对着 persona 取值——换卡、卡里改文案都不会牵动这些用例。
        """
        return self.stack.persona.long_holiday_activity

    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    def _long_holidays(self):
        return _holiday_run(0, 8)

    def _her_line(self, prompt: str) -> str:
        """取提示词里【她此刻】那一行：这是代码唯一能注入作息活动的段"""
        for line in prompt.splitlines():
            if line.startswith("【她此刻】"):
                return line
        self.fail("提示词里找不到【她此刻】行")

    def _fact_line(self, prompt: str) -> str:
        for line in prompt.splitlines():
            if line.startswith("【事实】"):
                return line
        self.fail("提示词里找不到【事实】行")

    async def test_system_prompt_long_holiday(self):
        self.assembler._holidays_provider = self._long_holidays
        prompt = await self.assembler.assemble_system_prompt("在吗")

        self.assertIn(LONG_HOLIDAY_PROMPT_NOTE, self._fact_line(prompt), "时间行必须注入长假附注")
        self.assertNotIn(SHORT_HOLIDAY_PROMPT_NOTE, prompt, "长假不许再注入短假附注")

        her_line = self._her_line(prompt)
        self.assertIn(self._long_holiday_text(), her_line, "【她此刻】必须是卡里的长假文案")
        for word in CAMPUS_WORDS:
            self.assertNotIn(
                word, her_line, f"【她此刻】泄漏校园作息词 {word}: {her_line}"
            )

    async def test_system_prompt_short_holiday_keeps_saturday(self):
        self.assembler._holidays_provider = lambda: [self._today()]
        prompt = await self.assembler.assemble_system_prompt("在吗")

        self.assertIn(SHORT_HOLIDAY_PROMPT_NOTE, self._fact_line(prompt))
        self.assertNotIn(LONG_HOLIDAY_PROMPT_NOTE, prompt)
        self.assertNotIn(self._long_holiday_text(), prompt, "短假不得套长假文案")

    async def test_system_prompt_without_holiday_unchanged(self):
        self.assembler._holidays_provider = lambda: []
        prompt = await self.assembler.assemble_system_prompt("在吗")
        self.assertNotIn("法定节假日", prompt)
        self.assertNotIn(self._long_holiday_text(), prompt)

    async def test_proactive_decision_prompt_long_holiday(self):
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
            ("user", "在干嘛", (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M")),
        )
        self.proactive._holidays_provider = self._long_holidays
        self.gateway.chat = AsyncMock(
            side_effect=[json.dumps({"choice": "B", "topic_hint": "", "reason": "x"})]
        )
        await self.proactive.trigger_cycle()

        decision_prompt = self.gateway.chat.call_args_list[0].kwargs["messages"][-1]["content"]
        self.assertIn(LONG_HOLIDAY_PROMPT_NOTE, decision_prompt, "决策层时间行必须注入长假附注")
        self.assertNotIn(SHORT_HOLIDAY_PROMPT_NOTE, decision_prompt)
        self.assertIn(self._long_holiday_text(), decision_prompt, "决策层看到的作息必须是卡里的长假文案")
        for word in CAMPUS_WORDS:
            self.assertNotIn(word, decision_prompt, f"决策层泄漏校园词 {word}")

    async def test_proactive_topic_material_long_holiday(self):
        self.proactive._holidays_provider = self._long_holidays
        material = await self.proactive._select_topic_material()
        self.assertIn(self._long_holiday_text(), material)
        for word in CAMPUS_WORDS:
            self.assertNotIn(word, material, f"话题切入点泄漏校园词 {word}: {material}")

    async def test_proactive_topic_material_short_holiday(self):
        self.proactive._holidays_provider = lambda: [self._today()]
        material = await self.proactive._select_topic_material()
        self.assertIn(SHORT_HOLIDAY_PROMPT_NOTE, material, "短假仍按留校口径附注")


# ==========================================
# 任务 2-a：observer facts 字典形态容忍
# ==========================================


def _observer_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


class _ObserverCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    def _base(self, **overrides) -> dict:
        payload = {
            "self_disclosure": 5.0,
            "responsiveness": 5.0,
            "warmth_score": 5.0,
            "resonance": 5.0,
            "moments": [],
            "mood_impact": {"v": 0.0, "a": 0.0, "trust": 0.0},
            "facts": [],
            "followups": [],
            "done_followups": [],
            "collect_sticker": False,
            "sticker_name": "",
        }
        payload.update(overrides)
        return payload

    async def _settle(self, **overrides):
        gw = make_mock_gateway(chat_return_value=_observer_json(self._base(**overrides)))
        self.gateway = gw
        self.stack = make_engine_stack(self.db, gateway=gw)
        observer = Observer(
            gw,
            self.stack.affection,
            self.stack.mood,
            self.stack.memory,
            self.stack.stickers,
            self.db,
        )
        return await observer.settle_turn("随便聊聊", "嗯嗯")

    async def _facts(self):
        return await self.stack.memory.get_all_facts()

    async def _followups(self):
        return await self.db.fetchall("SELECT topic, remind_after FROM followups ORDER BY id")


class TestTask2FactsDictTolerance(_ObserverCase):
    """E11：facts 字典形态必须被提取入库，而不是整批丢弃"""

    def test_fact_dict_keys_constant(self):
        for key in ("内容", "content", "fact", "text"):
            self.assertIn(key, FACT_DICT_KEYS)
        self.assertIn("value", FACT_DICT_KEYS, "FIXES10 老形态 value 不能被回退")

    async def test_each_dict_key_extracted(self):
        """任务书点名的四个键各提取成功（E11 生产日志里的 '内容' 正是第一个）"""
        for key in ("内容", "content", "fact", "text"):
            with self.subTest(key=key):
                await self.db.execute("DELETE FROM facts")  # 子测试共用一个库，先清干净
                await self._settle(facts=[{key: f"他不吃香菜（{key}）"}])
                self.assertEqual(await self._facts(), [f"他不吃香菜（{key}）"])

    async def test_legacy_value_key_still_works(self):
        await self._settle(facts=[{"value": "他周三有组会"}])
        self.assertIn("他周三有组会", await self._facts())

    async def test_key_priority_order(self):
        """多个键同时存在时按 FACT_DICT_KEYS 顺序取第一个非空字符串"""
        await self._settle(facts=[{"内容": "取内容", "content": "取content", "value": "取value"}])
        self.assertEqual(await self._facts(), ["取内容"])

    async def test_skip_empty_then_take_next_key(self):
        await self._settle(facts=[{"内容": "   ", "text": "他不吃香菜"}])
        self.assertEqual(await self._facts(), ["他不吃香菜"])

    async def test_no_valid_key_is_dropped(self):
        await self._settle(facts=[{"foo": "bar"}])
        self.assertEqual(await self._facts(), [], "无有效键的字典项必须丢弃且不入库")

    async def test_non_string_value_is_dropped(self):
        """只认字符串值：数字/嵌套字典不许 str() 硬转成垃圾事实"""
        await self._settle(facts=[{"内容": 12345}, {"content": {"a": 1}}])
        self.assertEqual(await self._facts(), [])

    async def test_mixed_forms_same_batch(self):
        """字符串 + 字典 + 畸形项同批：各自正确处理，坏项不牵连好项"""
        await self._settle(
            facts=[
                "他纯字符串形态的事实",
                {"内容": "他不吃香菜"},
                {"text": "他周三有组会"},
                {"foo": "bar"},
                None,
                12345,
            ]
        )
        facts = await self._facts()
        self.assertIn("他纯字符串形态的事实", facts)
        self.assertIn("他不吃香菜", facts)
        self.assertIn("他周三有组会", facts)
        self.assertEqual(len(facts), 3, f"只有 3 条有效事实该入库，实际: {facts}")

    async def test_bad_fact_does_not_break_settlement(self):
        """畸形 fact 不许炸整轮结算：followups 照常入库"""
        await self._settle(
            facts=[{"foo": "bar"}],
            followups=[{"topic": "问他科创比赛", "remind_after_hours": 24}],
        )
        self.assertEqual(await self._facts(), [])
        self.assertEqual(len(await self._followups()), 1, "整轮结算必须跑到 followups 入库")


class TestTask2FollowupsTolerance(_ObserverCase):
    """E11：followups 的 topic/remind_after_hours 形态入库，裸字典容器降级为单项"""

    async def test_hours_form_converts_remind_after(self):
        await self._settle(
            facts=[{"内容": "他不吃香菜"}],
            followups=[{"topic": "问他科创比赛", "remind_after_hours": 24}],
        )
        rows = await self._followups()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic"], "问他科创比赛")
        expected = datetime.now() + timedelta(hours=24)
        self.assertLess(
            abs((parse_dt(rows[0]["remind_after"]) - expected).total_seconds()),
            120,
            f"remind_after 应≈now+24h，实际 {rows[0]['remind_after']}",
        )
        self.assertIn("他不吃香菜", await self._facts())

    async def test_float_hours_converts(self):
        await self._settle(followups=[{"topic": "半小时后问他", "remind_after_hours": 0.5}])
        rows = await self._followups()
        expected = datetime.now() + timedelta(hours=0.5)
        self.assertLess(
            abs((parse_dt(rows[0]["remind_after"]) - expected).total_seconds()), 120
        )

    async def test_bare_dict_container_degrades_to_single_item(self):
        """模型把单条 followup 回成裸字典（E11 同款形态）时降级成单项，不得整批忽略"""
        await self._settle(followups={"topic": "问他科创比赛", "remind_after_hours": 24})
        rows = await self._followups()
        self.assertEqual(len(rows), 1, "裸字典容器必须被当成一条待跟进入库")
        self.assertEqual(rows[0]["topic"], "问他科创比赛")

    async def test_mixed_followup_forms(self):
        await self._settle(
            followups=[
                {"topic": "问他补考", "remind_after_hours": 3},
                {"topic": "问他实验课"},
                {"remind_after_hours": 6},
                {"topic": 42, "remind_after_hours": 6},
                "字符串形态（现状丢弃）",
            ]
        )
        rows = await self._followups()
        topics = [r["topic"] for r in rows]
        self.assertEqual(topics, ["问他补考", "问他实验课"], "只有 topic 合法的项该入库")
        expected = datetime.now() + timedelta(hours=3)
        self.assertLess(
            abs((parse_dt(rows[0]["remind_after"]) - expected).total_seconds()), 120
        )

    async def test_bad_followup_does_not_break_settlement(self):
        await self._settle(
            followups=[{"topic": "", "remind_after_hours": 4}],
            facts=[{"内容": "他不吃香菜"}],
        )
        self.assertEqual(await self._followups(), [])
        self.assertIn("他不吃香菜", await self._facts(), "坏 followup 不许牵连 facts 入库")

    async def test_non_list_container_still_ignored(self):
        """非 list 非 dict 的容器维持现状忽略（不扩大容错面）"""
        await self._settle(followups="问他科创比赛")
        self.assertEqual(await self._followups(), [])
