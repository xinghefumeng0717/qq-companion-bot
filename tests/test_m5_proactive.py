"""M5 验收测试：主动消息三层决策、规则闸门与素材优选"""

import os
import unittest
from datetime import datetime, timedelta

from companion.affection import AffectionEngine
from companion.config import ProactiveConfig, ReplyConfig
from companion.db import Database, now_str
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager


class TestM5Proactive(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "data/test_m5.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_tables()

        self.persona = Persona.load("characters/example")
        self.affection = AffectionEngine(self.db)
        self.mood = MoodEngine(self.db)
        self.memory = MemoryManager(self.db)
        self.stickers = StickerManager("characters/example/stickers", self.db)
        self.replier = Replier(ReplyConfig(), self.stickers)

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    async def test_rules_gate_unanswered_limit(self):
        config = ProactiveConfig(enabled=True, max_unanswered=2, quiet_hours=[])
        scheduler = ProactiveScheduler(
            config=config,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=None,
            db=self.db,
            send_msg_fn=None,
        )

        # 累加到达到 max_unanswered
        await scheduler.increment_unanswered_count()
        await scheduler.increment_unanswered_count()

        blocked, reason = await scheduler._check_rules_gate()
        self.assertTrue(blocked)
        self.assertIn("未回复", reason)

        # 机主发消息后清零
        await scheduler.reset_unanswered_count()
        cnt = await scheduler.get_unanswered_count()
        self.assertEqual(cnt, 0)

    async def test_rules_gate_goodnight(self):
        config = ProactiveConfig(enabled=True, quiet_hours=[])
        scheduler = ProactiveScheduler(
            config=config,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=None,
            db=self.db,
            send_msg_fn=None,
        )

        # 插入包含晚安的用户消息
        await self.db.execute(
            "INSERT INTO turns (role, content, created_at) VALUES ('user', '好困呀，晚安啦', ?)",
            (now_str(),),
        )

        blocked, reason = await scheduler._check_rules_gate()
        self.assertTrue(blocked)
        self.assertIn("晚安", reason)

    async def test_topic_priority_due_followup(self):
        scheduler = ProactiveScheduler(
            config=ProactiveConfig(),
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=None,
            db=self.db,
            send_msg_fn=None,
        )

        # 插入一条已到期的待跟进
        past_str = (datetime.now() - timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
        await self.db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES ('明天要考科目三', ?, 0, ?)",
            (past_str, past_str),
        )

        topic = await scheduler._select_topic_material()
        self.assertIn("考科目三", topic)


if __name__ == "__main__":
    unittest.main()
