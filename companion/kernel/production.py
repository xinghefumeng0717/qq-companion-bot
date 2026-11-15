"""新内核与现有生产轮次/投递链路的薄桥接。

承接文字、真实表情段、入站引用与当前消息图片；旧协议样式只作为用户原文，不触发第二条生成链。
被引旧图维持仅文本描述且不触发重新视觉识别；有图但路径缺失时只补充“内容不可用”的事实。
新内核失败绝不调用第二个模型，也不改原稿。
受支持轮次只经注入的同一个 DeliveryExecutor 投递：只认真实回执，历史与语音状态只认实际消息。
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import random
import re
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional, Sequence

from companion.kernel.contracts import (
    DeliveryPlan,
    DeliveryResult,
    IncomingMessage,
    StandardizedTurnInput,
)
from companion.db import parse_dt
from companion.image_perception import extract_image_description
from companion.kernel.delivery import DeliveryExecutor, plan_explicit_voice
from companion.kernel.semantic import (
    build_proactive_turn_input,
    build_turn_input,
    create_delivery_plan,
    create_reply_draft,
    semantic_history_view,
)
from companion.stickers import image_to_base64_data_url
from companion.voice_planner import VoicePlanner, is_voice_refusal, is_voice_request

logger = logging.getLogger(__name__)

# 主模型已经没有可用原稿时，由系统直接接话；不再调用模型改写或补写。
MODEL_FAILURE_TEXTS = (
    "咳……刚分神了。你再说一次",
    "等等，刚那句我没看进去。再发一次",
    "你刚才说什么？再说一次",
)


# 生成前告诉她的本轮投递事实：只陈述系统这轮会做什么，不要求她输出任何标记。
VOICE_FACT_WILL_SEND = "他这句是在要语音；这一轮你的话里会有一条由系统用语音发出"
VOICE_FACT_TEXT_ONLY = "他这句是在要语音；但此刻发不了语音，这一轮只有文字"
VOICE_FACT_RETRY = "上一条语音没有发出去；这一轮你的话里会有一条由系统用语音重新发出"

# 他已经说了要走（去忙、去睡、先这样），之后只补一个表情：这一轮她不回。
# 只认这几种明确的收尾说法；窗口内才算，隔了很久再发表情是新的开头。
_CLOSING_RE = re.compile(
    r"晚安|先睡|去睡|先撤|先忙|去忙|不聊了|先这样|回头聊|回头说|回头细说|拜拜|去开会|要开会|开会去"
)
_CLOSING_WINDOW = timedelta(minutes=30)
_FACE_ONLY_TEXT_RE = re.compile(r"^(?:\s*\[[^\[\]\n]{1,12}\]\s*)+$")
_SHORT_ACK_RE = re.compile(r"^[好嗯哦噢行成对okOK的吧呀啦～~。！!，, ]{1,6}$")
_FACE_KINDS = {"face", "emoji", "sticker"}


def _is_face_only(user_text: str, batch: Sequence[dict[str, Any]], has_image: bool) -> bool:
    """这一批是不是只有表情：没有任何文字、图片或引用。"""
    if has_image:
        return False
    structured = [item.get("parts") for item in batch if item.get("parts") is not None]
    if structured and len(structured) == len(batch):
        kinds = [str(part.get("kind") or "text").lower() for parts in structured for part in parts]
        return bool(kinds) and all(kind in _FACE_KINDS for kind in kinds)
    return bool(_FACE_ONLY_TEXT_RE.match(user_text or ""))


def _message_id(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _turn_input(
    user_text: str,
    batch: Sequence[dict[str, Any]],
    image_desc: Optional[str] = None,
    image_path: Optional[str] = None,
) -> StandardizedTurnInput:
    """批次 → 内核输入。

    支持：
    1. 真实文本段与表情段（parts 携带 kind="text" / "face"）；
    2. 入站引用（quoted_message_id / quoted_text）；
    3. 图片消息（kind="image"）：多图时只有目标最后一张带视觉自然语义描述，
       较早的图片项仅保留图片事实（空描述）；纯图片项不产生多余空文本消息，
       引用元数据随 image 消息保留。
    4. 无批次直接调用时（batch 为空），根据 user_text 与 image_path 产生消息。
    """
    messages: list[IncomingMessage] = []
    has_image_input = bool(image_path)

    if not batch:
        # 无批次直接调用
        text_body = (user_text or "").strip()
        if text_body:
            messages.append(
                IncomingMessage(
                    message_id=None,
                    quoted_message_id=None,
                    text=text_body,
                    kind="text",
                )
            )
        if has_image_input:
            messages.append(
                IncomingMessage(
                    message_id=None,
                    quoted_message_id=None,
                    text=image_desc or "",
                    kind="image",
                )
            )
        return build_turn_input(
            session_id="production-private-chat",
            messages=messages,
            received_at=datetime.now(),
        )

    # 批次模式：定位目标图片项（被视觉感知的那一张）
    target_item = None
    for item in batch:
        if item.get("image_index") is not None:
            t_idx = item.get("image_index")
            for candidate in batch:
                if candidate.get("index") == t_idx:
                    target_item = candidate
                    break
            if target_item is None:
                target_item = item
            break
    if target_item is None:
        # 无 image_index 时，按最后一张 has_image 作为目标，与 aggregator 最后一张 wins 一致
        for item in reversed(batch):
            if item.get("has_image"):
                target_item = item
                break

    # 若 batch 中没有任何项标记 has_image，但调用方传入了 image_path，则以最后一项作为挂载目标
    attached_implicit_image = False
    if has_image_input and target_item is None:
        target_item = batch[-1]
        attached_implicit_image = True

    for source_scope, item in enumerate(batch, start=1):
        message_id = _message_id(item.get("message_id"))
        quoted_id = _message_id(item.get("quoted_message_id"))
        quoted_text = item.get("quoted_text")
        payload: dict[str, Any] = {"source_scope": source_scope}
        if quoted_text is not None:
            payload["quoted_text"] = str(quoted_text)
        quoted_role = item.get("quoted_role")
        if quoted_role is not None:
            payload["quoted_role"] = str(quoted_role)
        is_target_image = (item is target_item)
        item_has_image = bool(item.get("has_image")) or (attached_implicit_image and is_target_image)
        item_image_desc = (image_desc or "") if is_target_image else ""

        parts = item.get("parts")
        if parts is None and quoted_id is not None:
            # 旧批次正文已含引用前缀；展示层保留原文，不再添加一次。
            payload["quote_prefix_in_text"] = True
        if parts is not None:
            # 结构化消息：以 parts 为准展开真实正文段
            has_parts = bool(parts)
            for part in parts:
                messages.append(
                    IncomingMessage(
                        message_id=message_id,
                        quoted_message_id=quoted_id,
                        text=str(part.get("text") or ""),
                        kind=str(part.get("kind") or "text").lower(),
                        payload=payload,
                    )
                )
            if item_has_image:
                # 附加图片消息，复用该消息的 message_id 与引用 payload
                messages.append(
                    IncomingMessage(
                        message_id=message_id,
                        quoted_message_id=quoted_id,
                        text=item_image_desc,
                        kind="image",
                        payload=payload,
                    )
                )
            elif not has_parts and quoted_id is not None:
                # quote-only 且无图
                messages.append(
                    IncomingMessage(
                        message_id=message_id,
                        quoted_message_id=quoted_id,
                        text="",
                        kind="text",
                        payload=payload,
                    )
                )
            continue

        # 无 parts 项
        item_text = str(item.get("text") or "").strip()
        if item_has_image:
            if item_text:
                messages.append(
                    IncomingMessage(
                        message_id=message_id,
                        quoted_message_id=quoted_id,
                        text=item_text,
                        kind="text",
                        payload=payload,
                    )
                )
            # 纯图片不产生空 text 消息；引用元数据直接挂在 image 消息上
            messages.append(
                IncomingMessage(
                    message_id=message_id,
                    quoted_message_id=quoted_id,
                    text=item_image_desc,
                    kind="image",
                    payload=payload,
                )
            )
        else:
            messages.append(
                IncomingMessage(
                    message_id=message_id,
                    quoted_message_id=quoted_id,
                    text=str(item.get("text") or ""),
                    kind=str(item.get("kind") or "text").lower(),
                    payload=payload,
                )
            )

    return build_turn_input(
        session_id="production-private-chat",
        messages=messages,
        received_at=datetime.now(),
    )


def body_from_delivery_result(result: DeliveryResult) -> str:
    """只拼接实际送达的正文；语音是载体，贴纸不编码成助手正文。"""
    parts: list[str] = []
    for message in result.messages:
        if message.channel == "sticker":
            continue
        if message.channel not in {"text", "voice"}:
            raise ValueError(f"未知实际投递类型: {message.channel}")
        if not message.attachment:
            parts.append(message.text)
    return "\n".join(parts).strip()


def delivery_actions_from_result(
    result: DeliveryResult, plan: DeliveryPlan
) -> list[dict[str, Any]]:
    """只把实际送达的语音/贴纸投成动作记录，计划本身不产生动作。"""
    if result.draft_id != plan.draft_id:
        raise ValueError("DeliveryResult 与 DeliveryPlan 的 draft_id 不匹配")
    planned = {action.bubble_id: action for action in plan.actions}
    records: list[dict[str, Any]] = []
    for message in result.messages:
        if message.channel not in {"voice", "sticker"}:
            continue
        action = planned.get(message.bubble_id)
        if action is None:
            raise ValueError(f"实际消息找不到对应动作: {message.bubble_id}")
        metadata = {
            "draft_id": result.draft_id,
            "bubble_id": message.bubble_id,
            "message_id": message.message_id,
            "quote_message_id": message.quote_message_id,
            "attachment": message.attachment,
            "fallback_from": message.fallback_from,
            "delivery_status": result.status,
            "errors": list(result.errors),
        }
        content = message.text
        if message.channel == "sticker":
            sticker = action.extra.get("sticker")
            if isinstance(sticker, dict):
                desc = str(sticker.get("desc") or sticker.get("name") or "")
                file_path = sticker.get("file")
            else:
                desc = str(sticker or "")
                file_path = sticker
            content = desc
            metadata["sticker_desc"] = desc
            metadata["sticker_file"] = str(file_path) if file_path is not None else ""
        records.append({
            "channel": message.channel,
            "content": content,
            "metadata": metadata,
        })
    return records


def semantic_observer_history(turns: Sequence[Any]) -> list[dict[str, Any]]:
    """给 observer 的近期历史副本剥掉旧动作外壳，数据库原记录保持不变。"""
    views: list[dict[str, Any]] = []
    for turn in turns:
        item = dict(turn)
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = semantic_history_view(content, str(item.get("role") or "user"))
        views.append(item)
    return views


def voice_status_from_delivery_result(
    result: DeliveryResult, plan: DeliveryPlan
) -> Optional[str]:
    """语音状态只认实际载体；整轮后续失败不抹掉已确认语音。"""
    if not any(action.channel == "voice" for action in plan.actions):
        return None
    if any(
        message.channel == "text" and message.fallback_from == "voice"
        for message in result.messages
    ):
        return "text_fallback"
    if any(message.channel == "voice" for message in result.messages):
        return "sent"
    return "failed"


class KernelTurnHandler:
    """所有聚合输入只走新内核；运行时适配器不提供第二条生成路径。

    冻结原稿与系统短句都只经注入的同一个执行器逐条真实投递；历史与语音状态只认实际消息。
    """

    def __init__(
        self,
        *,
        kernel: Any,
        runtime: Any,
        memory: Any,
        observer: Any,
        proactive: Any,
        delivery_executor: DeliveryExecutor,
        vision_gateway: Any = None,
        expression_picker: Any = None,
    ) -> None:
        self.kernel = kernel
        self._runtime = runtime
        self.memory = memory
        self.observer = observer
        self.proactive = proactive
        # 整个 handler 生命周期复用同一个执行器，同稿回放保护才不会被每轮新建冲掉。
        self.delivery_executor = delivery_executor
        # 仅用于生成前图片自然描述；不是第二条主聊生成入口。
        self.vision_gateway = vision_gateway
        # 系统替她挑表情（可选）：只决定要不要多发一个附件，不碰原稿。
        self.expression_picker = expression_picker
        self._observer_tasks: set[asyncio.Task] = set()

    def __getattr__(self, name: str) -> Any:
        # 语音闸门等旧能力仍由旧 handler 承担，不在新桥里复制一份。
        return getattr(self._runtime, name)

    @property
    def _last_voice_sent(self) -> bool:
        return bool(getattr(self._runtime, "_last_voice_sent", False))

    @_last_voice_sent.setter
    def _last_voice_sent(self, value: bool) -> None:
        self._runtime._last_voice_sent = bool(value)

    @property
    def _last_voice_failed(self) -> bool:
        return bool(getattr(self._runtime, "_last_voice_failed", False))

    @_last_voice_failed.setter
    def _last_voice_failed(self, value: bool) -> None:
        self._runtime._last_voice_failed = bool(value)

    async def _handle_model_failure(
        self,
        record_user_text: str,
        has_image: bool,
        reason: str,
    ) -> None:
        """没有可用原稿时由系统接话；故障不进入情绪或关系结算。"""
        text = random.choice(MODEL_FAILURE_TEXTS)
        logger.warning("[Kernel] 主模型无可用原稿，系统兜底接话: %s", reason)
        bot_msg = await self._deliver_system_line(text)
        # 他的原话在生成前已经入库，这里只补她实际送达的那句。
        if bot_msg is not None:
            await self.memory.save_assistant_turn(bot_msg)
        await self.memory.finish_turn()
        await self.memory.reinforce_memories(record_user_text)

    async def _deliver_system_line(self, text: str) -> Optional[str]:
        """系统短句（兜底接话、图片读取提醒）同样经执行器投递，返回实际送达的正文。

        每句独立 draft_id，不与任何原稿共用回放缓存；不带显示引用。
        """
        draft = create_reply_draft(text, draft_id=f"system_line_{uuid.uuid4().hex}")
        plan = create_delivery_plan(draft, None)
        delivery = await self.delivery_executor.execute(draft, plan)
        delivered = body_from_delivery_result(delivery) or None
        if delivered is None:
            logger.warning("[Kernel] 系统短句未确认送达: %s", "; ".join(delivery.errors))
        return delivered

    def _delivery_action_events(self) -> Any:
        """真实动作记录是新内核的基础历史，不受旧实验开关控制。"""
        return getattr(self._runtime, "runtime_events", None)

    async def _unanswered_proactive(self) -> int:
        getter = getattr(self.proactive, "get_unanswered_count", None)
        if not callable(getter):
            return 0
        try:
            return int(await getter() or 0)
        except Exception:
            return 0

    async def _wait_first_reply(self) -> bool:
        """她拿起手机的第一条先晾一会儿；取不到时机配置或出错就不等。"""
        wait = getattr(self._runtime, "wait_first_reply", None)
        if not callable(wait):
            return False
        try:
            return bool(await wait(self.memory))
        except Exception as exc:
            logger.warning("[Kernel] 首条延迟异常，按不等待处理: %s", exc)
            return False

    async def _typing(self, draft: Any, plan: DeliveryPlan, is_first_reply: bool) -> None:
        """发送前演"正在输入"：只算仍以文字发出的气泡，语音不是她打出来的字。"""
        play = getattr(self._runtime, "play_typing", None)
        if not callable(play):
            return
        channels = {action.bubble_id: action.channel for action in plan.actions}
        text = "\n".join(
            bubble.text for bubble in draft.bubbles if channels.get(bubble.bubble_id) == "text"
        )
        if not text:
            return
        try:
            await play(text, is_first_reply)
        except Exception as exc:
            logger.warning("[Kernel] 正在输入异常，直接发送: %s", exc)

    def _content_voice(self, draft: Any, plan: DeliveryPlan, planner: Any, allowed: bool) -> tuple[DeliveryPlan, str]:
        """她没被要求发语音时，由程序看内容挑一句适合念的；只换载体，不改原话。

        挑中的那句必须恰好是一条完整气泡，否则保持文字。普通回复和主动消息共用这一处。
        """
        choose = getattr(planner, "plan", None)
        if not callable(choose):
            return plan, ""
        choice = choose("", draft.text, voice_allowed=allowed and planner.allows(""))
        if not choice.has_voice:
            return plan, choice.reason
        return plan_explicit_voice(
            draft, plan, requested=True, allowed=True,
            max_chars=self._runtime._voice_max_chars(),
            preferred_text=choice.voice_text,
        ), choice.reason

    async def _with_expression(
        self, draft: Any, plan: DeliveryPlan, user_text: str, typing: Any
    ) -> DeliveryPlan:
        """一边演"正在输入"一边让系统挑表情，两件事同时做，不额外拖慢回复。"""
        picker = self.expression_picker
        if picker is None or not plan.actions:
            await typing
            return plan

        async def _pick() -> Any:
            try:
                turns = await self.memory.get_recent_turns(limit=6)
                ids = [t["id"] for t in turns if t.get("role") == "assistant" and t.get("id")]
                return await picker.pick(user_text, draft.text, ids)
            except Exception as exc:
                logger.info("[Kernel] 这轮不发表情: %s", exc)
                return None

        picked, _ = await asyncio.gather(_pick(), typing)
        if not picked:
            return plan
        last = plan.actions[-1]
        actions = plan.actions[:-1] + (dataclasses.replace(
            last, extra={**dict(last.extra), "sticker": picked, "sticker_after": True}
        ),)
        return dataclasses.replace(plan, actions=actions)

    async def _deliver(
        self, result: Any, plan: DeliveryPlan
    ) -> tuple[DeliveryResult, bool]:
        """执行投递。被取消（停机）时取回执行器保全的已确认部分，第二个返回值为 True。"""
        try:
            return await self.delivery_executor.execute(
                result.draft, plan, trace_id=result.trace_id
            ), False
        except asyncio.CancelledError:
            partial = self.delivery_executor.result_for(result.draft.draft_id)
            if partial is None:
                raise
            return partial, True

    async def _record_delivery(
        self,
        delivery: DeliveryResult,
        result: Any,
        plan: DeliveryPlan,
        *,
        voice_promised: bool,
        explicit_request: bool,
        voice_why: str,
        received_at: datetime,
        turn_id: Optional[int],
        may_retry: bool = False,
    ) -> None:
        """把真实动作和语音结果记到她说出口的那一轮上；状态只认实际消息。"""
        voice_status = voice_status_from_delivery_result(delivery, plan)
        if voice_status is None and voice_promised:
            # 生成前已告诉她这轮会发语音，结果没有一条气泡能用语音发：按没发出去如实记下。
            voice_status = "failed"
        # 答应了却没发出去：下一轮补发一次。闸门没放行的不算欠，补发失败的也不再追。
        retry_requested = bool(
            may_retry and voice_promised and voice_status in {"failed", "text_fallback"}
        )
        self._runtime._voice_retry_requested = retry_requested
        self._runtime._voice_retry_requested_at = received_at if retry_requested else None
        voice_sent = voice_status == "sent"
        self._last_voice_sent = voice_sent
        self._last_voice_failed = bool(explicit_request and not voice_sent)
        events = self._delivery_action_events()
        if events is None:
            return
        actual_actions = delivery_actions_from_result(delivery, plan)
        if actual_actions:
            try:
                # 冷却与审计关心动作何时完成，不回填成本轮用户消息的接收时间。
                await events.record_delivery_actions(actual_actions, evidence_turn_id=turn_id)
            except Exception as exc:
                logger.warning("[Kernel] 实际动作记录写入失败，不影响实际投递结果: %s", exc)
        try:
            if voice_status is not None:
                # 程序自己挑的语音没发出去时，她从没提过语音，不往那一轮历史旁边挂说明。
                noted = voice_promised or voice_status == "sent"
                await events.record_voice(
                    voice_status,
                    result.draft.text,
                    now=received_at,
                    reason="; ".join(delivery.errors) or delivery.status,
                    evidence_turn_id=turn_id if noted else None,
                )
            elif explicit_request and voice_why:
                # 点名要语音但闸门没放行：她写之前已经知道这轮只有文字，只留审计记录。
                await events.record_voice(
                    "not_attempted", result.draft.text, now=received_at, reason=voice_why,
                    evidence_turn_id=turn_id,
                )
        except Exception as exc:
            logger.warning("[Kernel] 语音状态写入失败，不影响实际投递结果: %s", exc)

    async def handle_proactive(
        self,
        material: str,
        *,
        received_at: Optional[datetime] = None,
    ) -> bool:
        """系统主动事件复用同一生成与真实投递；没有实际送达就不写主动历史。"""
        if not (material or "").strip():
            return False
        turn_input = build_proactive_turn_input(material, received_at=received_at)
        turn_input = dataclasses.replace(
            turn_input, unanswered_proactive=await self._unanswered_proactive()
        )
        try:
            result = await self.kernel.handle_turn(turn_input)
        except Exception as exc:
            # 没有用户问话时不用“再说一次”兜底，也不把故障写成主动历史。
            logger.warning(
                "[Kernel] 主动消息生成失败，本次不主动发: %s: %s",
                type(exc).__name__, exc,
            )
            return False
        if not result.validation_passed:
            logger.warning(
                "[Kernel] 主动原稿无效，本次不主动发: %s",
                ",".join(result.validation_errors) or "validation_failed",
            )
            return False

        # 主动语音由系统按现有闸门与内容规划选择；只改完整气泡的载体，不改正文。
        assembler = getattr(self._runtime, "assembler", None)
        planner = VoicePlanner(
            getattr(assembler, "voice_frequency", "frequent"),
            max_chars=self._runtime._voice_max_chars(),
        )
        allowed = await self.proactive._voice_gate_ok()
        plan, voice_reason = self._content_voice(result.draft, result.plan, planner, allowed)

        # typing 只计算仍以文字投递的气泡；语音不是她“打出来”的字。
        actions_by_bubble = {action.bubble_id: action for action in plan.actions}
        typing_text = "\n".join(
            bubble.text for bubble in result.draft.bubbles
            if actions_by_bubble[bubble.bubble_id].channel == "text"
        )
        if typing_text:
            await self.proactive._play_typing_indicator(typing_text)

        delivery, cancelled = await self._deliver(result, plan)
        if delivery.status != "sent":
            logger.warning(
                "[Kernel] 主动消息投递未完全按计划: status=%s, errors=%s",
                delivery.status, "; ".join(delivery.errors),
            )
        bot_record = body_from_delivery_result(delivery) or None
        turn_id: Optional[int] = None
        if bot_record is not None:
            turn_id = await self.memory.save_proactive_turn(bot_record)
        await self._record_delivery(
            delivery, result, plan,
            voice_promised=False,
            explicit_request=False,
            voice_why=voice_reason,
            received_at=turn_input.received_at,
            turn_id=turn_id,
        )
        if cancelled:
            raise asyncio.CancelledError
        if not delivery.messages:
            logger.warning("[Kernel] 主动消息没有任何确认送达，不写历史或增加未回复计数")
            return False
        await self.proactive.increment_unanswered_count()
        return True

    async def _closing_face_only(
        self,
        user_text: str,
        batch: Sequence[dict[str, Any]],
        has_image: bool,
        user_turn_id: int,
    ) -> bool:
        """他刚说了要走，这一批又只有表情：这轮她不回。"""
        if not _is_face_only(user_text, batch, has_image):
            return False
        try:
            turns = await self.memory.get_recent_turns(limit=3, before_id=user_turn_id)
        except Exception as exc:
            logger.warning("[Kernel] 读取收尾上下文失败，按正常轮次处理: %s", exc)
            return False
        now = datetime.now()
        for turn in reversed(turns):
            if turn.get("role") != "user":
                continue
            when = parse_dt(turn.get("created_at"))
            if when is None or now - when > _CLOSING_WINDOW:
                return False
            content = str(turn.get("content") or "").strip()
            if _FACE_ONLY_TEXT_RE.match(content) or _SHORT_ACK_RE.match(content):
                # "好～"、单个表情这类应答不改变"他已经要走了"的状态，继续往前看。
                continue
            return bool(_CLOSING_RE.search(content))
        return False

    async def handle_turn(
        self,
        user_text: str,
        image_path: Optional[str],
        quote_targets: Optional[list[dict[str, Any]]] = None,
    ) -> None:
        batch = list(quote_targets or [])
        has_batch_image = any(item.get("has_image") for item in batch)
        if not (user_text or "").strip() and not image_path and not has_batch_image:
            logger.warning("[Kernel] 忽略没有文字或图片事实的空输入")
            return

        image_desc: Optional[str] = None
        record_suffix = ""
        has_image_turn = bool(image_path) or has_batch_image

        if image_path:
            data_url, err = image_to_base64_data_url(image_path)
            if err:
                # 读图失败是系统提醒：照常投递但不入历史，本轮到此结束。
                await self._deliver_system_line(err)
                return

            config = getattr(self._runtime, "config", None)
            vision_model = getattr(getattr(config, "llm", None), "vision_model", None)
            gateway = self.vision_gateway

            if vision_model and gateway:
                desc = await extract_image_description(gateway, vision_model, data_url)
                if desc is not None:
                    image_desc = desc
                    record_suffix = f"[发来一张照片：{desc}]"
                else:
                    image_desc = "没能看清"
                    record_suffix = "[发来一张图片，但没能看清]"
            else:
                image_desc = "你看不到内容"
                record_suffix = "[对方发来一张图片，你看不到内容]"
        elif has_batch_image:
            image_desc = "图片内容不可用"
            record_suffix = "[对方发来一张图片，但图片内容不可用]"

        record_user_text = user_text
        if record_suffix:
            record_user_text = f"{user_text} {record_suffix}".strip() if user_text else record_suffix

        # 清零之前先记下：她找过他几次他没回，是这一轮的背景事实。
        unanswered = await self._unanswered_proactive()
        await self.proactive.reset_unanswered_count()
        # 与旧流水线同序：到点的见面先补纪要，再准备本轮对话。
        meetups = getattr(getattr(self._runtime, "assembler", None), "meetups", None)
        process_due = getattr(meetups, "process_due", None)
        if callable(process_due):
            try:
                await process_due()
            except Exception as exc:
                logger.warning("[Kernel] 见面纪要处理异常，继续本轮: %s", exc)

        # 他的话先入库：之后生成、投递哪一步出事，这句都不会丢。
        user_turn_id = await self.memory.save_user_turn(
            record_user_text, has_image=has_image_turn
        )

        if await self._closing_face_only(user_text, batch, has_image_turn, user_turn_id):
            logger.info("[Kernel] 他已道别后只补了表情，本轮不回复")
            await self.memory.finish_turn()
            return

        # 她拿起手机的第一条先晾一会儿（相当于还没看到消息）；等完再取时间、定语音、拼提示词。
        is_first_reply = await self._wait_first_reply()

        # 语音在生成前定下来并作为事实告诉她：她写的时候就知道这轮会不会出声。
        # 上一轮答应的语音没发出去时，这一轮由系统补发一次：她最自然的反应就是"我再发一条"，
        # 让这句话有真实动作兑现。只补一次，不连环重试。
        # 欠账只在实际补发终态里结算；生成失败或取消时保留到下一轮。
        retry_at = getattr(self._runtime, "_voice_retry_requested_at", None)
        retry_owed = bool(
            getattr(self._runtime, "_voice_retry_requested", False)
            and isinstance(retry_at, datetime)
            and datetime.now() - retry_at <= _CLOSING_WINDOW
        )
        if not retry_owed or is_voice_refusal(user_text):
            retry_owed = False
            self._runtime._voice_retry_requested = False
            self._runtime._voice_retry_requested_at = None
        asked_now = is_voice_request(user_text)
        explicit_request = asked_now or retry_owed
        allowed = False
        voice_why = ""
        delivery_fact = ""
        if explicit_request:
            allowed, voice_why = await self._runtime._check_voice_gate(user_text)
            allowed = allowed and (retry_owed or self._runtime.voice_planner.allows(user_text))
            if allowed:
                delivery_fact = VOICE_FACT_WILL_SEND if asked_now else VOICE_FACT_RETRY
            elif asked_now:
                delivery_fact = VOICE_FACT_TEXT_ONLY

        logger.info("[Kernel] 轮次进入新内核 (has_image=%s)", has_image_turn)
        turn_input = _turn_input(user_text, batch, image_desc=image_desc, image_path=image_path)
        if delivery_fact or unanswered:
            turn_input = dataclasses.replace(
                turn_input, delivery_fact=delivery_fact, unanswered_proactive=unanswered
            )
        try:
            result = await self.kernel.handle_turn(turn_input, history_before_id=user_turn_id)
        except Exception as exc:
            await self._handle_model_failure(
                record_user_text, has_image_turn, f"{type(exc).__name__}: {exc}"
            )
            return
        logger.info(
            "[Kernel] 新内核轮次完成: trace_id=%s, passed=%s",
            result.trace_id, result.validation_passed,
        )
        if not result.validation_passed:
            await self._handle_model_failure(
                record_user_text,
                has_image_turn,
                ",".join(result.validation_errors) or "validation_failed",
            )
            return

        plan = plan_explicit_voice(
            result.draft,
            result.plan,
            requested=explicit_request,
            allowed=allowed,
            max_chars=self._runtime._voice_max_chars() if explicit_request else 0,
        )
        if not explicit_request:
            # 他没要语音：看内容挑一句适合念的（晚安、想你这类），同样要过闸门。
            try:
                gate_ok, _why = await self._runtime._check_voice_gate("")
                plan, _reason = self._content_voice(
                    result.draft, plan, getattr(self._runtime, "voice_planner", None), gate_ok
                )
            except Exception as exc:
                logger.warning("[Kernel] 内容语音规划异常，本轮按文字发送: %s", exc)
        plan = await self._with_expression(
            result.draft, plan, user_text,
            self._typing(result.draft, plan, is_first_reply),
        )
        delivery, cancelled = await self._deliver(result, plan)
        if delivery.status != "sent":
            logger.warning(
                "[Kernel] 投递未完全按计划: status=%s, errors=%s",
                delivery.status, "; ".join(delivery.errors),
            )
        bot_record = body_from_delivery_result(delivery) or None
        has_delivery = bool(delivery.messages)

        # 只存确认送达的正文；真实动作挂在这一轮上。停机取消时已送达的部分同样入库。
        assistant_turn_id: Optional[int] = None
        if bot_record is not None:
            assistant_turn_id = await self.memory.save_assistant_turn(bot_record)
        await self._record_delivery(
            delivery, result, plan,
            voice_promised=bool(explicit_request and allowed),
            explicit_request=explicit_request,
            voice_why=voice_why,
            received_at=turn_input.received_at,
            turn_id=assistant_turn_id,
            may_retry=not retry_owed,
        )
        await self.memory.finish_turn()
        if cancelled:
            raise asyncio.CancelledError
        await self.memory.reinforce_memories(record_user_text)
        if not has_delivery:
            # 一条都没确认送达：她这轮没有真正说出口，与沉默、模型故障一样不结算观察者。
            logger.warning("[Kernel] 本轮没有任何确认送达的消息，只保存用户原话")
            return
        if bot_record is None:
            # 只有贴纸等实际动作，没有可供 observer 评价的正文；不能误报成零确认。
            logger.info("[Kernel] 本轮有实际动作送达但没有正文，跳过观察者结算")
            return
        try:
            recent_turns = semantic_observer_history(
                await self.memory.get_recent_turns(limit=10)
            )
        except Exception as exc:
            logger.warning("新内核观察者历史读取失败，按本轮处理: %s", exc)
            recent_turns = None
        task = asyncio.create_task(self.observer.settle_turn(
            user_message=record_user_text,
            assistant_reply=bot_record,
            user_image_path=image_path,
            recent_turns=recent_turns,
        ))
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)


__all__ = ["KernelTurnHandler"]
