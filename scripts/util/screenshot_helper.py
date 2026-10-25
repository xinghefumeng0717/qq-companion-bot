"""仪表盘截图辅助脚本 (scripts/util/screenshot_helper.py)

给 admin 仪表盘各页面截图，用于生成文档配图。演示数据一律写入独立的演示库
data/screenshot_demo.db —— 绝不使用生产库 data/companion.db，
否则会把假日记、假事实、假计费记录混进真实相处数据里。

命令行参数（都已给默认值，不带参数跑 = 与旧版行为一致：小艾 + journal、桌面 1200x960、原输出目录）：
  --width/--height  截图视口尺寸（默认 1200x960 = 桌面端）
  --sizes           一次跑多档宽度，如 "1200x960,390x844"（给定时忽略 --width/--height，
                    文件名自动加 _{宽} 后缀，避免互相覆盖）
  --card            角色卡目录（默认 characters/example；指定带 theme/skin.json 的卡
                    如指定带 theme/skin.json 的卡时，主题会按卡里的皮肤走）
  --theme           journal / plain / endfield（显式指定时优先于卡里的皮肤声明）
  --out             输出目录（默认仍是原来的绝对路径）
  --keep            保留临时浏览器 profile 目录（排查 CDP 问题时用）

手机端核对示例（390x844 是 iPhone 竖屏逻辑分辨率）：
  python scripts/util/screenshot_helper.py --width 390 --height 844 --out snapshots/mobile
宽度 ≤ 768 时自动开**手机视口仿真**（设备像素比 1 + viewport meta 生效）。

新主题全页核对示例（终末地 + 水墨皮肤，桌面与手机两档）：
  python scripts/util/screenshot_helper.py --card characters/example --theme endfield \
      --sizes 1200x960,390x844 --out snapshots/endfield

演示库 data/screenshot_demo.db 每次运行都会重建（固定样例，避免重复插入把图表撑歪）；
data/logs/bot.log 不存在时会临时写一份演示日志，拍完删除，绝不覆盖真实日志。

为什么不用 Edge 命令行的 `--window-size=390,844 --screenshot=`：
Windows 会把浏览器窗口卡在最小约 518px 宽，命令行参数要不到 390px 的布局视口——
渲染出来的是 518px 宽的排版，却被裁成 390px 的图，右侧内容整段被切掉（假证据）。
所以这里改用 CDP 的 `Emulation.setDeviceMetricsOverride` 精确指定视口，顺手把每页的
`scrollWidth / innerWidth` 量出来写进 overflow_report.json（横向溢出 = 手机端会左右晃）。

每档视口输出 7 张（单档时文件名与旧版一致；--sizes 多档时追加 _{宽}）：
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
                    " table_scroll_overflows: (function(){var ts=document.querySelectorAll('.table-scroll,.table-wrap');"
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
    parser.add_argument("--sizes", type=str, default="", help='多档尺寸，如 "1200x960,390x844"')
    parser.add_argument("--card", type=str, default="characters/example", help="角色卡目录")
    parser.add_argument("--theme", type=str, default="", choices=["", "journal", "plain", "endfield"],
                        help="看板主题；留空交给 config/角色卡皮肤决定")
    parser.add_argument("--out", type=str, default=OUTPUT_DIR, help="输出目录，默认沿用原路径")
    parser.add_argument("--keep", action="store_true", help="保留临时浏览器 profile 目录（排查用）")
    return parser.parse_args(argv)


def parse_sizes(args) -> list:
    """--sizes 优先；给出多档时返回 [(w,h), ...]，否则单档 [(--width, --height)]。"""
    if not args.sizes:
        return [(args.width, args.height)]
    sizes = []
    for chunk in args.sizes.split(","):
        chunk = chunk.strip().lower()
        if not chunk:
            continue
        try:
            w, h = chunk.split("x")
            sizes.append((int(w), int(h)))
        except ValueError:
            raise SystemExit(f"--sizes 里的 {chunk!r} 不是 宽x高 格式")
    if not sizes:
        raise SystemExit("--sizes 至少给一档尺寸")
    return sizes


# 演示日志：只在 data/logs/bot.log 不存在时临时写、拍完删掉
DEMO_LOG_LINES = [
    "2026-10-22 18:02:11 [INFO] [OneBot] 已连接 ws://127.0.0.1:3001",
    "2026-10-22 18:02:12 [INFO] [Persona] 角色卡已加载：小艾（缘缘庄）",
    "2026-10-22 18:02:13 [INFO] [Affection] 阶段 5 · 伙伴 · 复合分 68.4",
    "2026-10-22 18:07:44 [INFO] [Chat] 收到私聊消息，进入回合处理",
    "2026-10-22 18:07:46 [INFO] [Gateway] chat 调用完成，耗时 1.9s",
    "2026-10-22 18:07:47 [WARNING] [ReplyCheck] 回复里出现了系统提示词片段，已重写",
    "2026-10-22 18:12:03 [INFO] [Observer] 结算完成：自我表露 6.2 / 共鸣 6.8",
    "2026-10-22 18:12:04 [DEBUG] [Memory] 日记强度 7.31，召回次数 +1",
    "2026-10-22 18:20:15 [INFO] [Proactive] 主动消息未回 0 / 2，本轮不打扰",
    "2026-10-22 18:26:40 [ERROR] [TTS] 语音合成超时，已跳过本条语音",
    "2026-10-22 18:26:41 [INFO] [Chat] 回复已发送（纯文本）",
]


def ensure_demo_log() -> str:
    """没有真实日志时临时铺一份演示日志；返回拍完要删掉的路径（有真实日志则空串）。"""
    path = "data/logs/bot.log"
    if os.path.exists(path):
        return ""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(DEMO_LOG_LINES) + "\n")
    return path


async def main(args=None):
    if args is None:
        args = parse_args()
    # Edge 只吃绝对 Windows 路径（相对路径会报"找不到指定的路径"）
    out_dir = os.path.abspath(args.out)
    sizes = parse_sizes(args)
    db_path = DEMO_DB_PATH
    # 演示库每次重建：固定样例，重复跑不会把表格行数越堆越多
    if os.path.exists(db_path):
        os.remove(db_path)
    db = Database(db_path)
    await db.init_tables()
    config = Config.load("config.example.toml")
    persona_dir = args.card or config.character.path
    persona = Persona.load(persona_dir)
    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db)
    stickers = StickerManager(os.path.join(persona.base_dir, persona.stickers_dir), db)
    assembler = PromptAssembler(persona, affection, mood, memory, stickers, db)

    # 准备样例数据（总览 / 记忆 / 费用 / 调试四页都要有东西可看）
    await db.execute("INSERT INTO turns (role, content, created_at) VALUES ('user', '我回来了', '2026-09-29 12:00')")
    await db.execute("UPDATE counters SET value = 842 WHERE key = 'total_turns'")
    await db.execute("INSERT INTO milestones (stage, reached_at) VALUES (1, '2026-09-29 10:00')")
    await db.execute("INSERT INTO milestones (stage, reached_at) VALUES (3, '2026-10-06 21:10')")
    await db.execute("INSERT INTO milestones (stage, reached_at) VALUES (5, '2026-10-18 23:40')")
    await db.execute("INSERT INTO facts (content) VALUES ('机主平时喜欢在深夜写代码')")
    await db.execute("INSERT INTO facts (content, core) VALUES ('机主怕吵，晚上十一点后戴耳机', 1)")
    await db.execute("INSERT INTO self_facts (content, core) VALUES ('我不太会直说想他', 1)")
    await db.execute("INSERT INTO suppressed_desires (content, created_at) VALUES ('其实刚才还想问问他今天吃晚饭了没有…', '2026-10-22 12:15')")
    await db.execute("INSERT INTO suppressed_desires (content, created_at) VALUES ('想问他要不要一起去看银杏，又怕太主动', '2026-10-21 19:02')")
    await db.execute(
        "INSERT INTO meetups (when_at, place, plan, status, recap) "
        "VALUES ('2026-10-25 15:00', '城南旧书市', '翻一下午旧书', 0, '')"
    )
    await db.execute(
        "INSERT INTO meetups (when_at, place, plan, status, recap) "
        "VALUES ('2026-10-12 18:30', '缘缘庄', '吃火锅', 1, "
        "'锅底翻滚的时候她一直在给他夹菜，最后把账本推过去让他对账。')"
    )
    for content, importance, sentiment, recall, at in (
        ("今天在琴房练了大提琴，秋天的阳光照在窗棂上很舒服。", 8, "温暖", 3, "2026-10-22 21:30"),
        ("临湖散步时想到了一些旋律，回来就忘了。", 5, "思念", 1, "2026-10-21 17:00"),
        ("他说我记账太认真，我觉得他不懂。", 6, "平静", 2, "2026-10-20 22:10"),
        ("降温了，提醒他加衣服，他说知道了。", 4, "释然", 0, "2026-10-19 09:20"),
    ):
        await db.execute(
            "INSERT INTO diary (content, importance, sentiment, recall_count, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (content, importance, sentiment, recall, at),
        )
    for purpose, model, p_tok, c_tok, cost, hit, miss, at in (
        ("main_chat", "deepseek-chat", 1237706, 42485, 2.0012, 1000000, 237706, "2026-10-22 21:14"),
        ("proactive_message", "deepseek-chat", 288448, 26333, 0.9408, 240000, 48448, "2026-10-22 20:41"),
        ("observer", "deepseek-flash", 301710, 30330, 0.2398, 260000, 41710, "2026-10-22 21:15"),
        ("proactive_decision", "deepseek-flash", 110914, 14157, 0.2202, 90000, 20914, "2026-10-22 20:05"),
        ("diary_archive", "deepseek-flash", 35891, 24413, 0.1383, 20000, 15891, "2026-10-22 23:40"),
        ("life_arc", "deepseek-chat", 18935, 11905, 0.0627, 12000, 6935, "2026-10-21 10:02"),
        ("vision_perception", "qwen-vl", 7950, 2397, 0.0230, 6000, 1950, "2026-10-20 16:22"),
    ):
        await db.execute(
            "INSERT INTO llm_calls (purpose, model, prompt_tokens, completion_tokens, "
            "cost_estimate, cache_hit_tokens, cache_miss_tokens, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (purpose, model, p_tok, c_tok, cost, hit, miss, at),
        )
    for sd, rs, wa, re, at in (
        (6.4, 6.1, 6.6, 6.9, "2026-10-22 21:15"),
        (5.8, 6.4, 6.1, 6.2, "2026-10-22 20:41"),
        (6.9, 5.7, 6.8, 7.1, "2026-10-21 22:03"),
        (4.2, 4.6, 4.9, 4.4, "2026-10-15 19:30"),
        (5.1, 5.4, 5.2, 5.0, "2026-10-14 20:12"),
        (3.8, 4.1, 4.4, 3.9, "2026-10-03 18:26"),
    ):
        await db.execute(
            "INSERT INTO observer_scores (self_disclosure, responsiveness, warmth_score, "
            "resonance, created_at) VALUES (?, ?, ?, ?, ?)",
            (sd, rs, wa, re, at),
        )

    class MockOneBot:
        is_connected = True

    admin = AdminServer(
        config=AdminConfig(host="127.0.0.1", port=8899, theme=args.theme or "journal"),
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
    demo_log = ensure_demo_log()
    print(f"AdminServer started on http://127.0.0.1:8899 "
          f"(card={persona_dir}, theme={admin.theme}, skin={'有' if admin.skin else '无'})")

    # 截图列表
    page_list = [
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
        "card": persona_dir,
        "theme": admin.theme,
        "skin": bool(admin.skin),
        "out_dir": out_dir,
        "sizes": [],
        "pages": [],
    }

    try:
        for width, height in sizes:
            mobile = width <= MOBILE_WIDTH_THRESHOLD
            viewport = f"{width}x{height}" + ("（手机视口仿真）" if mobile else "")
            report["sizes"].append({"width": width, "height": height,
                                    "mobile_emulation": mobile})
            async with EdgeCdp(width, height, mobile, keep_profile=args.keep) as cdp:
                for path, base_name in page_list:
                    if len(sizes) > 1:
                        stem, ext = os.path.splitext(base_name)
                        filename = f"{stem}_{width}{ext}"
                    else:
                        filename = base_name
                    out_file = os.path.join(out_dir, filename)
                    url = f"http://127.0.0.1:8899{path}"
                    print(f"Capturing {url} @ {viewport} -> {out_file}...")
                    try:
                        shot = await cdp.capture(url)
                    except Exception as e:
                        print(f"Error capturing {url}: {e}")
                        report["pages"].append({"path": path, "file": out_file,
                                                "width": width, "error": str(e)})
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
                        {"path": path, "file": out_file, "width": width,
                         "bytes": os.path.getsize(out_file), **m, "overflow_px": overflow}
                    )
    finally:
        if demo_log and os.path.exists(demo_log):
            os.remove(demo_log)
        await admin.stop()
        await db.close()

    report_path = os.path.join(out_dir, "overflow_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"度量报告: {report_path}")
    print("All captures completed.")

if __name__ == "__main__":
    asyncio.run(main())
