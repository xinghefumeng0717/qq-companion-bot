"""真实 API 根因对照：同一历史、同一批候选，比较原始/择优/完整后处理。

只使用脱敏 turns 和临时 SQLite；不读生产库、不装配 TTS、不发 QQ。
每个场景只生成一批候选，三组处理共享它，避免把采样噪声误判成机制效果。
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime
from typing import Any

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from companion.affection import AffectionEngine  # noqa: E402
from companion.assembler import PromptAssembler  # noqa: E402
from companion.config import Config  # noqa: E402
from companion.db import Database  # noqa: E402
from companion.gateway import LLMGateway  # noqa: E402
from companion.memory import MemoryManager  # noqa: E402
from companion.mood import MoodEngine  # noqa: E402
from companion.persona import Persona  # noqa: E402
from companion.replier import (  # noqa: E402
    Replier,
    chunk_record_text,
    has_voice_intent,
    has_voice_promise,
    sanitize_voice_claims,
)
from companion.reply_check import (  # noqa: E402
    check_continuity,
    check_reply,
    gather_candidates,
    pick_candidate,
    trim_echo_lead,
)
from companion.stickers import StickerManager  # noqa: E402
from companion.runtime_state import RuntimeEventStore  # noqa: E402
from companion.voice_planner import VoicePlanner, VOICE_REQUEST_RE as _VOICE_REQUEST_RE  # noqa: E402
from companion.prompts import VOICE_FAILURE_NOTE  # noqa: E402
from companion.replier import fit_chunks, record_text_from_chunks, quote_chunk  # noqa: E402

EVIDENCE = os.path.join(ROOT, "docs", "evidence", "20261025_1115", "turns.redacted.json")
SCENARIOS = [
    {"name": "614_否认刚说过的话", "user_id": 613, "voice_allowed": False, "previous_sent": True},
    {"name": "616_自我打断与跳跃", "user_id": 615, "voice_allowed": False, "previous_sent": True},
    {"name": "618_普通对话对照", "user_id": 617, "voice_allowed": False, "previous_sent": True},
    {"name": "618_高频语音放行", "user_id": 617, "voice_allowed": True, "previous_sent": True},
    {"name": "624_过度解读顺路", "user_id": 623, "voice_allowed": False, "previous_sent": True},
    {"name": "630_坏语音标记", "user_id": 629, "voice_allowed": True, "previous_sent": True},
    {"name": "632_引用后的接续", "user_id": 631, "voice_allowed": False, "previous_sent": False, "previous_failed": True},
    {"name": "646_把好看成收到", "user_id": 645, "voice_allowed": False, "previous_sent": False, "previous_failed": True},
    {"name": "648_计划与重复跟进", "user_id": 647, "voice_allowed": False, "previous_sent": False, "previous_failed": True},
]
TIME_MODULES = (
    "companion.db", "companion.assembler", "companion.memory",
    "companion.mood", "companion.affection", "companion.config",
)
_NOW = datetime(2026, 10, 24, 12, 0)
_ORIGINAL_DATETIME: dict[str, Any] = {}


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _NOW if tz is None else _NOW.replace(tzinfo=tz)

    @classmethod
    def today(cls):
        return _NOW


def patch_time() -> None:
    for name in TIME_MODULES:
        module = importlib.import_module(name)
        _ORIGINAL_DATETIME.setdefault(name, getattr(module, "datetime"))
        setattr(module, "datetime", FakeDatetime)


def restore_time() -> None:
    for name, original in _ORIGINAL_DATETIME.items():
        setattr(importlib.import_module(name), "datetime", original)


def identities(persona: Persona) -> dict[str, str]:
    out: dict[str, str] = {}
    world = getattr(persona, "world", None)
    user = getattr(persona, "user", None)
    if isinstance(getattr(world, "her_identity", None), str):
        out["her_identity"] = world.her_identity
    if isinstance(getattr(user, "identity", None), str):
        out["his_identity"] = user.identity
    return out


def parsed_preview(chunks: list[dict[str, Any]]) -> str:
    return "\n".join(chunk_record_text(c) for c in chunks)


def parse_draft(replier: Replier, draft: str, batch: list[dict[str, Any]], voice_allowed: bool, cfg: Config) -> dict[str, Any]:
    chunks, record = replier.parse_reply(
        draft,
        quote_targets=batch,
        voice_allowed=voice_allowed,
        voice_max_chars=getattr(getattr(cfg, "tts", None), "max_chars", 60),
    )
    return {
        "draft": draft,
        "parsed": parsed_preview(chunks),
        "record": record,
        "chunks": chunks,
        "voice_intent_without_voice": bool(
            (has_voice_intent(draft) or has_voice_promise(draft))
            and not any(c.get("type") == "voice" for c in chunks)
        ),
    }


async def run_scenario(cfg: Config, rows: list[dict[str, Any]], scenario: dict[str, Any], tmp_root: str) -> dict[str, Any]:
    global _NOW
    by_id = {int(row["id"]): row for row in rows}
    user = by_id[scenario["user_id"]]
    reference = by_id.get(scenario["user_id"] + 1)
    _NOW = datetime.strptime(user["created_at"], "%Y-%m-%d %H:%M")
    current_text = user["content"]
    lines = [line for line in current_text.splitlines() if line.strip()]
    batch = [
        {"index": i + 1, "text": line, "message_id": 900000 + scenario["user_id"] * 10 + i, "has_image": False}
        for i, line in enumerate(lines)
    ]
    context = [row for row in rows if int(row["id"]) < int(user["id"])][-20:]
    voice_allowed = bool(scenario.get("voice_allowed"))
    previous_failed = bool(scenario.get("previous_failed"))
    previous_sent = bool(scenario.get("previous_sent"))
    delivery_note = VOICE_FAILURE_NOTE if previous_failed else ""
    db_path = os.path.join(tmp_root, f"{scenario['user_id']}.db")
    db = Database(db_path)
    gateway = LLMGateway(cfg.llm, db)
    try:
        await db.init_tables()
        persona = Persona.load(os.path.join(ROOT, "characters", "si"))
        stickers = StickerManager(os.path.join(persona.base_dir, persona.stickers_dir), db)
        await stickers.sync_initial_stickers()
        affection = AffectionEngine.from_persona(db, persona)
        mood = MoodEngine(db)
        memory = MemoryManager(db, gateway, affection, persona)
        memory.self_memory = bool(getattr(cfg.character, "self_memory", False))
        memory.impressions = bool(getattr(cfg.character, "impressions", False))
        memory.fact_layers = bool(getattr(cfg.character, "fact_layers", False))
        assembler = PromptAssembler(
            persona, affection, mood, memory, stickers, db, holidays_provider=lambda: []
        )
        assembler.self_memory = memory.self_memory
        assembler.impressions = memory.impressions
        runtime_enabled = getattr(cfg.reply, "runtime_state_v1", False) is True
        runtime_events = RuntimeEventStore(db)
        if runtime_enabled:
            await runtime_events.init()
            if previous_failed:
                await runtime_events.record_voice("failed", "上一轮语音", now=_NOW)
            elif previous_sent:
                await runtime_events.record_voice("sent", "上一轮语音", now=_NOW)
        assembler.runtime_state_v1 = runtime_enabled
        assembler.runtime_events = runtime_events
        assembler.voice_frequency = cfg.reply.voice_frequency
        runtime_block = await runtime_events.render_prompt() if runtime_enabled else ""
        replier = Replier(cfg.reply, stickers)
        for row in context:
            await db.execute(
                "INSERT INTO turns (id, role, content, proactive, has_image, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (int(row["id"]), row["role"], row["content"], int(row.get("proactive", 0)),
                 int(row.get("has_image", 0)), row["created_at"]),
            )
        tts_enabled = bool(getattr(getattr(cfg, "tts", None), "enabled", False))
        messages, system_prompt = await assembler.assemble_messages(
            current_text,
            numbered_batch=batch,
            voice_allowed=voice_allowed,
            voice_gated_note=tts_enabled,
            voice_delivery_note=delivery_note,
            now=_NOW,
            runtime_state_block=runtime_block,
        )

        async def generate_one() -> str:
            parts: list[str] = []
            async for piece in gateway.stream_chat(
                messages=messages, model=cfg.llm.active().chat, purpose="main_chat"
            ):
                parts.append(piece)
            return "".join(parts).strip()

        candidates = await gather_candidates(generate_one, 3)
        picked, candidate_info = pick_candidate(
            current_text, candidates, tiebreak=cfg.reply.candidate_tiebreak
        )
        picked_index = int(candidate_info["picked"])

        raw_arm = parse_draft(replier, candidates[0], batch, voice_allowed, cfg)
        selected_arm = parse_draft(replier, picked, batch, voice_allowed, cfg)

        full_draft = picked
        continuity_info: dict[str, Any] | None = None
        if getattr(cfg.reply, "continuity_check", False):
            history = await memory.get_recent_turns(limit=3)
            full_draft, continuity_info = await check_continuity(
                gateway,
                current_text,
                candidates,
                picked_index,
                history=history,
                timeout=getattr(cfg.reply, "continuity_timeout", 8.0),
                tiebreak=cfg.reply.candidate_tiebreak,
                **identities(persona),
            )
        trimmed = ""
        if getattr(cfg.reply, "trim_echo", False):
            full_draft, trimmed = trim_echo_lead(current_text, full_draft)
        self_check_info: dict[str, Any] | None = None
        if getattr(cfg.reply, "self_check", False):
            full_draft, self_check_info = await check_reply(
                gateway, current_text, full_draft, cfg.reply.self_check_timeout,
                **identities(persona),
            )
        voice_intent = has_voice_intent(full_draft)
        sanitized = sanitize_voice_claims(
            full_draft,
            voice_allowed=voice_allowed and voice_intent,
            previous_voice_sent=previous_sent,
            voice_context=(
                previous_failed or bool(VOICE_REQUEST_RE.search(current_text)) or voice_intent
            ),
        )
        full_arm = parse_draft(replier, sanitized, batch, voice_allowed, cfg)
        runtime_arm = None
        if runtime_enabled:
            planner = VoicePlanner(cfg.reply.voice_frequency, max_chars=getattr(cfg.tts, "max_chars", 90))
            runtime_plan = planner.plan(
                current_text, sanitized,
                voice_allowed=voice_allowed and planner.allows(current_text),
            )
            runtime_arm = parse_draft(
                replier,
                runtime_plan.body_text if runtime_plan.has_voice else sanitized,
                batch, False, cfg,
            )
            if runtime_plan.has_voice:
                runtime_arm["chunks"].append({
                    "type": "voice", "content": runtime_plan.voice_text,
                    "_runtime_voice": True,
                })
                runtime_arm["chunks"] = fit_chunks(runtime_arm["chunks"], cfg.reply.max_chunks)
                runtime_arm["parsed"] = "\n".join(chunk_record_text(c) for c in runtime_arm["chunks"])
                runtime_arm["record"] = record_text_from_chunks(runtime_arm["chunks"])
                if runtime_plan.quote_index is not None and runtime_arm["chunks"]:
                    quote = quote_chunk(runtime_plan.quote_index, batch)
                    if quote is not None:
                        runtime_arm["chunks"][0]["_quote"] = quote
            runtime_arm["voice_plan"] = {
                "mode": runtime_plan.mode, "voice_text": runtime_plan.voice_text,
                "reason": runtime_plan.reason, "quote_index": runtime_plan.quote_index,
            }
        cost_row = await db.fetchone("SELECT SUM(cost_estimate) AS cost FROM llm_calls")
        return {
            "name": scenario["name"],
            "user_id": scenario["user_id"],
            "current_user": current_text,
            "reference_assistant": reference["content"] if reference else "",
            "created_at": user["created_at"],
            "voice_allowed": voice_allowed,
            "previous_failed": previous_failed,
            "previous_sent": previous_sent,
            "context": context,
            "system_prompt": system_prompt,
            "messages": messages,
            "candidates": candidates,
            "candidate_info": candidate_info,
            "arms": {
                "raw_single": raw_arm,
                "selected_no_post": selected_arm,
                "full_pipeline": full_arm,
                **({"runtime_state_v1": runtime_arm} if runtime_arm is not None else {}),
            },
            "continuity": continuity_info,
            "trimmed": trimmed,
            "self_check": self_check_info,
            "sanitized_changed": sanitized != full_draft,
            "voice_recovery": "未运行：本轮只测主聊生成与后处理，不测 TTS/语音补救",
            "cost": float(cost_row["cost"] or 0.0) if cost_row else 0.0,
        }
    finally:
        await gateway.close()
        await db.close()


def report_text(meta: dict[str, Any], cells: list[dict[str, Any]]) -> str:
    lines = [
        "# 主聊根因真实 API 对照",
        "",
        f"- 模型：{meta['model']}，思考 {meta['effort']}；场景 {len(cells)} 个。",
        "- 每个场景只生成一批最多三条候选，三组处理共享同一批候选。",
        "- raw_single：第一条原始候选，只经过解析；selected_no_post：原有择优，只经过解析；full_pipeline：连贯性闸门→首泡裁剪→语音断言清理→解析。",
        "- 未运行 TTS、语音补救、观察者或生产发送；沙箱使用脱敏历史与角色卡初始状态，不是完整生产状态复刻。",
        "- 参考回复是事故当时的 assistant 记录，仅作对照，不作为自动评分标准。",
        "",
    ]
    for cell in cells:
        lines += [
            f"## {cell['name']}（用户 turn {cell['user_id']}，{cell['created_at']}）",
            f"他：{cell['current_user'].replace(chr(10), ' / ')}",
            f"当时她：{cell['reference_assistant'].replace(chr(10), ' / ')}",
            "",
            "### 原始候选",
        ]
        for i, candidate in enumerate(cell["candidates"], 1):
            lines.append(f"{i}. {candidate.replace(chr(10), ' / ')}")
        lines += [
            f"候选信息：{json.dumps(cell['candidate_info'], ensure_ascii=False)}",
            "",
        ]
        arms = [("raw_single", "原始第一条"), ("selected_no_post", "原择优不后处理"), ("full_pipeline", "当前完整后处理")]
        if "runtime_state_v1" in cell["arms"]:
            arms.append(("runtime_state_v1", "运行时事实与高频语音实验"))
        for arm, label in arms:
            data = cell["arms"][arm]
            lines += [
                f"### {label}",
                f"草稿：{data['draft'].replace(chr(10), ' / ')}",
                f"解析后：{data['parsed'].replace(chr(10), ' / ')}",
                f"语音意图未形成 voice 段：{data['voice_intent_without_voice']}",
                *([f"语音计划：{json.dumps(data.get('voice_plan'), ensure_ascii=False)}"] if data.get("voice_plan") else []),
                "",
            ]
        lines += [
            f"连贯性：{json.dumps(cell.get('continuity'), ensure_ascii=False)}",
            f"首泡裁剪：{cell.get('trimmed') or '无'}",
            f"语音断言清理有改动：{cell.get('sanitized_changed')}",
            "",
        ]
    return "\n".join(lines)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--only", default="", help="只跑名称包含这些逗号分隔片段的场景")
    parser.add_argument("--runtime", action="store_true", help="增加 runtime_state_v1 实验臂")
    args = parser.parse_args()
    os.chdir(ROOT)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    with open(EVIDENCE, encoding="utf-8") as f:
        rows = json.load(f)
    selected = [x for x in args.only.split(",") if x]
    scenarios = [s for s in SCENARIOS if not selected or any(x in s["name"] for x in selected)]
    cfg = Config.load()
    cfg.reply.runtime_state_v1 = args.runtime
    cfg.reply.candidates = 3
    cfg.reply.continuity_check = True
    meta = {
        "model": cfg.llm.active().chat,
        "effort": cfg.llm.thinking_effort_chat,
        "candidate_tiebreak": cfg.reply.candidate_tiebreak,
        "trim_echo": cfg.reply.trim_echo,
        "self_check": cfg.reply.self_check,
        "continuity_check": True,
        "runtime_state_v1": args.runtime,
        "voice_recovery": False,
        "state": "脱敏历史 + 祀卡初始状态；无生产库/生产记忆",
        "cost": 0.0,
    }
    cells: list[dict[str, Any]] = []

    def flush() -> None:
        meta["cost"] = round(sum(c.get("cost", 0.0) for c in cells), 6)
        with open(os.path.join(out, "raw.json"), "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "cells": cells}, f, ensure_ascii=False, indent=2)
        with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
            f.write(report_text(meta, cells))

    patch_time()
    tmp_root = tempfile.mkdtemp(prefix="root_cause_probe_")
    try:
        for scenario in scenarios:
            print(f"RUN {scenario['name']}", flush=True)
            try:
                cell = await run_scenario(cfg, rows, scenario, tmp_root)
            except Exception as exc:
                cell = {"name": scenario["name"], "user_id": scenario["user_id"], "error": f"{type(exc).__name__}: {exc}"}
                print(f"ERROR {cell['error']}", flush=True)
            cells.append(cell)
            flush()
            if "cost" in cell:
                print(f"DONE {scenario['name']} candidates={len(cell['candidates'])} cost={cell['cost']:.6f}", flush=True)
    finally:
        restore_time()
        shutil.rmtree(tmp_root, ignore_errors=True)
        flush()
    print(f"COMPLETE cells={len(cells)} cost={meta['cost']:.6f} out={out}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
