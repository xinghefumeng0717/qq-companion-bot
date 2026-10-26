"""一次性探针：把主聊的真实提示词发给 DeepSeek，保留 reasoning_content。

目的：看「祀」的思维链是不是完全沉浸在她的世界里（终端、管理员、客栈），
还是会露出"作为 AI 助手""根据设定"这类出戏内心戏。

做法：
1. 用 companion 的生产装配链（Persona / Affection / Mood / Memory / Stickers /
   LifeArcManager / PromptAssembler）在**全新临时 SQLite 库**里组装 messages，
   不读取、不复制任何生产数据。新库六维初值 80 → 复合分 80 → 阶段 5「伙伴」。
2. payload 按 companion/gateway.py 的主聊路径构造：同一套 apply_provider_params，
   模型 deepseek-flash、thinking={"type":"enabled"}、reasoning_effort=low
   （开启思考时 temperature 会被移除，与生产一致）。
3. 直接 HTTP 调 /chat/completions，同时留下 reasoning_content 与 content。

API key 只从环境变量 DEEPSEEK_API_KEY 读取，不打印、不落盘。

用法（在项目根目录）：
    python scripts/util/probe_reasoning.py
    python scripts/util/probe_reasoning.py --message "在吗" --message "今天好困"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_MESSAGES = [
    "在吗",
    "今天累死了 被人误会了",
    "你怎么十点多了还不睡",
]


def read_base_url(config_path: Path) -> str:
    """只从 config.toml 读取 deepseek 预设的 base_url（api_key 一律走环境变量）。"""
    import tomllib

    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    presets = data.get("models", {})
    preferred = data.get("llm", {}).get("current", "deepseek")
    preset = presets.get(preferred) or presets.get("deepseek") or {}
    base_url = str(preset.get("base_url", "")).strip()
    if not base_url:
        raise SystemExit(f"config.toml 里没有可用的 base_url: {config_path}")
    return base_url


async def build_assembler(config: Any):
    """按 main.py 的方式搭一套沙箱组件，返回 (assembler, db, gateway, persona, stage)。"""
    from companion.affection import AffectionEngine
    from companion.arcs import LifeArcManager
    from companion.assembler import PromptAssembler
    from companion.db import Database
    from companion.gateway import LLMGateway
    from companion.meetups import MeetupManager
    from companion.memory import MemoryManager
    from companion.mood import MoodEngine
    from companion.persona import Persona
    from companion.stickers import StickerManager

    tmp_dir = Path(tempfile.mkdtemp(prefix="probe_reasoning_"))
    db_path = tmp_dir / "sandbox.db"

    db = Database(str(db_path))
    await db.init_tables()

    persona = Persona.load(config.character.path, config.character.user_profile)
    stickers_dir = os.path.join(persona.base_dir, persona.stickers_dir)
    stickers = StickerManager(stickers_dir, db)
    await stickers.sync_initial_stickers()

    affection = AffectionEngine.from_persona(db, persona)
    mood = MoodEngine(db)
    gateway = LLMGateway(config.llm, db)
    memory = MemoryManager(db, gateway, affection, persona)
    memory.fact_layers = getattr(config.character, "fact_layers", False) is True

    holidays = (
        config.get_holidays if persona.world.follow_public_holidays else (lambda: [])
    )
    arcs = LifeArcManager(db, gateway, persona)
    assembler = PromptAssembler(
        persona,
        affection,
        mood,
        memory,
        stickers,
        db,
        holidays_provider=holidays,
        arcs=arcs,
    )

    if getattr(config.reply, "style_examples", False) is True:
        from companion.style_examples import StyleExampleBank

        bank_path = config.reply.style_examples_path
        if not os.path.isabs(bank_path):
            bank_path = os.path.join(str(ROOT), bank_path)
        assembler.style_examples = StyleExampleBank.load(bank_path)
    if getattr(config.character, "self_memory", False) is True:
        assembler.self_memory = True
        memory.self_memory = True
    if getattr(config.character, "impressions", False) is True:
        assembler.impressions = True
        memory.impressions = True
    if getattr(config.character, "meetup_recap", False) is True:
        assembler.meetups = MeetupManager(db, gateway, persona, memory)

    aff_state = await affection.get_state()
    stage = int(aff_state.get("stage", 0))
    stage_name = persona.get_stage(stage).name
    return assembler, db, gateway, persona, (stage, stage_name, float(aff_state.get("composite", 0)))


def build_payload(model: str, effort: str, base_url: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """与 gateway.stream_chat 的主聊 payload 对齐（非流式，仅去掉 stream 字段）。"""
    from companion.gateway import apply_provider_params

    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0.7,
    }
    apply_provider_params(
        payload,
        provider="deepseek",
        enabled=True,
        effort=effort,
        base_url=base_url,
    )
    return payload


async def call_api(
    api_key: str,
    base_url: str,
    payload: Dict[str, Any],
    max_retries: int = 2,
) -> Dict[str, Any]:
    import aiohttp

    url = f"{base_url.rstrip('/')}/chat/completions"
    timeout = aiohttp.ClientTimeout(total=180)
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    last_err: Exception | None = None
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        for attempt in range(max_retries + 1):
            try:
                started = time.monotonic()
                async with session.post(url, json=payload) as resp:
                    raw = await resp.text()
                    if resp.status != 200:
                        raise RuntimeError(f"HTTP {resp.status}: {raw[:500]}")
                    data = json.loads(raw)
                elapsed = time.monotonic() - started
                message = data["choices"][0].get("message", {}) or {}
                return {
                    "reasoning_content": message.get("reasoning_content")
                    or message.get("reasoning")
                    or "",
                    "content": message.get("content") or "",
                    "finish_reason": data["choices"][0].get("finish_reason"),
                    "usage": data.get("usage", {}),
                    "elapsed_sec": round(elapsed, 2),
                    "model_returned": data.get("model", ""),
                }
            except Exception as e:  # noqa: BLE001 - 探针需要看到原始错误
                last_err = e
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"调用失败: {last_err}")


def estimate_cost(gateway: Any, model: str, usage: Dict[str, Any]) -> float:
    p = int(usage.get("prompt_tokens", 0) or 0)
    c = int(usage.get("completion_tokens", 0) or 0)
    hit = int(usage.get("prompt_cache_hit_tokens", 0) or 0)
    miss = int(usage.get("prompt_cache_miss_tokens", 0) or 0)
    try:
        return gateway._estimate_cost(p, c, hit, miss, model=model)
    except Exception:
        return 0.0


async def run(args: argparse.Namespace) -> int:
    sys.stdout.reconfigure(encoding="utf-8")

    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("缺少环境变量 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2

    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))
    from companion.config import Config

    config_path = Path(args.config).resolve()
    config = Config.load(str(config_path))
    base_url = args.base_url or read_base_url(config_path)

    messages_to_probe = args.message or DEFAULT_MESSAGES
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(args.out_dir).resolve() / f"probe_{stamp}"
    out_dir.mkdir(parents=True, exist_ok=True)

    records: List[Dict[str, Any]] = []
    print(f"角色: {config.character.path} | 模型: {args.model} | effort: {args.effort}")
    print(f"base_url: {base_url} | 输出目录: {out_dir}")

    total_cost = 0.0
    for idx, user_text in enumerate(messages_to_probe, 1):
        # 每条消息一个全新临时库：独立开局，互不污染
        assembler, db, gateway, persona, (stage, stage_name, composite) = await build_assembler(config)
        try:
            messages, system_prompt = await assembler.assemble_messages(user_text)
            payload = build_payload(args.model, args.effort, base_url, messages)
            print(f"\n{'=' * 72}")
            print(f"[{idx}/{len(messages_to_probe)}] 他：{user_text}")
            print(
                f"阶段: {stage} {stage_name}（复合分 {composite:.2f}） | "
                f"messages: {len(messages)} 条 | system prompt: {len(system_prompt)} 字"
            )
            result = await call_api(api_key, base_url, payload)
            cost = estimate_cost(gateway, args.model, result["usage"])
            total_cost += cost
            usage = result["usage"]
            print(
                f"tokens: prompt={usage.get('prompt_tokens', 0)} "
                f"completion={usage.get('completion_tokens', 0)} "
                f"(reasoning≈{usage.get('completion_tokens_details', {}).get('reasoning_tokens', 'n/a')}) "
                f"| 耗时 {result['elapsed_sec']}s | 估算 ¥{cost:.4f}"
            )
            print("\n--- reasoning_content ---")
            print(result["reasoning_content"] or "（空）")
            print("\n--- content ---")
            print(result["content"] or "（空）")

            records.append(
                {
                    "index": idx,
                    "user_message": user_text,
                    "stage": stage,
                    "stage_name": stage_name,
                    "composite": composite,
                    "model_requested": args.model,
                    "model_returned": result["model_returned"],
                    "reasoning_content": result["reasoning_content"],
                    "content": result["content"],
                    "finish_reason": result["finish_reason"],
                    "usage": usage,
                    "elapsed_sec": result["elapsed_sec"],
                    "cost_estimate_cny": cost,
                    "payload": {k: v for k, v in payload.items() if k != "messages"},
                    "messages": messages,
                    "system_prompt": system_prompt,
                }
            )

            single = out_dir / f"{idx:02d}_{user_text[:12].replace('/', '_')}.json"
            single.write_text(
                json.dumps(records[-1], ensure_ascii=False, indent=2), encoding="utf-8"
            )
        finally:
            await db.close()
            tmp_dir = Path(db.db_path).parent
            shutil.rmtree(tmp_dir, ignore_errors=True)

    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "character_path": config.character.path,
        "model": args.model,
        "effort": args.effort,
        "base_url": base_url,
        "total_cost_estimate_cny": round(total_cost, 6),
        "records": records,
    }
    summary_path = out_dir / "summary.json"
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    transcript = out_dir / "transcript.md"
    with open(transcript, "w", encoding="utf-8") as f:
        f.write(f"# 思维链探针 · {summary['created_at']}\n\n")
        f.write(f"- 角色：`{config.character.path}`\n- 模型：`{args.model}` / effort `{args.effort}`\n")
        f.write(f"- 估算总费用：¥{total_cost:.4f}\n\n")
        for rec in records:
            f.write(f"## {rec['index']}. 他：{rec['user_message']}\n\n")
            f.write(f"阶段：{rec['stage']} {rec['stage_name']}（{rec['composite']:.2f}）\n\n")
            f.write("### reasoning_content\n\n```\n")
            f.write(rec["reasoning_content"] or "（空）")
            f.write("\n```\n\n### content\n\n```\n")
            f.write(rec["content"] or "（空）")
            f.write("\n```\n\n")
    print(f"\n{'=' * 72}")
    print(f"总估算费用: ¥{total_cost:.4f}")
    print(f"汇总: {summary_path}")
    print(f"可读版: {transcript}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="主聊思维链沉浸度探针（一次性）")
    parser.add_argument(
        "--message",
        action="append",
        default=None,
        help="要探测的消息，可重复；不给则用内置三条",
    )
    parser.add_argument("--config", default=str(ROOT / "config.toml"), help="config.toml 路径")
    parser.add_argument("--model", default="deepseek-flash", help="模型名（默认主聊 flash）")
    parser.add_argument("--effort", default="low", help="reasoning_effort（生产为 low）")
    parser.add_argument("--base-url", default="", help="覆盖 config.toml 里的 base_url")
    parser.add_argument(
        "--out-dir",
        default=str(ROOT / "data" / "probe_reasoning"),
        help="输出目录",
    )
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
