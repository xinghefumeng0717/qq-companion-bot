"""自我记忆：她说过的关于自己的事（[character].self_memory，默认关）"""

import json
import unittest

from companion.config import CharacterConfig
from companion.memory import MemoryManager
from companion.observer import Observer
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway


class _Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.memory = MemoryManager(self.db)

    async def asyncTearDown(self):
        await close_db(self.db)


class TestSelfFactStore(_Harness):
    async def test_近义去重(self):
        await self.memory.add_self_fact("她不吃香菜")
        await self.memory.add_self_fact("她不吃香菜的")
        self.assertEqual(len(await self.memory.get_self_facts_for_prompt()), 1)

    async def test_改口保留之前的说法(self):
        await self.memory.add_self_fact("她最喜欢喝冰美式")
        hit = await self.memory.supersede_self_fact("她喜欢喝冰美式", "她最近改喝热拿铁了")
        self.assertTrue(hit)
        items = await self.memory.get_self_facts_for_prompt()
        self.assertEqual(len(items), 1)
        self.assertIn("她最近改喝热拿铁了", items[0])
        self.assertIn("之前说过：她最喜欢喝冰美式", items[0])

    async def _insert(self, text, day, core=0):
        await self.db.execute(
            "INSERT INTO self_facts (content, core, created_at) VALUES (?, ?, ?)",
            (text, core, f"2026-10-{day:02d} 12:00"),
        )

    async def test_分层_核心常驻_非核心只带最近和相关(self):
        await self._insert("她老家是绍兴的", 1, core=1)
        await self._insert("她上周借了额尔古纳河右岸", 2)
        for i in range(10):
            await self._insert(f"事{i}", 3 + i)
        items = await self.memory.get_self_facts_for_prompt()
        self.assertTrue(items[0].startswith("她老家是绍兴的"))
        self.assertEqual(len(items), 1 + 5)
        self.assertFalse(any("额尔古纳" in x for x in items))
        items = await self.memory.get_self_facts_for_prompt(query="你那本额尔古纳河看完没")
        self.assertTrue(any("额尔古纳" in x for x in items))

    async def test_没有相同两字词也能靠实字调出来(self):
        await self._insert("她平时在食堂吃饭，常吃砂锅、麻辣烫和二楼的番茄鸡蛋面", 1)
        for i in range(10):
            await self._insert(f"事{i}", 3 + i)
        items = await self.memory.get_self_facts_for_prompt(query="诶你上次说你最爱吃的是啥来着")
        self.assertTrue(any("砂锅" in x for x in items))

    async def test_近义命中时核心标记只升不降(self):
        await self.memory.add_self_fact("她拉大提琴", core=True)
        await self.memory.add_self_fact("她拉大提琴的")
        row = await self.db.fetchone("SELECT core FROM self_facts")
        self.assertEqual(row["core"], 1)

    async def test_核心超上限才合并_结果不合格保持原样(self):
        from companion.memory import SELF_CORE_LIMIT
        for i in range(SELF_CORE_LIMIT):
            await self._insert(f"核心{i}", 1 + i % 28, core=1)
        self.memory.gateway = make_mock_gateway(chat_return_value='{"facts": ["她老家是绍兴的"]}')
        self.assertFalse(await self.memory.consolidate_self_core())
        await self._insert("再来一条", 28, core=1)
        self.assertTrue(await self.memory.consolidate_self_core())
        rows = await self.db.fetchall("SELECT content, core FROM self_facts")
        self.assertEqual([(r["content"], r["core"]) for r in rows], [("她老家是绍兴的", 1)])
        self.memory.gateway = make_mock_gateway(chat_return_value='{"facts": []}')
        await self._insert("x", 2, core=1)
        self.assertFalse(await self.memory.consolidate_self_core())


class TestObserverSelfFacts(_Harness):
    async def _settle(self, payload, enabled=True):
        gateway = make_mock_gateway(chat_return_value=json.dumps(payload, ensure_ascii=False))
        stack = make_engine_stack(self.db, gateway=gateway)
        obs = Observer(gateway, stack.affection, stack.mood, stack.memory, stack.stickers, self.db)
        obs.self_memory = enabled
        await obs.settle_turn("你喜欢吃什么", "麻辣烫，银泉那家")
        return gateway.chat.call_args.kwargs["messages"], stack.memory

    async def test_开着时提取入库且提示词带规则(self):
        msgs, mem = await self._settle({"self_facts": ["她喜欢吃银泉那家麻辣烫"], "self_facts_core": ["她老家是绍兴的"]})
        self.assertIn("self_facts", msgs[0]["content"])
        self.assertIn("【她说过的关于自己的事】", msgs[1]["content"])
        items = await mem.get_self_facts_for_prompt()
        self.assertEqual(len(items), 2)
        row = await self.db.fetchone("SELECT core FROM self_facts WHERE content LIKE '%绍兴%'")
        self.assertEqual(row["core"], 1)

    async def test_关着时提示词不变且不入库(self):
        msgs, mem = await self._settle({"self_facts": ["她喜欢吃麻辣烫"]}, enabled=False)
        self.assertNotIn("self_facts", msgs[0]["content"])
        self.assertNotIn("【她说过的关于自己的事】", msgs[1]["content"])
        self.assertEqual(await mem.get_self_facts_for_prompt(), [])

    async def test_畸形字段不炸整轮(self):
        _, mem = await self._settle({"self_facts": "不是列表", "updated_self_facts": [1, {"old": ""}]})
        self.assertEqual(await mem.get_self_facts_for_prompt(), [])


class TestAssemblerSelfBlock(_Harness):
    async def test_开着才注入(self):
        stack = make_engine_stack(self.db)
        await stack.memory.add_self_fact("她不吃香菜")
        off = await stack.assembler.assemble_system_prompt("在吗")
        self.assertNotIn("你说过的关于自己的事", off)
        stack.assembler.self_memory = True
        on = await stack.assembler.assemble_system_prompt("在吗")
        self.assertIn("【你说过的关于自己的事】", on)
        self.assertIn("她不吃香菜", on)

    def test_配置默认关(self):
        self.assertFalse(CharacterConfig().self_memory)


class TestGapNote(_Harness):
    def test_间隔够长才标(self):
        from datetime import datetime
        from companion.assembler import gap_note
        now = datetime(2026, 10, 17, 15, 1)
        self.assertEqual(gap_note(now, datetime(2026, 10, 17, 13, 30)), "")
        self.assertEqual(gap_note(now, None), "")
        note = gap_note(now, datetime(2026, 10, 15, 21, 12))
        self.assertEqual(note, "（10-17 15:01 星期六，离上一句已经过了 1 天 17 小时）\n")

    async def test_历史和当前消息都标_她的消息不标(self):
        from datetime import datetime, timedelta
        from companion.db import TIME_FORMAT
        stack = make_engine_stack(self.db)
        old = datetime.now() - timedelta(days=2)
        for role, text, dt in (("user", "晚安", old), ("assistant", "晚安", old),
                               ("user", "早", old + timedelta(hours=10)),
                               ("assistant", "早啊", old + timedelta(hours=10))):
            await self.db.execute(
                "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
                (role, text, dt.strftime(TIME_FORMAT)),
            )
        msgs, _ = await stack.assembler.assemble_messages("在吗")
        self.assertEqual(msgs[1]["content"], "晚安")
        self.assertIn("离上一句已经过了 10 小时）\n早", msgs[3]["content"])
        self.assertEqual(msgs[4]["content"], "早啊")
        self.assertTrue(msgs[-1]["content"].endswith("）\n在吗"))


if __name__ == "__main__":
    unittest.main()
