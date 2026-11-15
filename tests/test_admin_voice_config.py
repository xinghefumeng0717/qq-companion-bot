"""第三轮外部审计的安全小件回归（2026-10-05）：

1. CSRF：写端点必须拒绝 Origin 与本服务不一致的跨站表单（恶意网页借浏览器
   打 127.0.0.1:8080）；不带 Origin 的非浏览器客户端不受影响。
2. voice.py 的 file:// 白名单：伪造语音事件不能把服务器任意文件喂给 ffmpeg
   再进提示词（file:///etc/passwd 类）；只放行项目目录内的文件。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from companion.admin import AdminServer
from companion.config import AdminConfig
from companion.voice import VoiceProcessor


class _StubRequest:
    def __init__(self, headers=None, host="127.0.0.1:8080"):
        self.headers = headers or {}
        self.host = host


def _admin(token=""):
    srv = AdminServer.__new__(AdminServer)
    srv.config = AdminConfig(token=token)
    return srv


class TestCSRFGuard(unittest.IsolatedAsyncioTestCase):
    async def test_跨站Origin被拒(self):
        srv = _admin()
        req = _StubRequest(headers={"Origin": "http://evil.example.com"})
        self.assertFalse(await srv._write_authorized(req))

    async def test_同源Origin放行(self):
        srv = _admin()
        req = _StubRequest(headers={"Origin": "http://localhost:8080"})
        self.assertTrue(await srv._write_authorized(req))

    async def test_无Origin的非浏览器客户端放行(self):
        srv = _admin()
        req = _StubRequest()
        self.assertTrue(await srv._write_authorized(req))

    async def test_Referer跨站也拒(self):
        srv = _admin()
        req = _StubRequest(headers={"Referer": "http://evil.example.com/page"})
        self.assertFalse(await srv._write_authorized(req))


class TestVoiceFileWhitelist(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.vm = VoiceProcessor.__new__(VoiceProcessor)

    async def test_项目外的文件被拒(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            outside = f.name  # 系统临时目录，不在项目里
        try:
            result = await self.vm._download_silk(f"file://{outside}", None)
            self.assertIsNone(result)
        finally:
            os.unlink(outside)

    async def test_项目内的文件放行(self):
        subdir = os.path.join(os.getcwd(), "data", "_test_voice_wl")
        os.makedirs(subdir, exist_ok=True)
        inside = os.path.join(subdir, "ok.silk")
        with open(inside, "wb") as f:
            f.write(b"x")
        try:
            result = await self.vm._download_silk(f"file://{inside}", None)
            self.assertIsNotNone(result)
        finally:
            os.unlink(inside)
            os.rmdir(subdir)


if __name__ == "__main__":
    unittest.main()
