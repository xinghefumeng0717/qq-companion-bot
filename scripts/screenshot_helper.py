"""仪表盘截图辅助脚本 (scripts/screenshot_helper.py)

给 admin 仪表盘各页面截图，用于生成文档配图。演示数据一律写入独立的演示库
data/screenshot_demo.db —— 绝不使用生产库 data/companion.db，
否则会把假日记、假事实、假计费记录混进真实相处数据里。
"""

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
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

# 演示库：独立文件，跟生产库 data/companion.db 无任何关系（要重新截图可整份删掉重跑）
DEMO_DB_PATH = "data/screenshot_demo.db"

async def main():
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

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    for path, filename in pages:
        out_file = os.path.join(OUTPUT_DIR, filename)
        url = f"http://127.0.0.1:8899{path}"
        cmd = [
            EDGE_EXE,
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--virtual-time-budget=1500",
            "--window-size=1200,960",
            f"--screenshot={out_file}",
            url,
        ]
        print(f"Capturing {url} -> {filename}...")
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=10.0)
            if os.path.exists(out_file) and os.path.getsize(out_file) > 0:
                print(f"Captured: {out_file} ({os.path.getsize(out_file)} bytes)")
            else:
                print(f"Failed to capture {url}")
        except Exception as e:
            print(f"Error capturing {url}: {e}")

    await admin.stop()
    await db.close()
    print("All captures completed.")

if __name__ == "__main__":
    asyncio.run(main())
