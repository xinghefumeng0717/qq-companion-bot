"""FIXES10 引擎/数据层修复的单元测试 (tests/test_fixes10_engines.py)

覆盖：
1. mood.py 情绪均值回归过冲（隔夜符号反转）与动量 clamp
2. memory.py 汉字共现加固补 0.5 强度闸门
3. observer.py 结算类型容错（坏输出丢单项不丢整轮）
4. observer.py 失败兜底不写 observer_scores
5. memory.py archive_diary 的 facts 列表校验（字符串不得被拆成单字事实）
6. db.py 事务跨任务串扰 + 取消不回滚
"""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from companion.db import Database, TIME_FORMAT, now_str, parse_dt
from companion.memory import MemoryManager, RECALL_VISIBLE_THRESHOLD
from companion.mood import MoodEngine
from companion.observer import Observer

from helpers import close_db, make_db, make_engine_stack, make_mock_gateway


def _hours_ago_str(hours: float) -> str:
    return (datetime.now() - timedelta(hours=hours)).strftime(TIME_FORMAT)


async def _seed_mood(
    db: Database,
    v: float = 8.0,
    a: float = 1.0,
    t: float = 7.0,
    momentum_v: float = 0.0,
    momentum_a: float = 0.0,
    hours_ago: float = 24.0,
) -> None:
    """直接落一份情绪状态，last_updated 人为回拨 hours_ago 小时"""
    await db.set_state_json(
        "mood",
        {
            "v": v,
            "a": a,
            "t": t,
            "momentum_v": momentum_v,
            "momentum_a": momentum_a,
            "frustration": 0.0,
            "last_updated": _hours_ago_str(hours_ago),
        },
    )


# ==========================================================
# 修复 1：mood.py 均值回归过冲与动量 clamp
# ==========================================================


class TestMoodRegressionNoOvershoot(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_no_sign_flip_after_long_absence(self):
        """P0 复现：v=8.0 隔 24h 一次更新，绝不越过基线打到反向。

        旧实现 decay = 0.12*24 = 2.88 -> v = 8 + (4-8)*2.88 = -3.5（符号反转）。
        """
        baseline_v = 2.0 + min(2.0, 100.0 / 50.0)  # = 4.0
        await _seed_mood(self.db, v=8.0, hours_ago=24.0)
        mood = MoodEngine(self.db)

        with patch("companion.mood.random.gauss", return_value=0.0):
            st = await mood.update_mood(composite_affection=100.0)

        self.assertGreaterEqual(
            st["v"], baseline_v, f"长间隔单次更新不得越过基线（实得 v={st['v']}）"
        )
        self.assertGreater(st["v"], 0.0, "不得符号反转成负值")
        self.assertAlmostEqual(st["v"], baseline_v, places=1)

    async def test_decay_unchanged_within_8_33h(self):
        """≤ 8.33h 逐点不变：6h 间隔仍按 decay = 0.72 走（FIXES6 标定区间）"""
        await _seed_mood(self.db, v=8.0, hours_ago=6.0)
        mood = MoodEngine(self.db)

        with patch("companion.mood.random.gauss", return_value=0.0):
            st = await mood.update_mood(composite_affection=100.0)

        # v = 8 + (4-8)*0.72 = 5.12；momentum_v = 0.8*0 + (5.12-8)*0.2 = -0.576
        self.assertAlmostEqual(st["v"], 5.1, delta=0.05)
        self.assertAlmostEqual(st["momentum_v"], -0.58, delta=0.02)

    async def test_decay_at_1h_floor_unchanged(self):
        """不足 1 小时按 1 小时参与回归：decay = 0.12（既有行为不得回归）"""
        await _seed_mood(self.db, v=8.0, hours_ago=0.0)
        mood = MoodEngine(self.db)

        with patch("companion.mood.random.gauss", return_value=0.0):
            st = await mood.update_mood(composite_affection=100.0)

        # v = 8 + (4-8)*0.12 = 7.52 -> 7.5
        self.assertAlmostEqual(st["v"], 7.5, delta=0.05)

    async def test_momentum_clamped_under_extreme_drive(self):
        """动量自激回路必须被 clamp 在 ±5.0（不 clamp 时收敛到 -8.0）"""
        last_updated = _hours_ago_str(24.0)
        mood = MoodEngine(self.db)
        seen = []

        for _ in range(10):
            st = await mood.get_state()
            st["v"] = 10.0
            st["a"] = 10.0
            st["last_updated"] = last_updated  # 每轮都是 24h 长间隔
            await mood.save_state(st)
            with patch("companion.mood.random.gauss", return_value=0.0):
                st = await mood.update_mood(composite_affection=0.0, conv_v=8.0, conv_a=8.0)
            seen.append((st["momentum_v"], st["momentum_a"]))

        for m_v, m_a in seen:
            self.assertGreaterEqual(m_v, -5.0, f"momentum_v 越过下限: {m_v}")
            self.assertLessEqual(m_v, 5.0)
            self.assertGreaterEqual(m_a, -5.0, f"momentum_a 越过下限: {m_a}")
            self.assertLessEqual(m_a, 5.0)
        # clamp 必须真的生效（无 clamp 时该驱动下收敛到 -8.0）
        self.assertEqual(seen[-1][0], -5.0, "极端驱动下动量应恰好停在 clamp 边界")


# ==========================================================
# 修复 2：汉字共现加固的 0.5 强度闸门
# ==========================================================


COOCCUR_MESSAGE = "今天天气不错，晚上出去散散步吧"


class TestCooccurrenceReinforceGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.memory = MemoryManager(self.db)

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _insert_diary(self, content: str, importance: int, sentiment: str, days_ago: float) -> int:
        ts = (datetime.now() - timedelta(days=days_ago)).strftime(TIME_FORMAT)
        cur = await self.db.execute(
            """
            INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (content, importance, sentiment, ts, ts),
        )
        return cur.lastrowid

    async def test_faded_diary_not_revived_by_cooccurrence(self):
        """P1 复现：400 天前的已淡出日记不得被共现消息复活"""
        diary_id = await self._insert_diary("今天天气不错，我们在湖边散散步。", 1, "平静", 400.0)

        await self.memory.reinforce_memories(COOCCUR_MESSAGE)

        row = await self.db.fetchone(
            "SELECT recall_count, last_recall_at, created_at FROM diary WHERE id = ?", (diary_id,)
        )
        self.assertEqual(row["recall_count"], 0, "已淡出日记不得被加固")
        self.assertEqual(row["last_recall_at"], row["created_at"], "last_recall_at 不得被重置")

    async def test_fresh_diary_reinforced_by_cooccurrence(self):
        """新日记（强度 >= 0.5）仍按共现规则加固（修复不得一刀切）"""
        diary_id = await self._insert_diary("今天又出去散步啦。", 6, "温暖", 0.0)

        await self.memory.reinforce_memories(COOCCUR_MESSAGE)

        row = await self.db.fetchone("SELECT recall_count FROM diary WHERE id = ?", (diary_id,))
        self.assertEqual(row["recall_count"], 1)

    async def test_gate_is_per_row(self):
        """同一次调用里逐条判定：旧的跳过、新的加固"""
        old_id = await self._insert_diary("今天天气不错，我们在湖边散散步。", 1, "平静", 400.0)
        new_id = await self._insert_diary("今天又出去散步啦。", 6, "温暖", 0.0)

        await self.memory.reinforce_memories(COOCCUR_MESSAGE)

        old_row = await self.db.fetchone("SELECT recall_count FROM diary WHERE id = ?", (old_id,))
        new_row = await self.db.fetchone("SELECT recall_count FROM diary WHERE id = ?", (new_id,))
        self.assertEqual(old_row["recall_count"], 0)
        self.assertEqual(new_row["recall_count"], 1)

    async def test_threshold_constant_matches_visibility(self):
        """加固闸门与回忆注入可见阈值同源（0.5）"""
        self.assertEqual(RECALL_VISIBLE_THRESHOLD, 0.5)


# ==========================================================
# 修复 3：observer 结算类型容错
# ==========================================================


class _ObserverCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _make_observer(self, chat_return=None, chat_side_effect=None) -> Observer:
        gw = make_mock_gateway(chat_return_value=chat_return, chat_side_effect=chat_side_effect)
        self.stack = make_engine_stack(self.db, "characters/example", gateway=gw)
        return Observer(
            gw,
            self.stack.affection,
            self.stack.mood,
            self.stack.memory,
            self.stack.stickers,
            self.db,
        )

    async def _score_rows(self):
        return await self.db.fetchall("SELECT * FROM observer_scores")


def _observer_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False)


class TestObserverTypeTolerance(_ObserverCase):
    """四种坏输入都必须不抛异常，且结算继续跑到 facts 落库"""

    def _base(self, **overrides) -> dict:
        payload = {
            "self_disclosure": 6.0,
            "responsiveness": 6.0,
            "warmth_score": 6.0,
            "resonance": 6.0,
            "moments": [],
            "mood_impact": {"v": 0.0, "a": 0.0, "trust": 0.0},
            "facts": [],
            "followups": [],
            "done_followups": [],
            "collect_sticker": False,
            "sticker_name": "",
        }
        payload.update(overrides)
        return payload

    async def test_mood_impact_v_is_chinese_text(self):
        """mood_impact.v = "有点开心"：落 0.0，不抛异常，facts 照常入库"""
        observer = await self._make_observer(
            _observer_json(
                self._base(
                    mood_impact={"v": "有点开心", "a": None, "trust": "很暖"},
                    facts=["机主周二下午有实验课"],
                )
            )
        )
        res = await observer.settle_turn("今天做实验累死了", "辛苦了")
        self.assertEqual(res["mood_impact"]["v"], "有点开心")
        self.assertIn("机主周二下午有实验课", await self.stack.memory.get_all_facts())

    async def test_top_level_is_list(self):
        """顶层是数组：走失败兜底，不抛异常，也不写 observer_scores"""
        observer = await self._make_observer(_observer_json([1, 2, 3]))
        res = await observer.settle_turn("在吗", "在呢")
        self.assertEqual(res["self_disclosure"], 4.0)
        self.assertEqual(await self._score_rows(), [], "失败兜底不得落 observer_scores")

    async def test_remind_after_hours_is_null(self):
        """remind_after_hours = null：回退 24h 默认，followup 照常入库"""
        observer = await self._make_observer(
            _observer_json(
                self._base(
                    followups=[{"topic": "问问他实验课", "remind_after_hours": None}],
                    facts=["机主这周实验课很多"],
                )
            )
        )
        await observer.settle_turn("我这周实验课很多", "辛苦啦")

        rows = await self.db.fetchall("SELECT topic, remind_after FROM followups")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["topic"], "问问他实验课")
        expected = datetime.now() + timedelta(hours=24)
        self.assertLess(abs((parse_dt(rows[0]["remind_after"]) - expected).total_seconds()), 120)
        self.assertIn("机主这周实验课很多", await self.stack.memory.get_all_facts())

    async def test_self_disclosure_is_scored_text(self):
        """"5分" 这类字符串：落 4.0 中性默认，结算不中断"""
        observer = await self._make_observer(
            _observer_json(
                self._base(
                    self_disclosure="5分",
                    facts=["机主想去听音乐会"],
                )
            )
        )
        await observer.settle_turn("我想去听音乐会", "好呀")

        rows = await self._score_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["self_disclosure"], 4.0)
        self.assertIn("机主想去听音乐会", await self.stack.memory.get_all_facts())

    async def test_settlement_runs_to_last_step(self):
        """单项坏掉不丢整轮：facts / followups / done_followups / 收藏回路都要跑到"""
        await self.db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES (?, ?, 0, ?)",
            ("问问他实验课", now_str(), now_str()),
        )
        observer = await self._make_observer(
            _observer_json(
                self._base(
                    self_disclosure="说不清",
                    mood_impact="今天很暖",
                    moments="深度共情",
                    facts=["机主在准备补考", None, 12345],
                    followups=[{"topic": "问问他补考结果", "remind_after_hours": "明天"}],
                    done_followups=["问问他实验课", {"topic": "坏项"}],
                )
            )
        )
        await observer.settle_turn("我明天要补考", "别慌")

        facts = await self.stack.memory.get_all_facts()
        self.assertIn("机主在准备补考", facts)
        followups = await self.db.fetchall("SELECT topic, done FROM followups ORDER BY id")
        topics = {(r["topic"], r["done"]) for r in followups}
        self.assertIn(("问问他补考结果", 0), topics)
        self.assertIn(("问问他实验课", 1), topics)


# ==========================================================
# 修复 4：observer 失败不写 observer_scores
# ==========================================================


class TestObserverScoresOnlyOnSuccess(_ObserverCase):
    async def test_llm_failure_writes_no_scores(self):
        """LLM 调用失败：中性默认照常结算，但绝不落 observer_scores（锚点校准数据源）"""
        observer = await self._make_observer(chat_side_effect=RuntimeError("boom"))

        res = await observer.settle_turn("在吗", "在呢")

        self.assertEqual(res["self_disclosure"], 4.0)
        self.assertEqual(await self._score_rows(), [], "失败样本不得污染 observer_scores 分布")

    async def test_parse_failure_writes_no_scores(self):
        """返回非法 JSON 同样走失败兜底，不落库"""
        observer = await self._make_observer("这不是 JSON")

        await observer.settle_turn("在吗", "在呢")

        self.assertEqual(await self._score_rows(), [])

    async def test_success_still_writes_scores(self):
        """成功路径照常落库（修复不得把成功样本一起砍掉）"""
        observer = await self._make_observer(
            _observer_json(
                {
                    "self_disclosure": 8.0,
                    "responsiveness": 7.0,
                    "warmth_score": 7.5,
                    "resonance": 6.0,
                    "moments": ["深度共情"],
                    "mood_impact": {"v": 1.0, "a": 0.5, "trust": 0.08},
                    "facts": ["机主喜欢拿铁"],
                    "followups": [],
                    "done_followups": [],
                    "collect_sticker": False,
                    "sticker_name": "",
                }
            )
        )

        await observer.settle_turn("我喜欢喝拿铁", "那下次带你喝")

        rows = await self._score_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["self_disclosure"], 8.0)
        self.assertEqual(rows[0]["warmth_score"], 7.5)


# ==========================================================
# 修复 5：archive_diary 的 facts 列表校验
# ==========================================================


class TestArchiveDiaryFactsValidation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _archive(self, payload) -> MemoryManager:
        gw = make_mock_gateway(chat_return_value=_observer_json(payload))
        memory = MemoryManager(self.db, gw)
        turns = [
            {"role": "user", "content": "他这周实验课很多"},
            {"role": "assistant", "content": "嗯，那很辛苦"},
        ]
        await memory.archive_diary(turns, 42)
        return memory

    def _payload(self, facts):
        return {
            "content": "他这周实验课很多，说有点累。",
            "importance": 7,
            "sentiment": "平静",
            "facts": facts,
        }

    async def test_facts_as_string_is_not_split_into_chars(self):
        """P1 复现：facts 是整串时，旧实现 list() 把一句话拆成单字事实"""
        memory = await self._archive(self._payload("他这周实验课很多"))

        facts = await memory.get_all_facts()
        self.assertEqual(facts, [], "字符串 facts 应整体忽略，不得拆成单字")
        row = await self.db.fetchone("SELECT COUNT(*) AS cnt FROM facts")
        self.assertEqual(row["cnt"], 0)

    async def test_mixed_list_keeps_only_valid_strings(self):
        """混合列表：None / 数字 / 空白串跳过，正常字符串照常入库"""
        memory = await self._archive(
            self._payload([None, 12345, "   ", "他这周实验课很多", "他提到考完想去吃火锅"])
        )

        facts = await memory.get_all_facts()
        self.assertEqual(sorted(facts), sorted(["他这周实验课很多", "他提到考完想去吃火锅"]))
        for fact in facts:
            self.assertGreater(len(fact), 2, f"不得出现单字/碎片事实: {fact!r}")

    async def test_archive_still_writes_diary_and_cursor(self):
        """facts 坏掉不影响归档主流程：日记落库、游标推进照常"""
        await self._archive(self._payload("他这周实验课很多"))

        diary = await self.db.fetchone("SELECT content, importance FROM diary")
        self.assertIn("实验课", diary["content"])
        self.assertEqual(diary["importance"], 7)
        cur = await self.db.fetchone("SELECT value FROM counters WHERE key = 'archived_turns'")
        self.assertEqual(cur["value"], 42)

    async def test_valid_facts_list_unchanged(self):
        """正常列表行为回归：facts 逐条入库"""
        memory = await self._archive(self._payload(["他这周实验课很多", "他喜欢喝拿铁"]))

        facts = await memory.get_all_facts()
        self.assertEqual(sorted(facts), sorted(["他这周实验课很多", "他喜欢喝拿铁"]))


# ==========================================================
# 修复 6：db.py 事务跨任务串扰 + 取消不回滚
# ==========================================================


class TestDatabaseTransactionIsolation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        await self.db.execute("CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, v INTEGER)")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _values(self):
        rows = await self.db.fetchall("SELECT v FROM t ORDER BY v")
        return [r["v"] for r in rows]

    async def test_concurrent_write_not_swallowed_by_foreign_transaction(self):
        """P0 复现：事务 A 回滚时，并发任务 B 的写入不得被静默回滚掉"""
        started = asyncio.Event()

        async def txn_a():
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (1)")
                started.set()
                await asyncio.sleep(0.15)
                raise RuntimeError("boom")

        task = asyncio.create_task(txn_a())
        try:
            await asyncio.wait_for(started.wait(), 2.0)
            loop = asyncio.get_running_loop()
            t0 = loop.time()
            await asyncio.wait_for(self.db.execute("INSERT INTO t (v) VALUES (2)"), 2.0)
            waited = loop.time() - t0
            self.assertGreaterEqual(waited, 0.1, "并发写入必须等事务结束再走自己的提交")
        finally:
            with self.assertRaises(RuntimeError):
                await task

        self.assertEqual(await self._values(), [2], "A 回滚不得带走 B 的写入")

    async def test_cancel_inside_transaction_rolls_back(self):
        """P0 复现：事务体内被 cancel 必须回滚，残留不得被后续普通 execute 提交"""
        entered = asyncio.Event()

        async def txn_body():
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (7)")
                entered.set()
                await asyncio.sleep(30)

        task = asyncio.create_task(txn_body())
        await asyncio.wait_for(entered.wait(), 2.0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2.0)

        self.assertEqual(await self._values(), [], "取消后事务内的写入必须已回滚")
        self.assertFalse(self.db._in_transaction)
        self.assertIsNone(self.db._txn_owner)

        await asyncio.wait_for(self.db.execute("INSERT INTO t (v) VALUES (8)"), 2.0)
        self.assertEqual(await self._values(), [8], "后续 execute 不得把回滚残留一并提交")

    async def test_cancelled_error_raised_by_body_rolls_back(self):
        """事务体显式抛 CancelledError（BaseException）同样回滚"""
        async def txn_body():
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (9)")
                raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await txn_body()

        self.assertEqual(await self._values(), [])
        self.assertFalse(self.db._in_transaction)
        self.assertIsNone(self.db._txn_owner)

    async def test_normal_transaction_commit_and_rollback(self):
        """正常事务功能回归：正常提交、异常全回滚、属主内 execute 不自锁"""
        async with self.db.transaction():
            await self.db.execute("INSERT INTO t (v) VALUES (1)")
            await self.db.execute("INSERT INTO t (v) VALUES (2)")
        self.assertEqual(await self._values(), [1, 2])

        with self.assertRaises(ValueError):
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (3)")
                raise ValueError("rollback me")
        self.assertEqual(await self._values(), [1, 2])

    async def test_nested_transaction_reuses_outer(self):
        """同一任务内嵌套事务复用最外层，不重复提交也不自锁"""
        async with self.db.transaction():
            await self.db.execute("INSERT INTO t (v) VALUES (1)")
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (2)")
            await self.db.execute("INSERT INTO t (v) VALUES (3)")
        self.assertEqual(await self._values(), [1, 2, 3])

    async def test_other_task_transaction_waits(self):
        """两个任务的事务串行化：B 的事务不会与 A 的事务交织"""

        order = []
        first_entered = asyncio.Event()

        async def txn_a():
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (1)")
                first_entered.set()
                await asyncio.sleep(0.1)
            order.append("a_done")

        async def txn_b():
            await first_entered.wait()
            async with self.db.transaction():
                await self.db.execute("INSERT INTO t (v) VALUES (2)")
            order.append("b_done")

        task_a = asyncio.create_task(txn_a())
        task_b = asyncio.create_task(txn_b())
        await asyncio.wait_for(asyncio.gather(task_a, task_b), 3.0)

        self.assertEqual(order, ["a_done", "b_done"])
        self.assertEqual(await self._values(), [1, 2])

    async def test_executemany_inside_transaction_is_atomic(self):
        """executemany 同样走写锁：事务内属主免锁、回滚整体生效"""
        async with self.db.transaction():
            await self.db.executemany("INSERT INTO t (v) VALUES (?)", [(1,), (2,), (3,)])
        self.assertEqual(await self._values(), [1, 2, 3])

        with self.assertRaises(RuntimeError):
            async with self.db.transaction():
                await self.db.executemany("INSERT INTO t (v) VALUES (?)", [(4,), (5,), (6,)])
                raise RuntimeError("nope")
        self.assertEqual(await self._values(), [1, 2, 3])


if __name__ == "__main__":
    unittest.main()
