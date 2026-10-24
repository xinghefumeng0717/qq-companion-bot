"""关于他的事分层（[character].fact_layers，默认关）"""

import json
import os
import sqlite3
import tempfile
import unittest

from companion.config import CharacterConfig
from companion.db import Database
from companion.memory import FACT_CORE_LIMIT, FACT_RECENT_N, MemoryManager
from companion.observer import Observer
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway


class _Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.memory = MemoryManager(self.db)

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _insert(self, text, day, core=0):
        await self.db.execute(
            "INSERT INTO facts (content, core, created_at) VALUES (?, ?, ?)",
            (text, core, f"2026-10-{day:02d} 12:00"),
        )


class TestLayers(_Harness):
    async def test_关着时全部带上_与旧行为一致(self):
        for i in range(20):
            await self._insert(f"事{i}", 1 + i)
        self.assertEqual(len(await self.memory.get_facts_for_prompt()), 20)

    async def test_开着时核心常驻_非核心只带最近和相关(self):
        self.memory.fact_layers = True
        await self._insert("他在示例大学读农学", 1, core=1)
        await self._insert("他在准备数学建模比赛", 2)
        for i in range(15):
            await self._insert(f"事{i}", 3 + i)
        items = await self.memory.get_facts_for_prompt()
        self.assertEqual(len(items), 1 + FACT_RECENT_N)
        self.assertTrue(items[0].startswith("他在示例大学读农学"))
        self.assertFalse(any("数学建模" in x for x in items))
        items = await self.memory.get_facts_for_prompt(query="你那个建模比赛怎么样了")
        self.assertTrue(any("数学建模" in x for x in items))

    async def test_近义刷新时间_核心只升不降_改口保留核心(self):
        await self.memory.add_fact("他是绍兴人", core=True)
        await self.memory.add_fact("他是绍兴人啊")
        row = await self.db.fetchone("SELECT core FROM facts")
        self.assertEqual(row["core"], 1)
        await self.memory.supersede_fact("他是绍兴人", "他其实是宁波人，在绍兴长大")
        row = await self.db.fetchone("SELECT content, core FROM facts")
        self.assertEqual((row["content"], row["core"]), ("他其实是宁波人，在绍兴长大", 1))

    async def test_核心超上限才合并(self):
        for i in range(FACT_CORE_LIMIT):
            await self._insert(f"核心{i}", 1 + i % 28, core=1)
        self.memory.gateway = make_mock_gateway(chat_return_value='{"facts": ["他在示例大学读农学"]}')
        self.assertFalse(await self.memory.consolidate_fact_core())
        await self._insert("再来一条", 28, core=1)
        self.assertTrue(await self.memory.consolidate_fact_core())
        prompt = self.memory.gateway.chat.call_args.kwargs["messages"][0]["content"]
        self.assertIn("关于一个男生", prompt)
        rows = await self.db.fetchall("SELECT content, core FROM facts")
        self.assertEqual([(r["content"], r["core"]) for r in rows], [("他在示例大学读农学", 1)])


class TestObserver(_Harness):
    async def _settle(self, payload, layers):
        gateway = make_mock_gateway(chat_return_value=json.dumps(payload, ensure_ascii=False))
        stack = make_engine_stack(self.db, gateway=gateway)
        stack.memory.fact_layers = layers
        obs = Observer(gateway, stack.affection, stack.mood, stack.memory, stack.stickers, self.db)
        await obs.settle_turn("我是宁波人，最近在准备比赛", "好")
        return gateway.chat.call_args.kwargs["messages"]

    async def test_开着时提示词带规则_核心入库(self):
        msgs = await self._settle({"facts": ["他在准备比赛"], "facts_core": ["他是宁波人"]}, True)
        self.assertIn("facts_core", msgs[0]["content"])
        row = await self.db.fetchone("SELECT core FROM facts WHERE content = '他是宁波人'")
        self.assertEqual(row["core"], 1)

    async def test_关着时提示词不变_字段忽略(self):
        msgs = await self._settle({"facts_core": ["他是宁波人"]}, False)
        self.assertNotIn("facts_core", msgs[0]["content"])
        self.assertIsNone(await self.db.fetchone("SELECT 1 FROM facts"))

    def test_配置默认关(self):
        self.assertFalse(CharacterConfig().fact_layers)


class TestMigration(unittest.IsolatedAsyncioTestCase):
    async def test_旧库补上core列_旧数据都在(self):
        path = os.path.join(tempfile.mkdtemp(), "old.db")
        con = sqlite3.connect(path)
        con.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT UNIQUE, created_at TEXT)")
        con.execute("INSERT INTO facts (content, created_at) VALUES ('他在示例大学', '2026-10-05 12:00')")
        con.commit()
        con.close()
        db = Database(path)
        await db.init_tables()
        mem = MemoryManager(db)
        mem.fact_layers = True
        self.assertEqual(await mem.get_facts_for_prompt(), ["他在示例大学（10-05 记下）"])
        await mem.add_fact("他是宁波人", core=True)
        await db.close()


if __name__ == "__main__":
    unittest.main()
