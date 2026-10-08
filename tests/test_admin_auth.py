"""管理页鉴权测试 (tests/test_admin_auth.py)

外部评审发现：`/admin/reset`、`/admin/restart`、`/admin/backup` 等 POST 端点
零鉴权，唯一防线是 host 绑 127.0.0.1 + confirm=YES。第二轮审计进一步指出：
11 条读路由（9 条页面：/、/memory、/debug、/costs、/stickers、/logs 等，外加
/favicon.png、/apple-touch-icon.png 两条图标路由）在 host 被改成
非回环时同样裸奔（读侧泄漏记忆/日志/计费/关系状态）。本测试集钉住两条防线：

1. `[admin].token` 非空时：
   - 写操作必须带匹配的 `X-Admin-Token` 头或 `token` 字段，否则 403；
   - **读页面（GET）同样要求 token**，URL query `?token=` 或请求头均可，
     否则 403；页面内导航链接与表情包图片地址都要带上 token（点一下不掉线）；
   - `/api/status` 一并保护：它带的 stage_name/composite/today_cost 是关系状态与开销，
     不只是"活着没"的健康检查。
2. `token` 为空 = 仅 localhost 信任模式，行为与改动前逐字节一致
   （读写全部照常放行，页面里不多出任何 token 字样与隐藏域）。
3. host 绑非回环地址且 token 为空时，启动打醒目裸奔警告。

全部本地 mock 请求，零真实网络与真实 API。
"""

from __future__ import annotations

import json
import os
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

from companion.admin import warn_if_admin_exposed_without_token  # noqa: E402
from companion.config import AdminConfig  # noqa: E402
from helpers import close_db, make_db, make_engine_stack  # noqa: E402

from aiohttp import web  # noqa: E402
from aiohttp.test_utils import TestClient, TestServer  # noqa: E402


class _Req:
    """最小假请求：只实现鉴权与各处理器会碰到的属性。"""

    def __init__(
        self,
        *,
        headers=None,
        content_type: str = "",
        json_body=None,
        form=None,
        query=None,
        match_info=None,
    ):
        self.headers = headers or {}
        self.content_type = content_type
        self._json = json_body
        self._form = form
        self.query = query or {}
        self.match_info = match_info or {}

    async def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json

    async def post(self):
        if self._form is None:
            raise ValueError("no form body")
        return self._form


def _json_headers(extra=None):
    headers = {"Accept": "application/json"}
    if extra:
        headers.update(extra)
    return headers


class TestAdminWriteAuth(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_admin_auth_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.backup_dir = os.path.join(self.tmp, "backup", "daily")
        self.db = await make_db(self.db_path)

    async def asyncTearDown(self):
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _admin(self, token: str):
        return make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=token),
            db_path=self.db_path,
            backup_dir=self.backup_dir,
        ).admin

    # ---------------- token 为空：向后兼容 ----------------

    async def test_token为空时备份照常200(self):
        admin = self._admin("")
        resp = await admin.handle_admin_backup(_Req(headers=_json_headers()))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.text)["status"], "ok")

    async def test_token为空时重置照常200(self):
        admin = self._admin("")
        with patch(
            "companion.admin.reset_database",
            return_value={"backup_path": "x.db", "cleared_counts": {}},
        ):
            resp = await admin.handle_admin_reset(
                _Req(
                    headers=_json_headers(),
                    content_type="application/json",
                    json_body={"confirm": "YES"},
                )
            )
        self.assertEqual(resp.status, 200)

    async def test_管理页token为空时不出现隐藏域(self):
        admin = self._admin("")
        resp = await admin.handle_admin(_Req(query={}))
        self.assertNotIn('name="token"', resp.text)

    # ---------------- token 非空：鉴权生效 ----------------

    async def test_无token一律403(self):
        admin = self._admin("s3cret")
        resp = await admin.handle_admin_backup(_Req(headers=_json_headers()))
        self.assertEqual(resp.status, 403)
        self.assertEqual(json.loads(resp.text)["status"], "error")

    async def test_错token一律403(self):
        admin = self._admin("s3cret")
        resp = await admin.handle_admin_backup(
            _Req(headers=_json_headers({"X-Admin-Token": "wrong"}))
        )
        self.assertEqual(resp.status, 403)

    async def test_重启端点无token也403(self):
        """最关键的一条：重启端点若裸奔，一次 POST 就能打掉服务。"""
        admin = self._admin("s3cret")
        resp = await admin.handle_admin_restart(_Req(headers=_json_headers()))
        self.assertEqual(resp.status, 403)

    async def test_重置端点无token即使confirm是YES也403(self):
        """鉴权先于 confirm 校验：无 token 时连 confirm 都不该被处理。"""
        admin = self._admin("s3cret")
        with patch("companion.admin.reset_database") as mocked:
            resp = await admin.handle_admin_reset(
                _Req(
                    headers=_json_headers(),
                    content_type="application/json",
                    json_body={"token": "wrong", "confirm": "YES"},
                )
            )
        self.assertEqual(resp.status, 403)
        mocked.assert_not_called()

    async def test_正确token走请求头200(self):
        admin = self._admin("s3cret")
        resp = await admin.handle_admin_backup(
            _Req(headers=_json_headers({"X-Admin-Token": "s3cret"}))
        )
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.text)["status"], "ok")

    async def test_正确token走表单字段200(self):
        admin = self._admin("s3cret")
        resp = await admin.handle_admin_backup(
            _Req(
                headers=_json_headers(),
                content_type="application/x-www-form-urlencoded",
                form={"token": "s3cret"},
            )
        )
        self.assertEqual(resp.status, 200)

    async def test_正确token走JSON字段200(self):
        admin = self._admin("s3cret")
        with patch(
            "companion.admin.reset_database",
            return_value={"backup_path": "x.db", "cleared_counts": {}},
        ) as mocked:
            resp = await admin.handle_admin_reset(
                _Req(
                    headers=_json_headers(),
                    content_type="application/json",
                    json_body={"token": "s3cret", "confirm": "YES"},
                )
            )
        self.assertEqual(resp.status, 200)
        mocked.assert_called_once()

    async def test_管理页token非空时回填隐藏域(self):
        admin = self._admin("s3cret")
        resp = await admin.handle_admin(_Req(query={"token": "s3cret"}))
        self.assertIn(
            '<input type="hidden" name="token" value="s3cret">', resp.text
        )


class TestAdminWriteAuthOverHTTP(unittest.IsolatedAsyncioTestCase):
    """真 aiohttp 请求走一遍，确认"先读体取 token、处理器再读体"不会踩到体被消费。"""

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_admin_auth_http_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.backup_dir = os.path.join(self.tmp, "backup", "daily")
        self.db = await make_db(self.db_path)
        admin = make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token="s3cret"),
            db_path=self.db_path,
            backup_dir=self.backup_dir,
        ).admin
        app = web.Application()
        app.router.add_post("/admin/backup", admin.handle_admin_backup)
        app.router.add_post("/admin/reset", admin.handle_admin_reset)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_真实HTTP无token403(self):
        resp = await self.client.post(
            "/admin/backup", headers={"Accept": "application/json"}
        )
        self.assertEqual(resp.status, 403)

    async def test_真实HTTP请求头token200(self):
        resp = await self.client.post(
            "/admin/backup",
            headers={"Accept": "application/json", "X-Admin-Token": "s3cret"},
        )
        self.assertEqual(resp.status, 200)

    async def test_真实HTTP表单token200(self):
        resp = await self.client.post(
            "/admin/backup",
            data={"token": "s3cret"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(resp.status, 200)

    async def test_真实HTTP_json带token重置200(self):
        with patch(
            "companion.admin.reset_database",
            return_value={"backup_path": "x.db", "cleared_counts": {}},
        ):
            resp = await self.client.post(
                "/admin/reset",
                json={"token": "s3cret", "confirm": "YES"},
                headers={"Accept": "application/json"},
            )
        self.assertEqual(resp.status, 200)


class TestAdminReadAuth(unittest.IsolatedAsyncioTestCase):
    """读路由（GET）鉴权：token 非空时 11 条读路由（9 页面 + 2 图标）全部要 token。"""

    TOKEN = "s3cret"

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_admin_read_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.backup_dir = os.path.join(self.tmp, "backup", "daily")
        self.db = await make_db(self.db_path)

    async def asyncTearDown(self):
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _admin(self, token: str):
        return make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=token),
            db_path=self.db_path,
            backup_dir=self.backup_dir,
        ).admin

    # (路径, 处理器名, 额外 kwargs) —— 覆盖 admin.py 里注册的全部 GET 路由
    def _get_routes(self, admin):
        return [
            ("/", admin.handle_overview, {}),
            ("/memory", admin.handle_memory, {}),
            ("/debug", admin.handle_debug, {}),
            ("/costs", admin.handle_costs, {}),
            ("/stickers", admin.handle_stickers, {}),
            ("/logs", admin.handle_logs, {}),
            ("/admin", admin.handle_admin, {}),
            ("/api/status", admin.handle_api_status, {"headers": {"Accept": "application/json"}}),
            ("/stickers/img/x.png", admin.handle_sticker_image, {"match_info": {"name": "x.png"}}),
            ("/favicon.png", admin.handle_favicon, {}),
            ("/apple-touch-icon.png", admin.handle_apple_touch_icon, {}),
        ]

    # ---------------- token 为空：逐字节一致的旧行为 ----------------

    async def test_token为空时读页面全部照常放行(self):
        admin = self._admin("")
        for path, handler, kwargs in self._get_routes(admin):
            with self.subTest(path=path):
                resp = await handler(_Req(query={}, **kwargs))
                self.assertNotEqual(resp.status, 403, f"{path} 在 token 为空时不该被拦")

    async def test_token为空时页面里不出现任何token字样(self):
        admin = self._admin("")
        for path, handler, _kwargs in self._get_routes(admin):
            # /api/status 是 JSON、图片与图标路由不是文本页面：没有文本可查
            if path in ("/api/status", "/stickers/img/x.png", "/favicon.png", "/apple-touch-icon.png"):
                continue
            with self.subTest(path=path):
                resp = await handler(_Req(query={}))
                self.assertNotIn("token=", resp.text, f"{path} 不该凭空多出 token 链接")

    async def test_导航渲染默认参数与空串逐字节一致(self):
        """render_nav/html_shell 的新参数默认空串：老调用方输出一个字节都没变。"""
        from companion.admin_render import html_shell, render_nav

        self.assertEqual(render_nav("/memory"), render_nav("/memory", ""))
        self.assertEqual(
            html_shell("调试信息", "/debug", "BODY"),
            html_shell("调试信息", "/debug", "BODY", ""),
        )

    # ---------------- token 非空：读侧必须出示 token ----------------

    async def test_无token读页面一律403(self):
        admin = self._admin(self.TOKEN)
        for path, handler, kwargs in self._get_routes(admin):
            with self.subTest(path=path):
                resp = await handler(_Req(query={}, **kwargs))
                self.assertEqual(resp.status, 403, f"{path} 无 token 必须 403")

    async def test_查询串token可读页面(self):
        admin = self._admin(self.TOKEN)
        for path, handler, kwargs in self._get_routes(admin):
            with self.subTest(path=path):
                kwargs = dict(kwargs)
                kwargs.pop("headers", None)
                resp = await handler(_Req(query={"token": self.TOKEN}, **kwargs))
                self.assertNotEqual(resp.status, 403, f"{path} 带对 token 应当放行")

    async def test_请求头token可读页面(self):
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_overview(
            _Req(query={}, headers={"X-Admin-Token": self.TOKEN})
        )
        self.assertEqual(resp.status, 200)

    async def test_错token读页面403(self):
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_overview(_Req(query={"token": "wrong"}))
        self.assertEqual(resp.status, 403)
        resp = await admin.handle_memory(_Req(query={}, headers={"X-Admin-Token": "wrong"}))
        self.assertEqual(resp.status, 403)

    async def test_被拒页面不回显token(self):
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_overview(_Req(query={"token": "wrong"}))
        self.assertNotIn(self.TOKEN, resp.text, "403 页面不许把正确 token 写出来")

    async def test_api_status一并受保护(self):
        """审计给的例外选项里我们选了"一并保护"：/api/status 带关系状态与开销，
        不是纯健康检查；监控脚本改用 ?token= 即可（token 为空时行为不变）。"""
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_api_status(
            _Req(query={}, headers={"Accept": "application/json"})
        )
        self.assertEqual(resp.status, 403)
        self.assertEqual(json.loads(resp.text)["status"], "error")

    # ---------------- 页面内链接必须带 token，点了不掉线 ----------------

    async def test_导航链接带上token(self):
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_memory(_Req(query={"token": self.TOKEN}))
        for path in ("/", "/memory", "/debug", "/costs", "/stickers", "/logs", "/admin"):
            self.assertIn(
                f'href="{path}?token={self.TOKEN}"', resp.text, f"导航里的 {path} 必须带 token"
            )

    async def test_表情包图片地址带上token(self):
        admin = self._admin(self.TOKEN)
        admin.stickers.load_index()
        if not admin.stickers._index:
            self.skipTest("示例卡没有表情包索引")
        resp = await admin.handle_stickers(_Req(query={"token": self.TOKEN}))
        first_name = next(iter(admin.stickers._index))
        self.assertIn(
            f'/stickers/img/{first_name}?token={self.TOKEN}', resp.text, "图片不带 token 会 403（图全裂）"
        )

    async def test_管理页隐藏域回填配置里的token(self):
        """走请求头进来的人也能直接点表单：隐藏域用配置里的 token，不是请求里的。"""
        admin = self._admin(self.TOKEN)
        resp = await admin.handle_admin(_Req(query={}, headers={"X-Admin-Token": self.TOKEN}))
        self.assertIn(
            f'<input type="hidden" name="token" value="{self.TOKEN}">', resp.text
        )

    async def test_结果页链接也带token(self):
        admin = self._admin(self.TOKEN)
        with patch(
            "companion.admin.reset_database",
            return_value={"backup_path": "x.db", "cleared_counts": {"turns": 1}},
        ):
            resp = await admin.handle_admin_reset(
                _Req(
                    headers={"X-Admin-Token": self.TOKEN},
                    content_type="application/x-www-form-urlencoded",
                    form={"confirm": "YES"},
                )
            )
        self.assertEqual(resp.status, 200)
        self.assertIn(f'href="/?token={self.TOKEN}"', resp.text)


class TestAdminReadAuthOverHTTP(unittest.IsolatedAsyncioTestCase):
    """真 aiohttp 请求走一遍读页面：无 token 403、带 token 200。"""

    TOKEN = "s3cret"

    async def asyncSetUp(self):
        self.tmp = tempfile.mkdtemp(prefix="qqc_admin_read_http_")
        self.db_path = os.path.join(self.tmp, "companion.db")
        self.backup_dir = os.path.join(self.tmp, "backup", "daily")
        self.db = await make_db(self.db_path)
        admin = make_engine_stack(
            self.db,
            include_admin=True,
            admin_config=AdminConfig(host="127.0.0.1", port=8080, token=self.TOKEN),
            db_path=self.db_path,
            backup_dir=self.backup_dir,
        ).admin
        app = web.Application()
        app.router.add_get("/", admin.handle_overview)
        app.router.add_get("/memory", admin.handle_memory)
        app.router.add_get("/api/status", admin.handle_api_status)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await close_db(self.db, self.db_path)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_真实HTTP无token403(self):
        for path in ("/", "/memory", "/api/status"):
            with self.subTest(path=path):
                resp = await self.client.get(path)
                self.assertEqual(resp.status, 403)

    async def test_真实HTTP查询串token200(self):
        resp = await self.client.get(f"/?token={self.TOKEN}")
        self.assertEqual(resp.status, 200)
        self.assertIn(f'href="/memory?token={self.TOKEN}"', await resp.text())

    async def test_真实HTTP请求头token200(self):
        resp = await self.client.get("/memory", headers={"X-Admin-Token": self.TOKEN})
        self.assertEqual(resp.status, 200)


class TestAdminExposureWarning(unittest.TestCase):
    def test_非回环且空token启动警告(self):
        config = AdminConfig(host="0.0.0.0", port=8080, token="")
        with self.assertLogs("companion.admin", level="WARNING") as cm:
            warn_if_admin_exposed_without_token(config)
        self.assertTrue(
            any("安全警告" in msg for msg in cm.output),
            f"缺少裸奔警告：{cm.output}",
        )

    def test_回环主机不警告(self):
        config = AdminConfig(host="127.0.0.1", port=8080, token="")
        with self.assertNoLogs("companion.admin", level="WARNING"):
            warn_if_admin_exposed_without_token(config)

    def test_非回环但已设token不警告(self):
        config = AdminConfig(host="0.0.0.0", port=8080, token="s3cret")
        with self.assertNoLogs("companion.admin", level="WARNING"):
            warn_if_admin_exposed_without_token(config)


if __name__ == "__main__":
    unittest.main()
