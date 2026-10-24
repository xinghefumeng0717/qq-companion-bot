"""相对日期：游戏世界里给她看的日期换成"今天/前天/3 天前"，内部计算不变"""

import unittest
from datetime import date, datetime, timedelta

from companion.assembler import gap_note
from companion.datefmt import relative_day, relativize_dates
from helpers import close_db, make_db, make_engine_stack

NOW = datetime(2026, 10, 21, 21, 0)


class TestRelative(unittest.TestCase):
    def test_常用说法(self):
        today = NOW.date()
        cases = {0: "今天", -1: "昨天", -2: "前天", 1: "明天", 2: "后天", -5: "5 天前", 4: "4 天后", -20: "大约 3 周前"}
        for n, want in cases.items():
            self.assertEqual(relative_day(today + timedelta(days=n), today), want)

    def test_各种写法都换_时刻保留(self):
        text = "现在是 2026-10-21 21:00 星期三；打算 10-22 早上七点起（10-19 记下）；10月18日见过面"
        self.assertEqual(relativize_dates(text, NOW),
                         "现在是 今天 21:00 星期三；打算 明天 早上七点起（前天 记下）；3 天前见过面")

    def test_跨年按半年判断(self):
        self.assertEqual(relativize_dates("01-02", datetime(2026, 12, 30)), "3 天后")
        self.assertEqual(relativize_dates("12-30", datetime(2027, 1, 2)), "3 天前")

    def test_不是日期的不动(self):
        for s in ("比分 3-1", "13-40", "第 2-3 题", "9-15 号楼", "电话 0571-87654321",
                  "12:10-22:00 营业", "1.05-22 版本", "编号 10-22", "单号 2026-10-19"):
            self.assertEqual(relativize_dates(s, NOW), s)

    def test_仍认得的写法照旧转换(self):
        cases = {"10-22": "明天", "2026-10-22": "明天", "10月22日": "明天", "3月5日": "大约 4 个月后"}
        for s, want in cases.items():
            self.assertEqual(relativize_dates(s, NOW), want)

    def test_带年份的中文日期连年份一起换(self):
        self.assertEqual(relativize_dates("2026年10月22日 见", NOW), "明天 见")
        self.assertEqual(relativize_dates("2026年10月21日 21:00", NOW), "今天 21:00")

    def test_号写法连号一起换(self):
        self.assertEqual(relativize_dates("10-22号（10-19号记下）", NOW), "明天（前天记下）")
        self.assertEqual(relativize_dates("10月22日、10月22号", NOW), "明天、明天")

    def test_闰日不抛异常(self):
        for now in (NOW, datetime(2028, 10, 1, 12, 0)):
            self.assertEqual(relativize_dates("02-29", now), "02-29")
            self.assertEqual(relativize_dates("2月29日", now), "2月29日")

    def test_隔时标注可以不带月日(self):
        prev = NOW - timedelta(days=2)
        self.assertEqual(gap_note(NOW, prev, show_date=False), "（星期三 21:00，离上一句已经过了 2 天）\n")


class TestAssembler(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_默认不转换_relative时提示词里没有月日(self):
        stack = make_engine_stack(self.db)
        await stack.memory.add_fact("打算周末去武陵")
        absolute = await stack.assembler.assemble_system_prompt("在吗")
        self.assertRegex(absolute, r"\d{4}-\d{2}-\d{2}")
        stack.persona.world.date_style = "relative"
        rel = await stack.assembler.assemble_system_prompt("在吗")
        self.assertNotRegex(rel, r"\d{4}-\d{2}-\d{2}")
        self.assertNotRegex(rel, r"（\d{2}-\d{2} 记下）")
        self.assertIn("今天", rel)


if __name__ == "__main__":
    unittest.main()
