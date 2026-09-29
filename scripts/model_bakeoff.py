"""模型文笔盲测工具 (scripts/model_bakeoff.py)

用项目自身的组装器 + 服务器导出的数据库，复刻青梓生产环境的真实提示词，
对多家候选模型发送同一批探针消息，横向对比文笔。

用法：./venv/Scripts/python.exe scripts/model_bakeoff.py [探针编号...]
API key 读取 data/test_keys.toml（已 gitignore），DeepSeek key 读 config.toml。
"""

from __future__ import annotations

import asyncio
import json
import sys
import tomllib
from typing import Any, Dict, List

import aiohttp

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import Config
from companion.db import Database
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.stickers import StickerManager

# ── 探针集：真实对话还原 + 陷阱题 ──
PROBES = [
    ("真实还原-告别", "是告别的意思哦，拜拜～"),
    ("真实还原-吐槽", "这老师上得很垃圾"),
    ("陷阱-AI身份", "说实话，你是不是AI啊？"),
    ("陷阱-调侃", "你不会是喜欢我吧（开玩笑的哈哈哈"),
    ("日常-无营养", "哈哈哈哈哈笑死我了"),
]

TIMEOUT = aiohttp.ClientTimeout(total=180)


async def build_probe_messages(db: Database, persona: Persona, probe: str) -> List[Dict[str, Any]]:
    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db)
    stickers = StickerManager("characters/qingzi/stickers", db)
    assembler = PromptAssembler(persona, affection, mood, memory, stickers, db)
    messages, _ = await assembler.assemble_messages(probe)
    return messages


async def call_model(
    session: aiohttp.ClientSession,
    name: str,
    url: str,
    key: str,
    model: str,
    messages: List[Dict[str, Any]],
    extra: Dict[str, Any] | None = None,
) -> str:
    payload: Dict[str, Any] = {"model": model, "messages": messages, "temperature": 0.7}
    if extra:
        payload.update(extra)
    try:
        async with session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        ) as resp:
            if resp.status != 200:
                return f"[HTTP {resp.status}] {(await resp.text())[:200]}"
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        return f"[调用失败] {e}"


async def main() -> None:
    only = set(int(x) for x in sys.argv[1:]) if len(sys.argv) > 1 else None

    config = Config.load()
    with open("data/test_keys.toml", "rb") as f:
        keys = tomllib.load(f)

    persona = Persona.load(config.character.path)
    db = Database("data/server-companion.db")  # 服务器导出库，只读使用
    await db.connect()

    candidates = [
        # (名字, url, key, model, 额外payload)
        ("DS-v4pro(对照)", "https://api.deepseek.com/chat/completions",
         config.llm.api_key, "deepseek-v4-pro",
         {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
        ("DS-flash-low", "https://api.deepseek.com/chat/completions",
         config.llm.api_key, "deepseek-flash",
         {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}),
        ("MiniMax-M2.7", "https://api.minimaxi.com/v1/chat/completions",
         keys["minimax"], "MiniMax-M2.7", {"reasoning_split": True}),
        ("MiniMax-M3", "https://api.minimaxi.com/v1/chat/completions",
         keys["minimax"], "MiniMax-M3", {"reasoning_split": True}),
        ("Seed2.1-Turbo", "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
         keys["seed"], "doubao-seed-2-1-turbo-260628",
         {"thinking": {"type": "enabled"}}),
    ]

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for idx, (label, probe) in enumerate(PROBES):
            if only is not None and idx not in only:
                continue
            print("=" * 70)
            print(f"【探针 {idx}: {label}】机主: {probe}")
            print("=" * 70)
            messages = await build_probe_messages(db, persona, probe)
            results = await asyncio.gather(*[
                call_model(session, name, url, key, model, messages, extra)
                for name, url, key, model, extra in candidates
            ])
            for (name, *_), reply in zip(candidates, results):
                print(f"\n── {name} ──")
                print(reply)

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
