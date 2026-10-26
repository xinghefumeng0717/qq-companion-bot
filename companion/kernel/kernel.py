"""离线对话内核编排器。

普通一轮只做一次主模型生成；不写生产历史，不接真实发送器。
"""
from __future__ import annotations

import hashlib
import re
import time
from typing import Any, Optional, Sequence

from companion.kernel.context import ContextBuilder
from companion.kernel.contracts import (
    DeliveryPlan,
    KernelResult,
    MemoryPromptBlocks,
    PreparedTurn,
    ReplyDraft,
    StandardizedTurnInput,
)
from companion.kernel.memory_view import collect_memory_blocks
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.kernel.semantic import create_delivery_plan, create_reply_draft, validate_delivery_plan
from companion.kernel.style_view import collect_style_examples
from companion.kernel.tracing import JsonlTraceRecorder


_INTERNAL_PROTOCOL_RE = re.compile(
    r"\[(?:(?:label|quote|voice|face|sticker|表情)[:：][^\]]*|(?:沉默|SILENT|SILENCE))\]?"
    r"|^\s*（(?:语音(?:消息)?|表情)(?:）|[:：])",
    re.IGNORECASE | re.MULTILINE,
)


class DialogueKernel:
    def __init__(
        self,
        *,
        gateway: Any,
        persona: Any,
        snapshot_builder: PsychologicalSnapshotBuilder,
        context_builder: ContextBuilder,
        tracer: JsonlTraceRecorder,
        memory: Optional[Any] = None,
        arcs: Optional[Any] = None,
        self_memory: bool = False,
        impressions: bool = False,
        meetups: Optional[Any] = None,
        style_examples: Optional[Any] = None,
        model: Optional[str] = None,
        history_limit: int = 10,
        code_version: str = "kernel-stage1",
        max_retries: Optional[int] = None,
    ) -> None:
        self.gateway = gateway
        self.persona = persona
        self.snapshot_builder = snapshot_builder
        self.context_builder = context_builder
        self.tracer = tracer
        self.memory = memory
        self.arcs = arcs
        self.self_memory = self_memory
        self.impressions = impressions
        self.meetups = meetups
        self.style_examples = style_examples
        self.model = model
        self.history_limit = history_limit
        self.code_version = code_version
        self.max_retries = max_retries

    def _target_model(self) -> str:
        if self.model:
            return self.model
        if hasattr(self.gateway.config, "active"):
            return self.gateway.config.active().chat
        return getattr(self.gateway.config, "observer_model", "deepseek-flash")

    async def _history(self) -> Sequence[Any]:
        if self.memory is None:
            return []
        return await self.memory.get_recent_turns(limit=self.history_limit)

    async def prepare_turn(
        self,
        turn_input: StandardizedTurnInput,
        *,
        trace_id: Optional[str] = None,
    ) -> PreparedTurn:
        """只读准备一轮上下文；不调用主模型，不推进任何心理状态。"""
        event_trace_id = trace_id or self.tracer.new_trace()
        self.tracer.record("turn_input", turn_input, trace_id=event_trace_id)
        self.tracer.record(
            "runtime",
            {"code_version": self.code_version, "model": self._target_model()},
            trace_id=event_trace_id,
        )

        snapshot = await self.snapshot_builder.build_snapshot(
            event_trace_id,
            user_message=turn_input.text,
            now=turn_input.received_at,
        )
        self.tracer.record(
            "psychological_snapshot", snapshot, trace_id=event_trace_id,
        )

        history = tuple(await self._history())
        memory_blocks: Optional[MemoryPromptBlocks] = None
        if self.memory is not None:
            memory_blocks = await collect_memory_blocks(
                self.memory,
                arcs=self.arcs,
                query=turn_input.text,
                valence=float(snapshot.mood.get("v", 2.0)),
                now=turn_input.received_at,
                self_memory=self.self_memory,
                impressions=self.impressions,
                meetups=self.meetups,
            )
            self.tracer.record(
                "memory_context", memory_blocks, trace_id=event_trace_id,
            )
        style_examples = None
        if self.style_examples is not None:
            style_examples = collect_style_examples(self.style_examples, turn_input)
        messages = tuple(self.context_builder.build_messages(
            self.persona,
            snapshot,
            history,
            turn_input,
            memory_blocks=memory_blocks,
            style_examples=style_examples,
        ))
        if style_examples is not None:
            self.tracer.record(
                "style_examples",
                {
                    "query": style_examples.query,
                    "seed": style_examples.seed,
                    "indices": style_examples.indices,
                    "system_prompt_sha256": hashlib.sha256(
                        messages[0]["content"].encode("utf-8"),
                    ).hexdigest(),
                },
                trace_id=event_trace_id,
            )
        self.tracer.record(
            "model_context", {"messages": messages}, trace_id=event_trace_id,
        )
        return PreparedTurn(
            trace_id=event_trace_id,
            turn_input=turn_input,
            snapshot=snapshot,
            history=history,
            memory_blocks=memory_blocks,
            messages=messages,
            style_examples=style_examples,
        )

    async def handle_turn(self, turn_input: StandardizedTurnInput) -> KernelResult:
        """保持原入口：公共准备后立即生成，整轮只创建一个心理快照。"""
        started = time.perf_counter()
        prepared = await self.prepare_turn(turn_input)
        return await self._generate_prepared(prepared, started)

    async def handle_prepared_turn(self, prepared: PreparedTurn) -> KernelResult:
        """复用既有准备结果生成，不重新读取历史、记忆或心理状态。"""
        return await self._generate_prepared(prepared, time.perf_counter())

    async def _generate_prepared(
        self,
        prepared: PreparedTurn,
        started: float,
    ) -> KernelResult:
        trace_id = prepared.trace_id
        turn_input = prepared.turn_input
        parts: list[str] = []
        stream_options: dict[str, Any] = {}
        if self.max_retries is not None:
            stream_options["max_retries"] = self.max_retries
        try:
            async for piece in self.gateway.stream_chat(
                messages=list(prepared.messages),
                model=self._target_model(),
                purpose="main_chat",
                **stream_options,
            ):
                parts.append(piece)
        except Exception as exc:
            self.tracer.record(
                "model_error",
                {"type": type(exc).__name__, "message": str(exc)},
                trace_id=trace_id,
            )
            raise

        raw_text = "".join(parts)
        self.tracer.record("model_raw", {"text": raw_text}, trace_id=trace_id)
        draft = create_reply_draft(raw_text)
        errors: list[str] = []
        if not raw_text.strip():
            errors.append("empty_draft")
        protocol = _INTERNAL_PROTOCOL_RE.search(raw_text)
        if protocol:
            errors.append(f"internal_protocol:{protocol.group(0)}")

        if errors:
            plan = DeliveryPlan(draft_id=draft.draft_id, actions=())
            passed = False
        else:
            plan = create_delivery_plan(draft, turn_input.reply_to_message_id)
            validate_delivery_plan(plan, draft)
            passed = True

        result = KernelResult(
            trace_id=trace_id,
            turn_input=turn_input,
            snapshot=prepared.snapshot,
            draft=draft,
            plan=plan,
            messages=prepared.messages,
            validation_passed=passed,
            validation_errors=tuple(errors),
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        self.tracer.record(
            "validation",
            {"passed": passed, "errors": errors},
            trace_id=trace_id,
        )
        self.tracer.record("kernel_result", result, trace_id=trace_id)
        return result


async def replay_pure_text(
    draft: ReplyDraft,
    *,
    tracer: Optional[JsonlTraceRecorder] = None,
    quote_message_id: Optional[int] = None,
    trace_id: Optional[str] = None,
) -> DeliveryPlan:
    """固定原稿回放：不调用模型，只重建同一份纯文字计划。"""
    plan = create_delivery_plan(draft, quote_message_id)
    validate_delivery_plan(plan, draft)
    if tracer is not None:
        event_trace_id = trace_id or draft.draft_id
        tracer.record("replay_draft", draft, trace_id=event_trace_id)
        tracer.record("replay_plan", plan, trace_id=event_trace_id)
    return plan
