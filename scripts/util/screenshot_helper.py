"""仪表盘截图辅助脚本 (scripts/util/screenshot_helper.py)

给 admin 仪表盘各页面截图，用于生成文档配图。演示数据一律写入独立的演示库
data/screenshot_demo.db —— 绝不使用生产库 data/companion.db，
否则会把假日记、假事实、假计费记录混进真实相处数据里。

命令行参数（都已给默认值，不带参数跑 = 与旧版行为一致：桌面 1200x960、原输出目录）：
  --width/--height  截图视口尺寸（默认 1200x960 = 桌面端）
  --out             输出目录（默认仍是原来的绝对路径）
  --keep            保留临时浏览器 profile 目录（排查 CDP 问题时用）

手机端核对示例（390x844 是 iPhone 竖屏逻辑分辨率）：
  python scripts/util/screenshot_helper.py --width 390 --height 844 --out snapshots/mobile
宽度 ≤ 768 时自动开**手机视口仿真**（设备像素比 1 + viewport meta 生效）。

为什么不用 Edge 命令行的 `--window-size=390,844 --screenshot=`：
Windows 会把浏览器窗口卡在最小约 518px 宽，命令行参数要不到 390px 的布局视口——
渲染出来的是 518px 宽的排版，却被裁成 390px 的图，右侧内容整段被切掉（假证据）。
所以这里改用 CDP 的 `Emulation.setDeviceMetricsOverride` 精确指定视口，顺手把每页的
`scrollWidth / innerWidth` 量出来写进 overflow_report.json（横向溢出 = 手机端会左右晃）。

截图固定输出 7 张（文件名与旧版一致）：
  screenshot_paper_{overview,memory,debug,costs,stickers,logs,admin}.png
"""

import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
import aiohttp
from aiohttp import web
from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import AdminConfig, Config
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.stickers import StickerManager

OUTPUT_DIR = r"C:\Users\user\.gemini\antigravity\brain\7ad6ecf4-cb87-4dfd-8807-763774c45336"
EDGE_EXE = r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"

# 桌面端默认视口，与改动前逐字节一致
DEFAULT_WIDTH = 1200
DEFAULT_HEIGHT = 960

# 宽度小于等于这个值就按手机视口仿真（与样式表里 @media (max-width: 768px) 同一条线）
MOBILE_WIDTH_THRESHOLD = 768

# CDP 调试端口：只在脚本运行期间占用，跑完关掉
CDP_PORT = 19311

# 页面加载完成后再等一会儿，让入场动画与雷达图生长动画跑完（旧版靠 --virtual-time-budget=1500）
SETTLE_SECONDS = 1.6

# 演示库：独立文件，跟生产库 data/companion.db 无任何关系（要重新截图可整份删掉重跑）
DEMO_DB_PATH = "data/screenshot_demo.db"


class EdgeCdp:
    """一个 headless Edge + CDP 会话：能精确指定布局视口，并能读页面度量。"""

    def __init__(self, width: int, height: int, mobile: bool, keep_profile: bool = False):
        self.width = width
        self.height = height
        self.mobile = mobile
        self.keep_profile = keep_profile
        self._proc = None
        self._profile = None
        self._client = None
        self._ws = None
        self._msg_id = 0

    async def __aenter__(self) -> "EdgeCdp":
        self._profile = tempfile.mkdtemp(prefix="qqc_edge_profile_")
        self._proc = subprocess.Popen(
            [
                EDGE_EXE,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                f"--remote-debugging-port={CDP_PORT}",
                f"--user-data-dir={self._profile}",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self._client = aiohttp.ClientSession()
        ws_url = await self._wait_for_page_target()
        self._ws = await self._client.ws_connect(ws_url, max_msg_size=0)
        await self.call("Page.enable")
        await self.call(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": self.width,
                "height": self.height,
                "deviceScaleFactor": 1,
                "mobile": self.mobile,
            },
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except Exception:
                self._proc.kill()
        if self._profile and not self.keep_profile:
            shutil.rmtree(self._profile, ignore_errors=True)

    async def _wait_for_page_target(self) -> str:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            try:
                async with self._client.get(f"http://127.0.0.1:{CDP_PORT}/json/list") as resp:
                    targets = await resp.json()
                pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
                if pages:
                    return pages[0]["webSocketDebuggerUrl"]
            except Exception:
                pass
            await asyncio.sleep(0.2)
        raise RuntimeError("Edge CDP 端点未就绪（20 秒超时）")

    async def call(self, method: str, params: dict = None) -> dict:
        self._msg_id += 1
        mid = self._msg_id
        await self._ws.send_json({"id": mid, "method": method, "params": params or {}})
        while True:
            msg = json.loads(await self._ws.receive_str())
            if msg.get("id") != mid:
                continue
            if "error" in msg:
                raise RuntimeError(f"CDP {method} 失败: {msg['error']}")
            return msg.get("result", {})

    async def capture(self, url: str) -> dict:
        """打开 url，等加载+动画落定，返回 {'png': bytes, 'metrics': {...}}。"""
        await self.call("Page.navigate", {"url": url})
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            res = await self.call(
                "Runtime.evaluate",
                {"expression": "document.readyState", "returnByValue": True},
            )
            if res.get("result", {}).get("value") == "complete":
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(SETTLE_SECONDS)

        metrics_res = await self.call(
            "Runtime.evaluate",
            {
                "expression": (
                    "JSON.stringify({inner_width: window.innerWidth,"
                    " scroll_width: document.documentElement.scrollWidth,"
                    " body_scroll_width: document.body ? document.body.scrollWidth : 0,"
                    " mobile_meta: !!document.querySelector('meta[name=viewport]'),"
                    " media_768: window.matchMedia('(max-width: 768px)').matches,"
                    " nav_scrollable: (function(){var n=document.querySelector('.nav-links');"
                    " return n ? n.scrollWidth > n.clientWidth : null;})(),"
                    " table_scroll_overflows: (function(){var ts=document.querySelectorAll('.table-scroll');"
                    " var o=[]; for (var i=0;i<ts.length;i++){o.push(ts[i].scrollWidth-ts[i].clientWidth);}"
                    " return o;})()})"
                ),
                "returnByValue": True,
            },
        )
        metrics = json.loads(metrics_res["result"]["value"])

        shot = await self.call(
            "Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False}
        )
        return {"png": base64.b64decode(shot["data"]), "metrics": metrics}


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="给看板各页面截图（默认桌面 1200x960）")
    parser.add_argument("--width", type=int, default=DEFAULT_WIDTH, help=f"视口宽度，默认 {DEFAULT_WIDTH}")
    parser.add_argument("--height", type=int, default=DEFAULT_HEIGHT, help=f"视口高度，默认 {DEFAULT_HEIGHT}")
    parser.add_argument("--out", type=str, default=OUTPUT_DIR, help="输出目录，默认沿用原路径")
    parser.add_argument("--keep", action="store_true", help="保留临时浏览器 profile 目录（排查用）")
    return parser.parse_args(argv)


async def main(args=None):
    if args is None:
        args = parse_args()
    # Edge 只吃绝对 Windows 路径（相对路径会报"找不到指定的路径"）
    out_dir = os.path.abspath(args.out)
    mobile = args.width <= MOBILE_WIDTH_THRESHOLD
    viewport = f"{args.width}x{args.height}" + ("（手机视口仿真）" if mobile else "")
    db_path = DEMO_DB_PATH
    db = Database(db_path)
    await db.init_tables()
    config = Config.load("config.example.toml")
    persona_dir = "characters/qingzi" if os.path.exists("characters/qingzi") else config.character.path
    persona = Persona.load(persona_dir)
    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db)
    stickers = StickerManager(os.path.join(persona.base_dir, persona.stickers_dir), db)
    assembler = PromptAssembler(persona, affection, mood, memory, stickers, db)

    # 准备样例数据
    await db.execute("INSERT OR IGNORE INTO turns (role, content, created_at) VALUES ('user', '你好青梓', '2026-09-29 12:00')")
    await db.execute("UPDATE counters SET value = 88 WHERE key = 'total_turns'")
    await db.execute("INSERT OR IGNORE INTO milestones (stage, reached_at) VALUES (1, '2026-09-29 10:00')")
    await db.execute("INSERT OR IGNORE INTO diary (content, importance, sentiment, recall_count, created_at) VALUES ('今天在琴房练了大提琴，秋天的阳光照在窗棂上很舒服。', 8, '温暖', 3, '2026-09-29 11:30')")
    await db.execute("INSERT OR IGNORE INTO diary (content, importance, sentiment, recall_count, created_at) VALUES ('临湖散步时想到了一些旋律。', 5, '思念', 1, '2026-09-28 17:00')")
    await db.execute("INSERT OR IGNORE INTO facts (content) VALUES ('机主平时喜欢在深夜写代码')")
    await db.execute("INSERT OR IGNORE INTO suppressed_desires (content, created_at) VALUES ('其实刚才还想问问他今天吃晚饭了没有…', '2026-09-29 12:15')")
    await db.execute("INSERT OR IGNORE INTO llm_calls (purpose, model, prompt_tokens, completion_tokens, cost_estimate, cache_hit_tokens, cache_miss_tokens, created_at) VALUES ('chat', 'deepseek-flash', 1200, 180, 0.0035, 1000, 200, '2026-09-29 13:00')")

    class MockOneBot:
        is_connected = True

    admin = AdminServer(
        config=AdminConfig(host="127.0.0.1", port=8899),
        persona=persona,
        affection=affection,
        mood=mood,
        memory=memory,
        stickers=stickers,
        proactive=None,
        assembler=assembler,
        db=db,
        onebot=MockOneBot(),
        db_path=db_path,
        backup_dir="data/backups",
    )
    await admin.start()
    print("AdminServer started on http://127.0.0.1:8899")

    # 截图列表
    pages = [
        ("/", "screenshot_paper_overview.png"),
        ("/memory", "screenshot_paper_memory.png"),
        ("/debug", "screenshot_paper_debug.png"),
        ("/costs", "screenshot_paper_costs.png"),
        ("/stickers", "screenshot_paper_stickers.png"),
        ("/logs", "screenshot_paper_logs.png"),
        ("/admin", "screenshot_paper_admin.png"),
    ]

    os.makedirs(out_dir, exist_ok=True)
    report = {
        "viewport": viewport,
        "width": args.width,
        "height": args.height,
        "mobile_emulation": mobile,
        "out_dir": out_dir,
        "pages": [],
    }

    async with EdgeCdp(args.width, args.height, mobile, keep_profile=args.keep) as cdp:
        for path, filename in pages:
            out_file = os.path.join(out_dir, filename)
            url = f"http://127.0.0.1:8899{path}"
            print(f"Capturing {url} @ {viewport} -> {out_file}...")
            try:
                shot = await cdp.capture(url)
            except Exception as e:
                print(f"Error capturing {url}: {e}")
                report["pages"].append({"path": path, "file": out_file, "error": str(e)})
                continue
            with open(out_file, "wb") as f:
                f.write(shot["png"])
            m = shot["metrics"]
            overflow = max(m["scroll_width"], m["body_scroll_width"]) - m["inner_width"]
            print(
                f"Captured: {out_file} ({os.path.getsize(out_file)} bytes) "
                f"inner={m['inner_width']} scroll={m['scroll_width']} "
                f"溢出={overflow}px 媒体查询命中={m['media_768']} "
                f"导航可横滑={m['nav_scrollable']} 表格横滑余量={m['table_scroll_overflows']}"
            )
            report["pages"].append(
                {"path": path, "file": out_file, "bytes": os.path.getsize(out_file), **m,
                 "overflow_px": overflow}
            )

    report_path = os.path.join(out_dir, "overflow_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"度量报告: {report_path}")

    await admin.stop()
    await db.close()
    print("All captures completed.")

if __name__ == "__main__":
    asyncio.run(main())
