"""角色卡自带好感度节奏（affection 段）：开局阶段、门槛、衰减、阶段地板"""

import unittest
from datetime import datetime, timedelta

from companion.affection import DECAY_RATES, STAGE_THRESHOLDS, AffectionEngine, determine_stage
from companion.persona import AffectionSettings, parse_affection_settings
from helpers import close_db, make_db


class _P:
    """最小 persona：只带 from_persona 用到的字段"""

    def __init__(self, affection=None, stage_max_drop=None):
        self.initial_dims = {"warmth": 40.0, "trust": 50.0, "intimacy": 35.0,
                             "intrigue": 30.0, "patience": 50.0, "tension": 3.0}
        self.affection = affection or AffectionSettings()
        self.stage_max_drop = stage_max_drop


class TestParse(unittest.TestCase):
    def test_合法值照收(self):
        s = parse_affection_settings({"start_stage": 5, "stage_thresholds": [0, 10, 20, 30, 40, 50, 60, 70, 80, 99],
                                      "decay_per_day": {"intrigue": 0.2}})
        self.assertEqual(s.start_stage, 5)
        self.assertEqual(s.stage_thresholds[-1], 99.0)
        self.assertEqual(s.decay_per_day, {"intrigue": 0.2})

    def test_不合法的单项丢掉_其余照用(self):
        with self.assertLogs("companion.persona", level="WARNING"):
            s = parse_affection_settings({"start_stage": 12, "stage_thresholds": [0, 5, 3],
                                          "decay_per_day": {"intrigue": 0.2, "瞎写": 1, "trust": -1}})
        self.assertIsNone(s.start_stage)
        self.assertIsNone(s.stage_thresholds)
        self.assertEqual(s.decay_per_day, {"intrigue": 0.2})

    def test_不填就是空设置(self):
        self.assertEqual(parse_affection_settings(None), AffectionSettings())


class TestEngine(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.now = datetime(2026, 10, 20, 12, 0)

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_旧卡不填时与默认标定完全一致(self):
        eng = AffectionEngine.from_persona(self.db, _P())
        self.assertEqual(eng.thresholds, list(STAGE_THRESHOLDS))
        self.assertEqual(eng.decay_rates, DECAY_RATES)
        st = await eng.get_state()
        self.assertEqual(st["dims"]["warmth"], 40.0)

    async def test_开局阶段_微酸开局且历史最高也记为微酸(self):
        eng = AffectionEngine.from_persona(self.db, _P(AffectionSettings(start_stage=5)))
        st = await eng.get_state()
        self.assertEqual((st["stage"], st["max_stage"]), (5, 5))

    async def test_卡自带门槛与衰减生效(self):
        th = [0, 10, 20, 30, 40, 50, 60, 70, 80, 99]
        eng = AffectionEngine.from_persona(
            self.db, _P(AffectionSettings(start_stage=3, stage_thresholds=th, decay_per_day={"intrigue": 0.0})))
        st = await eng.get_state()
        self.assertEqual(st["stage"], 3)
        self.assertAlmostEqual(st["composite"], 35.0, places=1)
        self.assertEqual(eng.decay_rates["intrigue"], 0.0)
        self.assertEqual(eng.decay_rates["warmth"], DECAY_RATES["warmth"])
        self.assertEqual(determine_stage(35.0, th), 3)

    async def test_阶段地板_一个月不聊最多退一阶(self):
        clock = {"t": self.now}
        eng = AffectionEngine.from_persona(self.db, _P(AffectionSettings(start_stage=5), stage_max_drop=1),
                                           now_fn=lambda: clock["t"])
        await eng.get_state()
        clock["t"] = self.now + timedelta(days=60)
        st, _ = await eng.update(4.0, 4.0, 4.0, 4.0, [])
        self.assertGreaterEqual(st["stage"], 4)


if __name__ == "__main__":
    unittest.main()
