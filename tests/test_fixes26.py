"""FIXES26：真实日衰减下的好感度节奏重标定 + 阶段迟滞/地板 + 冷落按时间计。

1. 节奏：基准观察者分在真实 decay_anchor 衰减下命中 FIXES8 目标（±25%），一年内不掉阶，相守不可达；
2. 好奇维不再因衰减过快中途掉头；六维不再被 round(,2) 舍入卡死；
3. resolve_stage：降阶迟滞、角色卡 stage_max_drop 地板、历史最高阶段持久化、里程碑不重复写；
4. 冷落：v / a / frustration 只取决于经过的时间，与 update_mood 被调多频繁无关。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
import unittest
from typing import Dict, Optional
from unittest.mock import patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "sim"))

import score_simulation as S  # noqa: E402

from companion import affection as A  # noqa: E402
from companion.affection import (  # noqa: E402
    STAGE_HYSTERESIS,
    STAGE_THRESHOLDS,
    AffectionEngine,
    determine_stage,
    resolve_stage,
)
from companion.db import STATE_KEY_AFFECTION, TIME_FORMAT, Database  # noqa: E402
from companion.mood import MoodEngine, neglect_level  # noqa: E402
from companion.persona import Persona, parse_stage_max_drop  # noqa: E402

START = datetime.datetime(2026, 3, 1, 20, 0)


class _Clock:
    def __init__(self, t: datetime.datetime = START):
        self.now = t

    def __call__(self) -> datetime.datetime:
        return self.now


# ======================================================================
# 1. 节奏标定（真实引擎 + 假时钟）
# ======================================================================

class TestPacingUnderRealDecay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = asyncio.run(S.run_affection(days=365))

    def test_各阶段到达天数命中目标容差(self):
        arr = self.base["arrivals"]
        for stage, target in S.TARGET_DAYS.items():
            with self.subTest(stage=stage):
                self.assertIn(stage, arr, f"阶段 {stage} 一年内没到")
                self.assertGreaterEqual(arr[stage], target * 0.75)
                self.assertLessEqual(arr[stage], target * 1.25)

    def test_持续聊天一年不掉阶(self):
        self.assertEqual(self.base["drops"], [], "基准节奏下到达过的阶段又掉回去了")

    def test_相守一年内不可达(self):
        self.assertNotIn(9, self.base["arrivals"])
        self.assertLess(self.base["max_composite"], STAGE_THRESHOLDS[9] - 3.0)

    def test_好奇维不再中途掉头(self):
        """旧 intrigue 衰减 2.0/天：一年后好奇被磨到 ~2，复合分随之掉阶。"""
        self.assertGreater(self.base["final"]["dims"]["intrigue"], 80.0)

    def test_顶部门槛不再挤成一团(self):
        gaps = [STAGE_THRESHOLDS[i + 1] - STAGE_THRESHOLDS[i] for i in range(5, 9)]
        self.assertGreaterEqual(min(gaps), 2.0, f"高阶段门槛间隔过窄: {gaps}")
        # 迟滞必须小于最窄的阶段间隔，否则降阶判断失去意义
        narrowest = min(STAGE_THRESHOLDS[i + 1] - STAGE_THRESHOLDS[i] for i in range(1, 9))
        self.assertLess(STAGE_HYSTERESIS, narrowest)

    def test_高分段微小增量不被舍入吃掉(self):
        """阻力很大时单轮增量 < 0.005，旧 round(,2) 会让六维纹丝不动。"""
        async def run():
            db = Database(":memory:")
            await db.init_tables()
            clock = _Clock()
            dims = {"warmth": 90.0, "trust": 90.0, "intimacy": 90.0,
                    "intrigue": 90.0, "patience": 90.0, "tension": 0.0}
            eng = AffectionEngine(db, dims, now_fn=clock)
            await eng.get_state()
            st, _ = await eng.update(5.0, 5.5, 6.0, 5.5, [])
            await db.close()
            return st
        st = asyncio.run(run())
        self.assertGreater(st["dims"]["patience"], 90.0)


# ======================================================================
# 2. 阶段迟滞 / 地板
# ======================================================================

class TestResolveStage(unittest.TestCase):
    def test_无上一阶段时等于determine_stage(self):
        for c in (0.0, 20.0, 50.0, 80.0, 95.0):
            self.assertEqual(resolve_stage(c, 0, 0, None), determine_stage(c))

    def test_门槛下方迟滞带内保持旧阶段(self):
        th7 = STAGE_THRESHOLDS[7]
        self.assertEqual(resolve_stage(th7 - STAGE_HYSTERESIS + 0.1, 7, 7), 7)
        self.assertEqual(resolve_stage(th7 - STAGE_HYSTERESIS - 0.1, 7, 7), 6)

    def test_迟滞不会让阶段上涨(self):
        th7 = STAGE_THRESHOLDS[7]
        self.assertEqual(resolve_stage(th7 - 0.1, 6, 6), 6)

    def test_地板限制最多比历史最高低几阶(self):
        self.assertEqual(resolve_stage(16.0, 8, 8, stage_max_drop=1), 7)
        self.assertEqual(resolve_stage(16.0, 8, 8, stage_max_drop=2), 6)
        self.assertEqual(resolve_stage(16.0, 8, 8, stage_max_drop=None), 1)
        self.assertEqual(resolve_stage(16.0, 8, 8, stage_max_drop=0), 8)


class TestEngineStageFloor(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = Database(":memory:")
        await self.db.init_tables()

    async def asyncTearDown(self):
        await self.db.close()

    async def _seed(self, comp: float, stage: int, max_stage: Optional[int], days_ago: int) -> None:
        then = (START - datetime.timedelta(days=days_ago)).strftime(TIME_FORMAT)
        dims = {k: comp for k in ("warmth", "trust", "intimacy", "intrigue", "patience")}
        dims["tension"] = 0.0
        st = {"dims": dims, "composite": comp, "stage": stage,
              "last_updated": then, "decay_anchor": then}
        if max_stage is not None:
            st["max_stage"] = max_stage
        await self.db.set_state_json(STATE_KEY_AFFECTION, st)

    async def _one_turn_after_month(self, stage_max_drop: Optional[int], max_stage: Optional[int] = 8):
        await self._seed(94.0, 8, max_stage, days_ago=30)
        eng = AffectionEngine(self.db, stage_max_drop=stage_max_drop, now_fn=_Clock())
        st, _ = await eng.update(5.0, 5.5, 6.0, 5.5, [])
        return st

    async def test_无地板时一个月不聊掉两阶(self):
        st = await self._one_turn_after_month(None)
        self.assertLessEqual(st["stage"], 6)
        self.assertEqual(st["max_stage"], 8, "历史最高阶段必须保留")

    async def test_地板1时最多掉一阶(self):
        st = await self._one_turn_after_month(1)
        self.assertEqual(st["stage"], 7)
        # 地板只管阶段标签，复合分照常衰减
        self.assertLess(st["composite"], STAGE_THRESHOLDS[7])

    async def test_旧档没有max_stage按当前阶段兜底(self):
        st = await self._one_turn_after_month(1, max_stage=None)
        self.assertEqual(st["stage"], 7)
        self.assertEqual(st["max_stage"], 8)

    async def test_降了又回来不重复写里程碑(self):
        clock = _Clock()
        th = STAGE_THRESHOLDS[3]
        await self._seed(th + 0.5, 3, 3, days_ago=0)
        eng = AffectionEngine(self.db, now_fn=clock)
        # 硬伤害把复合分打到迟滞带以下 -> 降阶
        st = await eng.get_state()
        st["dims"]["tension"] = 30.0
        await eng.save_state(st)
        st, _ = await eng.update(5.0, 5.5, 6.0, 5.5, [])
        self.assertLess(st["stage"], 3)
        # 修复关系，复合分回到门槛之上 -> 阶段回来，但不是历史新高
        st["dims"]["tension"] = 0.0
        await eng.save_state(st)
        st, _ = await eng.update(5.0, 5.5, 6.0, 5.5, [])
        self.assertEqual(st["stage"], 3)
        rows = await self.db.fetchall("SELECT stage FROM milestones")
        self.assertEqual(rows, [], "回到已到达过的阶段不该再写里程碑")

    async def test_新初始化状态带max_stage(self):
        eng = AffectionEngine(self.db, now_fn=_Clock())
        st = await eng.get_state()
        self.assertEqual(st["max_stage"], st["stage"])


class TestCardStageMaxDrop(unittest.TestCase):
    def test_解析(self):
        self.assertIsNone(parse_stage_max_drop(None))
        self.assertEqual(parse_stage_max_drop(1), 1)
        self.assertEqual(parse_stage_max_drop(0), 0)
        for bad in (-1, 10, "1", 1.5, True):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_stage_max_drop(bad))

    def test_角色卡加载(self):
        src = os.path.join(_REPO_ROOT, "characters", "example", "character.json")
        with open(src, encoding="utf-8") as f:
            data = json.load(f)
        d = tempfile.mkdtemp(prefix="qqc_fixes26_")
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "character.json")
        data.pop("stage_max_drop", None)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.assertIsNone(Persona.load(d).stage_max_drop, "缺字段 = 不设地板（旧行为）")
        data["stage_max_drop"] = 1
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        self.assertEqual(Persona.load(d).stage_max_drop, 1)


# ======================================================================
# 3. 冷落按时间计（与调用频率无关）
# ======================================================================

async def _neglect_after(hours: float, every: float, composite: float = 25.0) -> Dict[str, float]:
    db = Database(":memory:")
    await db.init_tables()
    clock = _Clock(START)
    await db.execute("INSERT INTO turns (role, content, created_at) VALUES ('user', '晚安', ?)",
                     (START.strftime(TIME_FORMAT),))
    mood = MoodEngine(db, now_fn=clock)
    await mood.get_state()
    st = None
    n = int(round(hours / every))
    with patch("companion.mood.random.gauss", return_value=0.0):
        for i in range(1, n + 1):
            clock.now = START + datetime.timedelta(hours=i * every)
            st = await mood.update_mood(composite_affection=composite)
    await db.close()
    return st


class TestNeglectIsTimeBased(unittest.IsolatedAsyncioTestCase):
    async def test_48小时冷落与调用频率无关(self):
        res = {every: await _neglect_after(48.0, every) for every in (0.5, 1.0, 6.0, 24.0)}
        vs = [r["v"] for r in res.values()]
        frus = [r["frustration"] for r in res.values()]
        # 旧实现：1h 调用 v=-10 / frustration=10，24h 调用 v≈-0.5 / frustration≈1
        self.assertLess(max(vs) - min(vs), 0.8, f"v 随调用频率变形: {res}")
        self.assertLess(max(frus) - min(frus), 0.05, f"frustration 随调用频率变形: {res}")
        for every, r in res.items():
            with self.subTest(every=every):
                self.assertTrue(-3.0 <= r["v"] <= -0.5, f"48h v 偏离设计区间: {r}")
                self.assertTrue(1.8 <= r["frustration"] <= 2.8, f"48h frustration 偏离设计: {r}")
                self.assertTrue(-2.0 <= r["a"] <= -0.3, f"48h a 偏离设计: {r}")

    async def test_长时间冷落是平台不是深渊(self):
        r1 = await _neglect_after(96.0, 1.0)
        r24 = await _neglect_after(96.0, 24.0)
        self.assertGreater(r1["v"], -4.0)
        self.assertAlmostEqual(r1["v"], r24["v"], delta=0.8)
        self.assertEqual(r1["frustration"], 10.0)

    async def test_冷落前12小时不罚(self):
        r = await _neglect_after(12.0, 1.0)
        self.assertEqual(r["frustration"], 0.0)
        self.assertEqual(neglect_level(12.0), 0.0)
        self.assertGreater(r["v"], 2.0)

    async def test_冷落中不足1小时的调用不被放大成1小时(self):
        """冷落中每 10 分钟调一次，不应比每小时调一次回落得更快（旧实现按 1h 计，快 6 倍）。"""
        r10m = await _neglect_after(30.0, 1.0 / 6.0)
        r1h = await _neglect_after(30.0, 1.0)
        self.assertAlmostEqual(r10m["v"], r1h["v"], delta=0.4)
        self.assertAlmostEqual(r10m["frustration"], r1h["frustration"], delta=0.05)


if __name__ == "__main__":
    unittest.main()
