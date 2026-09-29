"""计费升级验收测试：峰谷时段判定、缓存命中计费、旧库迁移"""

import os
import unittest
from datetime import datetime

from companion.config import LLMConfig, PricingConfig, BEIJING_TZ
from companion.db import Database
from companion.gateway import LLMGateway

BJ = BEIJING_TZ


def _bj(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=BJ)


class TestPeakHours(unittest.TestCase):
    """峰谷时段判定（北京时间，周一至周五 9-12 / 14-18 为高峰）"""

    def test_weekday_peak_morning(self):
        # 2026-09-29 是周二，10:00 属于 9-12 高峰
        self.assertTrue(PricingConfig().is_peak(_bj(2026, 9, 29, 10)))

    def test_weekday_peak_afternoon(self):
        # 周二 15:00 属于 14-18 高峰
        self.assertTrue(PricingConfig().is_peak(_bj(2026, 9, 29, 15)))

    def test_weekday_offpeak_lunch(self):
        # 周二 12:30 不在高峰区间
        self.assertFalse(PricingConfig().is_peak(_bj(2026, 9, 29, 12, 30)))

    def test_weekday_offpeak_night(self):
        # 周二 20:00 空闲
        self.assertFalse(PricingConfig().is_peak(_bj(2026, 9, 29, 20)))

    def test_weekend_always_offpeak(self):
        # 2026-09-26 是周六，即使 10:00 也是空闲
        self.assertFalse(PricingConfig().is_peak(_bj(2026, 9, 26, 10)))

    def test_holiday_always_offpeak(self):
        # 周二但在法定节假日列表里 → 空闲
        pricing = PricingConfig(holidays=["2026-09-29"])
        self.assertFalse(pricing.is_peak(_bj(2026, 9, 29, 10)))


class TestCostEstimate(unittest.TestCase):
    """缓存命中 + 峰谷的混合计费数值"""

    def setUp(self):
        self.gateway = LLMGateway(LLMConfig())

    def test_peak_mixed_cost(self):
        # 高峰：50万命中×0.04 + 50万未命中×2.0 + 10万输出×8.0 = 0.02+1.0+0.8 = 1.82 元
        cost = self.gateway._estimate_cost(
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            cache_hit_tokens=500_000,
            cache_miss_tokens=500_000,
            at=_bj(2026, 9, 29, 10),
        )
        self.assertAlmostEqual(cost, 1.82, places=6)

    def test_offpeak_half_price(self):
        # 空闲（周六同时刻）：全部半价 = 0.91 元
        cost = self.gateway._estimate_cost(
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            cache_hit_tokens=500_000,
            cache_miss_tokens=500_000,
            at=_bj(2026, 9, 26, 10),
        )
        self.assertAlmostEqual(cost, 0.91, places=6)

    def test_missing_cache_fields_all_miss(self):
        # 缓存字段缺失 → 全部按未命中：高峰 100万×2.0 = 2.0 元
        cost = self.gateway._estimate_cost(
            prompt_tokens=1_000_000,
            completion_tokens=0,
            at=_bj(2026, 9, 29, 10),
        )
        self.assertAlmostEqual(cost, 2.0, places=6)

    def test_pro_model_rate(self):
        # v4-pro 高峰：50万命中×0.30 + 50万未命中×9.0 + 10万输出×27 = 0.15+4.5+2.7 = 7.35 元
        cost = self.gateway._estimate_cost(
            prompt_tokens=1_000_000,
            completion_tokens=100_000,
            cache_hit_tokens=500_000,
            cache_miss_tokens=500_000,
            at=_bj(2026, 9, 29, 10),
            model="deepseek-v4-pro",
        )
        self.assertAlmostEqual(cost, 7.35, places=6)

    def test_unknown_model_falls_back_to_flash(self):
        # 未知模型回退 Flash 价：高峰 100万未命中×2.0 = 2.0 元
        cost = self.gateway._estimate_cost(
            prompt_tokens=1_000_000,
            completion_tokens=0,
            at=_bj(2026, 9, 29, 10),
            model="some-future-model",
        )
        self.assertAlmostEqual(cost, 2.0, places=6)


class TestOldDbMigration(unittest.IsolatedAsyncioTestCase):
    """旧库（无缓存两列的 llm_calls 表）迁移后不报错、新列可用"""

    async def asyncSetUp(self):
        self.db_path = f"data/test_m8_{id(self)}.db"
        self.db = Database(self.db_path)
        # 先手工建一个旧版 llm_calls 表
        await self.db.execute(
            """
            CREATE TABLE llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose TEXT, model TEXT, prompt_tokens INTEGER,
                completion_tokens INTEGER, cost_estimate REAL, created_at TEXT
            )
            """
        )

    async def asyncTearDown(self):
        await self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    async def test_migration_adds_columns(self):
        await self.db.init_tables()  # 触发迁移
        gateway = LLMGateway(LLMConfig(), db=self.db)
        await gateway._log_call("test", "deepseek-flash", 1000, 100, 800, 200)
        row = await self.db.fetchone(
            "SELECT cache_hit_tokens, cache_miss_tokens, cost_estimate FROM llm_calls"
        )
        self.assertIsNotNone(row)
        self.assertEqual(row["cache_hit_tokens"], 800)
        self.assertEqual(row["cache_miss_tokens"], 200)
        self.assertGreater(row["cost_estimate"], 0.0)


if __name__ == "__main__":
    unittest.main()
