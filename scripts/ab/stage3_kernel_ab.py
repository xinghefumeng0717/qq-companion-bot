"""第三阶段：旧链路与新内核的真实模型离线 A/B 沙箱。

默认只组装提示词并估算费用；只有 --execute --confirm-real-api 才调用模型。
每题每边使用同一本地数据库副本的独立沙箱，不发送、不结算、不连接生产库。
"""
from __future__ import annotations

import argparse
import asyncio
import copy
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys
from contextlib import closing, contextmanager
from dataclasses import replace
from datetime import datetime
from typing import Any, Mapping, Optional, Sequence

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from companion.affection import AffectionEngine
from companion.arcs import LifeArcManager
from companion.assembler import PromptAssembler
from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.kernel.ab import (
    ABCase,
    HistoryTurn,
    SideOutcome,
    estimate_tokens,
    outcomes_from_json,
    outcomes_to_json,
    render_blind_markdown,
)
from companion.kernel.context import ContextBuilder
from companion.kernel.contracts import IncomingMessage
from companion.kernel.kernel import DialogueKernel
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.kernel.semantic import build_turn_input
from companion.kernel.tracing import JsonlTraceRecorder
from companion.memory import MemoryManager
from companion.meetups import MeetupManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.replier import Replier, record_text_from_chunks
from companion.stickers import StickerManager
from companion.style_examples import StyleExampleBank

MODEL = "deepseek-flash"
TIME_MODULES = (
    "companion.db", "companion.assembler", "companion.memory",
    "companion.mood", "companion.affection",
)
_QUOTED_RE = re.compile(r"^（他引用了你之前说的「(.*?)」）\s*(.*)$", re.DOTALL)
_FACE_RE = re.compile(r"\[(?!(?:face|voice|quote|label|sticker|表情)[:：])([^\[\]\n]{1,40})\]", re.IGNORECASE)
_NESTED_FACE_RE = re.compile(r"^\[+(?!(?:face|voice|quote|label|sticker|表情)[:：])([^\[\]]+)\]+$", re.IGNORECASE)


def _parse_time(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%d %H:%M")


def _readonly_connection(path: Path) -> sqlite3.Connection:
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_cases(path: Path, only: Optional[set[str]] = None) -> tuple[list[ABCase], dict[str, Path]]:
    definitions = json.loads(path.read_text(encoding="utf-8"))["cases"]
    cases: list[ABCase] = []
    sources: dict[str, Path] = {}
    for item in definitions:
        case_id = str(item["case_id"])
        if only and case_id not in only:
            continue
        source_db = (ROOT / item["source_db"]).resolve()
        if not source_db.is_file():
            raise FileNotFoundError(f"本地输入副本不存在：{source_db}")
        sources[case_id] = source_db
        with closing(_readonly_connection(source_db)) as connection:
            if item.get("source_turn_id") is not None:
                turn_id = int(item["source_turn_id"])
                target = connection.execute(
                    "select id, content, created_at from turns where id=? and role='user'",
                    (turn_id,),
                ).fetchone()
                if target is None:
                    raise ValueError(f"{case_id} 找不到真实用户 turn {turn_id}")
                user_text = str(target["content"])
                received_at = _parse_time(str(target["created_at"]))
                rows = connection.execute(
                    "select role, content, created_at from turns where id<? "
                    "order by id desc limit 10", (turn_id,),
                ).fetchall()
                history_rows = list(reversed(rows))
            else:
                user_text = str(item["user_text"])
                received_at = _parse_time(item["received_at"])
                if "history" in item:
                    history_rows = item["history"]
                else:
                    history_rows = []
        history = tuple(
            HistoryTurn(str(row["role"]), str(row["content"]), str(row["created_at"]))
            for row in history_rows
        )
        cases.append(ABCase(
            case_id=case_id,
            title=str(item["title"]),
            user_text=user_text,
            received_at=received_at,
            history=history,
            source=str(item.get("source", "")),
            constructed=bool(item.get("constructed", False)),
            source_turn_id=item.get("source_turn_id"),
        ))
    if only:
        missing = only - {case.case_id for case in cases}
        if missing:
            raise ValueError(f"未知题号：{sorted(missing)}")
    if not cases:
        raise ValueError("题集为空")
    return cases, sources


def incoming_for_case(case: ABCase):
    """把数据库中的显示文本还原为通用语义输入，不伪造 QQ message_id。"""
    match = _QUOTED_RE.match(case.user_text)
    plain = match.group(2) if match else case.user_text
    quoted_text = match.group(1) if match else ""
    nested = _NESTED_FACE_RE.fullmatch(plain.strip())
    if nested:
        messages = [IncomingMessage(message_id=None, text=nested.group(1), kind="face")]
    else:
        messages = []
        cursor = 0
        for found in _FACE_RE.finditer(plain):
            before = plain[cursor:found.start()].strip()
            if before:
                messages.append(IncomingMessage(message_id=None, text=before))
            messages.append(IncomingMessage(
                message_id=None, text=found.group(1), kind="face",
            ))
            cursor = found.end()
        tail = plain[cursor:].strip()
        if tail:
            messages.append(IncomingMessage(message_id=None, text=tail))
        if not messages:
            messages = [IncomingMessage(message_id=None, text=plain)]
    turn = build_turn_input(
        session_id="stage3-" + case.case_id,
        messages=messages,
        received_at=case.received_at,
    )
    if quoted_text:
        turn = replace(turn, quoted_text=quoted_text)
    return turn


@contextmanager
def frozen_time(at: datetime):
    originals: dict[str, Any] = {}
    class FakeDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.replace(tzinfo=tz)
        @classmethod
        def utcnow(cls):
            return at
        @classmethod
        def today(cls):
            return at
    try:
        for name in TIME_MODULES:
            module = importlib.import_module(name)
            originals[name] = module.datetime
            module.datetime = FakeDatetime
        yield
    finally:
        for name, original in originals.items():
            importlib.import_module(name).datetime = original


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class SideSandbox:
    def __init__(
        self,
        case: ABCase,
        side: str,
        source_db: Path,
        run_dir: Path,
        *,
        style_examples_enabled: Optional[bool] = None,
    ):
        self.case = case
        self.side = side
        self.source_db = source_db
        self.style_examples_enabled = style_examples_enabled
        self.db_path = run_dir / "sandboxes" / f"{case.case_id}-{side}.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_db, self.db_path)
        with closing(sqlite3.connect(self.db_path)) as connection:
            with connection:
                connection.execute("delete from turns")
                connection.executemany(
                    "insert into turns(role, content, proactive, has_image, created_at) "
                    "values(?, ?, 0, 0, ?)",
                    [(h.role, h.content, h.created_at) for h in case.history],
                )
        config_path = ROOT / "config.toml"
        if not config_path.is_file():
            raise FileNotFoundError(f"真实沙箱配置不存在：{config_path}")
        self.config = copy.deepcopy(Config.load(str(config_path)))
        character_path = Path(self.config.character.path)
        if not character_path.is_absolute():
            character_path = ROOT / character_path
        if not character_path.is_dir():
            raise FileNotFoundError(f"角色目录不存在：{character_path}")
        self.config.character.path = str(character_path)
        if self.config.character.user_profile:
            profile_path = Path(self.config.character.user_profile)
            if not profile_path.is_absolute():
                profile_path = ROOT / profile_path
            if not profile_path.is_file():
                raise FileNotFoundError(f"用户档案不存在：{profile_path}")
            self.config.character.user_profile = str(profile_path)
        self.config.llm.thinking_chat = True
        self.config.llm.thinking_effort_chat = "low"
        self.tracer = JsonlTraceRecorder(run_dir / "traces")

    async def initialize(self) -> None:
        self.db = Database(str(self.db_path))
        await self.db.init_tables()
        self.persona = Persona.load(
            self.config.character.path, self.config.character.user_profile,
        )
        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)
        await self.stickers.sync_initial_stickers()
        self.affection = AffectionEngine.from_persona(self.db, self.persona)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(self.config.llm, self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)
        self.memory.self_memory = self.config.character.self_memory
        self.memory.impressions = self.config.character.impressions
        self.memory.fact_layers = self.config.character.fact_layers
        self.arcs = LifeArcManager(self.db, self.gateway, self.persona)
        self.meetups = (
            MeetupManager(self.db, self.gateway, self.persona, self.memory)
            if self.config.character.meetup_recap else None
        )
        # 旧 assembler 链路本阶段不重写：它自己的外部闸门原样保留。
        self.assembler = PromptAssembler(
            self.persona, self.affection, self.mood, self.memory, self.stickers, self.db,
            holidays_provider=(
                self.config.get_holidays
                if self.persona.world.follow_public_holidays else (lambda: [])
            ),
        )
        # A/B 必须读取同一冻结情绪，旧组装器不得推进随机噪声。
        self.assembler.runtime_state_v1 = True
        self.assembler.arcs = self.arcs
        self.assembler.self_memory = self.config.character.self_memory
        self.assembler.impressions = self.config.character.impressions
        self.assembler.meetups = self.meetups
        self.replier = Replier(self.config.reply, self.stickers)
        # 节假日：直接把只读取值函数交给快照构建器。
        # follow_public_holidays=False 的闸门由 PsychologicalSnapshotBuilder 内部独家负责，
        # 这里不再重复判断（否则闸门会散落在两处，将来必然走偏）。
        self.snapshot_builder = PsychologicalSnapshotBuilder(
            affection_engine=self.affection, mood_engine=self.mood, persona=self.persona,
            holidays_provider=self.config.get_holidays,
        )
        self.context_builder = ContextBuilder()
        # 动态示例只在装配层读取配置与加载文件；内核只接收已加载的只读 bank。
        style_examples = None
        style_enabled = (
            getattr(self.config.reply, "style_examples", False)
            if self.style_examples_enabled is None
            else self.style_examples_enabled
        )
        if style_enabled:
            bank_path = Path(self.config.reply.style_examples_path)
            if not bank_path.is_absolute():
                bank_path = ROOT / bank_path
            style_examples = StyleExampleBank.load(str(bank_path))
        # 旧侧与生产 main 一样挂 bank；新侧由公共准备路径做确定性选择。
        self.assembler.style_examples = style_examples
        self.kernel = DialogueKernel(
            gateway=self.gateway,
            persona=self.persona,
            snapshot_builder=self.snapshot_builder,
            context_builder=self.context_builder,
            tracer=self.tracer,
            memory=self.memory,
            arcs=self.arcs,
            self_memory=self.config.character.self_memory,
            impressions=self.config.character.impressions,
            meetups=self.meetups,
            style_examples=style_examples,
            model=MODEL,
            code_version="kernel-stage3-ab",
            max_retries=0,
        )

    async def prepare(self) -> dict[str, Any]:
        turn_input = incoming_for_case(self.case)
        if self.side == "old":
            user_text = turn_input.text
            if turn_input.quoted_text:
                user_text = f"他引用之前的“{turn_input.quoted_text}”时说：\n{user_text}"
            with frozen_time(self.case.received_at):
                messages, _ = await self.assembler.assemble_messages(
                    user_text, now=self.case.received_at,
                )
        else:
            # 新侧与实际 handle_turn 共用同一公共准备路径；生成时复用该结果。
            with frozen_time(self.case.received_at):
                prepared_turn = await self.kernel.prepare_turn(turn_input)
            messages = list(prepared_turn.messages)
        prompt_tokens = estimate_tokens(messages)
        estimated_cost = self.gateway._estimate_cost(
            prompt_tokens, 4096, cache_miss_tokens=prompt_tokens,
            at=self.case.received_at, model=MODEL,
        )
        payload = {
            "side": self.side,
            "messages": messages,
            "turn_input": turn_input,
            "prompt_tokens": prompt_tokens,
            "estimated_cost": estimated_cost,
        }
        if self.side == "new":
            payload["prepared_turn"] = prepared_turn
        return payload

    async def generate(self, prepared: Mapping[str, Any]) -> SideOutcome:
        before = await self.db.fetchone("select coalesce(max(id), 0) as max_id from llm_calls")
        first_new_id = int(before["max_id"]) + 1
        if self.side == "old":
            trace_id = self.tracer.new_trace()
            self.tracer.record("turn_input", {
                "case_id": self.case.case_id, "text": self.case.user_text,
            }, trace_id=trace_id)
            self.tracer.record("model_context", {
                "messages": prepared["messages"],
            }, trace_id=trace_id)
            parts: list[str] = []
            async for piece in self.gateway.stream_chat(
                messages=prepared["messages"], model=MODEL,
                purpose="main_chat", max_retries=0,
            ):
                parts.append(piece)
            raw = "".join(parts).strip()
            self.tracer.record("model_raw", {"text": raw}, trace_id=trace_id)
            chunks, _ = self.replier.parse_reply(raw, voice_allowed=False)
            display = record_text_from_chunks(chunks)
            silent = self.replier.is_silence_decision(raw)
            status = "silent" if silent else ("sent" if display else "empty")
            errors: tuple[str, ...] = ()
            self.tracer.record("parsed_result", {
                "status": status, "display": display, "chunks": chunks,
            }, trace_id=trace_id)
        else:
            result = await self.kernel.handle_prepared_turn(prepared["prepared_turn"])
            trace_id = result.trace_id
            raw = result.draft.text
            display = "\n".join(bubble.text for bubble in result.draft.bubbles) if result.validation_passed else ""
            status = "sent" if result.validation_passed else "format_failed"
            errors = result.validation_errors
        usage_rows = await self.db.fetchall(
            "select prompt_tokens, completion_tokens, cost_estimate "
            "from llm_calls where id>=?", (first_new_id,),
        )
        if not usage_rows:
            raise RuntimeError("真实调用没有产生沙箱计费记录，停止后续执行")
        return SideOutcome(
            side=self.side,
            raw_text=raw,
            display_text=display,
            status=status,
            errors=tuple(errors),
            cost=round(sum(float(row["cost_estimate"]) for row in usage_rows), 6),
            prompt_tokens=sum(int(row["prompt_tokens"]) for row in usage_rows),
            completion_tokens=sum(int(row["completion_tokens"]) for row in usage_rows),
            trace_id=trace_id,
        )

    async def close(self) -> None:
        if getattr(self, "gateway", None) is not None:
            await self.gateway.close()
        if getattr(self, "db", None) is not None:
            await self.db.close()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.db_path) + suffix).unlink(missing_ok=True)


async def prepare_all(cases: Sequence[ABCase], sources: Mapping[str, Path], run_dir: Path):
    prepared: dict[str, dict[str, Any]] = {}
    for index, case in enumerate(cases, 1):
        print(f"[组装 {index}/{len(cases)}] {case.case_id} {case.title}", flush=True)
        prepared[case.case_id] = {}
        for side in ("old", "new"):
            sandbox = SideSandbox(case, side, sources[case.case_id], run_dir)
            try:
                await sandbox.initialize()
                prepared[case.case_id][side] = await sandbox.prepare()
            finally:
                await sandbox.close()
    return prepared


def save_results(path: Path, outcomes: Mapping[str, Mapping[str, SideOutcome]], metadata: Mapping[str, Any]) -> None:
    path.write_text(json.dumps({
        "metadata": dict(metadata),
        "outcomes": outcomes_to_json(outcomes),
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_blind(run_dir: Path, cases: Sequence[ABCase], outcomes, seed: int) -> None:
    report, mapping = render_blind_markdown(cases, outcomes, seed)
    (run_dir / "BLIND.md").write_text(report, encoding="utf-8")
    (run_dir / "private_mapping.json").write_text(
        json.dumps(mapping, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )


async def main_async(args: argparse.Namespace) -> None:
    only = set(args.only.split(",")) if args.only else None
    cases, sources = load_cases(Path(args.cases).resolve(), only)
    if args.from_results:
        data = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
        outcomes = outcomes_from_json(data["outcomes"])
        complete_ids = {
            case_id for case_id, pair in outcomes.items()
            if isinstance(pair, Mapping) and set(pair) == {"old", "new"}
        }
        cases = [case for case in cases if case.case_id in complete_ids]
        if not cases:
            raise ValueError("既有结果没有可组成 A/B 的完整题目")
        seed = args.seed if args.seed is not None else int(data.get("metadata", {}).get("seed", 20261026))
        output_dir = Path(args.output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        write_blind(output_dir, cases, outcomes, seed)
        print(f"已仅从既有结果重排 {len(cases)} 题盲测稿，没有调用 API。")
        return

    seed = args.seed if args.seed is not None else 20261026
    run_dir = Path(args.output_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    source_hashes = {case_id: file_sha256(path) for case_id, path in sources.items()}
    prepared = await prepare_all(cases, sources, run_dir)
    estimates = [{
        "case_id": case.case_id,
        "old": {
            "prompt_tokens": prepared[case.case_id]["old"]["prompt_tokens"],
            "estimated_cost": prepared[case.case_id]["old"]["estimated_cost"],
        },
        "new": {
            "prompt_tokens": prepared[case.case_id]["new"]["prompt_tokens"],
            "estimated_cost": prepared[case.case_id]["new"]["estimated_cost"],
        },
    } for case in cases]
    estimated_total = round(sum(
        prepared[case.case_id][side]["estimated_cost"]
        for case in cases for side in ("old", "new")
    ), 6)
    estimate_document = {
        "model": MODEL,
        "thinking_effort": "low",
        "case_count": len(cases),
        "real_history_cases": sum(not case.constructed for case in cases),
        "constructed_cases": sum(case.constructed for case in cases),
        "call_count": len(cases) * 2,
        "completion_token_reserve_per_call": 4096,
        "estimated_total_yuan": estimated_total,
        "budget_yuan": args.budget,
        "within_budget": estimated_total <= args.budget,
        "cases": estimates,
    }
    (run_dir / "estimate.json").write_text(
        json.dumps(estimate_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in estimate_document.items() if k != "cases"}, ensure_ascii=False, indent=2))
    if not args.execute:
        print("仅完成组装与估价，未调用真实 API。")
        return
    if not args.confirm_real_api:
        raise RuntimeError("真实执行还必须显式提供 --confirm-real-api")
    if estimated_total > args.budget:
        raise RuntimeError(f"保守估价 {estimated_total} 元超过预算 {args.budget} 元，未调用 API")

    outcomes: dict[str, dict[str, SideOutcome]] = {}
    actual_total = 0.0
    metadata = {
        "model": MODEL, "thinking_effort": "low", "seed": seed,
        "estimated_total_yuan": estimated_total, "budget_yuan": args.budget,
        "actual_total_yuan": 0.0, "complete": False,
    }
    results_path = run_dir / "results.json"
    try:
        for index, case in enumerate(cases, 1):
            outcomes[case.case_id] = {}
            for side in ("old", "new"):
                estimate = prepared[case.case_id][side]["estimated_cost"]
                if actual_total + estimate > args.budget:
                    raise RuntimeError("实际费用加下一次保守估价将超过预算，停止后续调用")
                print(f"[真实生成 {index}/{len(cases)} {side}] {case.case_id}", flush=True)
                sandbox = SideSandbox(case, side, sources[case.case_id], run_dir)
                try:
                    await sandbox.initialize()
                    with frozen_time(case.received_at):
                        outcome = await sandbox.generate(prepared[case.case_id][side])
                finally:
                    await sandbox.close()
                outcomes[case.case_id][side] = outcome
                actual_total = round(actual_total + outcome.cost, 6)
                metadata["actual_total_yuan"] = actual_total
                save_results(results_path, outcomes, metadata)
        metadata["complete"] = True
        save_results(results_path, outcomes, metadata)
        write_blind(run_dir, cases, outcomes, seed)
        print(f"完成：{len(cases)} 题，{len(cases) * 2} 次调用，实际估算费用 {actual_total} 元")
    finally:
        for case_id, path in sources.items():
            if file_sha256(path) != source_hashes[case_id]:
                raise RuntimeError(f"本地输入副本发生变化：{path}")
        metadata["actual_total_yuan"] = actual_total
        save_results(results_path, outcomes, metadata)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", default=str(ROOT / "scripts/ab/stage3_cases.json"))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--only", help="逗号分隔题号，例如 R01,C02")
    parser.add_argument("--seed", type=int, help="盲测随机种子；from-results 默认沿用原结果")
    parser.add_argument("--budget", type=float, default=1.0)
    parser.add_argument("--execute", action="store_true", help="估价通过后执行真实生成")
    parser.add_argument("--confirm-real-api", action="store_true", help="确认允许真实聊天 API 支出")
    parser.add_argument("--from-results", help="只从既有 results.json 重排盲测稿")
    return parser


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
