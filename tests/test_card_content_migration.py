"""角色卡内容归位测试 (tests/test_card_content_migration.py)

第三轮外部审计任务 B：**"人设与代码彻底分离"被证伪的修复**。

原先三处卡内容硬编码在公开代码里（长假文案、校历锚点、生活主线素材池），
现全部迁入角色卡 JSON 的三个可选字段：

    long_holiday_activity   长假当天的活动文案（不填 → 通用默认）
    calendar_anchors        日历/校历锚点 [[起, 止, 一句话], ...]（不填 → 无锚点功能）
    life_arc_seed_pool      生活主线取材范围（字符串或字符串数组；空 → 主线生成整条跳过）

本文件钉四件事：
1. 三个字段的读取与兜底（含畸形数据不炸、不静默变垃圾）；
2. **行为兼容**：卡里填了什么，引擎就逐字用什么（长假文案 / 锚点 / 素材池），
   夹具卡提供内容、断言对着卡取值——换卡不改测试；
3. 公开模板卡 characters/example 的示例值本身是通用虚构且全年无缺口；
4. 公开代码自检：companion/ 下不再出现私有卡内容，模块也不再导出旧常量。

全本地、零真实网络与真实 API。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

from companion import persona as persona_module  # noqa: E402
from companion import prompts as prompts_module  # noqa: E402
from companion.arcs import LifeArcManager  # noqa: E402
from companion.db import TIME_FORMAT  # noqa: E402
from companion.persona import (  # noqa: E402
    DEFAULT_LONG_HOLIDAY_ACTIVITY,
    Persona,
    parse_calendar_anchors,
    parse_life_arc_seed_pool,
)
from helpers import (  # noqa: E402
    FIXTURE_CALENDAR_ANCHORS,
    FIXTURE_LIFE_ARC_SEED_POOL,
    FIXTURE_LONG_HOLIDAY_ACTIVITY,
    close_db,
    make_db,
    make_fixture_card,
)

EXAMPLE_CARD = os.path.join(_REPO_ROOT, "characters", "example")
COMPANION_DIR = os.path.join(_REPO_ROOT, "companion")

# 私有卡（或者任何具体卡）独有的地名：公开代码与公开模板卡里一个都不许出现。
# 注意：只查"地名"这类卡内容，不查文档里的历史说明（docs/ 不在本测试范围内）。
PRIVATE_PLACE_WORDS = (
    "绍兴", "浙大", "紫金港", "玉泉", "文琴", "蒙民伟", "启真湖", "银泉", "临湖", "丹青",
)


class _CardCase(unittest.TestCase):
    def make_card(self, **kwargs) -> str:
        d = tempfile.mkdtemp(prefix="qqc_card_migration_")
        self.addCleanup(shutil.rmtree, d, True)
        make_fixture_card(d, **kwargs)
        return d


# ==========================================
# 1. 三个可选字段的读取与兜底
# ==========================================


class TestOptionalFieldParsing(_CardCase):
    def test_卡里没这三个字段时落到功能关掉的默认值(self):
        persona = Persona.load(self.make_card())
        self.assertEqual(persona.long_holiday_activity, DEFAULT_LONG_HOLIDAY_ACTIVITY)
        self.assertEqual(persona.calendar_anchors, [])
        self.assertEqual(persona.life_arc_seed_pool, "")

    def test_卡里填了就逐字用卡里的(self):
        persona = Persona.load(
            self.make_card(
                long_holiday_activity=FIXTURE_LONG_HOLIDAY_ACTIVITY,
                calendar_anchors=FIXTURE_CALENDAR_ANCHORS,
                life_arc_seed_pool=FIXTURE_LIFE_ARC_SEED_POOL,
            )
        )
        self.assertEqual(persona.long_holiday_activity, FIXTURE_LONG_HOLIDAY_ACTIVITY)
        self.assertEqual(
            persona.calendar_anchors,
            [tuple(a) for a in FIXTURE_CALENDAR_ANCHORS],
        )
        self.assertEqual(persona.life_arc_seed_pool, "\n".join(FIXTURE_LIFE_ARC_SEED_POOL))

    def test_长假文案取自卡里_不填才用默认(self):
        """行为兼容的核心断言：填了就用卡里的，空/缺省才回落到通用默认。"""
        with_card = Persona.load(
            self.make_card(long_holiday_activity=FIXTURE_LONG_HOLIDAY_ACTIVITY)
        )
        self.assertEqual(
            with_card.get_current_activity(10, 3, holiday_span=8), FIXTURE_LONG_HOLIDAY_ACTIVITY
        )
        without = Persona.load(self.make_card())
        self.assertEqual(
            without.get_current_activity(10, 3, holiday_span=8), DEFAULT_LONG_HOLIDAY_ACTIVITY
        )

    def test_空字符串长假文案也回落默认(self):
        """卡里写了空串（手滑/占位没删）不该让看板与提示词出现空活动。"""
        persona = Persona.load(self.make_card(long_holiday_activity=""))
        self.assertEqual(persona.long_holiday_activity, DEFAULT_LONG_HOLIDAY_ACTIVITY)

    def test_素材池数组形态按行拼接(self):
        self.assertEqual(
            parse_life_arc_seed_pool(["第一行", "第二行"]), "第一行\n第二行"
        )
        self.assertEqual(parse_life_arc_seed_pool("整段文本"), "整段文本")

    def test_素材池含非字符串成员时逐条跳过(self):
        with self.assertLogs("companion.persona", level="WARNING"):
            parsed = parse_life_arc_seed_pool(["好的", 42, None, "也好"])
        self.assertEqual(parsed, "好的\n也好")

    def test_素材池类型不认识时按空池处理(self):
        with self.assertLogs("companion.persona", level="WARNING"):
            self.assertEqual(parse_life_arc_seed_pool({"a": 1}), "")
        self.assertEqual(parse_life_arc_seed_pool(None), "")

    def test_锚点畸形成员逐条跳过_好成员照常保留(self):
        raw = [
            ["01-07", "12-30", "好锚点"],
            ["01-07", "12-30"],                    # 不是三元组
            ["2026-01-07", "12-30", "格式不对"],     # 不是 MM-DD
            [1, 2, 3],                              # 字段不是字符串
            ["02-01", "02-28", "   "],              # 文本为空
            ["12-31", "01-06", "跨年锚点"],
        ]
        with self.assertLogs("companion.persona", level="WARNING") as cm:
            parsed = parse_calendar_anchors(raw)
        self.assertEqual(parsed, [("01-07", "12-30", "好锚点"), ("12-31", "01-06", "跨年锚点")])
        self.assertGreaterEqual(len(cm.output), 4, "每条畸形都要留下告警，不许静默丢弃")

    def test_锚点整体类型异常不炸(self):
        with self.assertLogs("companion.persona", level="WARNING"):
            self.assertEqual(parse_calendar_anchors({"start": "01-01"}), [])
        self.assertEqual(parse_calendar_anchors(None), [])


# ==========================================
# 2. 生活主线：素材池/锚点来自卡，缺了就安全降级
# ==========================================


class TestArcsReadsCardFields(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.calls = []
        self.card_dirs = []

    async def asyncTearDown(self):
        await close_db(self.db)
        for d in self.card_dirs:
            shutil.rmtree(d, ignore_errors=True)

    def _card(self, **kwargs) -> Persona:
        d = tempfile.mkdtemp(prefix="qqc_arcs_card_")
        self.card_dirs.append(d)
        make_fixture_card(d, **kwargs)
        return Persona.load(d)

    def _gateway(self):
        gw = MagicMock()
        gw.config.observer_model = "flash"
        gw.chat = AsyncMock(return_value='{"arcs": []}')
        return gw

    async def _ensure(self, persona: Persona) -> int:
        return await LifeArcManager(self.db, self._gateway(), persona).ensure_arcs()

    def _d(self, offset: int) -> str:
        return (datetime.now() + timedelta(days=offset)).strftime("%Y-%m-%d")

    async def test_没有素材池时跳过生成且不调API(self):
        """安全降级：宁可这张卡没有生活主线，也不让模型凭空编卡外事实。"""
        gw = self._gateway()
        arcs = LifeArcManager(self.db, gw, self._card())
        added = await arcs.ensure_arcs()
        self.assertEqual(added, 0)
        gw.chat.assert_not_called()
        self.assertEqual(await arcs.count_active(), 0)

    async def test_有素材池没锚点时照常生成_锚点用缺失兜底(self):
        """卡里不配锚点 = 无锚点功能，但素材池还在，生成照跑（只在提示词里注明缺锚点）。"""
        gw = self._gateway()
        gw.chat = AsyncMock(
            return_value='{"arcs": [{"title":"测试主线","detail":"测试背景","key_date":"'
            + self._d(5)
            + '","emotional_stake":"有点紧张"}]}'
        )
        persona = self._card(life_arc_seed_pool=FIXTURE_LIFE_ARC_SEED_POOL)
        arcs = LifeArcManager(self.db, gw, persona)
        added = await arcs.ensure_arcs()
        self.assertEqual(added, 1)
        prompt = gw.chat.call_args.kwargs["messages"][0]["content"]
        self.assertIn("测试素材甲", prompt, "素材池必须来自角色卡")
        self.assertIn("锚点缺失", prompt, "无锚点时提示词要注明，而不是留个空标题")

    async def test_提示词里的锚点与素材池都取自当前卡(self):
        gw = self._gateway()
        persona = self._card(
            calendar_anchors=FIXTURE_CALENDAR_ANCHORS,
            life_arc_seed_pool=FIXTURE_LIFE_ARC_SEED_POOL,
        )
        arcs = LifeArcManager(self.db, gw, persona)
        await arcs._generate_raw()
        prompt = gw.chat.call_args.kwargs["messages"][0]["content"]
        self.assertIn("测试素材乙", prompt)
        # 夹具锚点两段覆盖全年，任何一天都至少能命中一段
        self.assertIn("眼下：", prompt)


# ==========================================
# 3. 公开模板卡 characters/example 的示例值
# ==========================================


class TestExampleCardSample(unittest.TestCase):
    def setUp(self):
        self.persona = Persona.load(EXAMPLE_CARD)

    def test_三个可选字段都给了示例值(self):
        self.assertTrue(self.persona.long_holiday_activity.strip())
        self.assertTrue(self.persona.calendar_anchors, "示例卡应演示 calendar_anchors 怎么写")
        self.assertTrue(self.persona.life_arc_seed_pool.strip(), "示例卡应演示素材池怎么写")

    def test_示例值不含私有卡地名(self):
        blob = "\n".join(
            [
                self.persona.long_holiday_activity,
                self.persona.life_arc_seed_pool,
                *[f"{s}{e}{n}" for s, e, n in self.persona.calendar_anchors],
            ]
        )
        for word in PRIVATE_PLACE_WORDS:
            self.assertNotIn(word, blob, f"示例卡里出现了具体卡的地名 {word}（应换成通用虚构内容）")

    def test_示例锚点全年无缺口_含闰年(self):
        missing = []
        for year, days in ((2026, 365), (2028, 366)):
            cur = datetime(year, 1, 1)
            for i in range(days):
                d = (cur + timedelta(days=i)).strftime("%Y-%m-%d")
                if not self.persona.calendar_anchor_note(d):
                    missing.append(d)
        self.assertEqual(missing, [], f"示例卡的日历锚点有缺口: {missing[:10]}")

    def test_示例卡不填字段时也能加载(self):
        """公开 clone 的用户把三个字段删掉，也不能炸（功能关掉即可）。"""
        tmp = tempfile.mkdtemp(prefix="qqc_example_strip_")
        self.addCleanup(shutil.rmtree, tmp, True)
        make_fixture_card(tmp)
        persona = Persona.load(tmp)
        self.assertEqual(persona.long_holiday_activity, DEFAULT_LONG_HOLIDAY_ACTIVITY)
        self.assertEqual(persona.calendar_anchors, [])


# ==========================================
# 4. 公开代码自检：companion/ 里不再有硬编码卡内容
# ==========================================


def _companion_sources():
    out = {}
    for root, _dirs, files in os.walk(COMPANION_DIR):
        if "__pycache__" in root:
            continue
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(root, name)
                with open(path, "r", encoding="utf-8") as f:
                    out[path] = f.read()
    return out


class TestNoHardcodedCardContent(unittest.TestCase):
    def test_companion里没有具体卡的地名(self):
        """任务 B 的自检口径：`grep -rn "绍兴\\|浙大" companion/` 必须清零。"""
        hits = []
        for path, text in _companion_sources().items():
            for word in PRIVATE_PLACE_WORDS:
                if word in text:
                    hits.append(f"{os.path.relpath(path, _REPO_ROOT)}: {word}")
        self.assertEqual(hits, [], f"companion/ 里仍有具体卡内容: {hits}")

    def test_旧常量不再从代码导出(self):
        self.assertFalse(hasattr(persona_module, "LONG_HOLIDAY_ACTIVITY"))
        self.assertFalse(hasattr(persona_module, "ZJU_CALENDAR_ANCHORS"))
        self.assertFalse(hasattr(prompts_module, "LIFE_ARC_SEED_POOL"))

    def test_默认长假文案本身不含具体卡信息(self):
        self.assertTrue(DEFAULT_LONG_HOLIDAY_ACTIVITY)
        for word in PRIVATE_PLACE_WORDS:
            self.assertNotIn(word, DEFAULT_LONG_HOLIDAY_ACTIVITY)


if __name__ == "__main__":
    unittest.main()
