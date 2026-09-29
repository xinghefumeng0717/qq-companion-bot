"""FIXES4 迭代任务自动化单元测试集 (tests/test_fixes4.py)
验证任务 1 重启预告、任务 2 应用内每日备份与轮转、任务 3 /api/status 及 /admin 管理路由、任务 4 桌面控制台核心纯函数。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop

from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.backup import get_last_backup_time, run_daily_backup
from companion.config import AdminConfig, Config, ReplyConfig
from companion.db import Database
from companion.main import CompanionBot
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager
from helpers import make_db, close_db, make_engine_stack
from launcher.core import (
    format_header_info,
    get_backup_command,
    get_simulation_cmd_args,
    get_ssh_tunnel_command,
    load_sync_state,
    parse_status_data,
    save_sync_state,
)


class TestTask1RestartNotice(unittest.TestCase):
    """任务 1：彻底移除重启预告，停机不再发送任何消息且无 pending task 警告 (FIXES5B 任务 6)"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test.db")
        conn = sqlite3.connect(self.db_path)
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_graceful_shutdown_sends_no_goodbye_message(self):
        """测试优雅停机在断开时不再发送任何告别通知（干掉'我去喝口水，马上回来'）"""
        async def _test():
            config = Config.load("config.example.toml")
            bot = CompanionBot(config)
            bot.db = Database(self.db_path)
            await bot.db.init_tables()

            # Mock OneBot 连接与发送
            bot.onebot._ws = MagicMock()
            bot.onebot._ws.closed = False
            bot.onebot._ws.close = AsyncMock()
            mock_send = AsyncMock()
            bot._send_chunk_to_onebot = mock_send

            # 执行优雅停机
            await bot.stop_gracefully()

            # 断言绝对不发送任何消息
            mock_send.assert_not_called()

        asyncio.run(_test())

    def test_graceful_shutdown_leaves_no_pending_tasks(self):
        """测试停机流程中无残留 pending task，所有后台任务均被统一取消并收集完毕"""
        async def _test():
            config = Config.load("config.example.toml")
            bot = CompanionBot(config)
            bot.db = Database(self.db_path)
            await bot.db.init_tables()

            # 启动后台任务调度器
            bot.aggregator.start()
            bot.proactive.start()
            bot.backup_scheduler.start()

            # 执行优雅停机
            await bot.stop_gracefully()

            # 验证所有后台任务均已完成 (done)
            for t in [
                bot.aggregator._consumer_task,
                bot.aggregator._debounce_task,
                bot.proactive._task,
                bot.backup_scheduler._task,
            ]:
                if t is not None:
                    self.assertTrue(t.done(), f"Task {t} should be done after stop_gracefully")

        asyncio.run(_test())

    def test_shutdown_silent_and_does_not_block_shutdown(self):
        """测试停机过程完全静默安全，且无论 WS 状态如何均能顺利退出"""
        async def _test():
            config = Config.load("config.example.toml")
            bot = CompanionBot(config)
            bot.db = Database(self.db_path)
            await bot.db.init_tables()

            bot.onebot._ws = MagicMock()
            bot.onebot._ws.closed = False
            bot.onebot._ws.close = AsyncMock(side_effect=RuntimeError("WS close failed"))

            try:
                await bot.close()
            except Exception as e:
                self.fail(f"close() 不应在停机时抛出未处理异常: {e}")

        asyncio.run(_test())

    def test_startup_connect_sends_no_notice(self):
        """测试 OneBot 连接后不再发送任何启动恢复通知（干掉'回来了'）"""
        async def _test():
            config = Config.load("config.example.toml")
            bot = CompanionBot(config)

            mock_send = AsyncMock()
            bot._send_chunk_to_onebot = mock_send

            # 验证 bot 上已无 _has_announced_connected 或 _on_onebot_connected 路径
            self.assertFalse(hasattr(bot, "_has_announced_connected"))
            self.assertFalse(hasattr(bot, "_on_onebot_connected"))
            mock_send.assert_not_called()

        asyncio.run(_test())


class TestTask2DailyBackup(unittest.TestCase):
    """任务 2：应用内每日备份 (companion/backup.py)"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "companion.db")
        self.backup_dir = os.path.join(self.temp_dir, "backup", "daily")

        # 初始化测试源数据库
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("CREATE TABLE test_data (id INTEGER PRIMARY KEY, msg TEXT)")
        c.execute("INSERT INTO test_data (msg) VALUES ('hello backup')")
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_run_daily_backup_creates_timestamped_file_and_latest_db(self):
        """测试备份生成正确的时间戳文件与 latest.db 镜像，且数据完整"""
        backup_path = run_daily_backup(self.db_path, self.backup_dir)

        # 断言文件生成
        self.assertTrue(os.path.exists(backup_path))
        latest_path = os.path.join(self.backup_dir, "latest.db")
        self.assertTrue(os.path.exists(latest_path))

        # 验证 latest.db 数据完整一致
        conn = sqlite3.connect(latest_path)
        c = conn.cursor()
        c.execute("SELECT msg FROM test_data WHERE id=1")
        row = c.fetchone()
        conn.close()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], "hello backup")

    def test_rotation_keeps_max_14_and_removes_oldest(self):
        """测试保留最近 14 份，超期的更旧备份自动删除"""
        os.makedirs(self.backup_dir, exist_ok=True)
        # 预先生成 16 份模拟历史备份
        created_files = []
        for i in range(1, 17):
            name = f"companion-202609{i:02d}-0417.db"
            p = os.path.join(self.backup_dir, name)
            with open(p, "w", encoding="utf-8") as f:
                f.write(f"dummy content {i}")
            created_files.append(name)

        # 执行一次备份（max_keep=14）
        run_daily_backup(self.db_path, self.backup_dir, max_keep=14)

        remaining_backups = [
            f for f in os.listdir(self.backup_dir)
            if f.startswith("companion-") and f.endswith(".db")
        ]
        # 严格保留 14 份
        self.assertEqual(len(remaining_backups), 14)

        # 最旧的文件（如 0901, 0902, 0903）已被删除
        self.assertNotIn("companion-20260901-0417.db", remaining_backups)
        self.assertNotIn("companion-20260902-0417.db", remaining_backups)
        # 较新的仍在
        self.assertIn("companion-20260916-0417.db", remaining_backups)

    def test_get_last_backup_time(self):
        """测试获取最后备份时间格式化文本"""
        os.makedirs(self.backup_dir, exist_ok=True)
        # 放置一个备份
        p = os.path.join(self.backup_dir, "companion-20260930-0417.db")
        with open(p, "w") as f:
            f.write("test")

        ts_str = get_last_backup_time(self.backup_dir)
        self.assertEqual(ts_str, "2026-09-30 04:17")


class TestTask3AdminAPIAndManagement(AioHTTPTestCase):
    """任务 3：/api/status 与 /admin 管理页测试"""

    async def get_application(self):
        self.temp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.temp_dir, "test_companion.db")
        self.backup_dir = os.path.join(self.temp_dir, "backup", "daily")

        self.db = await make_db(self.db_path)

        self.config = Config.load("config.example.toml")
        stack = make_engine_stack(self.db, persona_path=self.config.character.path)
        self.persona = stack.persona
        self.affection = stack.affection
        self.mood = stack.mood
        self.memory = stack.memory
        self.stickers = stack.stickers
        self.assembler = stack.assembler

        # Mock OneBot
        self.mock_onebot = MagicMock()
        self.mock_onebot.is_connected = True

        self.admin = AdminServer(
            config=AdminConfig(host="127.0.0.1", port=8080),
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            proactive=None,
            assembler=self.assembler,
            db=self.db,
            onebot=self.mock_onebot,
            db_path=self.db_path,
            backup_dir=self.backup_dir,
        )

        app = web.Application()
        app.router.add_get("/", self.admin.handle_overview)
        app.router.add_get("/api/status", self.admin.handle_api_status)
        app.router.add_get("/admin", self.admin.handle_admin)
        app.router.add_post("/admin/backup", self.admin.handle_admin_backup)
        app.router.add_post("/admin/restart", self.admin.handle_admin_restart)
        app.router.add_post("/admin/reset", self.admin.handle_admin_reset)
        return app

    async def tearDownAsync(self):
        await close_db(self.db)
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        await super().tearDownAsync()

    @unittest_run_loop
    async def test_api_status_json_fields(self):
        """测试 /api/status 返回完整且合法的 JSON 字段"""
        resp = await self.client.request("GET", "/api/status")
        self.assertEqual(resp.status, 200)
        data = await resp.json()

        expected_fields = [
            "bot_alive",
            "onebot_connected",
            "stage_name",
            "composite",
            "today_cost",
            "last_backup_time",
            "uptime_minutes",
        ]
        for field in expected_fields:
            self.assertIn(field, data, f"缺少必要字段: {field}")

        self.assertTrue(data["bot_alive"])
        self.assertTrue(data["onebot_connected"])
        self.assertIsInstance(data["composite"], (int, float))
        self.assertIsInstance(data["today_cost"], (int, float))

    @unittest_run_loop
    async def test_admin_backup_returns_path(self):
        """测试 POST /admin/backup 成功触发并返回备份路径"""
        resp = await self.client.request(
            "POST", "/admin/backup", headers={"Accept": "application/json"}
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data.get("status"), "ok")
        self.assertTrue(os.path.exists(data.get("backup_path")))

    @unittest_run_loop
    async def test_admin_reset_without_yes_is_rejected(self):
        """测试 POST /admin/reset 缺少 confirm=YES 时拒绝执行且数据库无改变"""
        # 先写入一条数据
        await self.db.execute(
            "INSERT INTO turns (role, content) VALUES ('user', 'hi')"
        )

        resp = await self.client.request(
            "POST",
            "/admin/reset",
            json={"confirm": "NO"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(resp.status, 400)

        # 检查数据依然保留
        row = await self.db.fetchone("SELECT COUNT(*) as cnt FROM turns")
        self.assertEqual(row["cnt"], 1)

    @unittest_run_loop
    async def test_admin_reset_with_yes_executes(self):
        """测试 POST /admin/reset 带 confirm=YES 时正常清档并返回清除详情"""
        await self.db.execute(
            "INSERT INTO turns (role, content) VALUES ('user', 'hi')"
        )

        resp = await self.client.request(
            "POST",
            "/admin/reset",
            json={"confirm": "YES"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(resp.status, 200)
        data = await resp.json()
        self.assertEqual(data.get("status"), "ok")

        # 检查 turns 表已被清空
        row = await self.db.fetchone("SELECT COUNT(*) as cnt FROM turns")
        self.assertEqual(row["cnt"], 0)


class TestTask4LauncherCore(unittest.TestCase):
    """任务 4：桌面控制台核心纯函数测试 (launcher/core.py)"""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.sync_file = os.path.join(self.temp_dir, "sync_state.json")

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_parse_status_data_normal(self):
        """测试正常状态 JSON 的解析与机器人在线判断"""
        raw = {
            "bot_alive": True,
            "onebot_connected": True,
            "stage_name": "相识",
            "composite": 35.5,
            "today_cost": 0.42,
            "last_backup_time": "2026-09-30 04:17",
            "uptime_minutes": 120,
        }
        res = parse_status_data(raw)
        self.assertTrue(res["bot_alive"])
        self.assertTrue(res["onebot_connected"])
        self.assertTrue(res["robot_online"])
        self.assertEqual(res["stage_name"], "相识")
        self.assertEqual(res["today_cost"], 0.42)

    def test_parse_status_data_offline_or_none(self):
        """测试断线或 None 数据的安全降级"""
        res = parse_status_data(None)
        self.assertFalse(res["bot_alive"])
        self.assertFalse(res["robot_online"])

        raw_dc = {"bot_alive": True, "onebot_connected": False}
        res_dc = parse_status_data(raw_dc)
        self.assertFalse(res_dc["robot_online"])

    def test_format_header_info(self):
        """测试头部信息栏文本格式化"""
        txt = format_header_info("相识", 0.42)
        self.assertEqual(txt, "当前阶段：相识 · 今日费用：¥0.42")

    def test_sync_state_roundtrip(self):
        """测试 sync_state.json 的持久化写入与读取"""
        self.assertEqual(load_sync_state(self.sync_file), {})
        state = {"last_sync": "13:45", "timestamp": "2026-09-29T13:45:00"}
        save_sync_state(self.sync_file, state)

        loaded = load_sync_state(self.sync_file)
        self.assertEqual(loaded["last_sync"], "13:45")

    def test_backup_and_tunnel_command_generation(self):
        """测试备份拉取与 SSH 隧道命令拼接无误"""
        scp_cmd = get_backup_command(local_dest=r"D:\dest\latest.db")
        self.assertEqual(scp_cmd[0], "scp")
        self.assertIn("latest.db", scp_cmd[1])

        tunnel_cmd = get_ssh_tunnel_command(local_port=8080)
        self.assertEqual(
            tunnel_cmd,
            [
                "ssh", "-N", "-T",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "ServerAliveInterval=30",
                "-L", "8080:127.0.0.1:8080", "ubuntu@SERVER_IP",
            ],
        )

        sim_cmd = get_simulation_cmd_args()
        self.assertEqual(sim_cmd[0], "cmd.exe")


if __name__ == "__main__":
    unittest.main()
