"""看板快照与零变化比对工具 (scripts/util/snapshot_dashboard.py)
用于 REFACTOR 任务：建立看板 HTML 与 /api/status 的零变化比对基线。
用法:
  python scripts/util/snapshot_dashboard.py --save snapshots/baseline
  python scripts/util/snapshot_dashboard.py --diff snapshots/baseline snapshots/after
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
import re
import shutil
import sys
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import AdminConfig, ProactiveConfig, ReplyConfig
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager

ROUTES = [
    ("/", "index.html"),
    ("/memory", "memory.html"),
    ("/debug", "debug.html"),
    ("/costs", "costs.html"),
    ("/stickers", "stickers.html"),
    ("/logs", "logs.html"),
    ("/admin", "admin.html"),
    ("/api/status", "api_status.json"),
]


def normalize_content(route: str, content: str) -> str:
    """根据 REFACTOR.md 规则归一化动态内容以做确定性比对"""
    # 1. 时间戳归一化: \d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})? -> <TS>
    content = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", "<TS>", content)

    # 2. uptime_minutes 归一化
    content = re.sub(r'"uptime_minutes":\s*\d+', '"uptime_minutes": <UPTIME>', content)

    # 3. 闲置小时数归一化: 他已有 \d+ 小时没说话了 -> 他已有 <HOURS> 小时没说话了
    content = re.sub(r"他已有\s*\d+\s*小时没说话了", "他已有 <HOURS> 小时没说话了", content)

    # 4. 日志页面整体豁免内容，但校验骨架包含 log-window
    if route == "/logs":
        if "log-window" in content:
            # 保留骨架，把 log-window 内部可变文本折叠
            content = re.sub(
                r'(<div class="log-window">)(.*?)(</div>)',
                r"\1\n<LOG_ENTRIES_EXEMPTED>\n\3",
                content,
                flags=re.DOTALL,
            )
        else:
            raise ValueError("日志页面缺少 log-window 容器骨架！")

    # 5. 如果是 JSON，做格式化保证键顺序稳定
    if route.endswith(".json") or route == "/api/status":
        try:
            data = json.loads(content)
            content = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
            # 再跑一次时间戳/uptime 归一化避免 JSON 转换还原
            content = re.sub(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?", "<TS>", content)
            content = re.sub(r'"uptime_minutes":\s*\d+', '"uptime_minutes": <UPTIME>', content)
        except Exception:
            pass

    return content


async def fetch_snapshots(port: int = 18080, char_dir: str = "characters/qingzi") -> Dict[str, str]:
    """启动独立 AdminServer 并抓取全部页面"""
    prod_db = "data/companion.db"
    tmp_db = "data/snapshot_tmp.db"

    if os.path.exists(tmp_db):
        try:
            os.remove(tmp_db)
        except Exception:
            pass

    if os.path.exists(prod_db):
        shutil.copy2(prod_db, tmp_db)
    else:
        # 没有生产库时使用干净库
        pass

    db = Database(tmp_db)
    await db.init_tables()

    if not os.path.exists(char_dir):
        char_dir = "characters/example"
    persona = Persona.load(char_dir)
    stickers_dir = os.path.join(persona.base_dir, persona.stickers_dir)

    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db, gateway=None, affection=affection, persona=persona)
    stickers = StickerManager(stickers_dir, db)
    await stickers.sync_initial_stickers()
    replier = Replier(ReplyConfig(), stickers)
    proactive = ProactiveScheduler(
        config=ProactiveConfig(),
        persona=persona,
        affection=affection,
        mood=mood,
        memory=memory,
        stickers=stickers,
        replier=replier,
        gateway=None,
        db=db,
        send_msg_fn=None,
    )
    assembler = PromptAssembler(persona, affection, mood, memory, stickers, db)

    admin = AdminServer(
        config=AdminConfig(port=port),
        persona=persona,
        affection=affection,
        mood=mood,
        memory=memory,
        stickers=stickers,
        proactive=proactive,
        assembler=assembler,
        db=db,
        db_path=tmp_db,
    )

    # 启动 HTTP 服务
    app = web.Application()
    app.router.add_get("/", admin.handle_overview)
    app.router.add_get("/api/status", admin.handle_api_status)
    app.router.add_get("/memory", admin.handle_memory)
    app.router.add_get("/debug", admin.handle_debug)
    app.router.add_get("/costs", admin.handle_costs)
    app.router.add_get("/stickers", admin.handle_stickers)
    app.router.add_get("/stickers/img/{name}", admin.handle_sticker_image)
    app.router.add_get("/logs", admin.handle_logs)
    app.router.add_get("/admin", admin.handle_admin)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    snapshots: Dict[str, str] = {}
    base_url = f"http://127.0.0.1:{port}"

    try:
        async with aiohttp.ClientSession() as client:
            for route, filename in ROUTES:
                url = f"{base_url}{route}"
                async with client.get(url) as resp:
                    if resp.status != 200:
                        raise RuntimeError(f"抓取页面 {route} 失败，HTTP 状态码: {resp.status}")
                    text = await resp.text()
                    normalized = normalize_content(route, text)
                    snapshots[filename] = normalized
    finally:
        await runner.cleanup()
        await db.close()
        if os.path.exists(tmp_db):
            try:
                os.remove(tmp_db)
            except Exception:
                pass

    return snapshots


def save_snapshots(snapshots: Dict[str, str], target_dir: str) -> None:
    os.makedirs(target_dir, exist_ok=True)
    for filename, content in snapshots.items():
        filepath = os.path.join(target_dir, filename)
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
    print(f"✓ 已抓取并保存 {len(snapshots)} 个端点快照至: {target_dir}")


def diff_directories(dir_a: str, dir_b: str) -> bool:
    print(f"比对快照: {dir_a} <==> {dir_b}")
    diff_found = False

    files_a = set(os.listdir(dir_a))
    files_b = set(os.listdir(dir_b))

    all_files = sorted(files_a | files_b)
    for f in all_files:
        if f not in files_a:
            print(f"❌ 差异: 文件 {f} 只存在于 {dir_b}")
            diff_found = True
            continue
        if f not in files_b:
            print(f"❌ 差异: 文件 {f} 只存在于 {dir_a}")
            diff_found = True
            continue

        file_a_path = os.path.join(dir_a, f)
        file_b_path = os.path.join(dir_b, f)

        with open(file_a_path, "r", encoding="utf-8") as fa, open(file_b_path, "r", encoding="utf-8") as fb:
            lines_a = fa.readlines()
            lines_b = fb.readlines()

        if lines_a == lines_b:
            print(f"  ✓ {f:<18} 完全一致 (byte-for-byte identical after normalization)")
        else:
            print(f"❌ 差异发现: {f}")
            diff_found = True
            diff = difflib.unified_diff(
                lines_a, lines_b, fromfile=f"{dir_a}/{f}", tofile=f"{dir_b}/{f}", lineterm=""
            )
            for d in list(diff)[:30]:
                print(f"    {d}")

    if not diff_found:
        print("\n🎉 比对结果: 100% 空 diff！重构前后看板 HTML 与 API 状态完全零变化！")
        return True
    else:
        print("\n❌ 比对结果: 发现差异，重构引入了非预期行为变化！")
        return False


def main():
    parser = argparse.ArgumentParser(description="看板零变化快照与比对工具")
    parser.add_argument("--save", type=str, help="抓取并保存快照的目标目录")
    parser.add_argument("--diff", nargs=2, metavar=("DIR_A", "DIR_B"), help="比对两个快照目录")
    parser.add_argument("--port", type=int, default=18080, help="测试 HTTP 端口 (默认 18080)")

    args = parser.parse_args()

    if args.save:
        snaps = asyncio.run(fetch_snapshots(port=args.port))
        save_snapshots(snaps, args.save)
    elif args.diff:
        dir_a, dir_b = args.diff
        ok = diff_directories(dir_a, dir_b)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
