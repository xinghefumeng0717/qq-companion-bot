"""手机端看板适配 + 网页图标接线测试 (tests/test_mobile_dashboard.py)

本轮改动解决的问题：
1. `html_shell()` 的 `<head>` 没有 viewport meta —— 手机上整页会按 ~980px 缩放，
   字小得看不清（这是"手机端不能看"的最大根因）；
2. 浏览器标签页 / 手机"添加到主屏幕"没有图标（缺 favicon 与 apple-touch-icon）；
3. 手机竖屏下导航 7 个链接被挤成一团、宽表把页面撑出横向滚动。

本测试集钉住四件事：
- html_shell 一定输出 viewport meta 与两行图标 link；
- 图标 link 与导航链接同一套 token 规则（token 非空带 `?token=`，空则一个 token 字样都不多）；
- `/favicon.png`、`/apple-touch-icon.png` 两条路由：token 空模式 200 + image/png，
  token 非空模式无 token 403 / 带对 token 200；文件缺失返回 404 而不是抛异常；
- /costs 的宽表包了 `.table-scroll`，且手机样式全部收在 `@media (max-width: 768px)` 里
  （桌面端一个字节都不许变）。

调优层（在适配之上叠加的手机端专项优化）另钉住：
- 触控目标：导航链接盒子 ≥40px、`.btn-action` 44px；
- 总览页信息重排：`.container.page-overview` 变 flex 列 + order 把手机端顺序排成
  hero → 阶段台阶 → 好感度 → PAD → 关系档案，且这条 flex 规则不许挂到全站 .container 上；
- 关键数字：复合好感分 ≥28px、阶段名与进度文字加重；
- 语义钩子只加 class（其余 6 页的容器仍是 `<div class="container">`）。

全部本地 mock 请求，零真实网络与真实 API。
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

from companion.admin import (  # noqa: E402
    APPLE_TOUCH_ICON_FILE,
    ASSETS_DIR,
    FAVICON_FILE,
    KAI_FONT_FILE,
    AdminServer,
)
from companion.admin_render import HTML_STYLE, html_shell, render_nav  # noqa: E402
from companion.config import AdminConfig  # noqa: E402
from helpers import close_db, make_db, make_engine_stack  # noqa: E402

MOBILE_MEDIA_QUERY = "@media (max-width: 768px)"


class _Req:
    """最小假请求：够 handle_favicon / 各读页面处理器用。"""

    def __init__(self, *, headers=None, query=None):
        self.headers = headers or {}
        self.query = query or {}


class TestHtmlShellMobile(unittest.TestCase):
    """页面外壳：viewport meta + 图标 link。"""

    def test_head含viewport_meta(self):
        html = html_shell("总览", "/", "BODY")
        self.assertIn(
            '<meta name="viewport" content="width=device-width, initial-scale=1">',
            html,
            "缺 viewport meta，手机上整页会按 ~980px 缩放",
        )

    def test_head含两行图标link(self):
        html = html_shell("总览", "/", "BODY")
        self.assertIn('<link rel="icon" type="image/png" href="/favicon.png">', html)
        self.assertIn('<link rel="apple-touch-icon" href="/apple-touch-icon.png">', html)

    def test_token为空时图标link不带query且不出现token字样(self):
        html = html_shell("总览", "/", "BODY", "")
        self.assertIn('href="/favicon.png"', html)
        self.assertIn('href="/apple-touch-icon.png"', html)
        self.assertNotIn("token=", html, "token 为空时输出必须与旧版同一风格")

    def test_token非空时图标link带上token(self):
        html = html_shell("总览", "/", "BODY", "?token=s3cret")
        self.assertIn('href="/favicon.png?token=s3cret"', html)
        self.assertIn('href="/apple-touch-icon.png?token=s3cret"', html)

    def test_图标link的token规则与导航一致(self):
        """两者用同一个 token_query 参数：导航带 token 时图标也必须带，否则图标请求被 403。"""
        token_query = "?token=abc%2Fdef"
        html = html_shell("总览", "/", "BODY", token_query)
        self.assertIn(f'href="/memory{token_query}"', render_nav("/", token_query))
        self.assertIn(f'href="/favicon.png{token_query}"', html)
        self.assertIn(f'href="/apple-touch-icon.png{token_query}"', html)


class TestMobileStyleBlock(unittest.TestCase):
    """手机样式收在 media 块里，桌面端不受影响。"""

    def _media_block(self) -> str:
        start = HTML_STYLE.index(MOBILE_MEDIA_QUERY)
        return HTML_STYLE[start:]

    def test_media块存在且含手机规则(self):
        block = self._media_block()
        for selector in (
            ".container { padding: 14px; }",
            ".nav-links {",
            ".nav-right { display: none; }",
            ".table-scroll { overflow-x: auto; }",
            ".log-window { max-height: 55vh; }",
            "h2 { font-size: 18px; }",
            "svg { max-width: 100%; height: auto; }",
        ):
            with self.subTest(selector=selector):
                self.assertIn(selector, block)

    def test_导航横滑设置齐备(self):
        block = self._media_block()
        for rule in ("overflow-x: auto;", "flex-wrap: nowrap;", "-webkit-overflow-scrolling: touch;"):
            with self.subTest(rule=rule):
                self.assertIn(rule, block)

    def test_栅格下限不再卡住窄屏(self):
        """minmax(min(300px, 100%), 1fr)：视口宽时等价旧值，390px 屏才不会被 300px 下限撑破。"""
        self.assertIn("grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));", HTML_STYLE)


class TestIconRoutes(unittest.IsolatedAsyncioTestCase):
    """两条图标路由：鉴权与返回类型。"""

    TOKEN = "s3cret"

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_mobile_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.db = await make_db(self.db_path)

    async def asyncTearDown(self):
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _admin(self, token: str = "") -> AdminServer:
        return make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=token),
            db_path=self.db_path,
            backup_dir=os.path.join(self.tmp, "backup"),
        ).admin

    async def test_两张图标文件都在仓库里(self):
        for filename in (FAVICON_FILE, APPLE_TOUCH_ICON_FILE):
            with self.subTest(filename=filename):
                self.assertTrue(
                    os.path.isfile(os.path.join(ASSETS_DIR, filename)),
                    f"companion/assets/{filename} 缺失，图标路由会一直 404",
                )

    async def test_token为空时图标照常200且是png(self):
        admin = self._admin("")
        for handler in (admin.handle_favicon, admin.handle_apple_touch_icon):
            with self.subTest(handler=handler.__name__):
                resp = await handler(_Req())
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.content_type, "image/png")
                self.assertGreater(len(resp.body), 0)

    async def test_返回的确是png魔数(self):
        admin = self._admin("")
        resp = await admin.handle_favicon(_Req())
        self.assertEqual(resp.body[:8], b"\x89PNG\r\n\x1a\n")

    async def test_token非空时无token一律403(self):
        admin = self._admin(self.TOKEN)
        for handler in (admin.handle_favicon, admin.handle_apple_touch_icon):
            with self.subTest(handler=handler.__name__):
                resp = await handler(_Req())
                self.assertEqual(resp.status, 403)

    async def test_token非空时带对token可读(self):
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_favicon(_Req(query={"token": self.TOKEN}))
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "image/png")

    async def test_文件缺失返回404而不是炸服务(self):
        admin = self._admin("")
        with patch("companion.admin.ASSETS_DIR", os.path.join(self.tmp, "no-such-dir")):
            resp = await admin.handle_favicon(_Req())
        self.assertEqual(resp.status, 404)


class TestKaiFontRoute(unittest.IsolatedAsyncioTestCase):
    """霞鹜文楷 webfont 路由：刻意免鉴权（CSS url() 带不了 ?token=），钉住防回退。"""

    TOKEN = "s3cret"

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_kaifont_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.db = await make_db(self.db_path)

    async def asyncTearDown(self):
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _admin(self, token: str = "") -> AdminServer:
        return make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=token),
            db_path=self.db_path,
            backup_dir=os.path.join(self.tmp, "backup"),
        ).admin

    async def test_字库文件在仓库里且是woff2魔数(self):
        path = os.path.join(ASSETS_DIR, KAI_FONT_FILE)
        self.assertTrue(os.path.isfile(path), f"companion/assets/{KAI_FONT_FILE} 缺失")
        with open(path, "rb") as f:
            self.assertEqual(f.read(4), b"wOF2")

    async def test_token模式下不带token也照常200(self):
        """免鉴权是有意设计（字体是公开 OFL 资产），不许被"统一鉴权"顺手收编。"""
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_kai_font(_Req())
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.content_type, "font/woff2")

    async def test_长缓存头且文件缺失404(self):
        admin = self._admin("")
        resp = await admin.handle_kai_font(_Req())
        self.assertIn("immutable", resp.headers.get("Cache-Control", ""))
        with patch("companion.admin.ASSETS_DIR", os.path.join(self.tmp, "no-such-dir")):
            resp = await admin.handle_kai_font(_Req())
        self.assertEqual(resp.status, 404)

    def test_样式表声明fontface且字体栈webfont优先(self):
        self.assertIn("@font-face", HTML_STYLE)
        self.assertIn(f'/assets/{KAI_FONT_FILE}', HTML_STYLE)
        m = re.search(r"\.font-sentiment\s*\{[^}]*font-family:\s*([^;]+);", HTML_STYLE)
        self.assertIsNotNone(m)
        self.assertTrue(m.group(1).strip().startswith('"QZKai"'))


class TestIconRoutesOverHTTP(unittest.IsolatedAsyncioTestCase):
    """真起一遍 AdminServer（走的是 start() 里那段路由注册），确认两条图标路由真的接得上。

    上面那个类直接调处理器，绕过了路由表——万一忘了 app.router.add_get，处理器写得再好
    浏览器也只会拿到 404。这里用一个空闲端口真起服务、真发 HTTP 请求。
    """

    async def asyncSetUp(self):
        import socket

        import aiohttp

        self.tmp = tempfile.mkdtemp(prefix="qqc_mobile_http_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.db = await make_db(self.db_path)
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        admin = make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=self.port, token=""),
            db_path=self.db_path,
            backup_dir=os.path.join(self.tmp, "backup"),
        ).admin
        try:
            await admin.start()
        except OSError as e:  # pragma: no cover - 端口被抢时跳过，不算失败
            self.skipTest(f"端口 {self.port} 不可用: {e}")
        self.admin = admin
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.client.close()
        await self.admin.stop()
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_图标路由真接得上且返回png(self):
        for path in ("/favicon.png", "/apple-touch-icon.png"):
            with self.subTest(path=path):
                async with self.client.get(
                    f"http://127.0.0.1:{self.port}{path}"
                ) as resp:
                    self.assertEqual(resp.status, 200)
                    self.assertEqual(resp.headers["Content-Type"], "image/png")
                    self.assertEqual((await resp.read())[:8], b"\x89PNG\r\n\x1a\n")

    async def test_页面里的图标link指向的路径确实存在(self):
        async with self.client.get(f"http://127.0.0.1:{self.port}/") as resp:
            html = await resp.text()
        for path in ("/favicon.png", "/apple-touch-icon.png"):
            with self.subTest(path=path):
                self.assertIn(f'href="{path}"', html)
                async with self.client.get(f"http://127.0.0.1:{self.port}{path}") as r:
                    self.assertEqual(r.status, 200)


class TestCostsWideTable(unittest.IsolatedAsyncioTestCase):
    """宽表包 .table-scroll（手机端可横滑，不再把页面撑出横向滚动）。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_mobile_costs_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.db = await make_db(self.db_path)
        await self.db.execute(
            "INSERT INTO llm_calls (purpose, model, prompt_tokens, completion_tokens, "
            "cost_estimate, cache_hit_tokens, cache_miss_tokens, created_at) "
            "VALUES ('chat', 'deepseek-flash', 1200, 180, 0.0035, 1000, 200, '2026-10-08 13:00')"
        )
        self.admin = make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=""),
            db_path=self.db_path,
            backup_dir=os.path.join(self.tmp, "backup"),
        ).admin

    async def asyncTearDown(self):
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_计费页宽表包了table_scroll(self):
        resp = await self.admin.handle_costs(_Req())
        self.assertEqual(resp.status, 200)
        self.assertIn('<div class="table-scroll">', resp.text)
        # 两张表（按用途分组 + 最近 20 次明细）都要包
        self.assertEqual(resp.text.count('<div class="table-scroll">'), 2)
        self.assertEqual(resp.text.count("</table>"), 2)

    async def test_包在table_scroll里的确实是宽表(self):
        resp = await self.admin.handle_costs(_Req())
        idx = resp.text.index('<div class="table-scroll">')
        self.assertIn('<table class="table">', resp.text[idx : idx + 200])


class TestMobileTuningRules(unittest.TestCase):
    """调优层的样式规则：触控目标、关键数字放大，且全部落在 media 块内。"""

    def _media_block(self) -> str:
        start = HTML_STYLE.index(MOBILE_MEDIA_QUERY)
        return HTML_STYLE[start:]

    def test_触控目标规则齐备(self):
        block = self._media_block()
        self.assertIn(".nav-links a {", block)
        self.assertIn("padding: 10px 2px;", block)
        self.assertIn(".btn-action { min-height: 44px; }", block)

    def test_导航链接触控高度够40px(self):
        """14px 字 × 1.6 行高 + 上下各 10px ≈ 42.4px，必须 ≥40px。"""
        block = self._media_block()
        padding = re.search(r"\.nav-links a \{[^}]*padding: (\d+)px 2px;", block)
        self.assertIsNotNone(padding, "找不到导航链接的垂直 padding 规则")
        link_height = 14 * 1.6 + 2 * int(padding.group(1))
        self.assertGreaterEqual(link_height, 40.0, f"触控高度只有 {link_height}px")

    def test_复合好感分大数字不小于28px(self):
        block = self._media_block()
        match = re.search(r"\.composite-score \{ font-size: (\d+)px", block)
        self.assertIsNotNone(match, "找不到 .composite-score 的手机端字号规则")
        self.assertGreaterEqual(int(match.group(1)), 28)

    def test_阶段名与进度文字加重(self):
        block = self._media_block()
        self.assertIn(".stage-name {", block)
        self.assertIn(".stage-progress { font-weight: 600 !important; }", block)

    def test_调优规则全在媒体块内(self):
        """桌面端不许沾到这些规则：它们第一次出现的位置必须在 @media 之后。"""
        media_start = HTML_STYLE.index(MOBILE_MEDIA_QUERY)
        for rule in (
            ".btn-action { min-height: 44px; }",
            ".composite-score {",
            ".stage-name {",
            ".stage-progress {",
        ):
            with self.subTest(rule=rule):
                self.assertGreater(HTML_STYLE.index(rule), media_start)


class TestOverviewMobileReorder(unittest.TestCase):
    """总览页手机端重排：container 变 flex 列 + order 决定顺序（桌面没有这些规则）。"""

    def _media_block(self) -> str:
        return HTML_STYLE[HTML_STYLE.index(MOBILE_MEDIA_QUERY) :]

    def test_媒体块含总览页重排order规则(self):
        block = self._media_block()
        self.assertIn(".container.page-overview {", block)
        self.assertIn("display: flex;", block)
        self.assertIn("flex-direction: column;", block)
        for order in ("1", "2", "3", "4"):
            with self.subTest(order=order):
                self.assertIn(f"order: {order};", block)
        self.assertIn(".sec-vitals > .sec-stage { order: -1; }", block)

    def test_重排只认总览页容器类(self):
        """flex 列只为总览页的重排服务，不许挂到全站 .container 上（那会波及另外 6 页）。"""
        block = self._media_block()
        scoped = block.split(".container.page-overview {")[1].split("}")[0]
        self.assertIn("display: flex;", scoped)
        self.assertIn("flex-direction: column;", scoped)
        global_rule = block.split(".container {")[1].split("}")[0]
        self.assertNotIn("flex", global_rule, "全站 .container 的规则里不该出现 flex")

    def test_order映射到期望的阅读顺序(self):
        """把 selectors 抽出 order 值，验证 hero < 阶段块 < PAD < 关系档案。"""
        block = self._media_block()
        orders = dict(
            re.findall(
                r"\.container\.page-overview > \.(sec-\w+) \{ order: (-?\d+); \}", block
            )
        )
        self.assertEqual(
            orders,
            {"sec-hero": "1", "sec-vitals": "2", "sec-pad": "3", "sec-profile": "4"},
        )
        # 阶段台阶在 .sec-vitals 内部还要翻到好感度前面（手机上 .grid-2 已是单列）
        self.assertIn(".sec-vitals > .sec-stage { order: -1; }", block)


class TestOverviewClassHooks(unittest.IsolatedAsyncioTestCase):
    """语义钩子只加 class，且只有总览页带 page-overview。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_mobile_overview_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.db = await make_db(self.db_path)
        self.admin = make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=""),
            db_path=self.db_path,
            backup_dir=os.path.join(self.tmp, "backup"),
        ).admin

    async def asyncTearDown(self):
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_总览页带全部section类名(self):
        html = (await self.admin.handle_overview(_Req())).text
        for cls in (
            "card-hero sec-hero",
            "grid-2 section-block sec-vitals",
            'class="sec-affection"',
            'class="sec-stage"',
            "section-block sec-pad",
            "grid-2 section-block sec-profile",
            "composite-score",
            "stage-name",
            "stage-progress",
        ):
            with self.subTest(cls=cls):
                self.assertIn(cls, html)

    async def test_总览页容器类为page_overview(self):
        html = (await self.admin.handle_overview(_Req())).text
        self.assertIn('<div class="container page-overview">', html)

    async def test_其余页面容器类不带page_overview(self):
        handlers = (
            ("/memory", self.admin.handle_memory),
            ("/debug", self.admin.handle_debug),
            ("/costs", self.admin.handle_costs),
            ("/stickers", self.admin.handle_stickers),
            ("/logs", self.admin.handle_logs),
            ("/admin", self.admin.handle_admin),
        )
        for path, handler in handlers:
            with self.subTest(path=path):
                html = (await handler(_Req())).text
                self.assertIn('<div class="container">', html)
                self.assertNotIn('<div class="container page-overview">', html)

    async def test_html_shell容器类参数默认值不改变旧输出(self):
        self.assertIn('<div class="container">', html_shell("总览", "/", "BODY"))
        self.assertIn('<div class="container">', html_shell("总览", "/", "BODY", "", ""))
        self.assertIn(
            '<div class="container page-overview">',
            html_shell("总览", "/", "BODY", "", "page-overview"),
        )


if __name__ == "__main__":
    unittest.main()
