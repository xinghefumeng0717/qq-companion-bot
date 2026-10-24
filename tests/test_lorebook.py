"""世界书：常驻概要 + 关键词词条（卡字段 lorebook 或卡目录 lorebook.json）"""

import json
import os
import shutil
import tempfile
import unittest

from companion.lorebook import BLOCK_HEADER, Lorebook
from helpers import close_db, make_db, make_engine_stack

BOOK = {
    "always": "塔卫二，帝江号是科幻工业的轨道总部舰。",
    "entries": [
        {"keys": ["武陵"], "content": "武陵城：宏山科学院治理甚大裂隙的重点地区。"},
        {"keys": ["锅底", "火锅"], "content": "她爱在武陵城的火锅店试怪锅底。", "priority": 5},
        {"keys": ["明河"], "content": "明河：金发的龙，炎国禁军出身。"},
        {"keys": [], "content": "坏词条：没有关键词"},
    ],
    "max_entries": 2,
}


class TestMatch(unittest.TestCase):
    def setUp(self):
        self.book = Lorebook.from_dict(BOOK)

    def test_坏词条跳过(self):
        self.assertEqual(len(self.book.entries), 3)

    def test_命中关键词_优先级高的在前_条数有上限(self):
        picked = self.book.match(["明天去武陵吃火锅", "顺便叫上明河"])
        self.assertEqual([e.keys[0] for e in picked], ["锅底", "武陵"])

    def test_没命中返回空串(self):
        self.assertEqual(self.book.build_block(["今天好累"]), "")

    def test_块格式(self):
        block = self.book.build_block(["武陵"])
        self.assertTrue(block.startswith(BLOCK_HEADER))
        self.assertIn("武陵城：宏山科学院", block)


class TestLoad(unittest.TestCase):
    def test_卡目录的lorebook_json优先(self):
        d = tempfile.mkdtemp()
        try:
            with open(os.path.join(d, "lorebook.json"), "w", encoding="utf-8") as f:
                json.dump({"always": "文件里的"}, f, ensure_ascii=False)
            self.assertEqual(Lorebook.load(d, {"lorebook": {"always": "卡里的"}}).always, "文件里的")
            self.assertEqual(Lorebook.load(tempfile.mkdtemp(), {"lorebook": {"always": "卡里的"}}).always, "卡里的")
            self.assertFalse(Lorebook.load(tempfile.mkdtemp(), {}).enabled)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class TestAssembler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_没有世界书时提示词不变_有时常驻进角色块_词条按关键词进事实段(self):
        stack = make_engine_stack(self.db)
        before = await stack.assembler.assemble_system_prompt("想去武陵吃火锅")
        self.assertNotIn("【她的世界】", before)
        self.assertNotIn("【世界书】", before)
        stack.persona.lorebook = Lorebook.from_dict(BOOK)
        p = await stack.assembler.assemble_system_prompt("想去武陵吃火锅")
        self.assertIn("【她的世界】塔卫二", p)
        self.assertLess(p.index("【她的世界】"), p.index("【聊天规则】"))
        self.assertIn("试怪锅底", p)
        self.assertGreater(p.index("【世界书】"), p.index("【事实】"))
        self.assertNotIn("【世界书】", await stack.assembler.assemble_system_prompt("在吗"))

    async def test_最近聊天里提过也算(self):
        stack = make_engine_stack(self.db)
        stack.persona.lorebook = Lorebook.from_dict(BOOK)
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES ('user', '明河今天来了', 0, 0, '2026-10-20 12:00')"
        )
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES ('assistant', '哦', 0, 0, '2026-10-20 12:00')"
        )
        self.assertIn("金发的龙", await stack.assembler.assemble_system_prompt("嗯"))


if __name__ == "__main__":
    unittest.main()
