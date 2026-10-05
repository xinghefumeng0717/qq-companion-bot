"""FIXES16 迭代测试集 (tests/test_fixes16.py)

按任务书 §任务6 分组：
1. 建表平滑升级：全新库建表 / 老库无此表不炸 / 列齐全
2. 学期锚点：当前锚点 + 下一锚点、跨年区间、全年无缺口、脏日期不炸
3. 状态机：upcoming→near→today→resolved→faded 全链 + 各步幂等
4. 生成器：活跃上限、Jaccard 去重、key_date 越界丢弃、生成节流、LLM 失败静默
5. 注入区块：无主线整块省略 / 只有 resolved / 混合 / upcoming 不注入 / assembler 两态
6. 事件通道：置位、24h 窗口、日上限、回滚、免打扰等待、抢在决策层之前、计入未回复闸门
7. reset 清档
8. 零耦合红线：整条链跑完 mood/affection 一个字节都没动

时间敏感用例一律用 _FrozenDatetime 把 arcs 模块里的"现在"钉死，
不真等、不依赖跑测试的钟点。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

from companion.arcs import (
    ACTIVE_STATUSES,
    DEDUP_THRESHOLD,
    EVENT_DAILY_CAP,
    FADE_AFTER_DAYS,
    GENERATE_COOLDOWN_MINUTES,
    MAX_ACTIVE_ARCS,
    MIN_ACTIVE_ARCS,
    RESOLUTION_HOUR,
    LifeArcManager,
    _char_jaccard,
    _relative_day_label,
)
from companion.assembler import PromptAssembler
from companion.backup import DailyBackupScheduler
from companion.config import ProactiveConfig
from companion.db import TIME_FORMAT, Database
from companion.persona import (
    Persona,
    _anchor_contains,
    calendar_anchor_note,
)
from companion.proactive import ProactiveScheduler
from companion.reset import reset_database
from helpers import (
    FIXTURE_CALENDAR_ANCHORS,
    FIXTURE_LIFE_ARC_SEED_POOL,
    close_db,
    make_db,
    make_engine_stack,
    make_fixture_card,
)


# ==========================================
# 公共脚手架
# ==========================================


class _FrozenDatetime(datetime):
    """把 arcs 模块里的 datetime.now() 钉死在一个时刻上。

    直接继承 datetime 并覆盖 now()，这样 strptime/date() 等行为完全不变。
    """

    _frozen: datetime = datetime(2026, 10, 4, 12, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - 与 datetime.now 签名对齐
        return cls._frozen

    @classmethod
    def set(cls, value: datetime) -> None:
        cls._frozen = value


def _arc_json(arcs: List[Dict[str, str]]) -> str:
    return json.dumps({"arcs": arcs}, ensure_ascii=False)


def _arc(
    title: str,
    key_date: str,
    detail: str = "背景",
    stake: str = "有点紧张",
    status: str = "upcoming",
) -> Dict[str, str]:
    return {
        "title": title,
        "detail": detail,
        "key_date": key_date,
        "emotional_stake": stake,
    }


def _d(offset_days: int, base: Optional[datetime] = None) -> str:
    ref = base or datetime(2026, 10, 4, 12, 0)
    return (ref + timedelta(days=offset_days)).strftime("%Y-%m-%d")


class ArcsTestBase(unittest.IsolatedAsyncioTestCase):
    """带临时库 + 假 gateway 的基类。

    角色卡用**夹具卡**（自带 FIXTURE_CALENDAR_ANCHORS + FIXTURE_LIFE_ARC_SEED_POOL）：
    生活主线的锚点与素材池都来自角色卡，用夹具卡才能既不依赖私有卡、也不把
    卡内容写进代码里。
    """

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.gateway = MagicMock()
        self.gateway.config.observer_model = "flash"
        self.gateway.config.text_model = "pro"
        self.gateway.chat = AsyncMock(return_value=_arc_json([_arc("占位", _d(7))]))
        self.card_dir = tempfile.mkdtemp(prefix="qqc_arcs_card_")
        self.addCleanup(shutil.rmtree, self.card_dir, True)
        make_fixture_card(
            self.card_dir,
            calendar_anchors=FIXTURE_CALENDAR_ANCHORS,
            life_arc_seed_pool=FIXTURE_LIFE_ARC_SEED_POOL,
        )
        self.persona = Persona.load(self.card_dir)
        self.arcs = LifeArcManager(self.db, self.gateway, self.persona)
        self.calls: List[Dict[str, Any]] = []
        self.gateway.chat = AsyncMock(side_effect=self._record_call)

    async def _record_call(self, **kwargs):
        self.calls.append(kwargs)
        return _arc_json([_arc("占位", _d(7))])

    async def asyncTearDown(self):
        await close_db(self.db)

    def gen_calls(self) -> List[Dict[str, Any]]:
        return [c for c in self.calls if c.get("purpose") == "life_arc"]


# ==========================================
# 1. 建表平滑升级
# ==========================================


class TestTask1Schema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fixes16_schema_")
        self.db_path = os.path.join(self.tmp, "old.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_db_creates_life_arcs_with_all_columns(self):
        db = Database(self.db_path)
        asyncio.run(self._check(db))
        db2 = Database(self.db_path)
        asyncio.run(self._check(db2))

    async def _check(self, db: Database):
        await db.init_tables()
        cols = {
            r[1] for r in await db.fetchall("PRAGMA table_info(life_arcs)")
        }
        for col in (
            "id", "title", "detail", "key_date", "status",
            "emotional_stake", "resolution", "event_announced",
            "created_at", "resolved_at",
        ):
            self.assertIn(col, cols, f"life_arcs 缺列 {col}")
        await db.close()

    def test_legacy_db_without_life_arcs_upgrades_quietly(self):
        """老库平滑升级：库里没有 life_arcs，init_tables 不得炸，且补齐新表。"""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE turns (id INTEGER PRIMARY KEY, role TEXT, content TEXT, created_at TEXT);
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE counters (key TEXT PRIMARY KEY, value INTEGER);
            INSERT INTO turns (role, content) VALUES ('user', '老消息');
            """
        )
        conn.commit()
        conn.close()

        async def run():
            db = Database(self.db_path)
            await db.init_tables()
            tables = {r[0] for r in await db.fetchall("SELECT name FROM sqlite_master WHERE type='table'")}
            turns = await db.fetchall("SELECT content FROM turns")
            await db.close()
            return tables, [r["content"] for r in turns]

        tables, turns = asyncio.run(run())
        self.assertIn("life_arcs", tables)
        # 老数据一条不能少
        self.assertEqual(turns, ["老消息"])


# ==========================================
# 2. 学期节奏锚点
# ==========================================


class TestCalendarAnchors(unittest.TestCase):
    """锚点机制：锚点表来自角色卡，这里用夹具卡自带的 FIXTURE_CALENDAR_ANCHORS
    （两段覆盖全年，其中 12-31~01-06 是跨年区间）。"""

    def test_current_and_next_anchor(self):
        note = calendar_anchor_note("2026-12-30", FIXTURE_CALENDAR_ANCHORS, lookahead=1)
        self.assertIn("眼下", note)
        self.assertIn("接下来", note)
        # 12-30 落在 01-07~12-30（甲），下一段应是 12-31 起的跨年假期（乙）
        self.assertIn("测试锚点甲", note)
        self.assertIn("测试锚点乙", note)
        # lookahead 到了表尾就没有下一条，只剩"眼下"
        tail = calendar_anchor_note("2026-12-31", FIXTURE_CALENDAR_ANCHORS, lookahead=2)
        self.assertIn("眼下", tail)
        self.assertNotIn("接下来", tail)

    def test_year_wrap_anchor(self):
        # 12-31~01-06 跨年，两端都要能命中
        self.assertTrue(_anchor_contains("12-31", "01-06", "12-31"))
        self.assertTrue(_anchor_contains("12-31", "01-06", "01-03"))
        self.assertFalse(_anchor_contains("12-31", "01-06", "01-20"))
        self.assertIn("测试锚点乙", calendar_anchor_note("2026-12-31", FIXTURE_CALENDAR_ANCHORS))
        self.assertIn("测试锚点乙", calendar_anchor_note("2027-01-02", FIXTURE_CALENDAR_ANCHORS))

    def test_full_year_has_no_gap(self):
        """全年每一天都要能落到某个锚点上——查漏的唯一可靠办法。

        同时覆盖闰年（2028-02-29 这种多出来的一天最容易漏）。
        """
        missing = []
        for year, days in ((2026, 365), (2028, 366)):
            cur = datetime(year, 1, 1)
            for i in range(days):
                d = (cur + timedelta(days=i)).strftime("%Y-%m-%d")
                if not calendar_anchor_note(d, FIXTURE_CALENDAR_ANCHORS):
                    missing.append(d)
        self.assertEqual(missing, [], f"这些日期查不到锚点: {missing[:10]}")

    def test_bad_date_returns_empty_not_crash(self):
        self.assertEqual(calendar_anchor_note("", FIXTURE_CALENDAR_ANCHORS), "")
        self.assertEqual(calendar_anchor_note("not-a-date", FIXTURE_CALENDAR_ANCHORS), "")
        self.assertEqual(calendar_anchor_note(None, FIXTURE_CALENDAR_ANCHORS), "")

    def test_no_anchors_means_no_note(self):
        """卡里没有锚点（空表）= 无锚点功能：任何日期都返回空串，不炸。"""
        self.assertEqual(calendar_anchor_note("2026-12-30", []), "")
        self.assertEqual(calendar_anchor_note("2026-12-30", None), "")

    def test_fixture_anchors_well_formed(self):
        self.assertTrue(FIXTURE_CALENDAR_ANCHORS)
        for start, end, note in FIXTURE_CALENDAR_ANCHORS:
            self.assertRegex(start, r"^\d{2}-\d{2}$")
            self.assertRegex(end, r"^\d{2}-\d{2}$")
            self.assertTrue(note.strip())

    def test_persona_method_delegates_to_card_anchors(self):
        """Persona.calendar_anchor_note 用的是**这张卡**的锚点（卡里没锚点就是空串）。"""
        tmp = tempfile.mkdtemp(prefix="qqc_anchor_card_")
        self.addCleanup(shutil.rmtree, tmp, True)
        make_fixture_card(tmp, calendar_anchors=FIXTURE_CALENDAR_ANCHORS)
        persona = Persona.load(tmp)
        self.assertEqual(len(persona.calendar_anchors), len(FIXTURE_CALENDAR_ANCHORS))
        self.assertIn("测试锚点甲", persona.calendar_anchor_note("2026-12-30"))

        tmp2 = tempfile.mkdtemp(prefix="qqc_anchor_card2_")
        self.addCleanup(shutil.rmtree, tmp2, True)
        make_fixture_card(tmp2)
        self.assertEqual(Persona.load(tmp2).calendar_anchors, [])
        self.assertEqual(Persona.load(tmp2).calendar_anchor_note("2026-12-30"), "")


# ==========================================
# 3. 状态机
# ==========================================


class TestStateMachine(ArcsTestBase):
    async def test_full_chain_upcoming_near_today_resolved_faded(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 10, 12, 0))  # 距 key_date 5 天
            await self.arcs.insert_arc("乐团节目审查", "低音部没合齐", _d(5, datetime(2026, 10, 10, 12, 0)))
            self.assertEqual((await self.arcs.fetch_arcs())[0]["status"], "upcoming")

            # 距 2 天 → near
            _FrozenDatetime.set(datetime(2026, 10, 13, 12, 0))
            await self.arcs.advance_states()
            self.assertEqual((await self.arcs.fetch_arcs())[0]["status"], "near")

            # 当天但还没到 18 点：只转 today，不出结果
            _FrozenDatetime.set(datetime(2026, 10, 15, 12, 0))
            await self.arcs.advance_states()
            arc = (await self.arcs.fetch_arcs())[0]
            self.assertEqual(arc["status"], "today")
            self.assertEqual(arc["resolution"], "")
            self.assertEqual(len(self.gen_calls()), 0, "18 点前不该调 API")

            # 当天 18 点后 → resolved + 写 resolved_at
            self.gateway.chat = AsyncMock(side_effect=self._res_then_arc)
            _FrozenDatetime.set(datetime(2026, 10, 15, RESOLUTION_HOUR, 30))
            await self.arcs.advance_states()
            arc = (await self.arcs.fetch_arcs())[0]
            self.assertEqual(arc["status"], "resolved")
            self.assertEqual(arc["resolution"], "过了，就是低音部那段还有点糙。")
            self.assertTrue(arc["resolved_at"])

            # resolved 超过 2 天 → faded
            _FrozenDatetime.set(datetime(2026, 10, 18, 12, 0))
            await self.arcs.advance_states()
            self.assertEqual((await self.arcs.fetch_arcs())[0]["status"], "faded")

    async def _res_then_arc(self, **kwargs):
        self.calls.append(kwargs)
        return "过了，就是低音部那段还有点糙。"

    async def test_past_due_arc_goes_straight_to_today(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            # 昨天就该发生的事，不能卡在 upcoming
            await self.arcs.insert_arc("读书报告", "选题没定", "2026-10-03")
            await self.arcs.advance_states()
            self.assertEqual((await self.arcs.fetch_arcs())[0]["status"], "today")

    async def test_advance_is_idempotent(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            # 存进库时是 upcoming，但 2 天后就到了——第一次推进要把它推到 today
            await self.arcs.insert_arc("读书报告", "选题没定", _d(0))
            first = await self.arcs.advance_states()
            second = await self.arcs.advance_states()
            self.assertEqual(first, 1)
            self.assertEqual(second, 0, "第二次推进不该再产生迁移")
            self.assertEqual((await self.arcs.fetch_arcs())[0]["status"], "today")

    async def test_bad_key_date_row_does_not_break_advance(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            await self.db.execute(
                "INSERT INTO life_arcs (title, detail, key_date, created_at) VALUES (?,?,?,?)",
                ("脏数据", "key_date 根本不是日期", "not-a-date", datetime(2026, 10, 1).strftime(TIME_FORMAT)),
            )
            await self.arcs.insert_arc("正常主线", "背景", _d(5))
            # 不得抛异常，正常那条照样推进
            await self.arcs.advance_states()
            arcs = {a["title"]: a["status"] for a in await self.arcs.fetch_arcs()}
            self.assertEqual(arcs["脏数据"], "upcoming")
            self.assertEqual(arcs["正常主线"], "upcoming")

    async def test_resolution_failure_keeps_today_and_retries(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 19, 0))
            await self.arcs.insert_arc("体测", "800米怕跑不动", _d(0, datetime(2026, 10, 4, 19, 0)))
            self.gateway.chat = AsyncMock(side_effect=RuntimeError("API 挂了"))
            await self.arcs.advance_states()
            arc = (await self.arcs.fetch_arcs())[0]
            self.assertEqual(arc["status"], "today")
            self.assertEqual(arc["resolution"], "")

            # 下一轮 API 恢复 → 补上结果
            self.gateway.chat = AsyncMock(return_value="跑完了，六十刚过。")
            await self.arcs.advance_states()
            arc = (await self.arcs.fetch_arcs())[0]
            self.assertEqual(arc["status"], "resolved")
            self.assertEqual(arc["resolution"], "跑完了，六十刚过。")


# ==========================================
# 4. 生成器
# ==========================================


class TestGeneration(ArcsTestBase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))

    async def test_tops_up_to_min_and_saves_arcs(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            self.gateway.chat = AsyncMock(
                side_effect=lambda **kw: (
                    self.calls.append(kw),
                    _arc_json([_arc("读书报告选题", _d(5)), _arc("琴房抢位", _d(8))]),
                )[1]
            )
            added = await self.arcs.ensure_arcs()
            self.assertEqual(added, 2)
            self.assertEqual(await self.arcs.count_active(), 2)
            row = (await self.arcs.fetch_arcs())[0]
            self.assertEqual(row["status"], "upcoming")
            self.assertEqual(row["emotional_stake"], "有点紧张")

    async def test_max_active_discards_everything_without_calling_llm(self):
        """拍板决策 1：活跃 ≥3 条时本轮结果全部丢弃，且一个 API 都不该调。"""
        for i in range(MAX_ACTIVE_ARCS):
            await self.arcs.insert_arc(f"主线{i}", "背景", _d(5 + i))
        self.assertEqual(await self.arcs.count_active(), MAX_ACTIVE_ARCS)

        added = await self.arcs.ensure_arcs()
        self.assertEqual(added, 0)
        self.assertEqual(len(self.gen_calls()), 0, "达到上限时不该调 LLM")

    @patch("companion.arcs.datetime", _FrozenDatetime)
    async def test_at_most_max_active_even_if_model_returns_many(self):
        for i in range(MAX_ACTIVE_ARCS - 1):
            await self.arcs.insert_arc(f"已有{i}", "背景", _d(5 + i))
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (
                self.calls.append(kw),
                _arc_json([_arc(f"新{i}", _d(9 + i)) for i in range(5)]),
            )[1]
        )
        # 凌晨维护那一路会把线补到 3 条（拍板决策 1 的上界）
        added = await self.arcs.ensure_arcs(min_active=MAX_ACTIVE_ARCS)
        self.assertEqual(added, 1, "候选再多也只能补到上限")
        self.assertEqual(await self.arcs.count_active(), MAX_ACTIVE_ARCS)

    async def test_on_demand_topup_only_fires_below_two(self):
        """即时补的门槛是"不足 2 条"；已够 2 条时连 API 都不该调。"""
        for i in range(MIN_ACTIVE_ARCS):
            await self.arcs.insert_arc(f"已有{i}", "背景", _d(5 + i))
        self.assertEqual(await self.arcs.ensure_arcs(), 0)
        self.assertEqual(len(self.gen_calls()), 0)

    @patch("companion.arcs.datetime", _FrozenDatetime)
    async def test_jaccard_dedup_rejects_near_identical_arc(self):
        await self.arcs.insert_arc(
            "乐团节目审查",
            "下周三晚上彩排，低音部那段一直合不齐，她怕被指挥单独点名",
            _d(5),
        )
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (
                self.calls.append(kw),
                _arc_json([
                    {
                        "title": "乐团节目审查",
                        "detail": "下周三晚上彩排，低音部那段一直合不齐，她怕被指挥单独点名",
                        "key_date": _d(9),
                        "emotional_stake": "怕被点名",
                    }
                ]),
            )[1]
        )
        added = await self.arcs.ensure_arcs()
        self.assertEqual(added, 0, "与近 30 天主线几乎一字不差，应判重丢弃")
        self.assertEqual(await self.arcs.count_active(), 1)

    @patch("companion.arcs.datetime", _FrozenDatetime)
    async def test_jaccard_threshold_is_actually_used(self):
        """反向对照：相似度明显低的主题必须放行，证明闸门不是"一律拒绝"。"""
        await self.arcs.insert_arc("乐团节目审查", "下周三彩排，低音部没合齐", _d(5))
        sim = _char_jaccard(
            "期中小论文选题｜导师说选题方向可以，就是文献综述要再压一压",
            "乐团节目审查｜下周三彩排，低音部没合齐",
        )
        self.assertLess(sim, DEDUP_THRESHOLD)
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (
                self.calls.append(kw),
                _arc_json([{
                    "title": "期中小论文选题",
                    "detail": "导师说选题方向可以，就是文献综述要再压一压",
                    "key_date": _d(9),
                    "emotional_stake": "怕改不完",
                }]),
            )[1]
        )
        self.assertEqual(await self.arcs.ensure_arcs(), 1)

    async def test_key_date_out_of_range_is_dropped(self):
        cases = [
            (_d(1), "太近"),
            (_d(2), "刚好不够 3 天"),
            (_d(15), "太远"),
            ("2026-09-01", "已经过去"),
            ("garbage", "根本不是日期"),
        ]
        # 追评（2026-10-05）：本用例的 `_d()` 基准写死 2026-10-04，而 arcs 的有效区间
        # 是"真实今天 +3/+14"——不把 arcs 的钟钉死，用例只在 10-04 当天绿（10-05 起
        # _d(15)=10-19 落进 +14 区间，断言翻转）。钉死时钟以保住原意。
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            for kd, why in cases:
                self.calls.clear()
                self.gateway.chat = AsyncMock(
                    side_effect=lambda kd=kd, **kw: (
                        self.calls.append(kw),
                        _arc_json([_arc(f"候选{kd}", kd)]),
                    )[1]
                )
                added = await self.arcs.ensure_arcs()
                self.assertEqual(added, 0, f"key_date={kd} 应被丢弃（{why}）")
                await self.db.set_state_json("life_arc_generate", {"last_attempt": ""})

    @patch("companion.arcs.datetime", _FrozenDatetime)
    async def test_generate_cooldown_blocks_second_attempt(self):
        # 两条候选必须写得足够不同：Jaccard 是按汉字集合算的，
        # "第一次｜背景" vs "第二次｜背景" 相似度 0.67，会被去重闸门当成同一条。
        first_arc = {
            "title": "读书报告选题",
            "detail": "导师让两周内定下选题，她还没想好写哪本书",
            "key_date": _d(5),
            "emotional_stake": "怕选题太冷门",
        }
        second_arc = {
            "title": "琴房预约",
            "detail": "期末前琴房抢不到位置，她每天刷预约 App",
            "key_date": _d(6),
            "emotional_stake": "怕整周都抢不到",
        }
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (self.calls.append(kw), _arc_json([first_arc]))[1]
        )
        self.assertEqual(await self.arcs.ensure_arcs(), 1)
        n_after_first = len(self.gen_calls())

        # 第二次：节流必须拦住，不能再调 API
        self.assertEqual(await self.arcs.ensure_arcs(), 0)
        self.assertEqual(len(self.gen_calls()), n_after_first, "1 小时内不该再发 API")

        # 把上次尝试挪到 2 小时前 → 放行。
        # 这里必须用 _FrozenDatetime.now()（与 arcs 同一口钟）：若用真实 now，
        # 钉死时钟后写进去的会是"未来时间"，节流闸把第三次也拦住。
        await self.db.set_state_json(
            "life_arc_generate",
            {"last_attempt": (_FrozenDatetime.now() - timedelta(minutes=GENERATE_COOLDOWN_MINUTES + 5)).strftime(TIME_FORMAT)},
        )
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (self.calls.append(kw), _arc_json([second_arc]))[1]
        )
        self.assertEqual(await self.arcs.ensure_arcs(), 1)

    async def test_llm_failure_is_silent_and_leaves_state_clean(self):
        self.gateway.chat = AsyncMock(side_effect=RuntimeError("网络炸了"))
        self.assertEqual(await self.arcs.ensure_arcs(), 0)
        self.assertEqual(await self.arcs.count_active(), 0)

    async def test_malformed_json_is_silent(self):
        self.gateway.chat = AsyncMock(return_value="这不是 JSON")
        self.assertEqual(await self.arcs.ensure_arcs(), 0)
        self.gateway.chat = AsyncMock(return_value=json.dumps({"arcs": "不是列表"}))
        self.assertEqual(await self.arcs.ensure_arcs(), 0)

    async def test_prompt_carries_seed_pool_and_calendar_anchor(self):
        """素材池与日历锚点必须真的进了提示词（负面清单第 3 条：禁止自编卡外事实）"""
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (
                self.calls.append(kw),
                _arc_json([_arc("选题", _d(5))]),
            )[1]
        )
        # 追评（2026-10-05）：下面两条日期断言是"今天+3/+14"，必须把 arcs 的钟钉在
        # _d() 的基准日上，否则用例只在写它的那天（10-04）绿。
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            await self.arcs.ensure_arcs()
        prompt = self.gen_calls()[0]["messages"][0]["content"]
        self.assertIn("测试素材甲", prompt)          # 夹具卡的 life_arc_seed_pool
        self.assertIn("眼下：", prompt)              # 夹具卡的日历锚点
        self.assertIn("接下来：", prompt)
        self.assertIn("2026-10-07", prompt)         # date_min = 今天+3
        self.assertIn("2026-10-18", prompt)         # date_max = 今天+14
        self.assertEqual(self.gen_calls()[0]["purpose"], "life_arc")

    @patch("companion.arcs.datetime", _FrozenDatetime)
    async def test_recent_titles_are_injected_to_avoid_repeat(self):
        await self.arcs.insert_arc("旧主线甲", "背景甲", _d(5))
        self.gateway.chat = AsyncMock(
            side_effect=lambda **kw: (
                self.calls.append(kw),
                _arc_json([_arc("新主线乙", _d(9))]),
            )[1]
        )
        await self.arcs.ensure_arcs()
        prompt = self.gen_calls()[0]["messages"][0]["content"]
        self.assertIn("旧主线甲", prompt)


# ==========================================
# 5. 提示词注入
# ==========================================


class TestPromptBlock(ArcsTestBase):
    async def test_no_arcs_block_fully_omitted(self):
        block = await self.arcs.build_prompt_block()
        self.assertEqual(block, "", "没有主线时整块省略，不能留空标题")

    async def test_only_resolved_shape(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            await self.db.execute(
                """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake,
                                          resolution, created_at, resolved_at)
                   VALUES (?,?,?,'resolved',?,?,?,?)""",
                ("期中读书报告", "选题没定", _d(-1), "纠结选题",
                 "交上了，老师说选题有意思",
                 datetime(2026, 10, 3).strftime(TIME_FORMAT),
                 datetime(2026, 10, 3, 19, 0).strftime(TIME_FORMAT)),
            )
        block = await self.arcs.build_prompt_block()
        self.assertIn("【她最近的生活】", block)
        self.assertIn("（有结果）", block)
        self.assertIn("交上了，老师说选题有意思", block)
        self.assertNotIn("（临近）", block)

    async def test_mixed_shape_and_upcoming_excluded(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            _FrozenDatetime.set(datetime(2026, 10, 4, 12, 0))
            base = datetime(2026, 10, 4, 12, 0)
            await self.db.execute(
                """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
                   VALUES (?,?,?,'near',?,?)""",
                ("乐团节目审查", "低音部没合齐", _d(2, base), "紧张，低音部那段还没合齐",
                 base.strftime(TIME_FORMAT)),
            )
            await self.db.execute(
                """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
                   VALUES (?,?,?,'upcoming',?,?)""",
                ("下个月体测", "800米", _d(9, base), "怕跑不动", base.strftime(TIME_FORMAT)),
            )
            await self.db.execute(
                """INSERT INTO life_arcs (title, detail, key_date, status, resolution,
                                          created_at, resolved_at)
                   VALUES (?,?,?,'resolved',?,?,?)""",
                ("读书报告", "选题没定", _d(-2, base), "交上了",
                 base.strftime(TIME_FORMAT), (base - timedelta(days=1)).strftime(TIME_FORMAT)),
            )
            block = await self.arcs.build_prompt_block()
            self.assertIn("（临近）乐团节目审查", block)
            self.assertIn("紧张，低音部那段还没合齐", block)
            self.assertIn("（有结果）读书报告", block)
            self.assertNotIn("下个月体测", block, "upcoming 还没到她心里，不该注入")
            self.assertEqual(block.count("【她最近的生活】"), 1)

    async def test_stale_resolved_over_2d_not_injected(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            base = datetime(2026, 10, 4, 12, 0)
            _FrozenDatetime.set(base)
            await self.db.execute(
                """INSERT INTO life_arcs (title, detail, key_date, status, resolution,
                                          created_at, resolved_at)
                   VALUES (?,?,?,'resolved',?,?,?)""",
                ("很旧的结果", "背景", _d(-5, base), "早忘了",
                 base.strftime(TIME_FORMAT), (base - timedelta(days=5)).strftime(TIME_FORMAT)),
            )
        self.assertEqual(await self.arcs.build_prompt_block(), "")

    async def test_relative_day_labels(self):
        base = datetime(2026, 10, 4, 12, 0)
        self.assertEqual(_relative_day_label("2026-10-04", base), "今天")
        self.assertEqual(_relative_day_label("2026-10-05", base), "明天")
        self.assertEqual(_relative_day_label("2026-10-06", base), "后天")
        self.assertEqual(_relative_day_label("2026-10-03", base), "昨天")
        self.assertEqual(_relative_day_label("", base), "")
        self.assertEqual(_relative_day_label("垃圾", base), "")

    async def test_assembler_injects_block(self):
        with patch("companion.arcs.datetime", _FrozenDatetime):
            base = datetime(2026, 10, 4, 12, 0)
            _FrozenDatetime.set(base)
            await self.db.execute(
                """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
                   VALUES (?,?,?,'today',?,?)""",
                ("体测", "800米", "2026-10-04", "怕跑不动", base.strftime(TIME_FORMAT)),
            )
        stack = make_engine_stack(self.db, "characters/example")
        stack.assembler.arcs = self.arcs
        prompt = await stack.assembler.assemble_system_prompt("")
        self.assertIn("【她最近的生活】", prompt)
        self.assertIn("（临近）体测", prompt)
        # 位置必须在【事实】区之后、阶段块之前
        self.assertLess(prompt.index("【事实】"), prompt.index("【她最近的生活】"))
        self.assertLess(prompt.index("【她最近的生活】"), prompt.index("【当前关系阶段"))

    async def test_assembler_without_arcs_object_is_unchanged(self):
        """既有调用方零改动：arcs 不传 = 提示词里一个生活字样都不能有，且不得 KeyError。"""
        stack = make_engine_stack(self.db, "characters/example")
        self.assertIsNone(stack.assembler.arcs)
        prompt = await stack.assembler.assemble_system_prompt("")
        self.assertNotIn("她最近的生活", prompt)


# ==========================================
# 6. 事件通道
# ==========================================


def _resolved_arc_sql(title="体测", resolution="跑完了，六十刚过", hours_ago=1.0, announced=0):
    resolved = (datetime.now() - timedelta(hours=hours_ago)).strftime(TIME_FORMAT)
    return (
        "INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, resolution,"
        " event_announced, created_at, resolved_at) VALUES (?,?,?,'resolved',?,?,?,?,?)",
        (title, "背景", "2026-10-04", "怕跑不动", resolution, announced,
         datetime.now().strftime(TIME_FORMAT), resolved),
    )


class TestEventChannel(ArcsTestBase):
    async def test_claim_marks_announced_and_returns_arc(self):
        sql, params = _resolved_arc_sql()
        await self.db.execute(sql, params)
        arc = await self.arcs.claim_event()
        self.assertIsNotNone(arc)
        self.assertEqual(arc["title"], "体测")
        row = (await self.arcs.fetch_arcs())[0]
        self.assertEqual(row["event_announced"], 1, "claim 必须立刻置位防重发")
        # 再 claim 拿不到了
        self.assertIsNone(await self.arcs.claim_event())

    async def test_24h_window(self):
        sql, params = _resolved_arc_sql(hours_ago=25)
        await self.db.execute(sql, params)
        self.assertIsNone(await self.arcs.claim_event(), "超过 24h 的结果不该再触发事件")

    async def test_already_announced_not_claimed(self):
        sql, params = _resolved_arc_sql(announced=1)
        await self.db.execute(sql, params)
        self.assertIsNone(await self.arcs.claim_event())

    async def test_daily_cap(self):
        for i in range(2):
            sql, params = _resolved_arc_sql(title=f"事件{i}", hours_ago=0.5)
            await self.db.execute(sql, params)
        first = await self.arcs.claim_event()
        self.assertIsNotNone(first)
        await self.arcs.mark_event_sent()
        self.assertIsNone(await self.arcs.claim_event(), f"每天最多 {EVENT_DAILY_CAP} 条")

    async def test_daily_cap_resets_next_day(self):
        await self.db.set_state_json(
            "life_arc_event_daily",
            {"date": (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d"), "count": 1},
        )
        sql, params = _resolved_arc_sql()
        await self.db.execute(sql, params)
        self.assertIsNotNone(await self.arcs.claim_event(), "昨天的账不该压今天")

    async def test_release_rolls_back_flag(self):
        sql, params = _resolved_arc_sql()
        await self.db.execute(sql, params)
        arc = await self.arcs.claim_event()
        await self.arcs.release_event(arc)
        self.assertEqual((await self.arcs.fetch_arcs())[0]["event_announced"], 0)
        self.assertIsNotNone(await self.arcs.claim_event(), "回滚后应能重新取到")

    async def test_no_event_returns_none(self):
        self.assertIsNone(await self.arcs.claim_event())

    async def test_event_material_carries_tone_instruction(self):
        arc = {
            "title": "乐团节目审查",
            "detail": "低音部没合齐",
            "resolution": "过了，指挥说整体还行",
            "key_date": "2026-10-04",
        }
        material = self.arcs.build_event_material(arc)
        self.assertIn("刚发生", material)
        self.assertIn("乐团节目审查", material)
        self.assertIn("过了，指挥说整体还行", material)
        self.assertIn("不是事后汇报", material)

    async def test_garbage_resolution_row_does_not_crash(self):
        await self.db.execute(
            """INSERT INTO life_arcs (title, detail, key_date, status, resolution, event_announced,
                                      created_at, resolved_at)
               VALUES ('空结果','背景','2026-10-04','resolved','',0,?,'')""",
            (datetime.now().strftime(TIME_FORMAT),),
        )
        self.assertIsNone(await self.arcs.claim_event(), "resolved_at 为空的行不该被选中")


class TestEventChannelInScheduler(ArcsTestBase):
    """把事件通道放进真实的 trigger_cycle 里验：闸门顺序、免打扰、未回复计数。"""

    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.sent: List[Any] = []
        stack = make_engine_stack(self.db, "characters/example", include_proactive=False)
        self.scheduler = ProactiveScheduler(
            config=ProactiveConfig(quiet_hours=[], max_unanswered=2),
            persona=stack.persona,
            affection=stack.affection,
            mood=stack.mood,
            memory=stack.memory,
            stickers=stack.stickers,
            replier=stack.replier,
            gateway=self.gateway,
            db=self.db,
            send_msg_fn=self._send,
            assembler=stack.assembler,
            arcs=self.arcs,
        )
        stack.assembler.arcs = self.arcs
        self.decision_calls = 0

    async def _send(self, chunk):
        self.sent.append(chunk)

    def _make_gateway(self, decision='{"choice":"B","topic_hint":"","reason":"不想发"}',
                      message="体测跑完了，六十刚过。累死了。"):
        async def fake_chat(**kwargs):
            self.calls.append(kwargs)
            if kwargs.get("purpose") == "proactive_decision":
                self.decision_calls += 1
                return decision
            return message
        self.gateway.chat = AsyncMock(side_effect=fake_chat)

    async def _seed_resolved(self):
        sql, params = _resolved_arc_sql()
        await self.db.execute(sql, params)

    async def test_event_preempts_decision_layer(self):
        await self._seed_resolved()
        self._make_gateway()
        await self.scheduler.trigger_cycle()

        self.assertEqual(self.decision_calls, 0, "事件通道命中时不该再进 LLM 决策层")
        self.assertTrue(self.sent, "事件消息必须真的发出去")
        row = (await self.arcs.fetch_arcs())[0]
        self.assertEqual(row["event_announced"], 1)
        await self.db.set_state_json("life_arc_event_daily", {"date": datetime.now().strftime("%Y-%m-%d"), "count": 1})

    async def test_event_counts_into_unanswered_gate(self):
        await self._seed_resolved()
        self._make_gateway()
        await self.scheduler.trigger_cycle()
        self.assertEqual(await self.scheduler.get_unanswered_count(), 1,
                         "事件消息必须计入现有连续未回闸门")

    async def test_quiet_hours_event_waits_and_is_not_dropped(self):
        """免打扰时段：事件**等待不丢**——跳过本轮，下轮再检查。"""
        await self._seed_resolved()
        self._make_gateway()
        self.scheduler.config = ProactiveConfig(quiet_hours=list(range(24)), max_unanswered=2)

        await self.scheduler.trigger_cycle()
        self.assertEqual(self.sent, [], "免打扰时段不该发")
        self.assertEqual(self.decision_calls, 0)
        row = (await self.arcs.fetch_arcs())[0]
        self.assertEqual(row["event_announced"], 0, "免打扰时不得置位，事件要留着下轮再发")

        # 解除免打扰 → 同一件事这轮就发出去
        self.scheduler.config = ProactiveConfig(quiet_hours=[], max_unanswered=2)
        await self.scheduler.trigger_cycle()
        self.assertTrue(self.sent, "解除免打扰后事件应被补发")
        self.assertEqual((await self.arcs.fetch_arcs())[0]["event_announced"], 1)

    async def test_no_event_falls_back_to_normal_decision(self):
        """当天无事件时，行为与改动前完全一致：照常走决策层。"""
        self._make_gateway(decision='{"choice":"B","topic_hint":"","reason":"不想发"}')
        await self.scheduler.trigger_cycle()
        self.assertEqual(self.decision_calls, 1, "无事件必须回落到常规决策层")
        self.assertEqual(self.sent, [])

    async def test_failed_event_generation_rolls_back_and_does_not_block_cycle(self):
        """事件消息生成失败：回滚置位 + 事件通道仍算"已处理"，不重复进决策层。"""
        await self._seed_resolved()
        self._make_gateway(message="[沉默]")  # 命中沉默 = 整轮不发送
        await self.scheduler.trigger_cycle()
        row = (await self.arcs.fetch_arcs())[0]
        self.assertEqual(row["event_announced"], 0, "没发出去就必须回滚，否则这件事永远说不出口")

    async def test_scheduler_without_arcs_behaves_exactly_as_before(self):
        """零改动回归：arcs 不传 = 无事件通道、无状态机、无补线。"""
        self.scheduler.arcs = None
        self._make_gateway()
        await self.scheduler.trigger_cycle()
        self.assertEqual(self.decision_calls, 1)
        self.assertEqual(self.sent, [])


# ==========================================
# 7. reset 清档
# ==========================================


class TestResetClearsArcs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="fixes16_reset_")
        self.db_path = os.path.join(self.tmp, "c.db")
        self.backup_dir = os.path.join(self.tmp, "backup")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_reset_clears_life_arcs_along_with_memory(self):
        async def seed():
            db = Database(self.db_path)
            await db.init_tables()
            await db.execute(
                "INSERT INTO life_arcs (title, detail, key_date, created_at) VALUES (?,?,?,?)",
                ("没交上的报告", "背景", "2026-10-20", datetime.now().strftime(TIME_FORMAT)),
            )
            await db.execute(
                "INSERT INTO turns (role, content, created_at) VALUES ('user','hi',?)",
                (datetime.now().strftime(TIME_FORMAT),),
            )
            await db.execute(
                "INSERT INTO llm_calls (purpose, created_at) VALUES ('chat',?)",
                (datetime.now().strftime(TIME_FORMAT),),
            )
            await db.close()

        asyncio.run(seed())
        res = reset_database(self.db_path, self.backup_dir, purge_all=False)
        self.assertIn("life_arcs", res["cleared_counts"])
        self.assertEqual(res["cleared_counts"]["life_arcs"], 1)

        conn = sqlite3.connect(self.db_path)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM life_arcs").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0], 0)
        # 计费不该被关系清档带走
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0], 1)
        conn.close()

    def test_reset_survives_legacy_db_without_life_arcs(self):
        """老库里根本没有这张表，reset 也不能炸。"""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE turns (id INTEGER PRIMARY KEY, role TEXT, content TEXT, created_at TEXT);
            CREATE TABLE counters (key TEXT PRIMARY KEY, value INTEGER);
            """
        )
        conn.commit()
        conn.close()
        res = reset_database(self.db_path, self.backup_dir)
        self.assertNotIn("life_arcs", res["cleared_counts"])


# ==========================================
# 8. 零耦合红线 + 凌晨维护钩子
# ==========================================


class TestNoEngineCoupling(ArcsTestBase):
    async def test_arcs_never_writes_mood_or_affection(self):
        """拍板决策 3：生活主线只进提示词，绝不写 mood/affection。"""
        with patch("companion.arcs.datetime", _FrozenDatetime):
            base = datetime(2026, 10, 4, 19, 0)
            _FrozenDatetime.set(base)
            stack = make_engine_stack(self.db, "characters/example")
            aff_before = await stack.affection.get_state()
            mood_before = await stack.mood.get_state()

            await self.arcs.insert_arc("体测", "800米", "2026-10-04", "怕跑不动")
            self.gateway.chat = AsyncMock(return_value="跑完了，六十刚过。")
            await self.arcs.advance_states()
            block = await self.arcs.build_prompt_block()
            self.assertIn("【她最近的生活】", block)

            aff_after = await stack.affection.get_state()
            mood_after = await stack.mood.get_state()
            self.assertEqual(aff_before, aff_after, "affection 一个字节都不许被生活主线改")
            self.assertEqual(mood_before, mood_after, "mood 一个字节都不许被生活主线改")

    async def test_arcs_never_touches_observer_table(self):
        await self.arcs.insert_arc("体测", "800米", "2026-10-04", "怕跑不动")
        await self.arcs.advance_states()
        n = await self.db.fetchone("SELECT COUNT(*) AS c FROM observer_scores")
        self.assertEqual(n["c"], 0)


class TestMaintenanceHook(ArcsTestBase):
    async def test_backup_scheduler_calls_hook(self):
        """凌晨备份跑完后要补一次主线；钩子抛错不得影响备份结果。"""
        tmp = tempfile.mkdtemp(prefix="fixes16_hook_")
        try:
            fired: List[int] = []

            async def hook():
                fired.append(1)

            sched = DailyBackupScheduler(
                db_path=":memory:",
                backup_dir=os.path.join(tmp, "daily"),
                on_maintenance=hook,
            )
            with patch("companion.backup.run_daily_backup", return_value="ok.db"):
                with patch("companion.backup.os.path.exists", return_value=True):
                    await sched._run_maintenance()
            self.assertEqual(fired, [1])

            # 钩子抛错必须被吞掉
            async def bad_hook():
                raise RuntimeError("钩子炸了")

            sched.on_maintenance = bad_hook
            await sched._run_maintenance()  # 不得抛出

            # 不传钩子 = 不调用，行为与改动前一致
            sched.on_maintenance = None
            await sched._run_maintenance()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
