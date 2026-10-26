"""新内核与现有生产轮次/投递链路的薄桥接。

只承接普通纯文字。图片、语音请求、表情协议和历史引用在生成前回退旧链路；
新内核失败绝不调用第二个模型，也不改原稿。
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any, Optional, Sequence

from companion.kernel.contracts import (
    DeliveryPlan,
    IncomingMessage,
    ReplyDraft,
    StandardizedTurnInput,
)
from companion.kernel.delivery import plan_explicit_voice, validate_action_delivery_plan
from companion.kernel.semantic import build_turn_input
from companion.replier import record_text_from_chunks
from companion.runtime_state import runtime_v1_enabled
from companion.voice_planner import VOICE_REQUEST_RE as _VOICE_REQUEST_RE

logger = logging.getLogger(__name__)

_LEGACY_ONLY_RE = re.compile(
    r"\[(?:face|sticker|表情|quote)[:：]|（他引用了你之前说的",
    re.IGNORECASE,
)


def can_use_kernel_text(
    user_text: str,
    image_path: Optional[str],
    quote_targets: Optional[Sequence[dict[str, Any]]],
) -> bool:
    """只接受当前桥接明确支持的输入；不支持时必须在生成前回退。"""
    if image_path or not (user_text or "").strip():
        return False
    if _LEGACY_ONLY_RE.search(user_text):
        return False
    # 非语音请求的括号/富文本先交给旧链路；明确语音由系统统一投递。
    if not _VOICE_REQUEST_RE.search(user_text) and ("[" in user_text or "]" in user_text):
        return False
    for item in quote_targets or ():
        if item.get("has_image") or item.get("quoted_message_id") is not None:
            return False
        if str(item.get("kind", "text")).lower() != "text":
            return False
    return True


def _message_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _turn_input(user_text: str, batch: Sequence[dict[str, Any]]) -> StandardizedTurnInput:
    rows = batch or [{"message_id": None, "text": user_text}]
    messages = [
        IncomingMessage(
            message_id=_message_id(item.get("message_id")),
            text=str(item.get("text") or ""),
            kind="text",
        )
        for item in rows
    ]
    return build_turn_input(
        session_id="production-private-chat",
        messages=messages,
        received_at=datetime.now(),
    )


def chunks_from_plan(draft: ReplyDraft, plan: DeliveryPlan) -> list[dict[str, Any]]:
    """把动作计划转成现有 chunk；引用只给文字，record 绝不与 reply 混合。"""
    validate_action_delivery_plan(plan, draft)
    bubbles = {bubble.bubble_id: bubble for bubble in draft.bubbles}
    quote_pending = plan.quote_message_id
    chunks = []
    for action in plan.actions:
        chunk = {
            "type": action.channel,
            "content": bubbles[action.bubble_id].text,
        }
        if action.channel == "text" and quote_pending is not None:
            chunk["_quote"] = {"message_id": quote_pending}
            quote_pending = None
        chunks.append(chunk)
    return chunks


class KernelTurnHandler:
    """普通文字走新内核；特殊输入复用旧 handler，保持同一回调接口。"""

    def __init__(
        self,
        *,
        kernel: Any,
        legacy_handler: Any,
        memory: Any,
        observer: Any,
        proactive: Any,
        replier: Any,
        send_chunk_fn: Any,
    ) -> None:
        self.kernel = kernel
        self._legacy = legacy_handler
        self.memory = memory
        self.observer = observer
        self.proactive = proactive
        self.replier = replier
        self.send_chunk_fn = send_chunk_fn
        self._observer_tasks: set[asyncio.Task] = set()

    def __getattr__(self, name: str) -> Any:
        # 语音闸门等旧能力仍由旧 handler 承担，不在新桥里复制一份。
        return getattr(self._legacy, name)

    @property
    def _last_voice_sent(self) -> bool:
        return bool(getattr(self._legacy, "_last_voice_sent", False))

    @_last_voice_sent.setter
    def _last_voice_sent(self, value: bool) -> None:
        self._legacy._last_voice_sent = bool(value)

    @property
    def _last_voice_failed(self) -> bool:
        return bool(getattr(self._legacy, "_last_voice_failed", False))

    @_last_voice_failed.setter
    def _last_voice_failed(self, value: bool) -> None:
        self._legacy._last_voice_failed = bool(value)

    async def recover_voice_failure(
        self,
        draft: str,
        reason: str,
        user_text: Optional[str] = None,
    ) -> str:
        """语音失败只恢复原文，不调用模型补写，也不改变用户看到的文字。"""
        return draft

    async def handle_turn(
        self,
        user_text: str,
        image_path: Optional[str],
        quote_targets: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        batch = list(quote_targets or [])
        if not can_use_kernel_text(user_text, image_path, batch):
            await self._legacy.handle_turn(user_text, image_path, batch)
            return

        await self.proactive.reset_unanswered_count()
        # 与旧流水线同序：到点的见面先补纪要，再准备本轮对话。
        meetups = getattr(getattr(self._legacy, "assembler", None), "meetups", None)
        process_due = getattr(meetups, "process_due", None)
        if callable(process_due):
            try:
                await process_due()
            except Exception as exc:
                logger.warning("[Kernel] 见面纪要处理异常，继续本轮: %s", exc)
        logger.info("[Kernel] 普通文字轮进入新内核")
        turn_input = _turn_input(user_text, batch)
        result = await self.kernel.handle_turn(turn_input)
        logger.info(
            "[Kernel] 新内核轮次完成: trace_id=%s, passed=%s",
            result.trace_id, result.validation_passed,
        )
        if not result.validation_passed:
            # 没有可投递原稿：不重试、不另写兜底话术、不调用第二个模型。
            await self.memory.save_turn_pair(
                user_msg=user_text, bot_msg=None, has_image=False,
            )
            await self.memory.reinforce_memories(user_text)
            return

        explicit_request = bool(_VOICE_REQUEST_RE.search(user_text or ""))
        allowed = False
        voice_why = ""
        if explicit_request:
            allowed, voice_why = await self._legacy._check_voice_gate(user_text)
            allowed = allowed and self._legacy.voice_planner.allows(user_text)
        plan = plan_explicit_voice(
            result.draft,
            result.plan,
            requested=explicit_request,
            allowed=allowed,
            max_chars=self._legacy._voice_max_chars() if explicit_request else 0,
        )
        chunks = chunks_from_plan(result.draft, plan)
        for chunk in chunks:
            if chunk["type"] == "voice":
                chunk["_voice_user_text"] = user_text
        await self.replier.send_reply_chunks(chunks, self.send_chunk_fn)

        # 实际发送结果决定历史文本和语音状态，不能用计划冒充已经发出。
        clean_record_text = record_text_from_chunks(chunks) or result.draft.text
        voice_sent = any(
            chunk.get("type") == "voice" and chunk.get("_voice_delivery") == "sent"
            for chunk in chunks
        )
        self._last_voice_sent = voice_sent
        self._last_voice_failed = bool(explicit_request and not voice_sent)
        config = getattr(self._legacy, "config", None)
        if config is not None and runtime_v1_enabled(config.reply, self._legacy.assembler):
            events = getattr(self._legacy, "runtime_events", None)
            if events is not None:
                try:
                    await events.record_delivery_chunks(
                        chunks,
                        result.draft.text,
                        now=turn_input.received_at,
                        blocked_reason=voice_why if explicit_request and not voice_sent else "",
                    )
                except Exception as exc:
                    logger.warning("[Kernel] 语音账本写入失败，不影响原稿: %s", exc)

        await self.memory.save_turn_pair(
            user_msg=user_text,
            bot_msg=clean_record_text,
            has_image=False,
        )
        await self.memory.reinforce_memories(user_text)
        try:
            recent_turns = await self.memory.get_recent_turns(limit=10)
        except Exception as exc:
            logger.warning("新内核观察者历史读取失败，按本轮处理: %s", exc)
            recent_turns = None
        task = asyncio.create_task(self.observer.settle_turn(
            user_message=user_text,
            assistant_reply=clean_record_text,
            user_image_path=None,
            recent_turns=recent_turns,
        ))
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)


__all__ = ["KernelTurnHandler", "can_use_kernel_text"]
