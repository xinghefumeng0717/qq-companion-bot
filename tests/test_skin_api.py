"""新式皮肤：主题判定、页面查找兜底、追踪还原、日志拆行、/api/v1 鉴权、小窝用卡里的页面"""

import json
import os
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import launcher.home as home_mod
from companion import skin_api
from companion.admin import resolve_theme
from companion.config import AdminConfig
from helpers import close_db, make_db, make_engine_stack


def _card_with_pages(root: str, files: dict) -> str:
    pages = os.path.join(root, "theme", "pages")
    os.makedirs(pages)
    for name, text in files.items():
        with open(os.path.join(pages, name), "w", encoding="utf-8") as f:
            f.write(text)
    return root


class TestThemeAndFiles(unittest.TestCase):
    def test_card_pages_win_unless_config_says_otherwise(self):
        with tempfile.TemporaryDirectory() as d:
            _card_with_pages(d, {"home.html": "x"})
            self.assertEqual(resolve_theme(AdminConfig(), d), "skin")
            self.assertEqual(resolve_theme(AdminConfig(theme="journal"), d), "journal")
            self.assertEqual(resolve_theme(AdminConfig(theme="skin"), "characters/example"), "skin")

    def test_card_first_then_default_and_nothing_outside(self):
        with tempfile.TemporaryDirectory() as d:
            pages = os.path.join(_card_with_pages(d, {"home.html": "mine", "x.py": "no"}), "theme", "pages")
            self.assertEqual(skin_api.find_file(pages, "home.html"), os.path.realpath(os.path.join(pages, "home.html")))
            self.assertTrue(skin_api.find_file(pages, "memory.html").startswith(os.path.realpath(skin_api.DEFAULT_SKIN_DIR)))
            self.assertIsNone(skin_api.find_file(pages, "x.py"))
            self.assertIsNone(skin_api.find_file(pages, "../../../config.example.toml"))
            self.assertIsNone(skin_api.find_file(pages, "../theme/pages/home.html/../../../../README.md"))


class TestTraces(unittest.TestCase):
    def _events(self):
        system = "你是她。\n\n【角色核心】\n她拉大提琴。\n\n【聊天准则】\n像人说话。"
        ev = lambda step, payload: {"trace_id": "tr_1", "step": step, "timestamp": "2026-11-19T23:30:00", "payload": payload}
        return [
            ev("turn_input", {"text": "困了，先睡啦", "source": "user"}),
            ev("model_context", {"messages": [
                {"role": "system", "content": system}, {"role": "user", "content": "在吗"},
                {"role": "system", "content": "【你此刻的状态】\n有点在意。"}, {"role": "user", "content": "困了，先睡啦"}]}),
            ev("model_raw", {"text": "晚安\n明天见", "served_model": "flash"}),
            ev("delivery_plan", {"draft": {"bubbles": [{"bubble_id": "b0", "text": "晚安"}, {"bubble_id": "b1", "text": "明天见"}]},
                                 "plan": {"actions": [{"bubble_id": "b0", "channel": "voice"}, {"bubble_id": "b1"}]}}),
            ev("delivery_result", {"status": "delivered", "messages": [{"channel": "voice", "text": "晚安", "message_id": 1}]}),
            ev("validation", {"passed": True, "errors": []}),
        ]

    def test_turn_rebuilt_from_trace(self):
        t = skin_api.turn_detail("tr_1", self._events())
        titles = [s["title"] for s in t["read"]["sections"]]
        self.assertEqual(titles, ["开场", "角色核心", "聊天准则", "最近对话", "你此刻的状态"])
        self.assertEqual(t["write"]["model"], "flash")
        self.assertEqual(len(t["send"]["planned"]), 2)
        self.assertEqual([m["message_id"] for m in t["send"]["sent"]], [1])  # 只认真实回执
        self.assertEqual(skin_api.latest_state_note({"tr_1": self._events()})[0], "有点在意。")
        self.assertTrue(skin_api.model_health({"tr_1": self._events()})["ok"])

    def test_log_line_split(self):
        l = skin_api.classify_log("2026-11-19 12:30:58,101 [WARNING] companion.onebot: [OneBot] 发送回执超时")
        self.assertEqual((l["time"], l["level"], l["module"], l["msg"]), ("12:30:58", "warn", "OneBot", "发送回执超时"))
        self.assertEqual(skin_api.classify_log("Traceback (most recent call last):")["level"], "error")


class TestApiRoutes(AioHTTPTestCase):
    async def get_application(self):
        self.db_path = f"data/test_skin_{id(self)}.db"
        self.db = await make_db(self.db_path)
        stack = make_engine_stack(self.db, include_admin=True, admin_config=AdminConfig(theme="skin", token="t0k"))
        self.admin = stack.admin
        self.admin.trace_dir = tempfile.mkdtemp()
        app = web.Application()
        app.router.add_get("/api/v1/{name}", self.admin.handle_api_v1)
        app.router.add_get("/theme/{path:.+}", self.admin.handle_theme_file)
        app.router.add_get("/", self.admin._skin_page_handler("home"))
        return app

    async def tearDownAsync(self):
        await close_db(self.db, self.db_path)
        await super().tearDownAsync()

    async def test_every_endpoint_answers_json_with_token_only(self):
        for name in ("now", "memory", "debug", "costs", "stickers", "logs", "admin"):
            bad = await self.client.request("GET", f"/api/v1/{name}")
            self.assertEqual(bad.status, 403, name)
            ok = await self.client.request("GET", f"/api/v1/{name}?token=t0k")
            self.assertEqual(ok.status, 200, name)
            json.loads(await ok.text())
        page = await self.client.request("GET", "/")
        self.assertEqual(page.status, 403)
        page = await self.client.request("GET", "/?token=t0k")
        self.assertIn("/theme/skin.js", await page.text())
        asset = await self.client.request("GET", "/theme/skin.js")  # 样子不带数据，不要 token
        self.assertEqual(asset.status, 200)


class TestLauncherPage(unittest.TestCase):
    def test_card_launcher_page_and_window_size(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(home_mod.skin_launcher_page(d))
            _card_with_pages(d, {"launcher.html": '<meta name="launcher-size" content="456,800">'})
            page = home_mod.skin_launcher_page(d)
            self.assertEqual(home_mod.launcher_window_size(page), "456,800")
        self.assertEqual(home_mod.launcher_window_size(None), "980,660")


class TestStrictStateRendering(unittest.TestCase):
    """云端审查回归：「一致 / 通过 / 已连接」只能由严格真值点亮，null 与缺失一律不算数。

    私有卡的页面不进公开仓库：这里用临时构造的卡页面固定同一契约——
    任何卡的皮肤页面都必须按严格真值判断，且会原样经皮肤机制对外提供。
    """

    # 契约：调试页把一轮算作「一致」的必要条件（缺/null 一律不算数）。
    STRICT_DEBUG_PAGE = """
        <script>
        var consistent = (t) =>
            t.check.passed === true &&   // 缺失校验（null）不得算一致
            !t.write.error &&            // 模型错误不得算一致
            plannedN > 0 &&              // 无计划不得算一致
            receiptN === plannedN;       // 失败/无回执不得算一致
        </script>
    """

    # 契约：小窝页只在 model.ok === true 时显示已连接（ok=null/缺失 = 未验证）。
    STRICT_LAUNCHER_PAGE = """
        <html><head><meta name="launcher-size" content="456,800"></head>
        <body><script>var connected = (model) => model.ok === true;</script></body></html>
    """

    def test_debug_page_served_from_card_keeps_strict_consistency(self):
        with tempfile.TemporaryDirectory() as d:
            _card_with_pages(d, {"debug.html": self.STRICT_DEBUG_PAGE})
            path = skin_api.find_file(skin_api.card_pages_dir(d), "debug.html")
            self.assertEqual(path, os.path.realpath(os.path.join(d, "theme", "pages", "debug.html")))
            with open(path, encoding="utf-8") as f:
                html = f.read()
            for marker in ("t.check.passed === true", "!t.write.error", "plannedN > 0", "receiptN === plannedN"):
                self.assertIn(marker, html)
            self.assertNotIn("passed !== false", html)

    def test_default_skin_check_has_three_states(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "companion", "skins", "default", "default.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn('t.check.passed === true ? "通过"', js)
        self.assertIn("未通过", js)
        self.assertIn("未校验", js)                       # passed=null 显示未校验，不是通过

    def test_launcher_page_model_connected_only_on_true(self):
        with tempfile.TemporaryDirectory() as d:
            _card_with_pages(d, {"launcher.html": self.STRICT_LAUNCHER_PAGE})
            page = home_mod.skin_launcher_page(d)
            self.assertIsNotNone(page)
            with open(page, encoding="utf-8") as f:
                html = f.read()
            self.assertIn("model.ok === true", html)     # ok=null/缺失 = 未验证，不当已连接
            self.assertNotIn("model.ok !== false", html)
            self.assertEqual(home_mod.launcher_window_size(page), "456,800")


class TestScreenshotThemeFollowsCard(unittest.TestCase):
    """不带 --theme 时角色卡 theme/pages 必须生效；只有显式 --theme 才覆盖。"""

    def test_only_explicit_theme_overrides_card(self):
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from scripts.util.screenshot_helper import admin_config_for
        with tempfile.TemporaryDirectory() as d:
            _card_with_pages(d, {"home.html": "x"})
            self.assertEqual(resolve_theme(admin_config_for(""), d), "skin")
            self.assertEqual(resolve_theme(admin_config_for("journal"), d), "journal")
            self.assertEqual(resolve_theme(admin_config_for("endfield"), d), "endfield")
        self.assertEqual(resolve_theme(admin_config_for(""), "characters/nope"), "journal")


if __name__ == "__main__":
    unittest.main()
