"""小黄脸日额度（world.face_usage = "rare"）：replier 层的硬闸。

提示词软约束会被历史上下文的旧习惯带偏，额度在 parse_reply 里强制执行：
超额的 [face:] 标记剥掉只留正文；None（默认）= 不限，旧卡行为不变。
"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from companion.db import Database  # noqa: E402
from companion.replier import (  # noqa: E402
    FACE_RARE_DAILY_BUDGET,
    count_today_faces,
    enforce_face_daily_budget,
    face_daily_budget,
)


class _World:
    def __init__(self, usage):
        self.face_usage = usage


class TestEnforceBudget(unittest.TestCase):
    def test_None不限(self):
        t = "晚安[face:撇嘴]\n好梦[face:呲牙]"
        self.assertEqual(enforce_face_daily_budget(t, None), t)

    def test_额度内全保留(self):
        t = "晚安[face:撇嘴]\n好梦[face:呲牙]"
        self.assertEqual(enforce_face_daily_budget(t, 2), t)

    def test_超额剥标记留正文(self):
        t = "行吧[face:撇嘴]\n睡吧[face:呲牙]\n晚安[face:doge]"
        out = enforce_face_daily_budget(t, 1)
        self.assertIn("[face:撇嘴]", out)
        self.assertNotIn("呲牙", out)
        self.assertNotIn("[face:doge]", out)
        self.assertIn("睡吧", out)
        self.assertIn("晚安", out)

    def test_零额度全剥(self):
        out = enforce_face_daily_budget("哦[face:撇嘴]", 0)
        self.assertEqual(out, "哦")

    def test_没有标记不动(self):
        self.assertEqual(enforce_face_daily_budget("晚安", 0), "晚安")


class TestBudgetMapping(unittest.TestCase):
    def test_rare给剩余额度(self):
        self.assertEqual(face_daily_budget(_World("rare"), 0), FACE_RARE_DAILY_BUDGET)
        self.assertEqual(face_daily_budget(_World("rare"), 1), FACE_RARE_DAILY_BUDGET - 1)
        self.assertEqual(face_daily_budget(_World("rare"), 99), 0)

    def test_normal和未声明都不限(self):
        self.assertIsNone(face_daily_budget(_World("normal"), 99))
        self.assertIsNone(face_daily_budget(object(), 99))


class TestCountTodayFaces(unittest.TestCase):
    def test_按今天assistant记录计数(self):
        async def run():
            with tempfile.TemporaryDirectory() as d:
                db = Database(os.path.join(d, "t.db"))
                await db.init_tables()
        # count_today_faces 用真实今天：昨天的不算、他的不算、今天的她的才算
        import datetime as _dt
        today = _dt.datetime.now().strftime("%Y-%m-%d")
        yesterday = (_dt.datetime.now() - _dt.timedelta(days=1)).strftime("%Y-%m-%d")

        async def run():
            with tempfile.TemporaryDirectory() as d:
                db = Database(os.path.join(d, "t.db"))
                await db.init_tables()
                await db.execute(
                    "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
                    ("assistant", "晚安[face:撇嘴] 好梦[face:呲牙]", today + " 08:00"),
                )
                await db.execute(
                    "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
                    ("assistant", "早[face:撇嘴]", yesterday + " 08:00"),
                )
                await db.execute(
                    "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
                    ("user", "[face:撇嘴]", today + " 08:01"),
                )
                n = await count_today_faces(db)
                await db.close()
                return n

        self.assertEqual(asyncio.run(run()), 2)  # 今天的她的 2 个；昨天、他的都不算


if __name__ == "__main__":
    unittest.main()
