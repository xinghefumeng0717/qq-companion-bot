"""青梓小窝 · 桌面控制台 (launcher/qingzi_home.py)
用于 Windows 本地一键连接远端伴侣机器人、监控状态灯、打开看板、拉取备份与启动沙箱仿真。
无第三方依赖，纯标准库 tkinter 实现。
"""

from __future__ import annotations

from datetime import datetime
import json
import os
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
import urllib.request
import webbrowser

# 确保项目根目录在 sys.path 中并设为当前工作目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
try:
    os.chdir(PROJECT_ROOT)
except Exception:
    pass

from launcher.core import (
    DEFAULT_REMOTE_HOST,
    format_header_info,
    get_backup_command,
    get_simulation_cmd_args,
    get_ssh_tunnel_command,
    load_sync_state,
    parse_status_data,
    save_sync_state,
)

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def load_admin_token() -> str:
    """从本地 config.toml 的 [admin].token 读取看板令牌（与服务器保持一致）。

    2026-10-08 起服务器看板开启了读写全量鉴权：不带 token 的页面与 /api/status
    一律 403。token 为空或读不到时返回空串，行为与旧版完全一致（裸访问模式）。
    """
    try:
        import tomllib

        config_path = os.path.join(PROJECT_ROOT, "config.toml")
        with open(config_path, "rb") as f:
            admin = tomllib.load(f).get("admin", {})
        token = admin.get("token", "")
        return str(token) if token else ""
    except Exception:
        return ""


ADMIN_TOKEN = load_admin_token()
TOKEN_QUERY = f"?token={ADMIN_TOKEN}" if ADMIN_TOKEN else ""

# 主题配色 (#1e1e2e 系深色)
BG_COLOR = "#1e1e2e"
CARD_BG = "#262638"
BTN_BG = "#313244"
BTN_HOVER = "#45475a"
BTN_ACTIVE = "#222336"
TEXT_COLOR = "#cdd6f4"
TEXT_MUTED = "#a6adc8"
ACCENT_GREEN = "#a6e3a1"
ACCENT_RED = "#f38ba8"
ACCENT_BLUE = "#89b4fa"
ACCENT_YELLOW = "#f9e2af"


class QingziHomeApp:
    def __init__(self, root: tk.Tk, scale: float = 1.0):
        self.root = root
        self.root.title("青梓小窝")
        # 物理尺寸随系统 DPI 缩放（420x560 为 96DPI 基准），固定尺寸不拉伸
        self.root.geometry(f"{int(420 * scale)}x{int(560 * scale)}")
        self.root.resizable(False, False)
        self.root.configure(bg=BG_COLOR)

        # 尝试设置应用图标
        ico_path = os.path.join(os.path.dirname(__file__), "assets", "qingzi_anime.ico")
        if not os.path.exists(ico_path):
            ico_path = os.path.join(os.path.dirname(__file__), "assets", "app.ico")
        if os.path.exists(ico_path):
            try:
                self.root.iconbitmap(ico_path)
            except Exception:
                pass

        self.tunnel_process: subprocess.Popen | None = None
        self.last_disconnect_alert: float = 0.0
        self.sync_state_file = os.path.join("data", "backups", "sync_state.json")

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # 启动后台轮询定时器
        self.poll_status()

    def _build_ui(self) -> None:
        # 1. 顶部标题与状态栏
        header_frame = tk.Frame(self.root, bg=BG_COLOR, padx=16, pady=12)
        header_frame.pack(fill=tk.X)

        self.title_label = tk.Label(
            header_frame,
            text="青梓小窝",
            font=("Microsoft YaHei UI", 16, "bold"),
            fg=TEXT_COLOR,
            bg=BG_COLOR,
        )
        self.title_label.pack(anchor="w")

        self.info_label = tk.Label(
            header_frame,
            text="当前阶段：-- · 今日费用：¥0.00",
            font=("Microsoft YaHei UI", 9),
            fg=TEXT_MUTED,
            bg=BG_COLOR,
        )
        self.info_label.pack(anchor="w", pady=(3, 0))

        # 2. 三张状态小卡片 (横向三等分)
        lights_frame = tk.Frame(self.root, bg=BG_COLOR)
        lights_frame.pack(fill=tk.X, padx=16, pady=(0, 14))
        lights_frame.columnconfigure(0, weight=1, uniform="status_col")
        lights_frame.columnconfigure(1, weight=1, uniform="status_col")
        lights_frame.columnconfigure(2, weight=1, uniform="status_col")

        # 隧道卡片
        self.card_tunnel = tk.Frame(lights_frame, bg=CARD_BG, padx=8, pady=8)
        self.card_tunnel.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        top_tunnel = tk.Frame(self.card_tunnel, bg=CARD_BG)
        top_tunnel.pack(anchor="w")
        self.lamp_tunnel_dot = tk.Label(top_tunnel, text="●", font=("Microsoft YaHei UI", 8), fg=ACCENT_RED, bg=CARD_BG)
        self.lamp_tunnel_dot.pack(side=tk.LEFT)
        self.lamp_tunnel_title = tk.Label(top_tunnel, text=" 隧道", font=("Microsoft YaHei UI", 9, "bold"), fg=TEXT_COLOR, bg=CARD_BG)
        self.lamp_tunnel_title.pack(side=tk.LEFT)
        self.lamp_tunnel_desc = tk.Label(self.card_tunnel, text="未连接", font=("Microsoft YaHei UI", 8), fg=TEXT_MUTED, bg=CARD_BG)
        self.lamp_tunnel_desc.pack(anchor="w", pady=(3, 0))

        # 看板卡片
        self.card_dash = tk.Frame(lights_frame, bg=CARD_BG, padx=8, pady=8)
        self.card_dash.grid(row=0, column=1, sticky="nsew", padx=2)
        top_dash = tk.Frame(self.card_dash, bg=CARD_BG)
        top_dash.pack(anchor="w")
        self.lamp_dash_dot = tk.Label(top_dash, text="●", font=("Microsoft YaHei UI", 8), fg=ACCENT_RED, bg=CARD_BG)
        self.lamp_dash_dot.pack(side=tk.LEFT)
        self.lamp_dash_title = tk.Label(top_dash, text=" 看板", font=("Microsoft YaHei UI", 9, "bold"), fg=TEXT_COLOR, bg=CARD_BG)
        self.lamp_dash_title.pack(side=tk.LEFT)
        self.lamp_dash_desc = tk.Label(self.card_dash, text="不可达", font=("Microsoft YaHei UI", 8), fg=TEXT_MUTED, bg=CARD_BG)
        self.lamp_dash_desc.pack(anchor="w", pady=(3, 0))

        # 伴侣卡片
        self.card_bot = tk.Frame(lights_frame, bg=CARD_BG, padx=8, pady=8)
        self.card_bot.grid(row=0, column=2, sticky="nsew", padx=(4, 0))
        top_bot = tk.Frame(self.card_bot, bg=CARD_BG)
        top_bot.pack(anchor="w")
        self.lamp_bot_dot = tk.Label(top_bot, text="●", font=("Microsoft YaHei UI", 8), fg=ACCENT_RED, bg=CARD_BG)
        self.lamp_bot_dot.pack(side=tk.LEFT)
        self.lamp_bot_title = tk.Label(top_bot, text=" 伴侣", font=("Microsoft YaHei UI", 9, "bold"), fg=TEXT_COLOR, bg=CARD_BG)
        self.lamp_bot_title.pack(side=tk.LEFT)
        self.lamp_bot_desc = tk.Label(self.card_bot, text="未知", font=("Microsoft YaHei UI", 8), fg=TEXT_MUTED, bg=CARD_BG)
        self.lamp_bot_desc.pack(anchor="w", pady=(3, 0))

        # 3. 功能按钮区域 (竖向排布)
        btn_frame = tk.Frame(self.root, bg=BG_COLOR, padx=16)
        btn_frame.pack(fill=tk.BOTH, expand=True)

        # 隧道开关按钮 (主按钮，与其余按钮拉开 12px 间距)
        self.btn_tunnel = self._create_btn(
            btn_frame, "一键连接 SSH 隧道", self.toggle_tunnel, bg="#3b4252"
        )
        self.btn_tunnel.pack(fill=tk.X, pady=(0, 12))

        # 打开看板按钮
        self.btn_dash = self._create_btn(
            btn_frame, "打开监控看板 (Web)", self.open_dashboard
        )
        self.btn_dash.pack(fill=tk.X, pady=(0, 6))

        # 一键拉取备份
        sync_state = load_sync_state(self.sync_state_file)
        last_sync = sync_state.get("last_sync", "--:--")
        self.backup_btn_text = tk.StringVar(value=f"一键拉取备份 (上次: {last_sync})")
        self.btn_backup = self._create_btn(
            btn_frame, "", self.fetch_backup_async, textvariable=self.backup_btn_text
        )
        self.btn_backup.pack(fill=tk.X, pady=(0, 6))

        # 仿真对话
        self.btn_chat = self._create_btn(
            btn_frame, "启动仿真对话 (沙箱调试)", self.launch_simulation
        )
        self.btn_chat.pack(fill=tk.X, pady=(0, 6))

        # 分割线
        sep = tk.Frame(btn_frame, height=1, bg="#363a4f")
        sep.pack(fill=tk.X, pady=(6, 10))

        # 快捷链接横排
        links_frame = tk.Frame(btn_frame, bg=BG_COLOR)
        links_frame.pack(fill=tk.X, pady=2)

        self.btn_proj_dir = tk.Button(
            links_frame,
            text="📂 项目目录",
            font=("Microsoft YaHei UI", 9),
            fg=ACCENT_BLUE,
            bg=BG_COLOR,
            activebackground=CARD_BG,
            activeforeground=ACCENT_BLUE,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            command=self.open_project_folder,
        )
        self.btn_proj_dir.pack(side=tk.LEFT, expand=True)

        self.btn_logs = tk.Button(
            links_frame,
            text="📜 运行日志 (/logs)",
            font=("Microsoft YaHei UI", 9),
            fg=ACCENT_YELLOW,
            bg=BG_COLOR,
            activebackground=CARD_BG,
            activeforeground=ACCENT_YELLOW,
            relief=tk.FLAT,
            bd=0,
            cursor="hand2",
            command=self.open_logs_page,
        )
        self.btn_logs.pack(side=tk.RIGHT, expand=True)

        # 底部状态栏：左对齐消息样式 (前缀"› "，颜色 #6c7086)
        self.status_msg = tk.Label(
            self.root,
            text="› 就绪",
            font=("Microsoft YaHei UI", 8),
            fg="#6c7086",
            bg=BG_COLOR,
            anchor="w",
            padx=16,
            pady=6,
        )
        self.status_msg.pack(side=tk.BOTTOM, fill=tk.X)

    def _set_status_msg(self, text: str) -> None:
        prefix = "› " if not text.startswith("› ") else ""
        self.status_msg.configure(text=f"{prefix}{text}")

    def _create_btn(
        self,
        parent: tk.Widget,
        text: str,
        command: Any,
        bg: str = BTN_BG,
        textvariable: Any = None,
    ) -> tk.Button:
        kwargs: dict[str, Any] = {
            "font": ("Microsoft YaHei UI", 10),
            "fg": TEXT_COLOR,
            "bg": bg,
            "activebackground": BTN_ACTIVE,
            "activeforeground": TEXT_COLOR,
            "relief": tk.FLAT,
            "bd": 0,
            "pady": 6,  # 约 38px 统一高度
            "cursor": "hand2",
            "command": command,
        }
        if textvariable:
            kwargs["textvariable"] = textvariable
        else:
            kwargs["text"] = text
        btn = tk.Button(parent, **kwargs)
        btn._default_bg = bg

        def _on_enter(e: Any) -> None:
            if btn["bg"] != BTN_ACTIVE:
                btn.configure(bg=BTN_HOVER)

        def _on_leave(e: Any) -> None:
            btn.configure(bg=getattr(btn, "_default_bg", bg))

        btn.bind("<Enter>", _on_enter)
        btn.bind("<Leave>", _on_leave)
        return btn

    # ==========================================
    # 业务功能
    # ==========================================
    def toggle_tunnel(self) -> None:
        """切换 SSH 端口转发隧道（异步验证后才标记连接成功）"""
        if self.tunnel_process is None:
            cmd = get_ssh_tunnel_command()
            try:
                self.tunnel_process = subprocess.Popen(
                    cmd,
                    creationflags=CREATE_NO_WINDOW,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            except Exception as e:
                self.tunnel_process = None
                messagebox.showerror("启动失败", f"无法启动 SSH 隧道: {e}")
                return
            self.btn_tunnel.configure(text="连接中…", bg="#6c7086")
            self.btn_tunnel._default_bg = "#6c7086"
            self._set_status_msg("正在建立 SSH 隧道，验证中…")
            threading.Thread(target=self._verify_tunnel_worker, daemon=True).start()
        else:
            self._stop_tunnel()
            self._set_status_msg("SSH 隧道已断开")

    def _verify_tunnel_worker(self) -> None:
        """启动后等待片刻，验证进程存活且端口真正可用，失败则读出 stderr 报错"""
        time.sleep(3.0)
        proc = self.tunnel_process
        if proc is None:
            return  # 已被手动断开
        if proc.poll() is not None:
            err = ""
            try:
                err = (proc.stderr.read() if proc.stderr else b"").decode("utf-8", "ignore").strip()
            except Exception:
                pass
            detail = err or f"ssh 进程已退出 (code {proc.returncode})"
            self.root.after(0, lambda: self._on_tunnel_failed(detail))
            return
        # 进程存活，再验证本地端口确实可连接
        try:
            s = socket.create_connection(("127.0.0.1", 8080), timeout=2.0)
            s.close()
        except Exception:
            self.root.after(0, lambda: self._on_tunnel_failed("进程存活但 127.0.0.1:8080 不可连接（远端看板服务可能未启动）"))
            return
        self.root.after(0, self._on_tunnel_ok)

    def _on_tunnel_ok(self) -> None:
        self.btn_tunnel.configure(text="断开 SSH 隧道", bg="#a54242")
        self.btn_tunnel._default_bg = "#a54242"
        self._set_status_msg(f"SSH 端口转发隧道已启动 (8080 -> {DEFAULT_REMOTE_HOST})")

    def _on_tunnel_failed(self, detail: str) -> None:
        self._stop_tunnel()
        self._set_status_msg("SSH 隧道连接失败")
        messagebox.showerror(
            "SSH 隧道连接失败",
            f"{detail}\n\n常见原因：服务器未配置免密公钥、网络不通或远端服务未启动。",
        )

    def _stop_tunnel(self) -> None:
        if self.tunnel_process:
            try:
                self.tunnel_process.terminate()
                self.tunnel_process.wait(timeout=2.0)
            except Exception:
                try:
                    self.tunnel_process.kill()
                except Exception:
                    pass
            self.tunnel_process = None
        self.btn_tunnel.configure(text="一键连接 SSH 隧道", bg="#3b4252")
        self.btn_tunnel._default_bg = "#3b4252"

    def open_dashboard(self) -> None:
        webbrowser.open(f"http://localhost:8080/{TOKEN_QUERY}")

    def open_logs_page(self) -> None:
        webbrowser.open(f"http://localhost:8080/logs{TOKEN_QUERY}")

    def open_project_folder(self) -> None:
        proj_dir = os.path.abspath(r"D:\qq-companion")
        if os.path.exists(proj_dir):
            if sys.platform == "win32":
                os.startfile(proj_dir)
            else:
                subprocess.Popen(["xdg-open", proj_dir])
        else:
            messagebox.showinfo("提示", f"项目目录不存在: {proj_dir}")

    def launch_simulation(self) -> None:
        """调出独立 CMD 终端运行远端 companion.chat"""
        try:
            cmd = get_simulation_cmd_args()
            subprocess.Popen(cmd, shell=True)
            self._set_status_msg("已启动仿真对话终端窗口")
        except Exception as e:
            messagebox.showerror("启动失败", f"无法打开仿真对话终端: {e}")

    def fetch_backup_async(self) -> None:
        """异步拉取远端 latest.db"""
        threading.Thread(target=self._fetch_backup_worker, daemon=True).start()

    def _fetch_backup_worker(self) -> None:
        self._set_status_msg("正在从远端拉取最新备份 latest.db...")
        dest_dir = os.path.abspath(r"D:\qq-companion\data\backups")
        os.makedirs(dest_dir, exist_ok=True)
        dest_path = os.path.join(dest_dir, "latest.db")

        cmd = get_backup_command(local_dest=dest_path)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                creationflags=CREATE_NO_WINDOW,
                timeout=30,
            )
            if proc.returncode == 0:
                now_str = datetime.now().strftime("%H:%M")
                save_sync_state(
                    self.sync_state_file,
                    {"last_sync": now_str, "timestamp": datetime.now().isoformat()},
                )
                self.root.after(
                    0,
                    lambda: self.backup_btn_text.set(f"一键拉取备份 (上次: {now_str})"),
                )
                self.root.after(
                    0,
                    lambda: self._set_status_msg(
                        f"备份拉取完成: data/backups/latest.db ({now_str})"
                    ),
                )
                self.root.after(
                    0,
                    lambda: messagebox.showinfo(
                        "备份拉取完成", f"最新备份已保存到:\ndata/backups/latest.db\n\n时间: {now_str}"
                    ),
                )
            else:
                err = proc.stderr.strip() or "SCP 命令退出异常"
                self.root.after(
                    0,
                    lambda: messagebox.showerror("备份拉取失败", f"错误详情: {err}"),
                )
                self.root.after(
                    0, lambda: self._set_status_msg("备份拉取失败")
                )
        except Exception as e:
            self.root.after(
                0,
                lambda: messagebox.showerror("拉取异常", f"连接或执行异常: {e}"),
            )
            self.root.after(0, lambda: self._set_status_msg("备份拉取异常"))

    # ==========================================
    # 状态检测与轮询 (每 10 秒)
    # ==========================================
    def poll_status(self) -> None:
        threading.Thread(target=self._check_all_status, daemon=True).start()
        # 10 秒后继续下一次轮询
        self.root.after(10000, self.poll_status)

    def _check_all_status(self) -> None:
        # 1. 隧道端口连通性
        tunnel_ok = False
        try:
            s = socket.create_connection(("127.0.0.1", 8080), timeout=2.0)
            s.close()
            tunnel_ok = True
        except Exception:
            tunnel_ok = False

        # 2. 看板 Web 页面连通性
        dash_ok = False
        if tunnel_ok:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:8080/{TOKEN_QUERY}", method="GET")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    dash_ok = (resp.status == 200)
            except Exception:
                dash_ok = False

        # 3. 伴侣机器人 /api/status 详情
        api_data: dict[str, Any] | None = None
        if dash_ok:
            try:
                req = urllib.request.Request(f"http://127.0.0.1:8080/api/status{TOKEN_QUERY}", method="GET")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    if resp.status == 200:
                        raw = resp.read().decode("utf-8")
                        api_data = json.loads(raw)
            except Exception:
                api_data = None

        parsed = parse_status_data(api_data)
        api_known = api_data is not None

        # 更新 UI
        self.root.after(
            0,
            lambda: self._apply_status_ui(tunnel_ok, dash_ok, parsed, api_known),
        )

    def _apply_status_ui(
        self, tunnel_ok: bool, dash_ok: bool, parsed: dict[str, Any], api_known: bool = True
    ) -> None:
        # 隧道卡片
        if tunnel_ok:
            self.lamp_tunnel_dot.configure(fg=ACCENT_GREEN)
            self.lamp_tunnel_desc.configure(text="已连接")
        else:
            self.lamp_tunnel_dot.configure(fg=ACCENT_RED)
            self.lamp_tunnel_desc.configure(text="未连接")

        # 看板卡片
        if dash_ok:
            self.lamp_dash_dot.configure(fg=ACCENT_GREEN)
            self.lamp_dash_desc.configure(text="可访问")
        else:
            self.lamp_dash_dot.configure(fg=ACCENT_RED)
            self.lamp_dash_desc.configure(text="不可达")

        # 机器人卡片（三态：离线红在线绿未知黄）
        if not api_known:
            self.lamp_bot_dot.configure(fg=ACCENT_YELLOW)
            self.lamp_bot_desc.configure(text="未知")
        elif parsed.get("robot_online", False):
            self.lamp_bot_dot.configure(fg=ACCENT_GREEN)
            self.lamp_bot_desc.configure(text="在线")
        else:
            self.lamp_bot_dot.configure(fg=ACCENT_RED)
            self.lamp_bot_desc.configure(text="离线")

        # 头部阶段与计费
        if dash_ok and parsed.get("bot_alive"):
            stg = parsed.get("stage_name", "相识")
            cst = parsed.get("today_cost", 0.0)
            self.info_label.configure(text=format_header_info(stg, cst))
        else:
            self.info_label.configure(text="当前阶段：-- · 今日费用：¥0.00")

        # 掉线提醒（若看板通但 onebot_connected 为 False）
        if dash_ok and parsed.get("bot_alive") and not parsed.get("onebot_connected"):
            now_ts = time.time()
            if now_ts - self.last_disconnect_alert >= 600:  # 每 10 分钟最多提醒一次
                self.last_disconnect_alert = now_ts
                messagebox.showwarning("掉线提醒", "青梓掉线了，该去 NapCat 扫码了")

    def on_close(self) -> None:
        self._stop_tunnel()
        self.root.destroy()


def main() -> None:
    # 高 DPI 处理：进程声明 DPI 感知（tk 会自动把字体控件按系统 DPI 缩放，画面清晰）；
    # 窗口几何尺寸手动乘 dpi/96 倍率（tk 的 geometry 永远是像素值，不自动缩放）。
    if sys.platform == "win32":
        try:
            import ctypes
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    root = tk.Tk()
    scale = 1.0
    if sys.platform == "win32":
        try:
            import ctypes
            dpi = ctypes.windll.user32.GetDpiForWindow(root.winfo_id())
            if dpi > 96:
                scale = dpi / 96.0
        except Exception:
            pass
    app = QingziHomeApp(root, scale=scale)
    root.mainloop()


if __name__ == "__main__":
    main()
