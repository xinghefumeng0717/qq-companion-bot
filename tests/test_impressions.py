"""对他的看法（[character].impressions，默认关）"""

import json
import unittest

from companion.config import CharacterConfig
from companion.memory import IMPRESSIONS_MAX, MemoryManager
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway

TURNS = [{"role": "user", "content": "知道了知道了"}, {"role": "assistant", "content": "早点睡"}]


class _Harness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    def mem(self, payload):
        gw = make_mock_gateway(chat_return_value=json.dumps(payload, ensure_ascii=False))
        return MemoryManager(self.db, gw), gw


class TestUpdate(_Harness):
    async def test_整表替换_把握夹在1到3(self):
        mem, gw = self.mem({"impressions": [
            {"看法": "他不喜欢被叮嘱", "把握": 5, "依据": "说早点睡时他回知道了知道了"},
            {"看法": "他累了会越回越短", "把握": "x"},
            {"看法": ""}, "坏项",
        ]})
        self.assertTrue(await mem.update_impressions(TURNS))
        rows = await mem.get_impressions()
        self.assertEqual([(r["content"], r["confidence"]) for r in rows],
                         [("他不喜欢被叮嘱", 3), ("他累了会越回越短", 1)])
        sent = gw.chat.call_args.kwargs["messages"][1]["content"]
        self.assertIn("他：知道了知道了", sent)
        self.assertIn("你：早点睡", sent)

    async def test_结果为空保持原样(self):
        mem, _ = self.mem({"impressions": [{"看法": "他嘴硬"}]})
        await mem.update_impressions(TURNS)
        mem2, _ = self.mem({"impressions": []})
        self.assertFalse(await mem2.update_impressions(TURNS))
        self.assertEqual(len(await mem2.get_impressions()), 1)

    async def test_最多保留上限条(self):
        mem, _ = self.mem({"impressions": [{"看法": f"他{i}"} for i in range(IMPRESSIONS_MAX + 3)]})
        await mem.update_impressions(TURNS)
        self.assertEqual(len(await mem.get_impressions()), IMPRESSIONS_MAX)


class TestPrompt(_Harness):
    async def test_开着才注入_带把握措辞与不说出口(self):
        stack = make_engine_stack(self.db)
        await self.db.execute(
            "INSERT INTO impressions (content, confidence, evidence, updated_at) VALUES ('他不喜欢被叮嘱', 2, '', '2026-10-20 12:00')"
        )
        self.assertNotIn("你对他的看法", await stack.assembler.assemble_system_prompt("在吗"))
        stack.assembler.impressions = True
        on = await stack.assembler.assemble_system_prompt("在吗")
        self.assertIn("他不喜欢被叮嘱（比较确定）", on)
        self.assertIn("不要说出口", on)

    def test_配置默认关(self):
        self.assertFalse(CharacterConfig().impressions)


if __name__ == "__main__":
    unittest.main()
