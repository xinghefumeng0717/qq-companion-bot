"""第二阶段离线统一投递动作层。

只执行冻结原稿与独立动作计划，不调用主模型，不修改正文，也不连接生产适配器。
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Mapping, Optional, Tuple

from companion.kernel.contracts import (
    DeliveryAction,
    DeliveryPlan,
    DeliveryResult,
    ReplyBubble,
    ReplyDraft,
    SendReceipt,
    SentMessage,
)
from companion.kernel.tracing import JsonlTraceRecorder

_ACTION_CHANNELS = {"text", "voice", "sticker"}
_BODY_OVERRIDE_KEYS = {"text", "content", "bubble_id"}


class UnconfirmedDeliveryError(RuntimeError):
    """发送结果不确定：可能已到达，禁止自动补发或降级重试。"""


def validate_action_delivery_plan(plan: DeliveryPlan, draft: ReplyDraft) -> None:
    """校验第二阶段动作计划；第一阶段纯文字校验器保持不变。"""
    if plan.draft_id != draft.draft_id:
        raise ValueError("DeliveryPlan draft_id 与 ReplyDraft draft_id 不匹配")
    if plan.quote_message_id is not None and (
        isinstance(plan.quote_message_id, bool) or not isinstance(plan.quote_message_id, int)
    ):
        raise ValueError("显示引用必须绑定真实整数 message_id")
    if plan.silent:
        if draft.text.strip() or draft.bubbles or plan.actions:
            raise ValueError("非空原稿或动作计划不得被后置判为沉默")
        if plan.quote_message_id is not None:
            raise ValueError("沉默计划不得携带显示引用")
        return
    if not draft.bubbles:
        raise ValueError("非沉默草稿至少需要一个气泡；沉默必须是显式决策")

    bubbles = {bubble.bubble_id: bubble for bubble in draft.bubbles}
    if len(bubbles) != len(draft.bubbles):
        raise ValueError("ReplyDraft 自身不得包含重复 bubble_id")
    seen: set[str] = set()
    for action in plan.actions:
        if action.bubble_id not in bubbles:
            raise ValueError(f"未知 bubble_id={action.bubble_id}")
        if action.bubble_id in seen:
            raise ValueError(f"bubble_id={action.bubble_id} 重复出现")
        if action.channel not in _ACTION_CHANNELS:
            raise ValueError(f"不支持的投递动作 channel={action.channel}")
        if _BODY_OVERRIDE_KEYS.intersection(action.extra):
            raise ValueError("动作参数不得复制或覆盖冻结原稿正文")
        if action.channel == "sticker" and not action.extra.get("sticker"):
            raise ValueError("sticker 动作必须提供附加资源")
        if action.channel == "text" and set(action.extra) - {"sticker"}:
            raise ValueError("text 动作只允许 sticker 附加参数")
        seen.add(action.bubble_id)
    missing = set(bubbles) - seen
    if missing:
        raise ValueError(f"DeliveryPlan 遗漏了部分气泡: {missing}")
    expected_order = tuple(draft_bubble.bubble_id for draft_bubble in draft.bubbles)
    actual_order = tuple(action.bubble_id for action in plan.actions)
    if actual_order != expected_order:
        raise ValueError("DeliveryPlan 不得重排冻结原稿气泡")


def plan_explicit_voice(
    draft: ReplyDraft,
    plan: DeliveryPlan,
    *,
    requested: bool,
    allowed: bool,
    max_chars: int,
    min_chars: int = 4,
) -> DeliveryPlan:
    """为明确语音请求分配一个完整气泡；只改 channel，不改任何正文。

    没有合适气泡、未请求或闸门未放行时，原计划逐字段保持不变。
    """
    validate_action_delivery_plan(plan, draft)
    if plan.silent or not requested or not allowed:
        return plan
    try:
        limit = int(max_chars)
    except (TypeError, ValueError):
        return plan
    if limit <= 0:
        return plan
    minimum = max(0, int(min_chars))

    bubbles = {bubble.bubble_id: bubble for bubble in draft.bubbles}
    actions = []
    assigned = False
    for action in plan.actions:
        text = bubbles[action.bubble_id].text
        if (
            not assigned
            and action.channel == "text"
            and len(text.strip()) >= minimum
            and len(text) <= limit
        ):
            action = DeliveryAction(
                bubble_id=action.bubble_id,
                channel="voice",
                extra=action.extra,
            )
            assigned = True
        actions.append(action)
    if not assigned:
        return plan
    candidate = DeliveryPlan(
        draft_id=plan.draft_id,
        actions=tuple(actions),
        quote_message_id=plan.quote_message_id,
        silent=plan.silent,
    )
    validate_action_delivery_plan(candidate, draft)
    return candidate


def _sticker_of(action: DeliveryAction) -> Any:
    return action.extra.get("sticker")


class DeliveryExecutor:
    """把冻结气泡投递为实际消息，并只记录发送器明确确认的结果。"""

    def __init__(
        self,
        sender: Any,
        *,
        recorder: Optional[Any] = None,
        tracer: Optional[JsonlTraceRecorder] = None,
    ) -> None:
        self.sender = sender
        self.recorder = recorder
        self.tracer = tracer
        self._results: dict[str, DeliveryResult] = {}
        self._requests: dict[str, tuple[ReplyDraft, DeliveryPlan]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _trace(self, step: str, payload: Any, trace_id: Optional[str]) -> None:
        if self.tracer is not None:
            self.tracer.record(step, payload, trace_id=trace_id)

    async def execute(
        self,
        draft: ReplyDraft,
        plan: DeliveryPlan,
        *,
        trace_id: Optional[str] = None,
    ) -> DeliveryResult:
        """执行计划；同一原稿已有实际结果时只回放结果，不重复发送。"""
        validate_action_delivery_plan(plan, draft)
        lock = self._locks.setdefault(draft.draft_id, asyncio.Lock())
        async with lock:
            cached = self._results.get(draft.draft_id)
            if cached is not None:
                if self._requests[draft.draft_id] != (draft, plan):
                    raise ValueError("同一 draft_id 不得替换原稿或投递计划")
                self._trace("delivery_replay", cached, trace_id)
                return cached

            self._trace("delivery_plan", {"draft": draft, "plan": plan}, trace_id)
            if plan.silent:
                result = DeliveryResult(draft_id=draft.draft_id, status="silent")
                self._requests[draft.draft_id] = (draft, plan)
                self._results[draft.draft_id] = result
                self._trace("delivery_result", result, trace_id)
                return result

            result = await self._execute_validated(draft, plan)
            # 所有终态都缓存：零消息的未决失败同样禁止调用方盲目重试。
            self._requests[draft.draft_id] = (draft, plan)
            self._results[draft.draft_id] = result
            self._trace("delivery_result", result, trace_id)
            if self.recorder is not None and result.messages:
                recorded = self.recorder.record_delivery(result)
                if inspect.isawaitable(recorded):
                    await recorded
            return result

    async def _execute_validated(self, draft: ReplyDraft, plan: DeliveryPlan) -> DeliveryResult:
        bubbles = {bubble.bubble_id: bubble for bubble in draft.bubbles}
        messages: list[SentMessage] = []
        errors: list[str] = []
        quote_pending = plan.quote_message_id
        text_only = False
        stopped = False

        for action in plan.actions:
            bubble = bubbles[action.bubble_id]
            sticker = _sticker_of(action)
            if not text_only and sticker is not None:
                try:
                    receipt = await self.sender.send_sticker(sticker)
                    messages.append(self._sent(bubble, "sticker", receipt, attachment=True))
                except UnconfirmedDeliveryError as exc:
                    errors.append(self._error(bubble, "sticker_unconfirmed", exc))
                    stopped = True
                    break
                except Exception as exc:
                    errors.append(self._error(bubble, "sticker", exc))
                    text_only = True

            requested = "text" if text_only or action.channel == "sticker" else action.channel
            fallback_from: Optional[str] = None
            if text_only and action.channel != "text":
                fallback_from = "sticker" if sticker is not None else action.channel
            try:
                message, quote_pending = await self._send_bubble(
                    bubble, requested, quote_pending, action.extra, fallback_from
                )
                messages.append(message)
            except UnconfirmedDeliveryError as exc:
                errors.append(self._error(bubble, f"{requested}_unconfirmed", exc))
                stopped = True
                break
            except Exception as exc:
                if requested != "voice":
                    errors.append(self._error(bubble, requested, exc))
                    stopped = True
                    break
                # 语音失败只替换当前气泡的载体，原文、顺序与引用均不变。
                errors.append(self._error(bubble, "voice", exc))
                try:
                    message, quote_pending = await self._send_bubble(
                        bubble, "text", quote_pending, {}, "voice"
                    )
                    messages.append(message)
                except Exception as text_exc:
                    errors.append(self._error(bubble, "text", text_exc))
                    stopped = True
                    break

        delivered = {message.bubble_id for message in messages if not message.attachment}
        complete = delivered == set(bubbles)
        if stopped or not complete:
            status = "failed"
        elif errors:
            status = "degraded"
        else:
            status = "sent"
        return DeliveryResult(
            draft_id=draft.draft_id,
            status=status,
            messages=tuple(messages),
            errors=tuple(errors),
        )

    async def _send_bubble(
        self,
        bubble: ReplyBubble,
        channel: str,
        quote_message_id: Optional[int],
        extra: Mapping[str, Any],
        fallback_from: Optional[str],
    ) -> Tuple[SentMessage, Optional[int]]:
        if channel == "voice":
            options = {key: value for key, value in extra.items() if key != "sticker"}
            receipt = await self.sender.send_voice(
                bubble.text, quote_message_id=quote_message_id, **options
            )
        else:
            receipt = await self.sender.send_text(
                bubble.text, quote_message_id=quote_message_id
            )
        message = self._sent(
            bubble,
            channel,
            receipt,
            quote_message_id=quote_message_id,
            fallback_from=fallback_from,
        )
        # 显示引用只消费一次，且只附着于原稿消息而非贴纸附件。
        return message, None

    @staticmethod
    def _sent(
        bubble: ReplyBubble,
        channel: str,
        receipt: SendReceipt,
        *,
        quote_message_id: Optional[int] = None,
        attachment: bool = False,
        fallback_from: Optional[str] = None,
    ) -> SentMessage:
        if not isinstance(receipt, SendReceipt):
            raise TypeError("sender 必须返回 SendReceipt，不能用布尔值猜测实际发送结果")
        return SentMessage(
            bubble_id=bubble.bubble_id,
            channel=channel,
            text="" if attachment else bubble.text,
            message_id=receipt.message_id,
            quote_message_id=quote_message_id,
            attachment=attachment,
            fallback_from=fallback_from,
        )

    @staticmethod
    def _error(bubble: ReplyBubble, channel: str, exc: Exception) -> str:
        return f"{bubble.bubble_id}:{channel}:{type(exc).__name__}:{exc}"


__all__ = ["DeliveryExecutor", "UnconfirmedDeliveryError", "validate_action_delivery_plan"]
