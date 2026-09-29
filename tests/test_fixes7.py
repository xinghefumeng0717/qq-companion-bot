"""FIXES7 全链路体检测试集 (tests/test_fixes7.py)
覆盖 C 类（时间/边界测试）与 A 类（A1 游标、A3 表情包去重）的可自动化部分。
"""

import asyncio
import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

from companion.affection import AffectionEngine
from companion.config import BEIJING_TZ, PricingConfig, ProactiveConfig, ReplyConfig
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona, RoutineItem
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import MAX_STICKERS_COUNT, StickerManager, compute_file_md5
from helpers import make_db, close_db, make_mock_gateway


class TestC1ProactiveQuietHours(unittest.TestCase):
    """C1: 主动消息免打扰跨午夜测试"""

    def setUp(self):
        self.config_standard = ProactiveConfig(enabled=True, quiet_hours=[0, 8])
        self.config_cross_midnight = ProactiveConfig(enabled=True, quiet_hours=[23, 7])

    def test_quiet_hours_standard_interval(self):
        """[0, 8] 时段判定：23:59 不静默、00:00~07:59 静默、08:00 恢复"""
        scheduler = ProactiveScheduler(
            config=self.config_standard,
            persona=None,
            affection=None,
            mood=None,
            memory=None,
            stickers=None,
            replier=None,
            gateway=None,
            db=None,
            send_msg_fn=None,
        )
        self.assertFalse(scheduler._is_in_quiet_hours(23), "23:59 应不静默")
        self.assertTrue(scheduler._is_in_quiet_hours(0), "00:00 应静默")
        self.assertTrue(scheduler._is_in_quiet_hours(4), "04:00 应静默")
        self.assertTrue(scheduler._is_in_quiet_hours(7), "07:59 应静默")
        self.assertFalse(scheduler._is_in_quiet_hours(8), "08:00 应恢复（不静默）")
        self.assertFalse(scheduler._is_in_quiet_hours(12), "12:00 应不静默")

    def test_quiet_hours_cross_midnight_interval(self):
        """[23, 7] 跨午夜时段判定"""
        scheduler = ProactiveScheduler(
            config=self.config_cross_midnight,
            persona=None,
            affection=None,
            mood=None,
            memory=None,
            stickers=None,
            replier=None,
            gateway=None,
            db=None,
            send_msg_fn=None,
        )
        self.assertFalse(scheduler._is_in_quiet_hours(22), "22:00 应不静默")
        self.assertTrue(scheduler._is_in_quiet_hours(23), "23:00 应静默")
        self.assertTrue(scheduler._is_in_quiet_hours(0), "00:00 应静默")
        self.assertTrue(scheduler._is_in_quiet_hours(6), "06:00 应静默")
        self.assertFalse(scheduler._is_in_quiet_hours(7), "07:00 应恢复")


class TestC2DailyUnansweredReset(unittest.IsolatedAsyncioTestCase):
    """C2: '当天停止'的跨午夜重置测试"""

    async def asyncSetUp(self):
        self.test_db_path = "data/test_c2.db"
        self.db = await make_db(self.test_db_path)

        self.config = ProactiveConfig(enabled=True, max_unanswered=2, quiet_hours=[])
        self.scheduler = ProactiveScheduler(
            config=self.config,
            persona=None,
            affection=None,
            mood=None,
            memory=None,
            stickers=None,
            replier=None,
            gateway=None,
            db=self.db,
            send_msg_fn=None,
        )

    async def asyncTearDown(self):
        await close_db(self.db, self.test_db_path)

    async def test_unanswered_count_resets_across_midnight(self):
        """23:50 连续 2 条未回触发当天停，次日 00:10 自动失效恢复"""
        day1 = datetime.datetime(2026, 9, 29, 23, 50)
        day2 = datetime.datetime(2026, 9, 30, 0, 10)

        # 模拟 Day 1 晚上 23:50 连续累加 2 条未回复
        with patch("companion.proactive.datetime") as mock_dt:
            mock_dt.now.return_value = day1
            mock_dt.strptime = datetime.datetime.strptime
            await self.scheduler.increment_unanswered_count()
            await self.scheduler.increment_unanswered_count()

            cnt_day1 = await self.scheduler.get_unanswered_count()
            self.assertEqual(cnt_day1, 2, "当天未回复应为 2")

        # 模拟 Day 2 凌晨 00:10 查看未回复计数
        with patch("companion.proactive.datetime") as mock_dt:
            mock_dt.now.return_value = day2
            mock_dt.strptime = datetime.datetime.strptime
            cnt_day2 = await self.scheduler.get_unanswered_count()
            self.assertEqual(cnt_day2, 0, "跨过午夜后日期过期，未回复计数应重置为 0")


class TestC3ScheduleDaysParsing(unittest.TestCase):
    """C3: 作息表 days 字段解析与向后兼容测试"""

    def setUp(self):
        # 构造带有 7 套不同 weekday 配置的 persona
        # 周一至周日 (0~6) 各一套专属日程，外加一条无 days 字段的兜底日程
        self.routine = [
            RoutineItem(start=8, end=10, activity="周一早八文献课", days=[0]),
            RoutineItem(start=8, end=10, activity="周二交响乐排练", days=[1]),
            RoutineItem(start=8, end=10, activity="周三小提琴合奏", days=[2]),
            RoutineItem(start=8, end=10, activity="周四专业写作研讨", days=[3]),
            RoutineItem(start=8, end=10, activity="周五大提琴独奏课", days=[4]),
            RoutineItem(start=8, end=10, activity="周六湖畔晨读散步", days=[5]),
            RoutineItem(start=8, end=10, activity="周日整理琴房乐谱", days=[6]),
            # 跨天日程
            RoutineItem(start=23, end=7, activity="睡眠休息", days=[0, 1, 2, 3, 4, 5, 6]),
            # 老格式：无 days 字段的通用日程 (兼容测试)
            RoutineItem(start=12, end=13, activity="食堂午餐时光", days=None),
        ]
        self.persona = Persona(
            name="测试",
            user_address="同学",
            core_description="",
            chat_style=None,
            initial_dims={},
            stages=[],
            daily_routine=self.routine,
            personal_memories=[],
            habits=[],
            stickers_dir="",
            base_dir="",
        )

    def test_days_specific_parsing(self):
        """周一到周日各取到各自专属的日程"""
        expected = [
            (0, "周一早八文献课"),
            (1, "周二交响乐排练"),
            (2, "周三小提琴合奏"),
            (3, "周四专业写作研讨"),
            (4, "周五大提琴独奏课"),
            (5, "周六湖畔晨读散步"),
            (6, "周日整理琴房乐谱"),
        ]
        for wd, act in expected:
            res = self.persona.get_current_activity(hour=9, weekday=wd)
            self.assertEqual(res, act, f"星期 {wd} 的活动应为 '{act}'，实际为 '{res}'")

    def test_cross_midnight_routine(self):
        """跨午夜日程 (23:00~07:00) 判定"""
        self.assertEqual(self.persona.get_current_activity(hour=23, weekday=0), "睡眠休息")
        self.assertEqual(self.persona.get_current_activity(hour=2, weekday=1), "睡眠休息")
        self.assertEqual(self.persona.get_current_activity(hour=6, weekday=2), "睡眠休息")

    def test_legacy_format_backward_compatibility(self):
        """无 days 字段的老格式向后兼容"""
        # 12:30 在所有星期都应命中通用的“食堂午餐时光”
        for wd in range(7):
            res = self.persona.get_current_activity(hour=12, weekday=wd)
            self.assertEqual(res, "食堂午餐时光", f"星期 {wd} 未命中通用老格式日程")


class TestC4PeakOffPeakPricing(unittest.TestCase):
    """C4: 峰谷计费切换点 ±1 分钟边界测试"""

    def setUp(self):
        self.pricing = PricingConfig(holidays=["2026-10-01"])

    def test_boundary_minutes(self):
        """工作日峰谷切换点 ±1 分钟判断：
        高峰：09:00~12:00, 14:00~18:00
        """
        # 2026-09-29 是周二 (工作日)
        test_cases = [
            (datetime.datetime(2026, 9, 29, 8, 59, tzinfo=BEIJING_TZ), False, "08:59 谷时"),
            (datetime.datetime(2026, 9, 29, 9, 0, tzinfo=BEIJING_TZ), True, "09:00 峰时起始"),
            (datetime.datetime(2026, 9, 29, 9, 1, tzinfo=BEIJING_TZ), True, "09:01 峰时"),
            (datetime.datetime(2026, 9, 29, 11, 59, tzinfo=BEIJING_TZ), True, "11:59 峰时末"),
            (datetime.datetime(2026, 9, 29, 12, 0, tzinfo=BEIJING_TZ), False, "12:00 谷时（午休）"),
            (datetime.datetime(2026, 9, 29, 12, 1, tzinfo=BEIJING_TZ), False, "12:01 谷时"),
            (datetime.datetime(2026, 9, 29, 13, 59, tzinfo=BEIJING_TZ), False, "13:59 谷时"),
            (datetime.datetime(2026, 9, 29, 14, 0, tzinfo=BEIJING_TZ), True, "14:00 峰时起始"),
            (datetime.datetime(2026, 9, 29, 14, 1, tzinfo=BEIJING_TZ), True, "14:01 峰时"),
            (datetime.datetime(2026, 9, 29, 17, 59, tzinfo=BEIJING_TZ), True, "17:59 峰时末"),
            (datetime.datetime(2026, 9, 29, 18, 0, tzinfo=BEIJING_TZ), False, "18:00 谷时起始"),
            (datetime.datetime(2026, 9, 29, 18, 1, tzinfo=BEIJING_TZ), False, "18:01 谷时"),
        ]

        for dt, expected_peak, desc in test_cases:
            res = self.pricing.is_peak(dt)
            self.assertEqual(res, expected_peak, f"切换点校验失败: {desc} 应为 {expected_peak}, 实际为 {res}")

    def test_weekend_and_holiday_always_offpeak(self):
        """周末及节假日全天谷时"""
        # 2026-10-01 是节假日
        holiday_noon = datetime.datetime(2026, 10, 1, 10, 0, tzinfo=BEIJING_TZ)
        self.assertFalse(self.pricing.is_peak(holiday_noon), "法定节假日 10:00 应为谷时")

        # 2026-10-03 是周六
        sat_afternoon = datetime.datetime(2026, 10, 3, 15, 0, tzinfo=BEIJING_TZ)
        self.assertFalse(self.pricing.is_peak(sat_afternoon), "周六下午 15:00 应为谷时")


class TestA1DiaryCursorConsistency(unittest.IsolatedAsyncioTestCase):
    """A1 自动化体检：日记归档游标推进行为（9/16/17 轮断言与 LLM 失败恢复）"""

    async def asyncSetUp(self):
        self.test_db_path = "data/test_a1.db"
        self.db = await make_db(self.test_db_path)
        self.mock_gateway = make_mock_gateway()
        self.memory = MemoryManager(self.db, self.mock_gateway)

    async def asyncTearDown(self):
        await close_db(self.db, self.test_db_path)

    async def _insert_turns(self, count: int) -> None:
        """插入指定轮次的 user+assistant 对话"""
        for i in range(1, count + 1):
            await self.db.execute(
                "INSERT INTO turns (role, content, created_at) VALUES ('user', ?, '2026-09-29 10:00')",
                (f"机主消息 {i}",),
            )
            await self.db.execute(
                "INSERT INTO turns (role, content, created_at) VALUES ('assistant', ?, '2026-09-29 10:01')",
                (f"机器人回复 {i}",),
            )

    async def test_cursor_with_9_and_16_turns(self):
        """测试 9 轮时归档前 8 轮、游标停在第 8 轮 assistant；再补到 16 轮归档第二段"""
        # 1. 插入 9 轮 (18 条消息)
        await self._insert_turns(9)

        fake_diary_json = json.dumps({
            "content": "今天和机主聊了前8轮的内容...",
            "importance": 5,
            "sentiment": "平静",
            "facts": ["机主喜欢吃面"],
        })
        self.mock_gateway.chat = AsyncMock(return_value=fake_diary_json)

        # 触发归档
        await self.memory.check_and_trigger_diary_archive()

        # 验证游标：应停在第 8 轮 assistant 的 turn id（即 16）
        cursor_row = await self.db.fetchone("SELECT value FROM counters WHERE key = 'archived_turns'")
        self.assertEqual(cursor_row["value"], 16, "游标应准确推进到第 8 轮 assistant (id=16)")

        diary_rows = await self.db.fetchall("SELECT id, content FROM diary")
        self.assertEqual(len(diary_rows), 1, "应生成 1 条日记")

        # 再次触发，剩余只有 1 轮 user（第 9 轮），不足 8 轮，不应触发新归档
        await self.memory.check_and_trigger_diary_archive()
        self.assertEqual(len(await self.db.fetchall("SELECT id FROM diary")), 1)

        # 2. 追加至 16 轮 (再插入 7 轮，共 16 轮)
        await self._insert_turns(7)
        await self.memory.check_and_trigger_diary_archive()

        cursor_row2 = await self.db.fetchone("SELECT value FROM counters WHERE key = 'archived_turns'")
        # 第 9 轮的 assistant id 是 18，追加 7 轮 (14 条)，总最大 id 为 18 + 14 = 32
        self.assertEqual(cursor_row2["value"], 32, "游标应准确推进到第 16 轮 assistant (id=32)")
        self.assertEqual(len(await self.db.fetchall("SELECT id FROM diary")), 2, "应生成第 2 条日记")

    async def test_llm_failure_does_not_advance_cursor(self):
        """LLM 调用失败时游标不推进，防止对话永久丢失"""
        await self._insert_turns(8)

        self.mock_gateway.chat = AsyncMock(side_effect=RuntimeError("API 500 Network Timeout"))
        await self.memory.check_and_trigger_diary_archive()

        cursor_row = await self.db.fetchone("SELECT value FROM counters WHERE key = 'archived_turns'")
        self.assertEqual(cursor_row["value"], 0, "LLM 失败时游标必须保持在 0，不得推进")
        diaries = await self.db.fetchall("SELECT id FROM diary")
        self.assertEqual(len(diaries), 0, "失败时不应插入任何日记记录")


class TestA3StickerMD5Deduplication(unittest.IsolatedAsyncioTestCase):
    """A3 自动化体检：表情包 MD5 计算双源一致性与 200 张上限行为"""

    async def asyncSetUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db = await make_db(":memory:")
        self.sticker_mgr = StickerManager(self.temp_dir, self.db)

    async def asyncTearDown(self):
        await close_db(self.db)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_md5_dedup_different_filenames(self):
        """相同二进制图片，不同文件名走两条路径：断言去重生效"""
        # 创建一张 1x1 假图片
        img_content = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        
        path_a = os.path.join(self.temp_dir, "sticker_a.png")
        path_b = os.path.join(self.temp_dir, "sticker_b.png")
        with open(path_a, "wb") as f:
            f.write(img_content)
        with open(path_b, "wb") as f:
            f.write(img_content)

        # 1. 模拟初始表情包同步
        self.sticker_mgr._index["表情A"] = {"file": "sticker_a.png", "desc": "测试表情A"}
        await self.sticker_mgr.sync_initial_stickers()

        # 2. 模拟运行时收藏同样内容的图片（但名为 sticker_b.png）
        mock_gw = MagicMock()
        mock_gw.config.vision_model = None
        collected = await self.sticker_mgr.collect_sticker(
            image_path=path_b,
            sticker_name="表情B",
            gateway=mock_gw,
        )
        self.assertFalse(collected, "相同内容的图片通过 MD5 必须被拒绝收藏，以防同图双重命名")

    async def test_max_200_limit_behavior(self):
        """达到 200 张上限时静默丢弃 (返回 False)"""
        # 预先向 SQLite 插入 200 个虚构表情包
        for i in range(MAX_STICKERS_COUNT):
            await self.db.execute(
                "INSERT INTO stickers (name, file, desc, md5, created_at) VALUES (?, ?, ?, ?, '2026-09-29')",
                (f"s_{i}", f"s_{i}.png", f"d_{i}", f"md5_{i}"),
            )

        test_img = os.path.join(self.temp_dir, "new_img.png")
        with open(test_img, "wb") as f:
            f.write(b"dummy_data_123")

        mock_gw = MagicMock()
        mock_gw.config.vision_model = None
        collected = await self.sticker_mgr.collect_sticker(
            image_path=test_img,
            sticker_name="第201张",
            gateway=mock_gw,
        )
        self.assertFalse(collected, "超过 200 张上限时必须拒绝收藏")


if __name__ == "__main__":
    unittest.main()
