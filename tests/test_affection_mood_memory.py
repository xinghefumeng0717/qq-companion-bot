"""M3 验收测试：状态引擎（好感度数学、PAD 连续情绪、记忆遗忘曲线与观察者结算）"""

import os
import unittest
from datetime import datetime, timedelta

from companion.affection import (
    AffectionEngine,
    calc_composite_score,
    calc_resistance,
    determine_stage,
)
from companion.db import Database, now_str
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.prompts import (
    get_mood_description,
    get_mood_label,
    get_trust_description,
)


from helpers import make_db, close_db


class TestM3Engines(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "data/test_m3.db"
        self.db = await make_db(self.test_db_path)

    async def asyncTearDown(self):
        await close_db(self.db, self.test_db_path)

    def test_affection_math(self):
        # 1. 复合分公式验证
        dims = {"warmth": 40.0, "trust": 50.0, "intimacy": 35.0, "intrigue": 30.0, "patience": 50.0, "tension": 3.0}
        comp = calc_composite_score(dims)
        # 40*0.25 + 50*0.25 + 35*0.25 + 30*0.1 + 50*0.15 - 3*0.3 = 10 + 12.5 + 8.75 + 3 + 7.5 - 0.9 = 40.85
        self.assertAlmostEqual(comp, 40.85, places=2)

        # 2. 阶段门槛
        self.assertEqual(determine_stage(0), 0)
        self.assertEqual(determine_stage(14.9), 0)
        self.assertEqual(determine_stage(15.0), 1)
        self.assertEqual(determine_stage(40.85), 2)
        self.assertEqual(determine_stage(99.8), 9)

        # 3. 高值阻力
        self.assertEqual(calc_resistance(15.0), 1.0)
        self.assertEqual(calc_resistance(20.0), 1.0)
        self.assertLess(calc_resistance(50.0), 1.0)
        self.assertEqual(calc_resistance(100.0), 0.0)
        self.assertEqual(calc_resistance(105.0), 0.0)

    async def test_affection_update_and_milestones(self):
        aff = AffectionEngine(self.db)
        state = await aff.get_state()
        self.assertEqual(state["stage"], 2)

        # 执行一次高分共情互动
        new_state, pulse = await aff.update(
            self_disclosure=9.0,
            responsiveness=9.0,
            warmth_score=9.0,
            resonance=9.0,
            moments=["深度共情", "分享脆弱"],
        )
        self.assertGreater(new_state["composite"], state["composite"])
        self.assertTrue(pulse)

        # 验证伤害行为
        hurt_state, _ = await aff.update(
            self_disclosure=1.0,
            responsiveness=1.0,
            warmth_score=1.0,
            resonance=1.0,
            moments=["伤害行为"],
        )
        self.assertGreater(hurt_state["dims"]["tension"], new_state["dims"]["tension"])

    async def test_mood_engine_ou_and_neglect(self):
        mood = MoodEngine(self.db)
        init_m = await mood.get_state()
        self.assertEqual(init_m["v"], 2.0)
        self.assertEqual(init_m["t"], 7.0)

        # 更新情绪（带正向对话冲击）
        m2 = await mood.update_mood(composite_affection=50.0, conv_v=1.5, conv_a=0.5, conv_trust=0.1)
        self.assertGreater(m2["v"], 2.0)

        # 测试情绪翻译层
        self.assertIn(get_mood_label(5.0, 5.0), ["欣喜", "愉悦"])
        self.assertIn(get_trust_description(8.0), "在你身边感到很安心")

    async def test_memory_forgetting_curve_and_reinforce(self):
        memory = MemoryManager(self.db)
        # 手动插入一条测试日记
        await self.db.execute(
            """
            INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)
            VALUES ('今天我们第一次在星空下散步聊天。', 8, '温暖', 0, ?, ?)
            """,
            (now_str(), now_str()),
        )

        diaries = await memory.get_active_diaries(current_valence=2.0)
        self.assertEqual(len(diaries), 1)
        self.assertIn("清晰地记得", diaries[0])

        # 只有当前消息与日记命中同一二字词组才加固；本条仍有足够强度。
        await memory.reinforce_memories("你还记得那次在星空下散步吗？")
        row = await self.db.fetchone("SELECT recall_count FROM diary WHERE id = 1")
        self.assertGreaterEqual(row["recall_count"], 1)

        # 语义事实去重测试
        await memory.add_fact("机主喜欢喝拿铁")
        await memory.add_fact("机主喜欢喝拿铁")
        facts = await memory.get_all_facts()
        self.assertEqual(len(facts), 1)
        self.assertEqual(facts[0], "机主喜欢喝拿铁")

    async def test_generic_longing_does_not_reinforce_unrelated_diaries(self):
        """只说想念时，吃饭与其他旧事都不能被批量加固或复活。"""
        memory = MemoryManager(self.db)
        old_time = (datetime.now() - timedelta(days=200)).strftime("%Y-%m-%d %H:%M")
        await self.db.execute(
            """
            INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)
            VALUES ('很久以前的一件小事。', 1, '平静', 0, ?, ?)
            """,
            (old_time, old_time),
        )
        await self.db.execute(
            """
            INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)
            VALUES ('今天一起喝了新开的拿铁。', 6, '温暖', 0, ?, ?)
            """,
            (now_str(), now_str()),
        )

        await memory.reinforce_memories("想你了")

        rows = await self.db.fetchall("SELECT content, recall_count, last_recall_at FROM diary ORDER BY id")
        self.assertEqual(rows[0]["recall_count"], 0)
        self.assertEqual(rows[1]["recall_count"], 0)

    async def test_diary_recall_gives_bounded_boost_to_current_topic(self):
        memory = MemoryManager(self.db)
        await self.db.execute(
            "INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at) "
            "VALUES ('上次一起复习了现代汉语。', 10, '平静', 0, ?, ?)",
            (now_str(), now_str()),
        )
        await self.db.execute(
            "INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at) "
            "VALUES ('上次一起吃了新开的火锅。', 6, '温暖', 0, ?, ?)",
            (now_str(), now_str()),
        )
        plain = await memory.get_active_diaries()
        related = await memory.get_active_diaries(query="还记得那次火锅吗")
        self.assertIn("现代汉语", plain[0])
        self.assertIn("火锅", related[0])

    async def test_hours_since_last_chat_ignores_assistant_turns(self):
        """冷落信号只看 user 行：机器人主动消息不得刷新/伪造最近对话时间"""
        mood = MoodEngine(self.db)

        # ① 库里只有一条 assistant 主动消息 → 视为从未对话，返回 0.0
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('assistant', '在吗？', 1, 0, ?)
            """,
            (now_str(),),
        )
        self.assertEqual(await mood.get_hours_since_last_chat(), 0.0)

        # ② user 消息在 5 小时前，assistant 回复刚发 → 仍按 user 行计 ~5 小时
        five_hours_ago = (datetime.now() - timedelta(hours=5)).strftime("%Y-%m-%d %H:%M")
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('user', '早', 0, 0, ?)
            """,
            (five_hours_ago,),
        )
        await self.db.execute(
            """
            INSERT INTO turns (role, content, proactive, has_image, created_at)
            VALUES ('assistant', '早呀', 0, 0, ?)
            """,
            (now_str(),),
        )
        hours = await mood.get_hours_since_last_chat()
        self.assertAlmostEqual(hours, 5.0, delta=0.1)


if __name__ == "__main__":
    unittest.main()
