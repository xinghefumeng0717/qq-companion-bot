"""主动消息沉默降频：他多日不回 → 降频 → 停发，他一回话即恢复"""

import os
import unittest
from datetime import datetime, timedelta

from companion.affection import AffectionEngine
from companion.config import ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT, Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager

DB_PATH = "data/test_silence_throttle.db"


class TestSilenceThrottle(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        if os.path.exists(DB_PATH):
            os.remove(DB_PATH)
        self.db = Database(DB_PATH)
        await self.db.init_tables()
        persona = Persona.load("characters/example")
        stickers = StickerManager("characters/example/stickers", self.db)
        self.sched = ProactiveScheduler(
            config=ProactiveConfig(enabled=True, quiet_hours=[]),
            persona=persona, affection=AffectionEngine(self.db), mood=MoodEngine(self.db),
            memory=MemoryManager(self.db), stickers=stickers,
            replier=Replier(ReplyConfig(), stickers), gateway=None, db=self.db, send_msg_fn=None,
        )
        self.now = datetime(2026, 10, 20, 15, 0)

    async def asyncTearDown(self):
        await self.db.close()
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(DB_PATH + suf):
                os.remove(DB_PATH + suf)

    async def _turn(self, role, hours_ago, proactive=0):
        ts = (self.now - timedelta(hours=hours_ago)).strftime(TIME_FORMAT)
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, 'x', ?, 0, ?)",
            (role, proactive, ts),
        )

    async def test_一天内不拦(self):
        await self._turn("user", 20)
        await self._turn("assistant", 2, proactive=1)
        self.assertEqual(await self.sched._silence_gate(self.now), "")

    async def test_满两天且一天前刚发过_拦(self):
        await self._turn("user", 60)
        await self._turn("assistant", 24, proactive=1)
        self.assertIn("降频", await self.sched._silence_gate(self.now))

    async def test_满两天且上条已过48小时_放行(self):
        await self._turn("user", 80)
        await self._turn("assistant", 50, proactive=1)
        self.assertEqual(await self.sched._silence_gate(self.now), "")

    async def test_满五天_停发(self):
        await self._turn("user", 5 * 24 + 1)
        self.assertIn("停止", await self.sched._silence_gate(self.now))

    async def test_他一回话就恢复(self):
        await self._turn("user", 6 * 24)
        await self._turn("assistant", 3 * 24, proactive=1)
        await self._turn("user", 1)
        self.assertEqual(await self.sched._silence_gate(self.now), "")

    async def test_从没说过话不拦(self):
        self.assertEqual(await self.sched._silence_gate(self.now), "")

    async def test_置零即关闭(self):
        self.sched.config.silence_stop_after_days = 0
        self.sched.config.silence_slow_after_days = 0
        await self._turn("user", 10 * 24)
        self.assertEqual(await self.sched._silence_gate(self.now), "")

    async def test_规则闸门接入(self):
        # 闸门用真实 datetime.now()，这里放远一点不受测试时钟影响
        await self._turn("user", 30 * 24)
        blocked, reason = await self.sched._check_rules_gate(skip_unanswered_reply=True)
        self.assertTrue(blocked)
        self.assertIn("没回", reason)


if __name__ == "__main__":
    unittest.main()
