"""FIXES11 观察期首轮修复测试集 (tests/test_fixes11.py)

按任务书分组：
1. 主动消息注入近期对话历史（E1/E2）
2. 节假日感知（E3）
3. facts 更新/作废通路（E4）
4. suppressed_desires 阶段门控 + 题材去重（E5）
5. 表情包列表稳定化 + 整轮硬上限（E6）
（任务6 日记红线为提示词文案变更，无新增测试）
"""

import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

from companion.config import (
    AccountConfig,
    Config,
    LLMConfig,
    PricingConfig,
    ProactiveConfig,
    ReplyConfig,
)
from companion.db import TIME_FORMAT, now_str
from companion.memory import MemoryManager
from companion.persona import ChatStyle, Persona, RoutineItem, is_holiday_date
from companion.prompts import STAGE_GATING_RESTRICTED
from companion.proactive import (
    _char_jaccard,
    format_recent_chat,
)
from companion.replier import Replier, keep_first_sticker
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway

YESTERDAY = (datetime.now() - timedelta(days=1)).strftime(TIME_FORMAT)


class _RecordingStickers:
    """假表情包管理器：按描述词直接回一个假路径（不落盘）"""

    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫", "狗头"]


# ==========================================
# 任务 1：主动消息注入近期对话历史
# ==========================================


class TestTask1RecentChatFormatting(unittest.TestCase):
    """近期聊天记录的格式化规则（MM-DD HH:MM 他/你：内容 + 截断 + 空历史兜底）"""

    def test_format_lines_and_truncation(self):
        turns = [
            {
                "role": "user",
                "content": "今天国庆，我打算坐动车回家，票已经买好了",
                "created_at": "2026-10-01 21:14",
            },
            {
                "role": "assistant",
                "content": "那你路上小心，到了跟我说一声",
                "created_at": "2026-10-01 21:15",
            },
        ]
        block = format_recent_chat(turns, max_chars=60)
        lines = block.split("\n")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[0], "10-01 21:14 他：今天国庆，我打算坐动车回家，票已经买好了")
        self.assertEqual(lines[1], "10-01 21:15 你：那你路上小心，到了跟我说一声")

        long_turns = [{"role": "user", "content": "啊" * 100, "created_at": "2026-10-01 21:14"}]
        short_block = format_recent_chat(long_turns, max_chars=60)
        self.assertEqual(len(short_block.split("：", 1)[1].rstrip("…")), 60)

    def test_empty_history_fallback(self):
        self.assertEqual(format_recent_chat([]), "（今天是你们第一次聊天）")

    def test_broken_timestamp_does_not_raise(self):
        block = format_recent_chat([{"role": "user", "content": "在吗", "created_at": None}])
        self.assertIn("他：在吗", block)


class _ProactiveHarness(unittest.IsolatedAsyncioTestCase):
    """主动消息 trigger_cycle 的公共骨架：真实库 + mock gateway + 可断言提示词"""

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.sent = []
        self.gateway = make_mock_gateway()
        self.stack = make_engine_stack(
            self.db,
            gateway=self.gateway,
            reply_config=ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0),
            proactive_config=ProactiveConfig(quiet_hours=[]),
            include_proactive=True,
            send_msg_fn=self._collect,
        )
        self.proactive = self.stack.proactive
        self.proactive.assembler = self.stack.assembler
        # 历史放在昨天：绕开"距上次发言不足 60 分钟"闸门
        await self._add_turn(
            "今天国庆，我打算坐动车回家", "那你路上小心，到了跟我说一声"
        )

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _collect(self, chunk):
        self.sent.append(chunk)

    async def _add_turn(self, user_msg, bot_msg, when=None):
        ts = when or YESTERDAY
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
            ("user", user_msg, ts),
        )
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
            ("assistant", bot_msg, ts),
        )

    def _queue(self, *responses):
        self.gateway.chat = AsyncMock(side_effect=list(responses))

    def _user_prompts(self):
        """取出每次 gateway.chat 收到的 user prompt"""
        return [
            c.kwargs["messages"][-1]["content"] for c in self.gateway.chat.call_args_list
        ]


class TestTask1GenerationLayer(_ProactiveHarness):
    """生成层（A 分支）：必须带近期聊天记录，且无历史时给兜底"""

    async def test_generate_prompt_contains_recent_chat(self):
        self._queue(
            json.dumps({"choice": "A", "topic_hint": "问他到家没", "reason": "关心"}),
            "到家了吗",
        )
        await self.proactive.trigger_cycle()

        gen_prompt = self._user_prompts()[1]
        self.assertIn("【最近的聊天记录】", gen_prompt)
        self.assertIn("我打算坐动车回家", gen_prompt)
        self.assertIn("那你路上小心", gen_prompt)
        # 格式：MM-DD HH:MM 他/你：内容
        self.assertRegex(gen_prompt, r"\d{2}-\d{2} \d{2}:\d{2} 他：")
        self.assertRegex(gen_prompt, r"\d{2}-\d{2} \d{2}:\d{2} 你：")
        # 硬规则条款必须在（E1 的处方）
        self.assertIn("他已经做过的事", gen_prompt)
        self.assertIn("阶段 0~2 严禁约线下见面", gen_prompt)
        self.assertTrue(self.sent, "A 分支应真的把消息发出去")

    async def test_generate_prompt_first_chat_fallback(self):
        await self.db.execute("DELETE FROM turns")
        self._queue(
            json.dumps({"choice": "A", "topic_hint": "打个招呼", "reason": "想说话"}),
            "在干嘛",
        )
        await self.proactive.trigger_cycle()

        self.assertIn("（今天是你们第一次聊天）", self._user_prompts()[1])

    async def test_generate_prompt_carries_current_time(self):
        self._queue(
            json.dumps({"choice": "A", "topic_hint": "闲聊", "reason": "x"}),
            "嗯",
        )
        await self.proactive.trigger_cycle()
        self.assertIn("现在时间：", self._user_prompts()[1])


class TestTask1DecisionLayer(_ProactiveHarness):
    """决策层：三个新块（近期聊天/已知事实/未遂念头）齐备，空数据写"无" """

    async def test_decision_prompt_contains_three_blocks(self):
        await self.stack.memory.add_fact("他国庆坐动车回了家")
        await self.db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            ("想问他在家有没有人去接", now_str()),
        )
        self._queue(json.dumps({"choice": "B", "topic_hint": "", "reason": "让他歇着"}))
        await self.proactive.trigger_cycle()

        decision_prompt = self._user_prompts()[0]
        self.assertIn("【最近聊过的内容】", decision_prompt)
        self.assertIn("我打算坐动车回家", decision_prompt)
        self.assertIn("【已经确立的事实】", decision_prompt)
        self.assertIn("他国庆坐动车回了家", decision_prompt)
        self.assertIn("【她之前忍住没说出口的念头】", decision_prompt)
        self.assertIn("想问他在家有没有人去接", decision_prompt)

    async def test_decision_prompt_empty_data_writes_wu(self):
        await self.db.execute("DELETE FROM turns")
        self._queue(json.dumps({"choice": "B", "topic_hint": "", "reason": "x"}))
        await self.proactive.trigger_cycle()

        decision_prompt = self._user_prompts()[0]
        facts_block = decision_prompt.split("【已经确立的事实】")[1].split("【她之前忍住没说出口的念头】")[0]
        self.assertEqual(facts_block.strip().endswith("无"), True, f"空 facts 必须写“无”: {facts_block!r}")
        desires_block = decision_prompt.split("【她之前忍住没说出口的念头】")[1].split("【")[0]
        self.assertEqual(desires_block.strip().endswith("无"), True, f"空 desires 必须写“无”: {desires_block!r}")
        self.assertIn("（今天是你们第一次聊天）", decision_prompt)


# ==========================================
# 任务 2：节假日感知
# ==========================================


class TestTask2PersonaHoliday(unittest.TestCase):
    """persona 作息：节假日按周六节奏，不改 character.json"""

    @staticmethod
    def _persona(routine):
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

    def setUp(self):
        self.persona = self._persona(
            [
                RoutineItem(start=9, end=12, activity="专业课", days=[0, 1, 2, 3, 4]),
                RoutineItem(start=10, end=12, activity="周末在宿舍睡懒觉", days=[5, 6]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )

    def test_holiday_hits_saturday_routine(self):
        # 周四 10 点本来是专业课；法定节假日应命中周六作息
        self.assertEqual(
            self.persona.get_current_activity(10, 3), "专业课"
        )
        self.assertEqual(
            self.persona.get_current_activity(10, 3, is_holiday=True), "周末在宿舍睡懒觉"
        )

    def test_non_holiday_unchanged(self):
        for wd in range(7):
            for hour in (8, 10, 13, 23):
                self.assertEqual(
                    self.persona.get_current_activity(hour, wd),
                    self.persona.get_current_activity(hour, wd, is_holiday=False),
                    f"非节假日路径必须逐格一致：wd={wd} hour={hour}",
                )

    def test_holiday_falls_back_when_no_saturday_routine(self):
        persona = self._persona(
            [
                RoutineItem(start=9, end=12, activity="专业课", days=[0, 1, 2, 3, 4]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )
        # 查不到周六作息时回落到 weekday 现有逻辑
        self.assertEqual(persona.get_current_activity(10, 3, is_holiday=True), "专业课")

    def test_is_holiday_date_helper(self):
        self.assertTrue(is_holiday_date("2026-10-01", ["2026-10-01"]))
        self.assertFalse(is_holiday_date("2026-10-02", ["2026-10-01"]))
        self.assertFalse(is_holiday_date("2026-10-01", []))
        self.assertFalse(is_holiday_date("2026-10-01", None))


class TestTask2ConfigHolidays(unittest.TestCase):
    """Config.get_holidays 是行为层取节假日 的唯一入口，且只读"""

    def test_get_holidays_reads_pricing_holidays(self):
        config = Config(
            account=AccountConfig(allowed_user_id=1),
            llm=LLMConfig(pricing=PricingConfig(holidays=["2026-10-01", "2026-10-02"])),
        )
        self.assertEqual(config.get_holidays(), ["2026-10-01", "2026-10-02"])
        # 只读：改返回值不影响源配置
        got = config.get_holidays()
        got.append("2026-10-03")
        self.assertEqual(config.get_holidays(), ["2026-10-01", "2026-10-02"])

    def test_pricing_logic_untouched(self):
        """计费侧的节假日豁免行为不受本次改动影响（回归护栏）"""
        from companion.config import BEIJING_TZ

        pricing = PricingConfig(holidays=["2026-10-01"])
        during_holiday = datetime(2026, 10, 1, 10, 0, tzinfo=BEIJING_TZ)
        self.assertFalse(pricing.is_peak(during_holiday))


class TestTask2AssemblerAndProactive(unittest.IsolatedAsyncioTestCase):
    """assembler / 主动消息的节假日告知与作息覆盖"""

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.sent = []
        self.gateway = make_mock_gateway()
        self.stack = make_engine_stack(
            self.db,
            gateway=self.gateway,
            reply_config=ReplyConfig(chunk_delay_min=0.0, chunk_delay_max=0.0),
            proactive_config=ProactiveConfig(quiet_hours=[]),
            include_proactive=True,
            send_msg_fn=self._collect,
        )
        self.proactive = self.stack.proactive
        self.proactive.assembler = self.stack.assembler

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _collect(self, chunk):
        self.sent.append(chunk)

    def _today(self):
        return datetime.now().strftime("%Y-%m-%d")

    async def test_system_prompt_has_holiday_note(self):
        self.stack.assembler._holidays_provider = lambda: [self._today()]
        prompt = await self.stack.assembler.assemble_system_prompt("在吗")
        self.assertIn("今天是法定节假日（学校放假，不上课）", prompt)

    async def test_system_prompt_without_holiday_unchanged(self):
        self.stack.assembler._holidays_provider = lambda: []
        prompt = await self.stack.assembler.assemble_system_prompt("在吗")
        self.assertNotIn("法定节假日", prompt)

    async def test_provider_exception_falls_back_to_no_holiday(self):
        def boom():
            raise RuntimeError("配置读炸了")

        self.stack.assembler._holidays_provider = boom
        prompt = await self.stack.assembler.assemble_system_prompt("在吗")
        self.assertNotIn("法定节假日", prompt)

    async def test_proactive_decision_carries_holiday_note(self):
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
            ("user", "在干嘛", YESTERDAY),
        )
        self.proactive._holidays_provider = lambda: [self._today()]
        self.gateway.chat = AsyncMock(
            side_effect=[json.dumps({"choice": "B", "topic_hint": "", "reason": "x"})]
        )
        await self.proactive.trigger_cycle()
        decision_prompt = self.gateway.chat.call_args_list[0].kwargs["messages"][-1]["content"]
        self.assertIn("今天是法定节假日（学校放假，不上课）", decision_prompt)

    async def test_proactive_activity_uses_holiday_routine(self):
        self.proactive._holidays_provider = lambda: [self._today()]
        material = await self.proactive._select_topic_material()
        self.assertIn("法定节假日", material)


# ==========================================
# 任务 3：facts 更新/作废通路
# ==========================================


class TestTask3SupersedeFact(unittest.IsolatedAsyncioTestCase):
    """supersede_fact：0.4 阈值命中删旧插新，未命中只插新"""

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.memory = MemoryManager(self.db)

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_hit_replaces_old_fact(self):
        await self.memory.add_fact("国庆期间打算坐动车回家")
        hit = await self.memory.supersede_fact(
            "打算坐动车回家", "国庆已经坐动车到家了"
        )
        self.assertTrue(hit, "表述漂移但语义同一条时必须命中")
        facts = await self.memory.get_all_facts()
        self.assertEqual(facts, ["国庆已经坐动车到家了"], "旧行必须消失且只留新行")

    async def test_miss_only_inserts_new(self):
        await self.memory.add_fact("他喜欢吃香菜")
        hit = await self.memory.supersede_fact("国庆坐了高铁去上海", "国庆坐了高铁去上海")
        self.assertFalse(hit, "完全不同的事实不应命中旧行")
        facts = await self.memory.get_all_facts()
        self.assertIn("国庆坐了高铁去上海", facts)
        self.assertIn("他喜欢吃香菜", facts, "无关旧事实不得被动到")

    async def test_empty_new_content_is_noop(self):
        await self.memory.add_fact("他喜欢吃香菜")
        self.assertFalse(await self.memory.supersede_fact("他喜欢吃香菜", "   "))
        self.assertEqual(await self.memory.get_all_facts(), ["他喜欢吃香菜"])

    async def test_new_fact_deduped_by_add_fact(self):
        await self.memory.add_fact("他国庆坐动车回了家")
        # new 与既有事实近义时走 add_fact 的 0.6 去重，不应出现两条
        await self.memory.supersede_fact("完全不相关的旧念头", "他国庆已经坐动车回家")
        facts = await self.memory.get_all_facts()
        self.assertEqual(len(facts), 1, f"近义新事实应被 add_fact 去重: {facts}")


class TestTask3ObserverUpdatedFacts(unittest.IsolatedAsyncioTestCase):
    """观察者 updated_facts：可选字段、逐项容错、绝不影响结算"""

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.gateway = make_mock_gateway()
        self.stack = make_engine_stack(self.db, gateway=self.gateway)
        from companion.observer import Observer

        self.observer = Observer(
            self.gateway,
            self.stack.affection,
            self.stack.mood,
            self.stack.memory,
            self.stack.stickers,
            self.db,
        )

    async def asyncTearDown(self):
        await close_db(self.db)

    def _observer_json(self, **extra):
        base = {
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
            "user_state": "平静",
        }
        base.update(extra)
        return json.dumps(base, ensure_ascii=False)

    async def test_without_updated_facts_field(self):
        """旧版输出（没有该字段）行为与现状一致"""
        await self.stack.memory.add_fact("国庆期间打算坐动车回家")
        self.gateway.chat = AsyncMock(return_value=self._observer_json())
        data = await self.observer.settle_turn("我到家了", "好那你歇着")
        self.assertNotIn("updated_facts", data)
        self.assertEqual(await self.stack.memory.get_all_facts(), ["国庆期间打算坐动车回家"])

    async def test_with_updated_facts(self):
        await self.stack.memory.add_fact("国庆期间打算坐动车回家")
        self.gateway.chat = AsyncMock(
            return_value=self._observer_json(
                updated_facts=[
                    {"old": "国庆期间打算坐动车回家", "new": "国庆已经坐动车到家了"}
                ]
            )
        )
        await self.observer.settle_turn("我刚到家了", "路上顺利吗")
        self.assertEqual(await self.stack.memory.get_all_facts(), ["国庆已经坐动车到家了"])

    async def test_known_facts_injected_into_observer_input(self):
        await self.stack.memory.add_fact("他国庆坐动车回了家")
        self.gateway.chat = AsyncMock(return_value=self._observer_json())
        await self.observer.settle_turn("在吗", "在")
        prompt = self.gateway.chat.call_args_list[0].kwargs["messages"][-1]["content"]
        self.assertIn("【当前已确立的事实】", prompt)
        self.assertIn("他国庆坐动车回了家", prompt)

    async def test_malformed_updated_facts_does_not_break_settlement(self):
        await self.stack.memory.add_fact("他喜欢吃香菜")
        for bad in (
            "不是列表",
            [123],
            [{"old": "只有旧"}, {"new": "只有新"}, {}],
        ):
            self.gateway.chat = AsyncMock(return_value=self._observer_json(updated_facts=bad))
            data = await self.observer.settle_turn("在吗", "在")
            self.assertEqual(data["self_disclosure"], 5.0, f"结算必须照常完成: {bad!r}")
        # 畸形项没有把既有事实写坏
        self.assertEqual(await self.stack.memory.get_all_facts(), ["他喜欢吃香菜"])

    async def test_supersede_exception_does_not_break_settlement(self):
        await self.stack.memory.add_fact("他喜欢吃香菜")
        self.gateway.chat = AsyncMock(
            return_value=self._observer_json(
                updated_facts=[{"old": "他喜欢吃香菜", "new": "他现在讨厌香菜"}]
            )
        )
        with patch.object(
            self.stack.memory, "supersede_fact", AsyncMock(side_effect=RuntimeError("炸了"))
        ):
            data = await self.observer.settle_turn("我不吃香菜了", "这么突然")
        self.assertEqual(data["warmth_score"], 5.0, "supersede 抛错也不得中断结算")


# ==========================================
# 任务 4：suppressed_desires 阶段门控 + 题材去重
# ==========================================


class TestTask4StageGating(unittest.IsolatedAsyncioTestCase):
    """决策 prompt 的 {stage_gating}：阶段 0~2 有克制条款，3+ 空串"""

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.gateway = make_mock_gateway()
        self.stack = make_engine_stack(
            self.db,
            gateway=self.gateway,
            proactive_config=ProactiveConfig(quiet_hours=[]),
            include_proactive=True,
        )
        self.proactive = self.stack.proactive
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
            ("user", "在干嘛", YESTERDAY),
        )

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _decision_prompt_at_stage(self, stage):
        with patch.object(
            self.stack.affection,
            "get_state",
            AsyncMock(return_value={"stage": stage, "composite": 30.0 + stage}),
        ):
            self.gateway.chat = AsyncMock(
                side_effect=[json.dumps({"choice": "B", "topic_hint": "", "reason": "x"})]
            )
            await self.proactive.trigger_cycle()
        return self.gateway.chat.call_args_list[0].kwargs["messages"][-1]["content"]

    async def test_stage_1_has_restriction(self):
        prompt = await self._decision_prompt_at_stage(1)
        self.assertIn(STAGE_GATING_RESTRICTED.splitlines()[0], prompt)
        self.assertIn("严禁\"想他/喜欢他/心疼/心动\"类依恋词", prompt)

    async def test_stage_4_has_no_restriction(self):
        prompt = await self._decision_prompt_at_stage(4)
        self.assertNotIn("念头同样受限", prompt)

    def test_build_stage_gating_boundaries(self):
        for stage in (0, 1, 2):
            self.assertEqual(self.proactive._build_stage_gating(stage), STAGE_GATING_RESTRICTED)
        for stage in (3, 5, 9):
            self.assertEqual(self.proactive._build_stage_gating(stage), "")
        self.assertEqual(self.proactive._build_stage_gating("坏值"), STAGE_GATING_RESTRICTED)


class TestTask4DesireDedupe(_ProactiveHarness):
    """C 分支同题材不落库（念头仍算发生过），不同题材正常落库"""

    async def _run_c_branch(self, topic_hint):
        self._queue(json.dumps({"choice": "C", "topic_hint": topic_hint, "reason": "忍住"}))
        await self.proactive.trigger_cycle()
        rows = await self.db.fetchall("SELECT content FROM suppressed_desires ORDER BY id ASC")
        return [r["content"] for r in rows]

    async def test_same_topic_not_inserted(self):
        await self.db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            ("想跟他一起咕咕嘎嘎叫", now_str()),
        )
        contents = await self._run_c_branch("想喊他一起咕咕嘎嘎")
        self.assertEqual(contents, ["想跟他一起咕咕嘎嘎叫"], "同题材念头不得连刷")

    async def test_recent_proactive_turn_blocks_same_topic(self):
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES ('assistant', ?, 1, 0, ?)",
            ("咕咕嘎嘎", now_str()),
        )
        contents = await self._run_c_branch("咕咕嘎嘎咕咕嘎嘎")
        self.assertEqual(contents, [], "已经说出口过的同题材念头不得再落库")

    async def test_different_topic_inserted(self):
        contents = await self._run_c_branch("想问他有没有在练琴")
        self.assertEqual(contents, ["想问他有没有在练琴"])

    def test_char_jaccard_basics(self):
        self.assertEqual(_char_jaccard("咕咕嘎嘎", "咕咕嘎嘎"), 1.0)
        self.assertEqual(_char_jaccard("", ""), 0.0)
        self.assertLess(_char_jaccard("abc", "xyz"), 0.4)


# ==========================================
# 任务 5：表情包列表稳定化 + 整轮硬上限
# ==========================================


class TestTask5StickerListStability(unittest.TestCase):
    """可用列表确定性：同一份 index.json 每次给出完全相同的列表（E6）"""

    def _manager(self, key_count):
        from companion.stickers import StickerManager

        mgr = StickerManager.__new__(StickerManager)
        mgr._index = {f"表情{i:02d}": {"file": f"{i}.png", "desc": f"表情{i:02d}"} for i in range(key_count)}
        return mgr

    def test_42_keys_identical_across_calls(self):
        mgr = self._manager(42)
        first = mgr.get_prompt_sticker_list()
        second = mgr.get_prompt_sticker_list()
        self.assertEqual(len(first), 42, "42 键应全量返回")
        self.assertEqual(first, second, "两次调用必须逐项相同（E6：不能这轮有下轮没）")
        self.assertEqual(first, sorted(first), "按键名排序，结果确定")

    def test_over_cap_truncates_deterministically(self):
        mgr = self._manager(80)
        first = mgr.get_prompt_sticker_list()
        self.assertEqual(len(first), 60, "超上限截到 60")
        self.assertEqual(first, mgr.get_prompt_sticker_list())
        self.assertEqual(first, sorted(mgr._index.keys())[:60])

    def test_empty_index(self):
        mgr = self._manager(0)
        self.assertEqual(mgr.get_prompt_sticker_list(), [])


class TestTask5StickerHardCap(unittest.TestCase):
    """整轮最多一个表情包：无论模型输出几个 [sticker:...]"""

    def _replier(self, max_chunks=5):
        from companion.config import ReplyConfig as RC

        return Replier(RC(max_chunks=max_chunks), _RecordingStickers())

    def test_three_stickers_collapse_to_first(self):
        raw = "[sticker:猫猫]\n第一句\n[sticker:狗头]\n第二句\n[sticker:兔子]"
        chunks, record = self._replier().parse_reply(raw)
        stickers = [c for c in chunks if c["type"] == "sticker"]
        self.assertEqual(len(stickers), 1, "三个表情包段必须压成一个")
        self.assertEqual(stickers[0]["desc"], "猫猫", "保留最靠前的那个")
        self.assertNotIn("狗头", record)
        self.assertNotIn("兔子", record)
        self.assertIn("第一句", record)
        self.assertIn("第二句", record)

    def test_single_sticker_untouched(self):
        chunks, record = self._replier().parse_reply("在吗\n[sticker:猫猫]")
        self.assertEqual(len([c for c in chunks if c["type"] == "sticker"]), 1)
        self.assertIn("[表情:猫猫]", record)

    def test_no_sticker_untouched(self):
        chunks, _ = self._replier().parse_reply("在吗\n吃了吗")
        self.assertEqual([c["type"] for c in chunks], ["text", "text"])

    def test_keep_first_sticker_unit(self):
        chunks = [
            {"type": "text", "content": "a"},
            {"type": "sticker", "file": "1", "desc": "猫猫"},
            {"type": "sticker", "file": "2", "desc": "狗头"},
            {"type": "sticker", "file": "3", "desc": "兔"},
        ]
        kept = keep_first_sticker(chunks)
        self.assertEqual([c["type"] for c in kept], ["text", "sticker"])
        self.assertEqual(kept[1]["desc"], "猫猫")

    def test_fit_chunks_semantics_preserved(self):
        """fit_chunks 的"优先保表情包"是有意设计，本次不动它"""
        from companion.replier import fit_chunks

        chunks = [
            {"type": "sticker", "file": "1", "desc": "猫猫"},
            {"type": "text", "content": "a"},
            {"type": "text", "content": "b"},
            {"type": "text", "content": "c"},
        ]
        kept = fit_chunks(list(chunks), 2)
        self.assertEqual([c["type"] for c in kept], ["sticker", "text"])


if __name__ == "__main__":
    unittest.main()
