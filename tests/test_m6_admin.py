"""M6 验收测试：admin.py 全部路由（总览、记忆、调试、计费、表情包）与数据渲染"""

import os
import unittest
from aiohttp.test_utils import AioHTTPTestCase, unittest_run_loop
from aiohttp import web

from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import AdminConfig, ProactiveConfig, ReplyConfig
from companion.db import Database, now_str
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager


class TestM6Admin(AioHTTPTestCase):
    async def get_application(self):
        self.test_db_path = f"data/test_m6_{id(self)}.db"
        self.db = Database(self.test_db_path)
        await self.db.init_tables()

        self.persona = Persona.load("characters/example")
        self.affection = AffectionEngine(self.db)
        self.mood = MoodEngine(self.db)
        self.memory = MemoryManager(self.db)
        self.stickers = StickerManager("characters/example/stickers", self.db)
        self.replier = Replier(ReplyConfig(), self.stickers)
        self.proactive = ProactiveScheduler(
            config=ProactiveConfig(),
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=None,
            db=self.db,
            send_msg_fn=None,
        )
        self.assembler = PromptAssembler(
            self.persona, self.affection, self.mood, self.memory, self.stickers, self.db
        )
        self.admin = AdminServer(
            config=AdminConfig(),
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            proactive=self.proactive,
            assembler=self.assembler,
            db=self.db,
        )

        app = web.Application()
        app.router.add_get("/", self.admin.handle_overview)
        app.router.add_get("/memory", self.admin.handle_memory)
        app.router.add_get("/debug", self.admin.handle_debug)
        app.router.add_get("/costs", self.admin.handle_costs)
        app.router.add_get("/stickers", self.admin.handle_stickers)
        app.router.add_get("/stickers/img/{name}", self.admin.handle_sticker_image)
        app.router.add_get("/logs", self.admin.handle_logs)
        return app

    async def tearDownAsync(self):
        await self.db.close()
        await super().tearDownAsync()
        if os.path.exists(self.test_db_path):
            try:
                os.remove(self.test_db_path)
            except Exception:
                pass

    @unittest_run_loop
    async def test_overview_route(self):
        resp = await self.client.request("GET", "/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("好感度状态", text)
        self.assertIn("情绪与心理", text)

    @unittest_run_loop
    async def test_memory_route(self):
        resp = await self.client.request("GET", "/memory")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("情景记忆日记", text)
        self.assertIn("关于机主的语义记忆", text)

    @unittest_run_loop
    async def test_debug_route(self):
        resp = await self.client.request("GET", "/debug")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("最近一次完整 System Prompt", text)

    @unittest_run_loop
    async def test_costs_route(self):
        resp = await self.client.request("GET", "/costs")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("总费用汇总", text)

    @unittest_run_loop
    async def test_stickers_route(self):
        resp = await self.client.request("GET", "/stickers")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("表情包图库", text)

    @unittest_run_loop
    async def test_logs_route(self):
        resp = await self.client.request("GET", "/logs")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        self.assertIn("运行日志", text)

    @unittest_run_loop
    async def test_overview_enhanced_features(self):
        # 写入测试数据检验 4A 与 4D
        await self.db.execute(
            "INSERT INTO turns (role, content, created_at) VALUES ('user', 'hi', '2026-09-01 10:00')"
        )
        await self.db.execute(
            "UPDATE counters SET value = 42 WHERE key = 'total_turns'"
        )
        await self.db.execute(
            "INSERT INTO milestones (stage, reached_at) VALUES (1, '2026-09-01 12:00')"
        )
        resp = await self.client.request("GET", "/")
        self.assertEqual(resp.status, 200)
        text = await resp.text()
        # 4A: 雷达图 SVG 与阶段进度条
        self.assertIn("<svg", text)
        self.assertIn("polygon", text)
        self.assertIn("进阶进度", text)
        # 4D: 关系档案字段
        self.assertIn("关系档案", text)
        self.assertIn("认识天数:", text)
        self.assertIn("42", text)
        self.assertIn("里程碑时间线:", text)


if __name__ == "__main__":
    unittest.main()
