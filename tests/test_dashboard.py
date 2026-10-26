"""新版看板：主题开关、角色卡皮肤、总览/记忆渲染、新路由（journal 默认不变）"""

import asyncio
import json
import os
import socket
import tempfile
import unittest

import aiohttp

from companion import dashboard, dashboard_pages as pages
from companion.admin import AdminServer
from companion.admin_render import html_shell
from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import AdminConfig, _admin_theme
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.stickers import StickerManager

SI = os.path.join("characters", "si")


class TestThemeConfig(unittest.TestCase):
    def test_default_and_fallback(self):
        self.assertEqual(AdminConfig().theme, "journal")
        self.assertEqual(_admin_theme("Endfield"), "endfield")
        self.assertEqual(_admin_theme("plain"), "plain")
        self.assertEqual(_admin_theme("赛博朋克"), "journal")
        self.assertEqual(_admin_theme(None), "journal")


class TestSkin(unittest.TestCase):
    def test_missing_skin_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(dashboard.load_skin(d))

    def test_broken_json_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "theme"))
            with open(os.path.join(d, "theme", "skin.json"), "w") as f:
                f.write("{坏")
            self.assertIsNone(dashboard.load_skin(d))

    @unittest.skipUnless(os.path.isdir(os.path.join(SI, "theme")), "祀的皮肤只在私人仓库")
    def test_si_skin(self):
        skin = dashboard.load_skin(SI)
        self.assertTrue(skin.ink)
        self.assertTrue(skin.avatar_for("欣喜"))
        self.assertEqual(skin.avatar_for("没这个心情"), skin.avatar["default"])
        self.assertEqual(skin.avatar_for("欣喜", asleep=True), skin.avatar["asleep"])
        for rel in list(skin.avatar.values()) + list(skin.cursor.values()) + list(skin.art.values()):
            self.assertIsNotNone(dashboard.skin_file(skin, rel), rel)

    def test_skin_file_stays_inside_theme_dir(self):
        with tempfile.TemporaryDirectory() as d:
            os.makedirs(os.path.join(d, "theme"))
            for name in ("ok.png", os.path.join("..", "secret.png"), "notes.txt"):
                with open(os.path.join(d, "theme", name), "wb") as f:
                    f.write(b"x")
            skin = dashboard.Skin(base_dir=os.path.join(d, "theme"))
            self.assertIsNotNone(dashboard.skin_file(skin, "ok.png"))
            self.assertIsNone(dashboard.skin_file(skin, "../secret.png"))
            self.assertIsNone(dashboard.skin_file(skin, "notes.txt"))
            self.assertIsNone(dashboard.skin_file(skin, "nope.png"))
            self.assertIsNone(dashboard.skin_file(None, "ok.png"))


def _overview_data(**over):
    from datetime import datetime

    d = {
        "now": datetime(2026, 10, 22, 21, 4), "name": "祀<script>", "stage": 5, "n_stages": 10,
        "stage_name": "伙伴", "tone": "傲娇在意", "next_name": "偏爱", "progress": 0.62, "floor_name": "旧识",
        "dims": [(zh, 80.0) for _, zh in dashboard.DIM_ORDER], "mood_label": "愉悦", "mood_desc": "心情不错",
        "activity": "在缘缘庄对账", "asleep": False, "arc": ("月底对账", "有点烦"), "last_chat": "3 小时前",
        "days_known": 31, "turns": 842, "impressions": ["<b>坏</b>"], "meetups": [], "followups": [],
        "desires": [], "unanswered": 0, "max_unanswered": 2, "onebot": True,
        "cost_today": 0.42, "cost_month": 9.8, "backup": "今天 04:00",
    }
    d.update(over)
    return d


class TestRender(unittest.TestCase):
    def test_overview_escapes_and_runs_without_skin(self):
        page = dashboard.render_overview(_overview_data(), None, "")
        self.assertNotIn("<script>", page)
        self.assertNotIn("<b>坏</b>", page)
        self.assertIn("心里挂着「月底对账」", page)
        self.assertIn("最近没有约好的见面", page)

    def test_shell_marks_theme_and_nav(self):
        html = dashboard.render_shell("总览", "/", "<p>x</p>", "endfield", None, "?token=t", "祀")
        self.assertIn('class="theme-endfield"', html)
        self.assertIn('href="/memory?token=t"', html)
        self.assertIn('aria-current="page"', html)

    def test_shell_uses_small_font_and_mobile_title(self):
        html = dashboard.render_shell("总览", "/", "<p>x</p>", "endfield", None, "", "祀")
        self.assertIn(f'/assets/{dashboard.DASHBOARD_KAI_FONT_FILE}', html)
        self.assertIn('"QZKai Full"', html)
        self.assertIn('content="祀的小窝"', html)
        self.assertIn('/assets/lxgw-wenkai-gb2312.woff2', html)

    def test_only_overview_refreshes_in_background(self):
        overview = dashboard.render_shell("总览", "/", "<p>x</p>", "endfield", None)
        other = dashboard.render_shell("日志", "/logs", "<p>x</p>", "endfield", None)
        for page in (overview, other):
            self.assertNotIn('http-equiv="refresh"', page)
        self.assertIn("window.setInterval(refresh, 30000)", overview)
        self.assertIn("current.replaceWith(next)", overview)
        self.assertIn("window.scrollTo(0, scrollY)", overview)
        self.assertNotIn("window.setInterval(refresh", other)

    def test_memory_empty_states(self):
        page = dashboard.render_memory(
            {"about": {"core": [], "recent": []}, "self": {"core": [], "recent": []},
             "diaries": [], "meetups": [], "unsaid": [], "unsaid_old": []}, None, "")
        self.assertIn("还没有日记", page)
        self.assertIn("没有憋着的话", page)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _now():
    from datetime import datetime

    return datetime(2026, 10, 22, 21, 4)


def _costs_data(**over):
    d = {
        "now": _now(), "total_calls": 7, "total_cost": 3.626, "cost_24h": 3.5403,
        "cost_today": 3.5403, "cache_hit": 1628000.0, "cache_total": 2001554.0,
        "cache_rate": 81.3,
        "groups": [{"purpose": "main_chat", "calls": 1, "p_tok": 1237706,
                    "c_tok": 42485, "cost": 2.0012}],
        "details": [{"id": 7, "purpose": "vision_perception", "model": "qwen-vl",
                     "p_tok": 7950, "c_tok": 2397, "cost": 0.023,
                     "at": "2026-10-20 16:22"}],
    }
    d.update(over)
    return d


def _debug_data(**over):
    d = {
        "now": _now(), "system_prompt": "<b>提示词</b>", "prompt_chars": 11,
        "all": {"cnt": 6, "sd": 5.37, "rs": 5.38, "wa": 5.67, "re": 5.58},
        "week": {},
        "records": ['{"score": 6.2}'],
    }
    d.update(over)
    return d


def _logs_data(**over):
    d = {
        "now": _now(), "path": "data/logs/bot.log", "missing": False, "error": "",
        "lines": ["2026-10-22 18:02:11 [INFO] 已连接",
                  "2026-10-22 18:26:40 [ERROR] 语音合成超时",
                  "2026-10-22 18:07:47 [WARNING] 回复被重写"],
    }
    d.update(over)
    return d


def _admin_data(**over):
    d = {
        "now": _now(), "last_backup": "2026-10-22 04:00", "token_field": "",
        "db_path": "data/companion.db", "token_on": False,
    }
    d.update(over)
    return d


class TestNewPageComponents(unittest.TestCase):
    """五页新组件：统一页头/装饰、无内联样式、空状态与转义。"""

    def _all_pages(self):
        return {
            "costs": pages.render_costs(_costs_data()),
            "debug": pages.render_debug(_debug_data()),
            "stickers": pages.render_stickers(
                {"now": _now(), "items": [
                    {"name": "祀<script>", "desc": "<b>坏</b>", "url": "/stickers/img/x?token=t"},
                ]}
            ),
            "logs": pages.render_logs(_logs_data()),
            "admin": pages.render_admin(_admin_data()),
        }

    def test_每页都有编号小标大标题与装饰件(self):
        for name, block in self._all_pages().items():
            with self.subTest(page=name):
                self.assertIn('class="ph"', block)
                self.assertIn('class="ph-mark wide no-ink"', block)
                self.assertIn('class="tickrule"', block)
                self.assertIn('class="seal seal-sm ink-only"', block)
                self.assertIn('class="foot"', block)
                self.assertIn('class="reg"', block)

    def test_每页都没有内联样式(self):
        for name, block in self._all_pages().items():
            with self.subTest(page=name):
                self.assertNotIn("style=", block)

    def test_内容转义(self):
        block = pages.render_stickers(
            {"now": _now(), "items": [
                {"name": "祀<script>", "desc": "<b>坏</b>", "url": "/u?x=1"},
            ]}
        )
        self.assertNotIn("<script>", block)
        self.assertNotIn("<b>坏</b>", block)
        debug = pages.render_debug(_debug_data())
        self.assertNotIn("<b>提示词</b>", debug)

    def test_费用页有统计栅格与两张表(self):
        block = pages.render_costs(_costs_data())
        for text in ("总费用汇总", "按业务用途分组统计", "最近 20 次调用明细",
                     "缓存命中率", "¥3.5403", "1,237,706"):
            self.assertIn(text, block)
        self.assertIn('class="grid-stats"', block)
        self.assertEqual(block.count('class="table-wrap"'), 2)
        self.assertIn("<progress", block)

    def test_费用空表给排版好的空状态(self):
        block = pages.render_costs(_costs_data(groups=[], details=[]))
        self.assertIn("还没有调用记录", block)
        self.assertIn("还没有调用明细", block)
        self.assertEqual(block.count('class="empty-box"'), 2)
        self.assertNotIn('class="table-wrap"', block)

    def test_调试页有打分条与折叠的原始记录(self):
        block = pages.render_debug(_debug_data())
        for text in ("观察者打分分布", "最近一次完整 System Prompt", "原始 JSON"):
            self.assertIn(text, block)
        self.assertIn("<details", block)
        self.assertIn("<progress", block)
        # 近 7 天没有数据 → 该面板给空状态，不是空白
        self.assertIn("还没有结算记录", block)

    def test_日志页按级别标注并且内容原样保留(self):
        block = pages.render_logs(_logs_data())
        self.assertIn("lvl-error", block)
        self.assertIn("lvl-warn", block)
        self.assertIn("语音合成超时", block)
        self.assertIn("data/logs/bot.log", block)

    def test_日志缺文件时是排版好的空状态(self):
        block = pages.render_logs(_logs_data(missing=True, lines=[]))
        self.assertIn("还没有日志文件", block)
        self.assertIn('class="empty-box"', block)
        self.assertIn("data/logs/bot.log", block)

    def test_管理页三张操作卡与二次确认(self):
        block = pages.render_admin(_admin_data())
        for text in ("管理控制台", "立即备份数据库", "重启伴侣机器人", "重置关系数据",
                     "输入大写", "YES"):
            self.assertIn(text, block)
        self.assertEqual(block.count('action="/admin/'), 3)
        self.assertEqual(block.count("return confirm("), 3)
        self.assertIn('class="btn quiet"', block)
        self.assertIn('class="btn danger"', block)

    def test_结果页三种状态与链接(self):
        ok = pages.render_notice("ok", "备份完成", "BACKUP / DONE",
                                 ["已生成 <code>x.db</code>"],
                                 [("/admin", "返回管理面板")])
        self.assertIn("notice ok", ok)
        self.assertIn('href="/admin"', ok)
        err = pages.render_notice("err", "备份失败", "BACKUP / FAILED", ["异常"])
        self.assertIn("notice err", err)
        listed = pages.render_notice("warn", "重置完成", "RESET / DONE",
                                     ["各表清除详情：", "<ul><li>turns: 清空 1 行</li></ul>"])
        self.assertIn("<ul><li>turns: 清空 1 行</li></ul>", listed)

    def test_样式表有整套页面组件与断点(self):
        css = dashboard.CSS
        for selector in (".ph{", ".grid-stats{", ".tbl{", ".code{", ".gallery{",
                         ".logbox{", ".actions{", ".notice{", ".empty-box{"):
            with self.subTest(selector=selector):
                self.assertIn(selector, css)
        self.assertIn("font-variant-numeric:tabular-nums", css)
        self.assertIn(":focus-visible", css)
        block = css[css.index("@media (max-width:760px)"):]
        for selector in (".ph{", ".grid-stats{", ".gallery{", ".actions{", ".notice{"):
            with self.subTest(mobile=selector):
                self.assertIn(selector, block)


class TestRoutes(unittest.TestCase):
    def _run(self, theme, checks):
        async def go():
            with tempfile.TemporaryDirectory() as tmp:
                db = Database(os.path.join(tmp, "t.db"))
                await db.init_tables()
                card = SI if os.path.isdir(SI) else os.path.join("characters", "example")
                persona = Persona.load(card)
                aff = AffectionEngine.from_persona(db, persona)
                mood = MoodEngine(db)
                mem = MemoryManager(db, gateway=None, affection=aff, persona=persona)
                st = StickerManager(os.path.join(tmp, "stickers"), db)
                asm = PromptAssembler(persona, aff, mood, mem, st, db)
                port = _free_port()
                admin = AdminServer(
                    config=AdminConfig(port=port, theme=theme), persona=persona, affection=aff, mood=mood,
                    memory=mem, stickers=st, proactive=None, assembler=asm, db=db,
                    db_path=os.path.join(tmp, "t.db"), backup_dir=os.path.join(tmp, "bk"),
                )
                await admin.start()
                try:
                    async with aiohttp.ClientSession() as c:
                        await checks(admin, c, f"http://127.0.0.1:{port}")
                finally:
                    await admin.stop()
                    await db.close()

        asyncio.run(go())

    def test_journal_default_unchanged(self):
        async def checks(admin, c, base):
            self.assertEqual(admin._page("计费统计", "/costs", "<p>x</p>"),
                             html_shell("计费统计", "/costs", "<p>x</p>", ""))
            async with c.get(base + "/") as r:
                self.assertIn("card-hero", await r.text())
        self._run("journal", checks)

    def test_endfield_pages_and_assets(self):
        async def checks(admin, c, base):
            anchors = {
                "/debug": ("调试信息", "观察者打分分布", "最近一次完整 System Prompt"),
                "/costs": ("计费统计", "总费用汇总", "按业务用途分组统计"),
                "/stickers": ("表情包库", "表情包图库"),
                "/logs": ("运行日志",),
                "/admin": ("管理控制台", "立即备份数据库", "确认重置数据"),
            }
            for path in ("/", "/memory", "/costs", "/logs", "/debug", "/stickers", "/admin"):
                async with c.get(base + path) as r:
                    self.assertEqual(r.status, 200, path)
                    html = await r.text()
                    self.assertIn('class="theme-endfield', html)
                    self.assertIn('aria-current="page"', html)
                    self.assertNotIn('http-equiv="refresh"', html, path)
                    for anchor in anchors.get(path, ()):
                        with self.subTest(path=path, anchor=anchor):
                            self.assertIn(anchor, html)
                    if path in anchors:
                        # 新主题只有一套 CSS：旧手账样式表与外溢的内联样式都不许出现
                        self.assertNotIn(".diary-tape", html, path)
                        self.assertNotIn(".section-block", html, path)
                        self.assertNotIn(' style="', html, path)
                        self.assertIn('class="ph"', html, path)
            async with c.get(base + "/api/home") as r:
                home = json.loads(await r.text())
                self.assertEqual(
                    set(home),
                    {"name", "stage", "stage_name", "tone", "mood_label", "activity",
                     "arc", "last_chat", "onebot_connected", "today_cost", "subtitle",
                     "ink", "char_color", "avatar", "art", "terms"},
                    "/api/home 的字段名是对外契约，不许改名",
                )
            async with c.get(base + "/assets/outfit.woff2") as r:
                self.assertEqual(r.status, 200)
            async with c.get(base + f"/assets/{dashboard.DASHBOARD_KAI_FONT_FILE}") as r:
                self.assertEqual(r.status, 200)
                self.assertEqual(r.content_type, "font/woff2")
                self.assertIn("immutable", r.headers.get("Cache-Control", ""))
                data = await r.read()
                self.assertEqual(data[:4], b"wOF2")
                self.assertLess(len(data), 250 * 1024)
            for bad in ("/assets/../config.toml", "/assets/evil.woff2", "/skin/../character.json",
                        "/skin/%2e%2e/character.json"):
                async with c.get(base + bad) as r:
                    self.assertEqual(r.status, 404, bad)
            if admin.skin:
                rel = admin.skin.avatar["default"]
                async with c.get(base + dashboard._skin_url(rel, "")) as r:
                    self.assertEqual(r.status, 200)
                    self.assertEqual(r.content_type, "image/png")
        self._run("endfield", checks)


if __name__ == "__main__":
    unittest.main()
