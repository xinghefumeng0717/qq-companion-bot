"""管理页写操作鉴权测试 (tests/test_admin_auth.py)

外部评审发现：`/admin/reset`、`/admin/restart`、`/admin/backup` 等 POST 端点
零鉴权，唯一防线是 host 绑 127.0.0.1 + confirm=YES。本测试集钉住新防线：

1. `[admin].token` 非空时，写操作必须带匹配的 `X-Admin-Token` 头或
   `token` 字段，否则 403（无 token / 错 token 都 403，对 token 200）；
2. `token` 为空 = 仅 localhost 信任模式，行为与改动前逐字节一致（写操作照常放行，
   管理页 HTML 不多出任何隐藏域）；
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
    """最小假请求：只实现鉴权与重置处理器会碰到的属性。"""

    def __init__(
        self,
        *,
        headers=None,
        content_type: str = "",
        json_body=None,
        form=None,
        query=None,
    ):
        self.headers = headers or {}
        self.content_type = content_type
        self._json = json_body
        self._form = form
        self.query = query or {}

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
