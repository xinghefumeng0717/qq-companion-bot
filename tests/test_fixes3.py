"""FIXES3 迭代功能全量验收测试
- 任务 1：模型预设化配置与 provider 参数分流
- 任务 2：仿真对话沙箱零副作用
- 任务 3：数据重置与备份恢复
"""

import asyncio
import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from companion.chat import ChatSession
from companion.config import Config, LLMConfig, ModelPreset
from companion.db import Database
from companion.gateway import LLMGateway, apply_provider_params
from companion.reset import main as reset_main, reset_database


class TestTask1Presets(unittest.TestCase):
    def test_presets_parsing_and_active(self):
        cfg = LLMConfig(
            current="minimax",
            presets={
                "deepseek": ModelPreset(
                    provider="deepseek",
                    base_url="https://api.deepseek.com",
                    api_key="ds-key",
                    chat="deepseek-v4-pro",
                    vision="deepseek-flash",
                    tasks="deepseek-flash",
                ),
                "minimax": ModelPreset(
                    provider="minimax",
                    base_url="https://api.minimaxi.com/v1",
                    api_key="mm-key",
                    chat="MiniMax-M3",
                    vision="MiniMax-M3",
                    tasks="MiniMax-M2.7",
                ),
            },
        )
        self.assertEqual(cfg.active().provider, "minimax")
        self.assertEqual(cfg.active().chat, "MiniMax-M3")
        self.assertEqual(cfg.api_key, "mm-key")
        self.assertEqual(cfg.text_model, "MiniMax-M3")
        self.assertEqual(cfg.vision_model, "MiniMax-M3")
        self.assertEqual(cfg.observer_model, "MiniMax-M2.7")

        # 切换 current
        cfg.current = "deepseek"
        self.assertEqual(cfg.active().provider, "deepseek")
        self.assertEqual(cfg.active().chat, "deepseek-v4-pro")
        self.assertEqual(cfg.api_key, "ds-key")

    def test_current_invalid_raises_error_with_available_list(self):
        cfg = LLMConfig(
            current="unknown_model",
            presets={
                "deepseek": ModelPreset("deepseek", "url1", "k1", "m1"),
                "minimax": ModelPreset("minimax", "url2", "k2", "m2"),
            },
        )
        with self.assertRaises(ValueError) as ctx:
            cfg.active()
        err_msg = str(ctx.exception)
        self.assertIn("unknown_model", err_msg)
        self.assertIn("deepseek", err_msg)
        self.assertIn("minimax", err_msg)

    def test_minimax_payload_has_reasoning_split_no_thinking(self):
        base_payload = {"model": "MiniMax-M3", "messages": [], "temperature": 0.7}
        payload = apply_provider_params(
            payload=base_payload,
            provider="minimax",
            enabled=True,
            effort="high",
            base_url="https://api.minimaxi.com/v1",
        )
        self.assertTrue(payload.get("reasoning_split"))
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)

    def test_generic_openai_provider_no_reasoning_params(self):
        base_payload = {"model": "doubao-seed", "messages": [], "temperature": 0.7}
        payload = apply_provider_params(
            payload=base_payload,
            provider="openai",
            enabled=True,
            effort="high",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
        )
        self.assertNotIn("thinking", payload)
        self.assertNotIn("reasoning_effort", payload)
        self.assertNotIn("reasoning_split", payload)
        self.assertEqual(payload["temperature"], 0.7)

    def test_deepseek_provider_keeps_thinking(self):
        base_payload = {"model": "deepseek-flash", "messages": [], "temperature": 0.7}
        payload = apply_provider_params(
            payload=base_payload,
            provider="deepseek",
            enabled=True,
            effort="low",
            base_url="https://api.deepseek.com",
        )
        self.assertEqual(payload.get("thinking"), {"type": "enabled"})
        self.assertEqual(payload.get("reasoning_effort"), "low")
        self.assertNotIn("temperature", payload)
        self.assertNotIn("reasoning_split", payload)


class TestTask2ChatSimulation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.prod_db = os.path.join(self.tmp_dir, "prod.db")
        self.sandbox_db = os.path.join(self.tmp_dir, "sandbox.db")

        # 初始化测试生产库并填充一些数据
        db = Database(self.prod_db)
        await db.init_tables()
        await db.execute(
            "INSERT INTO turns (role, content, created_at) VALUES ('user', 'orig 1', '2026-09-01 10:00')"
        )
        await db.execute(
            "INSERT INTO turns (role, content, created_at) VALUES ('assistant', 'orig 2', '2026-09-01 10:01')"
        )
        await db.close()

    async def asyncTearDown(self):
        if os.path.exists(self.tmp_dir):
            shutil.rmtree(self.tmp_dir, ignore_errors=True)

    async def test_simulation_zero_side_effect_on_prod_db(self):
        # 记录初始行数
        conn = sqlite3.connect(self.prod_db)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM turns")
        initial_turns_count = cur.fetchone()[0]
        self.assertEqual(initial_turns_count, 2)
        conn.close()

        session = ChatSession(
            prod_db_path=self.prod_db,
            sandbox_db_path=self.sandbox_db,
        )
        await session.initialize()
        self.assertTrue(os.path.exists(self.sandbox_db))

        # Mock stream_chat 避免网络调用
        async def mock_stream_chat(*args, **kwargs):
            yield "模拟回复内容"

        session.gateway.stream_chat = mock_stream_chat

        # 模拟对话 3 轮
        for i in range(3):
            reply = await session.handle_input(f"测试机主话语 {i+1}")
            self.assertIn("模拟回复内容", reply)

        # 检查沙箱中的 turns 增加了 3 对 (6 条)
        s_conn = sqlite3.connect(self.sandbox_db)
        s_cur = s_conn.cursor()
        s_cur.execute("SELECT COUNT(*) FROM turns")
        sandbox_count = s_cur.fetchone()[0]
        self.assertEqual(sandbox_count, initial_turns_count + 6)
        s_conn.close()

        # 退出并清理
        await session.close()
        self.assertFalse(os.path.exists(self.sandbox_db))

        # 核心断言：生产库中的 turns 行数与启动前严格完全一致！
        p_conn = sqlite3.connect(self.prod_db)
        p_cur = p_conn.cursor()
        p_cur.execute("SELECT COUNT(*) FROM turns")
        final_turns_count = p_cur.fetchone()[0]
        p_conn.close()
        self.assertEqual(final_turns_count, initial_turns_count)


class TestTask3DataReset(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test.db")
        self.backup_dir = os.path.join(self.tmp_dir, "backup")

        # 建表并填充数据
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.executescript(
            """
            CREATE TABLE turns (id INTEGER PRIMARY KEY, role TEXT, content TEXT, created_at TEXT);
            CREATE TABLE counters (key TEXT PRIMARY KEY, value INTEGER);
            CREATE TABLE diary (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE diary_archive (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE facts (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE followups (id INTEGER PRIMARY KEY, topic TEXT);
            CREATE TABLE suppressed_desires (id INTEGER PRIMARY KEY, content TEXT);
            CREATE TABLE observer_scores (id INTEGER PRIMARY KEY, warmth_score REAL);
            CREATE TABLE milestones (id INTEGER PRIMARY KEY, stage INTEGER);
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE stickers (name TEXT PRIMARY KEY, file TEXT);
            CREATE TABLE llm_calls (id INTEGER PRIMARY KEY, purpose TEXT);

            INSERT INTO turns (role, content) VALUES ('user', 'hello');
            INSERT INTO counters (key, value) VALUES ('total_turns', 10), ('archived_turns', 5);
            INSERT INTO diary (content) VALUES ('test diary');
            INSERT INTO diary_archive (content) VALUES ('old diary');
            INSERT INTO facts (content) VALUES ('fact 1');
            INSERT INTO followups (topic) VALUES ('topic 1');
            INSERT INTO suppressed_desires (content) VALUES ('desire 1');
            INSERT INTO observer_scores (warmth_score) VALUES (8.5);
            INSERT INTO milestones (stage) VALUES (1);
            INSERT INTO state (key, value) VALUES ('affection', '{"stage": 1}');
            INSERT INTO stickers (name, file) VALUES ('cat', 'cat.png');
            INSERT INTO llm_calls (purpose) VALUES ('chat');
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_reset_clears_relationship_tables_preserves_stickers_and_calls(self):
        res = reset_database(
            db_path=self.db_path,
            backup_dir=self.backup_dir,
            purge_all=False,
        )

        # 验证备份文件存在且大小一致
        backup_path = res["backup_path"]
        self.assertTrue(os.path.exists(backup_path))
        self.assertGreater(os.path.getsize(backup_path), 0)

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()

        # 关系数据表全部为空
        for tbl in ["turns", "diary", "diary_archive", "facts", "followups",
                    "suppressed_desires", "observer_scores", "milestones", "state"]:
            c.execute(f"SELECT COUNT(*) FROM {tbl}")
            cnt = c.fetchone()[0]
            self.assertEqual(cnt, 0, f"Table {tbl} should be empty after reset")

        # counters 归零
        c.execute("SELECT value FROM counters WHERE key = 'total_turns'")
        self.assertEqual(c.fetchone()[0], 0)
        c.execute("SELECT value FROM counters WHERE key = 'archived_turns'")
        self.assertEqual(c.fetchone()[0], 0)

        # stickers 和 llm_calls 被妥善保留！
        c.execute("SELECT COUNT(*) FROM stickers")
        self.assertEqual(c.fetchone()[0], 1)
        c.execute("SELECT COUNT(*) FROM llm_calls")
        self.assertEqual(c.fetchone()[0], 1)

        conn.close()

    def test_reset_purge_all_clears_stickers_and_llm_calls(self):
        reset_database(
            db_path=self.db_path,
            backup_dir=self.backup_dir,
            purge_all=True,
        )

        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM stickers")
        self.assertEqual(c.fetchone()[0], 0)
        c.execute("SELECT COUNT(*) FROM llm_calls")
        self.assertEqual(c.fetchone()[0], 0)
        conn.close()

    def test_default_backup_dir_is_daily_rotation(self):
        """C-10：reset 默认备份目录必须是有 14 份轮转的 data/backup/daily，
        否则一次性 CLI reset 会在 data/backup/ 无限堆积全库拷贝。"""
        self.assertEqual(
            inspect.signature(reset_database).parameters["backup_dir"].default,
            "data/backup/daily",
        )

    def test_cli_default_backup_lands_in_daily_dir(self):
        """CLI 不传 --backup-dir 时，快照落在 <cwd>/data/backup/daily/。"""
        cwd = os.getcwd()
        try:
            os.chdir(self.tmp_dir)
            os.makedirs("data", exist_ok=True)
            shutil.copy2(self.db_path, os.path.join("data", "companion.db"))
            with patch.object(sys, "argv", ["companion.reset", "--yes"]):
                reset_main()
        finally:
            os.chdir(cwd)
        daily = os.path.join(self.tmp_dir, "data", "backup", "daily")
        backups = [
            b for b in os.listdir(daily)
            if b.startswith("companion-") and b.endswith(".db")
        ]
        self.assertTrue(
            backups, f"CLI 默认应备份到 data/backup/daily，实际目录内容: {os.listdir(daily)}"
        )


if __name__ == "__main__":
    unittest.main()
