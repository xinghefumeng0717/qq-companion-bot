"""小窝 · 桌面控制台（网页版） (launcher/home.py)

和原来的 tkinter 版（qingzi_home.py，保留不动）做的事一样：连 SSH 隧道、看三盏灯、打开看板、
拉备份、开沙盒试聊。区别是界面换成本机一个小网页，优先用 Edge 应用模式弹成独立窗口，
样子和新看板一套（终末地工业风；角色卡带水墨皮肤时叠上水墨和 Q 版头像）。

- 她叫什么、皮肤（头像/插图/说法）从本地 config.toml 的 [character].path 指的卡里读；
- 在线状态、心情、阶段从服务器看板的 /api/status、/api/home 读（经 SSH 隧道）；
- 只监听 127.0.0.1；按钮操作要带本次启动随机生成的口令（防别的网页偷偷调用）；
- 窗口关掉 90 秒后自动退出并断开隧道。

用法：
  pythonw launcher\\home.py              启动并弹出窗口
  python  launcher\\home.py --shortcut   在桌面建「<她的名字>的小窝」快捷方式
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

from launcher.core import (  # noqa: E402
    DEFAULT_REMOTE_HOST,
    get_backup_command,
    get_simulation_cmd_args,
    get_ssh_tunnel_command,
    load_sync_state,
    parse_status_data,
    save_sync_state,
)

HOME_PORT = 8765
DASH = "http://127.0.0.1:8080"
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
IDLE_EXIT_SECONDS = 90
ASSETS_DIR = os.path.join(PROJECT_ROOT, "companion", "assets")
FONT_FILES = {"lxgw-wenkai-gb2312.woff2", "archivo-wide.woff2", "outfit.woff2", "space-grotesk.woff2"}
EDGE_PATHS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
]


def read_local_config() -> Dict[str, str]:
    """本地 config.toml 里小窝要的三样：看板令牌、角色卡目录、看板主题。读不到就用默认。"""
    out = {"token": "", "card": "characters/example", "theme": "endfield"}
    try:
        import tomllib

        with open(os.path.join(PROJECT_ROOT, "config.toml"), "rb") as f:
            data = tomllib.load(f)
        out["token"] = str(data.get("admin", {}).get("token", "") or "")
        out["card"] = str(data.get("character", {}).get("path", out["card"]) or out["card"])
        theme = str(data.get("admin", {}).get("theme", "") or "")
        if theme in ("plain", "endfield"):
            out["theme"] = theme
    except Exception:
        pass
    return out


def load_identity(card_dir: str) -> Dict[str, Any]:
    """她的名字 + 皮肤（没有皮肤时只有名字）。"""
    name = "她"
    try:
        with open(os.path.join(card_dir, "character.json"), "r", encoding="utf-8") as f:
            name = str(json.load(f).get("name") or name)
    except Exception:
        pass
    skin = None
    try:
        from companion.dashboard import load_skin

        skin = load_skin(card_dir)
    except Exception:
        skin = None
    return {"name": name, "skin": skin}


def port_open(port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_json(url: str, timeout: float = 3.0) -> Optional[Dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return data if isinstance(data, dict) else None
    except Exception:
        return None
    return None


class Home:
    def __init__(self) -> None:
        cfg = read_local_config()
        self.token_query = f"?token={cfg['token']}" if cfg["token"] else ""
        self.theme = cfg["theme"]
        ident = load_identity(os.path.join(PROJECT_ROOT, cfg["card"]))
        self.name: str = ident["name"]
        self.skin = ident["skin"]
        self.secret = secrets.token_urlsafe(16)
        self.lock = threading.Lock()
        self.tunnel_proc: Optional[subprocess.Popen] = None
        self.tunnel_busy = False
        self.message = "就绪"
        self.last_ping = 0.0
        self.snapshot: Dict[str, Any] = {
            "tunnel": False, "dash": False, "bot": None, "stage_name": "", "today_cost": 0.0, "home": {},
        }
        self.sync_file = os.path.join(PROJECT_ROOT, "data", "backups", "sync_state.json")

    # ---------- 状态 ----------

    def poll_once(self) -> None:
        tunnel = port_open(8080)
        status = fetch_json(f"{DASH}/api/status{self.token_query}") if tunnel else None
        home = fetch_json(f"{DASH}/api/home{self.token_query}") if status else None
        parsed = parse_status_data(status)
        with self.lock:
            self.snapshot = {
                "tunnel": tunnel, "dash": status is not None,
                "bot": None if status is None else parsed["robot_online"],
                "stage_name": parsed["stage_name"] if status else "",
                "today_cost": parsed["today_cost"],
                "home": home or {},
            }

    def poll_loop(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception:
                pass
            time.sleep(5)

    def state(self) -> Dict[str, Any]:
        self.last_ping = time.time()
        with self.lock:
            snap = dict(self.snapshot)
        home = snap.get("home") or {}
        avatar = ""
        if self.skin:
            rel = self.skin.avatar_for(home.get("mood_label", ""), "睡" in home.get("activity", ""))
            avatar = f"/skin/{rel}" if rel else ""
        snap.update({
            "tunnel_busy": self.tunnel_busy,
            "tunnel_mine": self.tunnel_proc is not None,
            "message": self.message,
            "avatar": avatar,
            "last_backup": load_sync_state(self.sync_file).get("last_sync", ""),
        })
        return snap

    def info(self) -> Dict[str, Any]:
        skin = self.skin
        art = {}
        if skin:
            art = {k: f"/skin/{v}" for k, v in skin.art.items() if skin.has(v)}
        return {
            "name": self.name, "theme": self.theme, "secret": self.secret,
            "ink": bool(skin and skin.ink), "subtitle": skin.subtitle if skin else "",
            "char_color": skin.char_color if skin else "#E6E6E6",
            "art": art, "terms": dict(skin.terms) if skin else {},
            "default_avatar": f"/skin/{skin.avatar_for('default')}" if skin and skin.avatar_for("default") else "",
            "remote": DEFAULT_REMOTE_HOST,
        }

    # ---------- 操作 ----------

    def toggle_tunnel(self) -> None:
        if self.tunnel_busy:
            return
        if self.tunnel_proc is not None:
            self.stop_tunnel()
            self.message = "已断开连接"
            return
        try:
            self.tunnel_proc = subprocess.Popen(
                get_ssh_tunnel_command(), creationflags=CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
        except Exception as e:
            self.tunnel_proc = None
            self.message = f"启动 ssh 失败：{e}"
            return
        self.tunnel_busy = True
        self.message = "正在连接服务器……"
        threading.Thread(target=self._verify_tunnel, daemon=True).start()

    def _verify_tunnel(self) -> None:
        time.sleep(3.0)
        proc = self.tunnel_proc
        try:
            if proc is None:
                return
            if proc.poll() is not None:
                err = ""
                try:
                    err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "ignore").strip()
                except Exception:
                    pass
                self.stop_tunnel()
                self.message = f"连接失败：{err or 'ssh 已退出'}（常见原因：没配免密公钥、网络不通）"
                return
            if not port_open(8080, 2.0):
                self.stop_tunnel()
                self.message = "隧道建起来了，但看板服务没响应（服务器上的机器人可能没启动）"
                return
            self.message = f"{datetime.now():%H:%M} 隧道已建立"
            self.poll_once()
        finally:
            self.tunnel_busy = False

    def stop_tunnel(self) -> None:
        proc, self.tunnel_proc = self.tunnel_proc, None
        if proc:
            try:
                proc.terminate()
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def fetch_backup(self) -> None:
        def work() -> None:
            self.message = "正在拉取最新备份……"
            dest_dir = os.path.join(PROJECT_ROOT, "data", "backups")
            os.makedirs(dest_dir, exist_ok=True)
            try:
                proc = subprocess.run(
                    get_backup_command(local_dest=os.path.join(dest_dir, "latest.db")),
                    capture_output=True, text=True, creationflags=CREATE_NO_WINDOW, timeout=60,
                )
                if proc.returncode == 0:
                    now = datetime.now().strftime("%H:%M")
                    save_sync_state(self.sync_file, {"last_sync": now, "timestamp": datetime.now().isoformat()})
                    self.message = f"{now} 备份已存到 data/backups/latest.db"
                else:
                    self.message = f"备份拉取失败：{(proc.stderr or '').strip()[:120] or 'scp 退出异常'}"
            except Exception as e:
                self.message = f"备份拉取失败：{e}"

        threading.Thread(target=work, daemon=True).start()

    def launch_sim(self) -> None:
        try:
            subprocess.Popen(get_simulation_cmd_args(), shell=True)
            self.message = "已打开沙盒试聊窗口"
        except Exception as e:
            self.message = f"打不开沙盒试聊：{e}"

    def open_folder(self) -> None:
        if sys.platform == "win32":
            os.startfile(PROJECT_ROOT)  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", PROJECT_ROOT])

    def open_page(self, path: str) -> None:
        if path not in ("/", "/logs", "/memory"):
            path = "/"
        webbrowser.open(f"http://localhost:8080{path}{self.token_query}")

    def watchdog(self, server: ThreadingHTTPServer) -> None:
        while True:
            time.sleep(10)
            if self.last_ping and time.time() - self.last_ping > IDLE_EXIT_SECONDS:
                self.stop_tunnel()
                server.shutdown()
                return


def make_handler(home: Home):
    page_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "home.html")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:  # 静默
            pass

        def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", cache)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any) -> None:
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            if path == "/":
                with open(page_path, "r", encoding="utf-8") as f:
                    page = f.read().replace("__HOME_INFO__", json.dumps(home.info(), ensure_ascii=False).replace("</", "<\\/"))
                return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            if path == "/api/state":
                return self._json(home.state())
            if path.startswith("/assets/"):
                name = path[len("/assets/"):]
                full = os.path.join(ASSETS_DIR, name)
                if name in FONT_FILES and os.path.isfile(full):
                    with open(full, "rb") as f:
                        return self._send(200, f.read(), "font/woff2", "public, max-age=31536000")
            if path.startswith("/skin/") and home.skin is not None:
                from urllib.parse import unquote

                from companion.dashboard import skin_file

                full = skin_file(home.skin, unquote(path[len("/skin/"):]))
                if full:
                    ext = os.path.splitext(full)[1].lower().lstrip(".")
                    ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, f"image/{ext}")
                    with open(full, "rb") as f:
                        return self._send(200, f.read(), ctype, "max-age=3600")
            self._send(404, b"Not Found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            if self.headers.get("X-Home-Secret") != home.secret:
                return self._send(403, b"Forbidden", "text/plain")
            action = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length") or 0)
            body: Dict[str, Any] = {}
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
                except Exception:
                    body = {}
            actions = {
                "/api/tunnel": home.toggle_tunnel,
                "/api/backup": home.fetch_backup,
                "/api/sim": home.launch_sim,
                "/api/folder": home.open_folder,
                "/api/open": lambda: home.open_page(str(body.get("path", "/"))),
            }
            fn = actions.get(action)
            if fn is None:
                return self._send(404, b"Not Found", "text/plain")
            try:
                fn()
            except Exception as e:
                home.message = f"操作失败：{e}"
            self._json(home.state())

    return Handler


def open_window(url: str) -> None:
    """优先用 Edge 应用模式打开独立小窗；没有 Edge 时回退到默认浏览器。"""
    for edge in EDGE_PATHS:
        if os.path.isfile(edge):
            subprocess.Popen([edge, f"--app={url}", "--window-size=980,660"])
            return
    webbrowser.open(url)


def get_shortcut_pythonw() -> str:
    """优先用基础 Python，避免 venv 转发器的低完整性标签传进浏览器进程。"""
    base = getattr(sys, "_base_executable", sys.executable)
    pythonw = os.path.join(os.path.dirname(base), "pythonw.exe")
    if os.path.isfile(pythonw):
        return pythonw
    return os.path.join(PROJECT_ROOT, "venv", "Scripts", "pythonw.exe")


def make_shortcut() -> None:
    """桌面建「<她的名字>的小窝」快捷方式；皮肤有头像就拿它做图标。"""
    ident = load_identity(os.path.join(PROJECT_ROOT, read_local_config()["card"]))
    name, skin = ident["name"], ident["skin"]
    ico = os.path.join(PROJECT_ROOT, "launcher", "assets", "app.ico")
    if skin and skin.avatar_for("default"):
        try:
            from PIL import Image

            from companion.dashboard import skin_file

            src = skin_file(skin, skin.avatar_for("default"))
            im = Image.open(src).convert("RGBA")
            side = max(im.size)
            canvas = Image.new("RGBA", (side, side), (0, 0, 0, 0))
            canvas.paste(im, ((side - im.width) // 2, (side - im.height) // 2))
            out_dir = os.path.join(PROJECT_ROOT, "data", "launcher")
            os.makedirs(out_dir, exist_ok=True)
            ico = os.path.join(out_dir, "home.ico")
            canvas.save(ico, sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        except Exception as e:
            print(f"头像转图标失败，用默认图标：{e}")
    pythonw = get_shortcut_pythonw()
    script = os.path.join(PROJECT_ROOT, "launcher", "home.py")
    ps = f"""
$s = (New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path ([Environment]::GetFolderPath('Desktop')) '{name}的小窝.lnk'))
$s.TargetPath = '{pythonw}'
$s.Arguments = '"{script}"'
$s.WorkingDirectory = '{PROJECT_ROOT}'
$s.IconLocation = '{ico},0'
$s.Description = '{name}的小窝：连服务器、看状态、拉备份'
$s.Save()
"""
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)
    print(f"已在桌面建好「{name}的小窝」快捷方式")


def main() -> None:
    if "--shortcut" in sys.argv:
        make_shortcut()
        return
    url = f"http://127.0.0.1:{HOME_PORT}/"
    if port_open(HOME_PORT, 0.5):
        open_window(url)  # 已经开着一个了：只把窗口再弹出来
        return
    home = Home()
    server = ThreadingHTTPServer(("127.0.0.1", HOME_PORT), make_handler(home))
    threading.Thread(target=home.poll_loop, daemon=True).start()
    threading.Thread(target=home.watchdog, args=(server,), daemon=True).start()
    open_window(url)
    try:
        server.serve_forever()
    finally:
        home.stop_tunnel()


if __name__ == "__main__":
    main()
