"""线下见面：约定识别、改期/取消、到点补纪要存成事实"""

import asyncio
import json
import unittest
from datetime import datetime, timedelta

from companion.config import CharacterConfig
from companion.meetups import (
    MEETUP_CANCELLED,
    MEETUP_DONE,
    MEETUP_MIN_INTERVAL_DAYS,
    MEETUP_PENDING,
    MEETUP_PROCESSING,
    MEETUP_SIMILARITY,
    MeetupManager,
    _similarity,
)
from companion.memory import MemoryManager
from companion.observer import MEETUP_HISTORY_MAX_CHARS, Observer, _meetup_history_block
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway


class _H(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.memory = MemoryManager(self.db)
        self.gw = make_mock_gateway(chat_return_value="和他在武陵城吃了青柠苦瓜锅底，他被酸得直皱眉")
        self.m = MeetupManager(self.db, self.gw, None, self.memory)

    async def asyncTearDown(self):
        await close_db(self.db)


class TestStore(_H):
    async def test_同一天同地点视为同一件事_改期更新(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "试新锅底")
        await self.m.add("2026-10-22 19:00", "武陵城", "吃火锅")
        rows = await self.m.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["when_at"], "2026-10-22 19:00")

    async def test_不同天不同地点不合并(self):
        # 冷却（MEETUP_MIN_INTERVAL_DAYS）挡住同一天的第二条约会，所以两条错开 4 天摆
        first = await self.m.add("2026-10-22 18:00", "南门火锅店", "吃火锅")
        second = await self.m.add("2026-10-26 19:00", "南门咖啡店", "喝咖啡")
        self.assertNotEqual(first, second)
        self.assertEqual(len(await self.m.pending()), 2)

    async def test_地点为空按事项相似度合并(self):
        first = await self.m.add("2026-10-22 18:00", "", "去江边散步")
        second = await self.m.add("2026-10-22 19:00", "", "去江边散步")
        self.assertEqual(first, second)
        third = await self.m.add("2026-10-26 20:00", "", "去书店买书")
        self.assertNotEqual(second, third)
        self.assertEqual(len(await self.m.pending()), 2)

    async def test_跨天改期更新旧行(self):
        first = await self.m.add("2026-10-22 18:00", "武陵城火锅店", "试新锅底")
        second = await self.m.add("2026-10-25 18:00", "武陵城火锅店", "试新锅底")
        self.assertEqual(first, second)
        rows = await self.m.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["when_at"], "2026-10-25 18:00")

    async def test_时间解析不了不记(self):
        self.assertIsNone(await self.m.add("明天晚上", "缘缘庄", "喝茶"))
        self.assertEqual(await self.m.pending(), [])

    async def test_取消按要点匹配(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "试新锅底")
        self.assertTrue(await self.m.cancel("周三武陵城火锅那次不去了"))
        row = await self.db.fetchone("SELECT status FROM meetups")
        self.assertEqual(row["status"], MEETUP_CANCELLED)

    async def test_唯一未完成时不去也能取消(self):
        await self.m.add("2026-10-22 18:00", "", "见面")
        self.assertTrue(await self.m.cancel("不去了"))
        row = await self.db.fetchone("SELECT status FROM meetups")
        self.assertEqual(row["status"], MEETUP_CANCELLED)

    async def test_多条时按相似度选唯一最高(self):
        await self.m.add("2026-10-22 18:00", "南门火锅店", "吃火锅")
        await self.m.add("2026-10-26 18:00", "南门咖啡店", "喝咖啡")
        self.assertTrue(await self.m.cancel("火锅店那个不去了"))
        rows = await self.m.pending()
        self.assertEqual([r["place"] for r in rows], ["南门咖啡店"])

    async def test_多条时并列最高放弃(self):
        await self.m.add("2026-10-22 18:00", "南门火锅店", "吃火锅")
        await self.m.add("2026-10-26 18:00", "南门咖啡店", "喝咖啡")
        self.assertFalse(await self.m.cancel("不去了"))
        self.assertEqual(len(await self.m.pending()), 2)


class TestRecap(_H):
    async def test_没到点不补_到点补成一条事实(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "试新锅底")
        self.assertEqual(await self.m.process_due(datetime(2026, 10, 22, 19, 0)), 0)
        self.gw.chat.assert_not_called()
        self.assertEqual(await self.m.process_due(datetime(2026, 10, 22, 22, 0)), 1)
        facts = await self.memory.get_all_facts()
        self.assertEqual(facts, ["10-22 见面：和他在武陵城吃了青柠苦瓜锅底，他被酸得直皱眉"])
        row = await self.db.fetchone("SELECT status FROM meetups")
        self.assertEqual(row["status"], MEETUP_DONE)

    async def test_模型失败退回待办_下次重试(self):
        self.gw.chat.side_effect = RuntimeError("超时")
        await self.m.add("2026-10-22 18:00", "缘缘庄", "喝茶")
        self.assertEqual(await self.m.process_due(datetime(2026, 10, 22, 22, 0)), 0)
        self.assertEqual(len(await self.m.pending()), 1)
        row = await self.db.fetchone("SELECT status FROM meetups")
        self.assertEqual(row["status"], MEETUP_PENDING)

    async def test_事实入库失败不置完成(self):
        await self.m.add("2026-10-22 18:00", "缘缘庄", "喝茶")

        async def boom(*args, **kwargs):
            raise RuntimeError("database is locked")

        self.memory.add_fact = boom
        self.assertEqual(await self.m.process_due(datetime(2026, 10, 22, 22, 0)), 0)
        row = await self.db.fetchone("SELECT status, recap FROM meetups")
        self.assertEqual(row["status"], MEETUP_PENDING)
        self.assertEqual(row["recap"], "")
        self.assertEqual(len(await self.m.pending()), 1)

    async def test_处理中状态不重复补记(self):
        meetup_id = await self.m.add("2026-10-22 18:00", "缘缘庄", "喝茶")
        await self.db.execute(
            "UPDATE meetups SET status = ? WHERE id = ?", (MEETUP_PROCESSING, meetup_id)
        )
        self.assertEqual(await self.m.process_due(datetime(2026, 10, 22, 22, 0)), 0)
        self.gw.chat.assert_not_called()
        self.assertEqual(await self.m.pending(), [])

    async def test_并发触发只补记一次(self):
        calls = 0

        async def slow_chat(**kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return "和他在武陵城吃了火锅"

        self.m.gateway = make_mock_gateway(chat_side_effect=slow_chat)
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "试新锅底")
        at = datetime(2026, 10, 22, 22, 0)
        results = await asyncio.gather(self.m.process_due(at), self.m.process_due(at))
        self.assertEqual(sum(results), 1)
        self.assertEqual(calls, 1)
        self.assertEqual(len(await self.memory.get_all_facts()), 1)
        row = await self.db.fetchone("SELECT status FROM meetups")
        self.assertEqual(row["status"], MEETUP_DONE)

    async def test_过期太久不补(self):
        await self.m.add("2026-10-01 18:00", "缘缘庄", "喝茶")
        await self.m.process_due(datetime(2026, 10, 22, 22, 0))
        self.assertEqual(await self.m.pending(), [])
        self.gw.chat.assert_not_called()


class TestWiring(_H):
    async def test_观察者记约定_关着时提示词不带(self):
        payload = {"meetups": [{"when": "2026-10-22 18:00", "where": "缘缘庄", "what": "喝茶"}]}
        gw = make_mock_gateway(chat_return_value=json.dumps(payload, ensure_ascii=False))
        stack = make_engine_stack(self.db, gateway=gw)
        obs = Observer(gw, stack.affection, stack.mood, stack.memory, stack.stickers, self.db)
        await obs.settle_turn("明天六点缘缘庄喝茶？", "行")
        self.assertNotIn("meetups", gw.chat.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(await self.m.pending(), [])
        obs.meetups = self.m
        await obs.settle_turn("明天六点缘缘庄喝茶？", "行")
        self.assertIn("meetups", gw.chat.call_args.kwargs["messages"][0]["content"])
        self.assertEqual(len(await self.m.pending()), 1)

    async def test_主聊提示词带约好的见面(self):
        stack = make_engine_stack(self.db)
        await self.m.add("2026-10-22 18:00", "缘缘庄", "喝茶")
        self.assertNotIn("约好的见面", await stack.assembler.assemble_system_prompt("在吗"))
        stack.assembler.meetups = self.m
        p = await stack.assembler.assemble_system_prompt("在吗")
        self.assertIn("【约好的见面】", p)
        self.assertIn("10-22 18:00 在缘缘庄，喝茶", p)

    def test_配置默认关(self):
        self.assertFalse(CharacterConfig().meetup_recap)


class TestSimilarity(unittest.TestCase):
    def test_短地点是子集不再判成同一条(self):
        # 旧实现直接返回 1.0：同一天"武陵买奶茶"和"武陵城火锅店吃火锅"会被当成改期吞掉
        self.assertLess(_similarity("武陵", "武陵城火锅店"), MEETUP_SIMILARITY)

    def test_地点字集相同仍是同一条(self):
        self.assertEqual(_similarity("武陵城火锅店", "武陵城火锅店"), 1.0)
        self.assertEqual(_similarity("武陵城火锅店", "店锅火城陵武"), 1.0)

    def test_子集占一半给中等分(self):
        # 3/6 = 0.5，到阈值：同一天把"武陵城"改口成"武陵城火锅店"仍走改期
        self.assertGreaterEqual(_similarity("武陵城", "武陵城火锅店"), MEETUP_SIMILARITY)


class TestFactDedup(_H):
    async def test_不同日期的见面纪要都留下(self):
        # 两次见面的纪要字面相似度 0.7+，只比汉字会被"近义去重"吞掉后一次，只剩一条
        await self.memory.add_fact("10-22 见面：和他在武陵城吃了青柠苦瓜锅底")
        await self.memory.add_fact("10-23 见面：和他在武陵城吃了青柠苦瓜锅底")
        self.assertEqual(len(await self.memory.get_all_facts()), 2)

    async def test_同一天相近的纪要仍去重(self):
        await self.memory.add_fact("10-22 见面：和他在武陵城吃了青柠苦瓜锅底")
        await self.memory.add_fact("10-22 见面：和他在武陵城吃青柠苦瓜锅底")
        self.assertEqual(len(await self.memory.get_all_facts()), 1)

    async def test_没日期的事实仍按近义去重(self):
        await self.memory.add_fact("他喜欢吃香菜")
        await self.memory.add_fact("他喜欢吃香菜啊")
        self.assertEqual(len(await self.memory.get_all_facts()), 1)


class TestCooldown(_H):
    async def test_不足三天不记(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "吃火锅")
        self.assertIsNone(await self.m.add("2026-10-24 18:00", "南门咖啡店", "喝咖啡"))
        self.assertEqual(len(await self.m.pending()), 1)

    async def test_正好三天记(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "吃火锅")
        self.assertIsNotNone(await self.m.add("2026-10-25 18:00", "南门咖啡店", "喝咖啡"))
        self.assertEqual(len(await self.m.pending()), 2)

    async def test_已补纪要的见面也计入间隔(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "吃火锅")
        self.assertEqual(await self.m.process_due(datetime(2026, 10, 22, 22, 0)), 1)
        self.assertIsNone(await self.m.add("2026-10-23 18:00", "南门咖啡店", "喝咖啡"))

    async def test_取消的约定不计入间隔(self):
        await self.m.add("2026-10-22 18:00", "武陵城火锅店", "吃火锅")
        self.assertTrue(await self.m.cancel("不去了"))
        self.assertIsNotNone(await self.m.add("2026-10-23 18:00", "南门咖啡店", "喝咖啡"))

    async def test_改期不受冷却限制(self):
        first = await self.m.add("2026-10-22 18:00", "武陵城火锅店", "试新锅底")
        second = await self.m.add("2026-10-23 18:00", "武陵城火锅店", "试新锅底")
        self.assertEqual(first, second)
        self.assertEqual(len(await self.m.pending()), 1)

    def test_冷却参数是模块级常量(self):
        self.assertEqual(MEETUP_MIN_INTERVAL_DAYS, 3)


class TestRecognition(_H):
    """观察者识别的二次校验 + 跨轮上下文"""

    async def _observer(self, gw):
        stack = make_engine_stack(self.db, gateway=gw)
        obs = Observer(gw, stack.affection, stack.mood, stack.memory, stack.stickers, self.db)
        obs.meetups = self.m
        return obs

    async def _settle(self, meetups):
        payload = {"meetups": meetups}
        gw = make_mock_gateway(chat_return_value=json.dumps(payload, ensure_ascii=False))
        obs = await self._observer(gw)
        await obs.settle_turn("哪天见个面吧", "好呀", recent_turns=[])
        return await self.m.pending()

    async def test_含糊提议不记(self):
        self.assertEqual(await self._settle([{"when": "", "where": "", "what": "有空一起吃个饭"}]), [])

    async def test_只有时间没地点不记(self):
        self.assertEqual(
            await self._settle([{"when": "2026-10-26 18:00", "where": "", "what": "吃饭"}]), []
        )

    async def test_地点是占位词也不记(self):
        self.assertEqual(
            await self._settle([{"when": "2026-10-26 18:00", "where": "（没说）", "what": "吃饭"}]), []
        )

    async def test_时间不具体不记(self):
        self.assertEqual(
            await self._settle([{"when": "改天", "where": "武陵城火锅店", "what": "吃火锅"}]), []
        )

    async def test_时间地点都明确才记(self):
        rows = await self._settle([{"when": "2026-10-26 18:00", "where": "武陵城火锅店", "what": "吃火锅"}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["place"], "武陵城火锅店")

    async def test_跨轮信息齐全的约定能记(self):
        # 第一轮只说了地点和事，时间在下一轮才定：只看本轮两句，观察者看不到"锅子店"
        await self.memory.save_turn_pair("武陵城新开了家锅子店，你看着办", "真的？那得去尝尝")
        turns = await self.memory.get_recent_turns(limit=4)

        def _reply(**kwargs):
            prompt = kwargs["messages"][-1]["content"]
            if "锅子店" in prompt:
                return json.dumps(
                    {"meetups": [{"when": "2026-10-26 18:00", "where": "武陵城火锅店", "what": "吃锅子"}]},
                    ensure_ascii=False,
                )
            return json.dumps({"meetups": []}, ensure_ascii=False)

        gw = make_mock_gateway(chat_side_effect=_reply)
        obs = await self._observer(gw)
        await obs.settle_turn("行，明天六点", "好，六点老地方等", recent_turns=turns)
        rows = await self.m.pending()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["place"], "武陵城火锅店")
        # 本轮两条不进上下文块，前几轮原文进
        prompt = gw.chat.call_args.kwargs["messages"][-1]["content"]
        history = prompt.partition("【现在】")[0]
        self.assertIn("刚才的几轮", history)
        self.assertIn("武陵城新开了家锅子店", history)
        self.assertNotIn("好，六点老地方等", history)

    async def test_不给上下文就看不到跨轮的地点和事(self):
        await self.memory.save_turn_pair("武陵城新开了家锅子店，你看着办", "真的？那得去尝尝")

        def _reply(**kwargs):
            prompt = kwargs["messages"][-1]["content"]
            if "锅子店" in prompt:
                return json.dumps(
                    {"meetups": [{"when": "2026-10-26 18:00", "where": "武陵城火锅店", "what": "吃锅子"}]},
                    ensure_ascii=False,
                )
            return json.dumps({"meetups": []}, ensure_ascii=False)

        gw = make_mock_gateway(chat_side_effect=_reply)
        obs = await self._observer(gw)
        await obs.settle_turn("行，明天六点", "好，六点老地方等", recent_turns=[])
        self.assertEqual(await self.m.pending(), [])
        self.assertNotIn("刚才的几轮", gw.chat.call_args.kwargs["messages"][-1]["content"])


class TestHistoryBlock(unittest.TestCase):
    def test_裁掉本轮两条只看前几轮(self):
        turns = [
            {"role": "user", "content": "武陵城新开了家锅子店，你看着办"},
            {"role": "assistant", "content": "真的？那得去尝尝"},
            {"role": "user", "content": "行，明天六点"},
            {"role": "assistant", "content": "好，六点老地方"},
        ]
        block = _meetup_history_block(turns, "行，明天六点", "好，六点老地方")
        self.assertIn("武陵城新开了家锅子店", block)
        self.assertNotIn("明天六点", block)

    def test_超长从最老的丢(self):
        turns = [{"role": "user" if i % 2 == 0 else "assistant", "content": f"第{i}条" + "字" * 120} for i in range(8)]
        block = _meetup_history_block(turns, "本轮", "回话")
        self.assertLessEqual(len(block), MEETUP_HISTORY_MAX_CHARS)
        self.assertNotIn("第0条", block)     # 最老那条被丢掉
        self.assertIn("第7条", block)        # 最近那条留着


class TestAssemblerBlock(_H):
    async def test_还没到和已经过了分开写(self):
        stack = make_engine_stack(self.db)
        now = datetime.now()
        past = (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        future = (now + timedelta(days=2)).strftime("%Y-%m-%d %H:%M")
        await self.m.add(past, "武陵城火锅店", "吃火锅")
        await self.m.add(future, "缘缘庄", "喝茶")
        stack.assembler.meetups = self.m
        prompt = await stack.assembler.assemble_system_prompt("在吗")
        upcoming_part, _, passed_part = prompt.partition("已经过了的")
        self.assertIn("还没到的", upcoming_part)
        self.assertIn("缘缘庄", upcoming_part)
        self.assertIn("武陵城火锅店", passed_part)
        self.assertIn("用过去时", passed_part)


if __name__ == "__main__":
    unittest.main()
