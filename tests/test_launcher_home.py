"""网页版小窝（launcher/home.py）：读本地配置、口令保护、只发皮肤目录里的图"""

import json
import os
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest import mock

import launcher.home as home_mod

EXAMPLE_CARD = os.path.join("characters", "example")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestIdentity(unittest.TestCase):
    def test_missing_card_falls_back(self):
        ident = home_mod.load_identity("characters/没有这张卡")
        self.assertEqual(ident["name"], "她")
        self.assertIsNone(ident["skin"])

    def test_example_identity(self):
        ident = home_mod.load_identity(EXAMPLE_CARD)
        self.assertTrue(ident["name"])

    def test_custom_identity_with_skin(self):
        with tempfile.TemporaryDirectory() as d:
            theme_dir = os.path.join(d, "theme")
            os.makedirs(theme_dir)
            with open(os.path.join(d, "character.json"), "w", encoding="utf-8") as f:
                json.dump({"name": "测试角色"}, f, ensure_ascii=False)
            with open(os.path.join(theme_dir, "skin.json"), "w", encoding="utf-8") as f:
                json.dump({"ink": True}, f)
            ident = home_mod.load_identity(d)
            self.assertEqual(ident["name"], "测试角色")
            self.assertTrue(ident["skin"].ink)


class TestWindow(unittest.TestCase):
    def test_app_mode_uses_default_edge_profile(self):
        with mock.patch.object(home_mod.os.path, "isfile", return_value=True), \
                mock.patch.object(home_mod.subprocess, "Popen") as popen:
            home_mod.open_window("http://127.0.0.1:8765/")
        self.assertEqual(popen.call_args.args[0], [
            home_mod.EDGE_PATHS[0],
            "--app=http://127.0.0.1:8765/",
            "--window-size=980,660",
        ])


class TestShortcut(unittest.TestCase):
    @unittest.skipUnless(sys.platform == "win32", "快捷方式只在 Windows 上建，路径拼法也只在 Windows 上成立")
    def test_base_pythonw_is_preferred(self):
        with mock.patch.object(home_mod.sys, "_base_executable", r"D:\base\python.exe"), \
                mock.patch.object(home_mod.os.path, "isfile", return_value=True):
            self.assertEqual(home_mod.get_shortcut_pythonw(), r"D:\base\pythonw.exe")


class TestServer(unittest.TestCase):
    def setUp(self):
        card = EXAMPLE_CARD
        cfg = {"token": "", "card": card, "theme": "endfield"}
        with mock.patch.object(home_mod, "read_local_config", return_value=cfg):
            self.home = home_mod.Home()
        self.port = _free_port()
        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), home_mod.make_handler(self.home))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.read()

    def _post(self, path, secret):
        req = urllib.request.Request(self.base + path, data=b"{}", method="POST",
                                     headers={"X-Home-Secret": secret, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_page_carries_identity_and_secret(self):
        status, body = self._get("/")
        self.assertEqual(status, 200)
        self.assertIn(self.home.secret, body.decode("utf-8"))
        self.assertNotIn("__HOME_INFO__", body.decode("utf-8"))

    def test_state_without_server(self):
        _, body = self._get("/api/state")
        state = json.loads(body)
        self.assertIn("tunnel", state)
        self.assertGreater(self.home.last_ping, 0)

    def test_post_needs_secret(self):
        self.assertEqual(self._post("/api/folder", "wrong-guess"), 403)
        with mock.patch.object(self.home, "open_folder") as op:
            self.assertEqual(self._post("/api/folder", self.home.secret), 200)
            op.assert_called_once()
        self.assertEqual(self._post("/api/rm-rf", self.home.secret), 404)

    def test_only_skin_images_and_fonts(self):
        for bad in ("/skin/../character.json", "/skin/%2e%2e/character.json", "/assets/../home.py", "/assets/x.woff2"):
            with self.assertRaises(urllib.error.HTTPError, msg=bad):
                self._get(bad)
        self.assertEqual(self._get("/assets/outfit.woff2")[0], 200)
        if self.home.skin:
            rel = self.home.skin.avatar_for("default")
            from urllib.parse import quote
            self.assertEqual(self._get("/skin/" + quote(rel))[0], 200)


if __name__ == "__main__":
    unittest.main()
