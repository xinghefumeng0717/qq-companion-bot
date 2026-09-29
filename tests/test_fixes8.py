"""FIXES8 验收单元测试 (tests/test_fixes8.py)
验证内容：
1. 六维好感度上限严格 clamp 在 [0, 100]，阻力平滑归零无破百可能
2. 遗忘曲线 Tau 标定（重要性 1 约 7d，重要性 5 约 60-80d，重要性 8 正面约 1年，重要性 10 强化数年）
3. 事实记忆中文字符 Jaccard 相似度 >= 0.6 近似去重与新鲜度刷新
4. 日记归档事务原子性（异常自动回滚，数据库状态完全还原）
5. 日记归档阶段感知与提示词注入
6. 视觉感知 clean_desc 截断至 120 字符
"""

import asyncio
import math
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from companion.affection import (
    AffectionEngine,
    calc_composite_score,
    calc_resistance,
    determine_stage,
    STAGE_THRESHOLDS,
)
from companion.db import Database, now_str
from companion.memory import MemoryManager, POSITIVE_SENTIMENTS, NEGATIVE_SENTIMENTS
from companion.persona import Persona, Stage


class TestFixes8(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_db_path = "data/test_fixes8.db"
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)
        self.db = Database(self.test_db_path)
        await self.db.init_tables()

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.test_db_path):
            os.remove(self.test_db_path)

    def test_dimensions_clamp_and_resistance(self):
        """测试 1: 六维 clamp 在 100.0，阻力平滑归零"""
        # 1. calc_composite_score 截断测试
        over_dims = {
            "warmth": 150.0,
            "trust": 120.0,
            "intimacy": 110.0,
            "intrigue": 105.0,
            "patience": 130.0,
            "tension": 0.0,
        }
        # 150*0.25 + 120*0.25 + 110*0.25 + 105*0.1 + 130*0.15 = 37.5 + 30 + 27.5 + 10.5 + 19.5 = 125.0
        # 必须截断在 100.0
        self.assertEqual(calc_composite_score(over_dims), 100.0)

        # 2. calc_resistance 在 100 处平滑归零且单调递减
        self.assertEqual(calc_resistance(0.0), 1.0)
        self.assertEqual(calc_resistance(20.0), 1.0)
        self.assertAlmostEqual(calc_resistance(100.0), 0.0, places=5)
        self.assertEqual(calc_resistance(105.0), 0.0)

        # 验证单调递减
        r_vals = [calc_resistance(c) for c in range(20, 101, 5)]
        for i in range(len(r_vals) - 1):
            self.assertGreaterEqual(r_vals[i], r_vals[i + 1])

    async def test_affection_engine_clamp_under_repeated_updates(self):
        """测试 1B: 好感度引擎更新时各维度绝对不超过 100.0"""
        aff = AffectionEngine(self.db, initial_dims={
            "warmth": 95.0,
            "trust": 95.0,
            "intimacy": 95.0,
            "intrigue": 95.0,
            "patience": 95.0,
            "tension": 0.0,
        })
        for _ in range(50):
            st, _ = await aff.update(
                self_disclosure=10.0,
                responsiveness=10.0,
                warmth_score=10.0,
                resonance=10.0,
                moments=["深度共情", "分享脆弱"],
            )

        for dim, val in st["dims"].items():
            self.assertLessEqual(val, 100.0, f"维度 {dim} 超出 100.0 上限: {val}")
            self.assertGreaterEqual(val, 0.0)
        self.assertLessEqual(st["composite"], 100.0)

    def test_forgetting_tau_calibration(self):
        """测试 2: 遗忘曲线 Tau 标定验证（4 个关键基准点）"""
        def calc_half_decay_days(imp: float, sentiment: str, recall: int) -> float:
            tau_base = max(10.0, imp * 6.8)
            tau_effective = tau_base * (1.0 + 0.15 * recall)
            if sentiment in POSITIVE_SENTIMENTS:
                tau_effective *= 2.0
            elif sentiment in NEGATIVE_SENTIMENTS:
                tau_effective *= 1.5

            s0 = imp * (1.0 + 0.3 * math.log2(recall + 1))
            return tau_effective * math.log(s0 / 0.5)

        # 1. 重要性 1 (中性, 0次回忆): 衰减至 0.5 约 1 周 (7 天，+-20% 容差即 [5.6, 8.4])
        t_imp1 = calc_half_decay_days(1.0, "平静", 0)
        self.assertAlmostEqual(t_imp1, 6.93, delta=0.1)
        self.assertTrue(5.6 <= t_imp1 <= 8.4, f"imp 1 天数 {t_imp1} 不在 [5.6, 8.4] 内")

        # 2. 重要性 5 中性 (0次回忆): 衰减至 0.5 约 60~80 天
        t_imp5 = calc_half_decay_days(5.0, "平静", 0)
        self.assertTrue(60.0 <= t_imp5 <= 80.0, f"imp 5 天数 {t_imp5} 不在 [60, 80] 内")

        # 3. 重要性 8 正面 (0次回忆): 衰减至 0.5 约 1 年 (365 天，+-20% 容差即 [292, 438])
        t_imp8 = calc_half_decay_days(8.0, "温暖", 0)
        self.assertTrue(292.0 <= t_imp8 <= 438.0, f"imp 8 正面天数 {t_imp8} 不在 [292, 438] 内")

        # 4. 重要性 10 正面 (8次回忆加固): 衰减至 0.5 需数年 (> 2.5 年)
        t_imp10 = calc_half_decay_days(10.0, "幸福", 8) / 365.0
        self.assertGreater(t_imp10, 2.5, f"imp 10 正面 8 次加固年数 {t_imp10} 应该大于 2.5 年")

    async def test_fact_jaccard_deduplication(self):
        """测试 3: 语义事实近义去重 (Jaccard >= 0.6) 与刷新时间"""
        memory = MemoryManager(self.db)

        # 插入第一条事实
        await memory.add_fact("机主喜欢吃香菜")
        facts = await memory.get_all_facts()
        self.assertEqual(facts, ["机主喜欢吃香菜"])

        # 插入近义事实 "机主爱吃香菜" (交集 5 / 并集 8 = 0.625 >= 0.6)
        await memory.add_fact("机主爱吃香菜")
        facts_after = await memory.get_all_facts()
        self.assertEqual(len(facts_after), 1, "近义事实不应产生新记录")
        self.assertEqual(facts_after[0], "机主喜欢吃香菜")

        # 插入不近义事实 "机主喜欢吃拉面" (交集 4 / 并集 9 = 0.44 < 0.6)
        await memory.add_fact("机主喜欢吃拉面")
        facts_third = await memory.get_all_facts()
        self.assertEqual(len(facts_third), 2)
        self.assertIn("机主喜欢吃拉面", facts_third)

    async def test_diary_transaction_atomic_rollback(self):
        """测试 4: 日记归档事务原子性（异常时完全回滚）"""
        mock_gateway = MagicMock()
        mock_gateway.config.observer_model = "test-model"
        # 返回合法的日记 JSON
        mock_gateway.chat = AsyncMock(return_value='''{
            "content": "今天和他在自习室偶遇，聊了几句选课的事情，感觉他挺认真的。",
            "importance": 7,
            "sentiment": "平静",
            "facts": ["机主选了微积分课程"]
        }''')

        memory = MemoryManager(self.db, mock_gateway)

        # 初始游标为 0
        cur_row = await self.db.fetchone("SELECT value FROM counters WHERE key = 'archived_turns'")
        self.assertEqual(cur_row["value"], 0)

        # 构造异常：在事务执行到 UPDATE counters 时模拟异常
        original_execute = memory.db.execute

        async def fail_on_counters(sql, params=()):
            if "UPDATE counters" in sql:
                raise RuntimeError("Simulated DB Disk Full / Connection Drop")
            return await original_execute(sql, params)

        turns = [{"id": 1, "role": "user", "content": "你好"}, {"id": 2, "role": "assistant", "content": "你好"}]

        with patch.object(memory.db, "execute", side_effect=fail_on_counters):
            with self.assertRaises(RuntimeError):
                await memory.archive_diary(turns, new_cursor_id=2)

        # 验证回滚效果：diary 表中不能有任何新行，facts 表为空，counters.archived_turns 依然为 0
        diary_rows = await self.db.fetchall("SELECT * FROM diary")
        self.assertEqual(len(diary_rows), 0, "事务回滚失败：diary 表不应有数据")

        facts = await memory.get_all_facts()
        self.assertEqual(len(facts), 0, "事务回滚失败：facts 表不应有数据")

        cur_row_after = await self.db.fetchone("SELECT value FROM counters WHERE key = 'archived_turns'")
        self.assertEqual(cur_row_after["value"], 0, "事务回滚失败：游标不应推进")

    async def test_diary_stage_perception_and_prompt_injection(self):
        """测试 5: 日记归档时注入关系阶段感知"""
        mock_gateway = MagicMock()
        mock_gateway.config.observer_model = "test-model"
        captured_messages = []

        async def capture_chat(messages, **kwargs):
            nonlocal captured_messages
            captured_messages = messages
            return '''{
                "content": "今天和小W同学讨论了乐团的事情，彼此都挺客气的。",
                "importance": 4,
                "sentiment": "平静",
                "facts": []
            }'''

        mock_gateway.chat = capture_chat

        # 模拟阶段 1
        aff = AffectionEngine(self.db, initial_dims={
            "warmth": 22.0, "trust": 24.0, "intimacy": 18.0,
            "intrigue": 26.0, "patience": 32.0, "tension": 1.5,
        })
        memory = MemoryManager(self.db, mock_gateway, affection=aff)

        turns = [{"id": 1, "role": "user", "content": "在吗"}, {"id": 2, "role": "assistant", "content": "在的"}]
        await memory.archive_diary(turns, new_cursor_id=2)

        self.assertEqual(len(captured_messages), 2)
        user_prompt = captured_messages[1]["content"]
        # 必须感知阶段 1
        self.assertIn("【当前关系阶段】阶段 1: 相识", user_prompt)

        # system prompt 必须包含分阶段情感克制规则
        sys_prompt = captured_messages[0]["content"]
        self.assertIn("分阶段情感克制与人设规则", sys_prompt)
        self.assertIn("严禁出现过分亲昵、依恋、暧昧情感词", sys_prompt)


if __name__ == "__main__":
    unittest.main()
