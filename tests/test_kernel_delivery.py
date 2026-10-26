"""第二阶段统一投递层测试 (tests/test_kernel_delivery.py)

测试对象：
- companion.kernel.delivery.DeliveryExecutor
- companion.kernel.delivery.validate_action_delivery_plan
- companion.kernel.contracts (DeliveryAction, DeliveryPlan, SendReceipt, SentMessage, DeliveryResult)
- companion.kernel.semantic.create_reply_draft

覆盖核心规范：
1. 纯文字两气泡与 quote (quote 仅附着于首条实际消息)
2. voice 成功原文 (voice 选项透传，正文无损)
3. voice 失败保原文与 quote (降级为 text，fallback_from='voice'，后续气泡继续)
4. sticker 不吞正文 (先发 sticker 附件 text=''，再发气泡正文)
5. sticker 失败完整纯文字 (降级为纯文字，text_only 持续到后续气泡)
6. voice+sticker 组合 (先发 sticker 附件，再发语音，除 sticker 外 extra 作为 send_voice 参数)
7. 部分成功后 sticker 失败不重发 (已发消息不重发，计数与 ID 稳定)
8. 显式沉默 (无 sender/recorder 调用，status='silent')
9. 非空原稿 silent 拒绝 (silent 仅允许空草稿、无 actions、无 quote)
10. 计划缺失/重复/未知/非法 (完整校验各维度异常)
11. recorder 只见实际 (messages 非空才调，全败与沉默不调，支持异步 recorder)
12. 重复执行幂等 (同一 draft_id 已有结果不再调用 sender)
13. trace 有计划和结果 (JsonlTraceRecorder 记录 delivery_plan, delivery_result 与 delivery_replay)
14. text 失败停止不盲重试 (status='failed'，后续气泡中止投递)
15. voice+sticker 组合中 voice 失败降级 (sticker 成功保留，语音降级为文本)
"""

from __future__ import annotations

import asyncio
import functools
import sys
from pathlib import Path
from typing import Any, List, Mapping, Optional, Set

import pytest

# 确保项目根目录在 sys.path 中，兼容直接运行 pytest 与 python -m pytest
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from companion.kernel.contracts import (
    DeliveryAction,
    DeliveryPlan,
    DeliveryResult,
    ReplyBubble,
    ReplyDraft,
    SendReceipt,
    SentMessage,
)
from companion.kernel.delivery import (
    DeliveryExecutor,
    validate_action_delivery_plan,
)
from companion.kernel.semantic import create_reply_draft
from companion.kernel.tracing import JsonlTraceRecorder


def async_test(coro_fn):
    """确保测试在无外部 pytest 异步插件及任何环境下均能稳定运行。"""
    @functools.wraps(coro_fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(coro_fn(*args, **kwargs))
    return wrapper


class FakeSender:
    """离线模拟发送器。

    递增生成整数 message_id，失败时抛出 RuntimeError。
    记录所有调用的详细载荷以供断言。
    """

    def __init__(
        self,
        *,
        fail_text_on: Optional[Set[str]] = None,
        fail_voice: bool = False,
        fail_voice_on: Optional[Set[str]] = None,
        fail_sticker_on: Optional[Set[str]] = None,
        fail_all_text: bool = False,
        fail_all_sticker: bool = False,
    ) -> None:
        self.text_calls: List[dict[str, Any]] = []
        self.voice_calls: List[dict[str, Any]] = []
        self.sticker_calls: List[dict[str, Any]] = []
        self.all_calls: List[dict[str, Any]] = []
        self._msg_id_counter = 5000
        self.fail_text_on = set(fail_text_on or ())
        self.fail_voice = fail_voice
        self.fail_voice_on = set(fail_voice_on or ())
        self.fail_sticker_on = set(fail_sticker_on or ())
        self.fail_all_text = fail_all_text
        self.fail_all_sticker = fail_all_sticker

    def _next_message_id(self) -> int:
        self._msg_id_counter += 1
        return self._msg_id_counter

    async def send_text(
        self, text: str, quote_message_id: Optional[int] = None
    ) -> SendReceipt:
        if self.fail_all_text or text in self.fail_text_on:
            raise RuntimeError(f"Simulated send_text failure: {text}")
        msg_id = self._next_message_id()
        record = {
            "type": "text",
            "text": text,
            "quote_message_id": quote_message_id,
            "message_id": msg_id,
        }
        self.text_calls.append(record)
        self.all_calls.append(record)
        return SendReceipt(message_id=msg_id)

    async def send_voice(
        self, text: str, quote_message_id: Optional[int] = None, **options: Any
    ) -> SendReceipt:
        if self.fail_voice or text in self.fail_voice_on:
            raise RuntimeError(f"Simulated send_voice failure: {text}")
        msg_id = self._next_message_id()
        record = {
            "type": "voice",
            "text": text,
            "quote_message_id": quote_message_id,
            "options": options,
            "message_id": msg_id,
        }
        self.voice_calls.append(record)
        self.all_calls.append(record)
        return SendReceipt(message_id=msg_id)

    async def send_sticker(self, sticker: Any) -> SendReceipt:
        if self.fail_all_sticker or sticker in self.fail_sticker_on:
            raise RuntimeError(f"Simulated send_sticker failure: {sticker}")
        msg_id = self._next_message_id()
        record = {
            "type": "sticker",
            "sticker": sticker,
            "message_id": msg_id,
        }
        self.sticker_calls.append(record)
        self.all_calls.append(record)
        return SendReceipt(message_id=msg_id)


class FakeRecorder:
    """离线模拟记录器，支持同步与异步 record_delivery。"""

    def __init__(self, *, async_mode: bool = False, fail: bool = False) -> None:
        self.calls: List[DeliveryResult] = []
        self.async_mode = async_mode
        self.fail = fail

    async def _async_record(self, result: DeliveryResult) -> None:
        if self.fail:
            raise RuntimeError("Simulated async recorder failure")
        self.calls.append(result)

    def record_delivery(self, result: DeliveryResult) -> Any:
        if self.async_mode:
            return self._async_record(result)
        if self.fail:
            raise RuntimeError("Simulated sync recorder failure")
        self.calls.append(result)
        return None


# =========================================================================
# 1. 纯文字两气泡与 quote
# =========================================================================
@async_test
async def test_text_two_bubbles_with_quote():
    draft = create_reply_draft("这是第一句气泡。\n\n这是第二句气泡。")
    assert len(draft.bubbles) == 2
    b0, b1 = draft.bubbles

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
        quote_message_id=12345,
    )
    validate_action_delivery_plan(plan, draft)

    sender = FakeSender()
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "sent"
    assert result.draft_id == draft.draft_id
    assert len(result.errors) == 0
    assert len(result.messages) == 2

    # quote 仅第一条实际原稿消息消费
    m0, m1 = result.messages
    assert m0.bubble_id == b0.bubble_id
    assert m0.channel == "text"
    assert m0.text == "这是第一句气泡。"
    assert m0.quote_message_id == 12345
    assert m0.attachment is False
    assert m0.fallback_from is None
    assert m0.message_id is not None

    assert m1.bubble_id == b1.bubble_id
    assert m1.channel == "text"
    assert m1.text == "这是第二句气泡。"
    assert m1.quote_message_id is None
    assert m1.attachment is False
    assert m1.fallback_from is None
    assert m1.message_id is not None
    assert m1.message_id > m0.message_id

    # 检查底层 sender 真实调用
    assert len(sender.text_calls) == 2
    assert sender.text_calls[0]["quote_message_id"] == 12345
    assert sender.text_calls[1]["quote_message_id"] is None


# =========================================================================
# 2. voice 成功原文
# =========================================================================
@async_test
async def test_voice_success_original_text():
    draft = create_reply_draft("这是一条需要转为语音的消息。")
    assert len(draft.bubbles) == 1
    b0 = draft.bubbles[0]

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id,
                channel="voice",
                extra={"voice": "soft", "speed": 1.1},
            ),
        ),
        quote_message_id=22222,
    )
    validate_action_delivery_plan(plan, draft)

    sender = FakeSender()
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "sent"
    assert len(result.errors) == 0
    assert len(result.messages) == 1

    m0 = result.messages[0]
    assert m0.bubble_id == b0.bubble_id
    assert m0.channel == "voice"
    assert m0.text == "这是一条需要转为语音的消息。"
    assert m0.quote_message_id == 22222
    assert m0.attachment is False
    assert m0.fallback_from is None

    assert len(sender.voice_calls) == 1
    assert sender.voice_calls[0]["text"] == "这是一条需要转为语音的消息。"
    assert sender.voice_calls[0]["quote_message_id"] == 22222
    assert sender.voice_calls[0]["options"] == {"voice": "soft", "speed": 1.1}


# =========================================================================
# 3. voice 失败保原文与 quote
# =========================================================================
@async_test
async def test_voice_failure_preserves_text_and_quote():
    draft = create_reply_draft("语音合成失败的句子。\n\n后续正常的句子。")
    assert len(draft.bubbles) == 2
    b0, b1 = draft.bubbles

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id,
                channel="voice",
                extra={"voice": "soft"},
            ),
            DeliveryAction(
                bubble_id=b1.bubble_id,
                channel="voice",
                extra={"voice": "soft"},
            ),
        ),
        quote_message_id=33333,
    )

    # 仅让第一个气泡的语音合成失败
    sender = FakeSender(fail_voice_on={"语音合成失败的句子。"})
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "degraded"
    assert len(result.errors) == 1
    assert b0.bubble_id in result.errors[0]
    assert "voice" in result.errors[0]
    assert len(result.messages) == 2

    # 第一个气泡原位降级为 text，保留原文与引用，记录 fallback_from='voice'
    m0 = result.messages[0]
    assert m0.bubble_id == b0.bubble_id
    assert m0.channel == "text"
    assert m0.text == "语音合成失败的句子。"
    assert m0.quote_message_id == 33333
    assert m0.attachment is False
    assert m0.fallback_from == "voice"

    # 第二个气泡不受影响，继续执行原计划的 voice 发送
    m1 = result.messages[1]
    assert m1.bubble_id == b1.bubble_id
    assert m1.channel == "voice"
    assert m1.text == "后续正常的句子。"
    assert m1.quote_message_id is None
    assert m1.attachment is False
    assert m1.fallback_from is None


# =========================================================================
# 4. sticker 不吞正文
# =========================================================================
@async_test
async def test_sticker_does_not_consume_body_text():
    draft = create_reply_draft("这是气泡的实际正文内容。")
    assert len(draft.bubbles) == 1
    b0 = draft.bubbles[0]

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id,
                channel="sticker",
                extra={"sticker": "candy_sticker_id"},
            ),
        ),
        quote_message_id=44444,
    )
    validate_action_delivery_plan(plan, draft)

    sender = FakeSender()
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "sent"
    assert len(result.errors) == 0
    # 先发送贴纸附件（text=''），再发气泡正文（text=原正文）
    assert len(result.messages) == 2

    m_sticker, m_text = result.messages
    assert m_sticker.bubble_id == b0.bubble_id
    assert m_sticker.channel == "sticker"
    assert m_sticker.text == ""
    assert m_sticker.attachment is True
    assert m_sticker.quote_message_id is None
    assert m_sticker.fallback_from is None

    assert m_text.bubble_id == b0.bubble_id
    assert m_text.channel == "text"
    assert m_text.text == "这是气泡的实际正文内容。"
    assert m_text.attachment is False
    assert m_text.quote_message_id == 44444
    assert m_text.fallback_from is None

    # 验证底层调用顺序与真实载荷
    assert len(sender.all_calls) == 2
    assert sender.all_calls[0]["type"] == "sticker"
    assert sender.all_calls[0]["sticker"] == "candy_sticker_id"
    assert sender.all_calls[1]["type"] == "text"
    assert sender.all_calls[1]["text"] == "这是气泡的实际正文内容。"
    assert sender.all_calls[1]["quote_message_id"] == 44444


# =========================================================================
# 5. sticker 失败完整纯文字
# =========================================================================
@async_test
async def test_sticker_failure_degrades_to_pure_text():
    draft = create_reply_draft("带有表情的句子。\n\n第二句原本是语音。")
    assert len(draft.bubbles) == 2
    b0, b1 = draft.bubbles

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id,
                channel="sticker",
                extra={"sticker": "broken_sticker"},
            ),
            DeliveryAction(
                bubble_id=b1.bubble_id,
                channel="voice",
                extra={"voice": "soft"},
            ),
        ),
    )

    sender = FakeSender(fail_sticker_on={"broken_sticker"})
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "degraded"
    assert len(result.errors) == 1
    assert "broken_sticker" in result.errors[0]

    # 贴纸失败后当前及后续未投递气泡均转为纯文字
    assert len(result.messages) == 2
    m0, m1 = result.messages

    assert m0.bubble_id == b0.bubble_id
    assert m0.channel == "text"
    assert m0.text == "带有表情的句子。"
    assert m0.attachment is False
    assert m0.fallback_from == "sticker"

    assert m1.bubble_id == b1.bubble_id
    assert m1.channel == "text"
    assert m1.text == "第二句原本是语音。"
    assert m1.attachment is False
    assert m1.fallback_from == "voice"

    assert len(sender.sticker_calls) == 0  # 失败未产生成功 receipt
    assert len(sender.voice_calls) == 0    # 后续 voice 降级为 text
    assert len(sender.text_calls) == 2


# =========================================================================
# 6. voice+sticker 组合
# =========================================================================
@async_test
async def test_voice_and_sticker_combination():
    draft = create_reply_draft("既有语音又有贴纸的气泡。")
    assert len(draft.bubbles) == 1
    b0 = draft.bubbles[0]

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id,
                channel="voice",
                extra={"sticker": "candy_combo", "voice": "soft", "pitch": "+2"},
            ),
        ),
        quote_message_id=55555,
    )
    validate_action_delivery_plan(plan, draft)

    sender = FakeSender()
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "sent"
    assert len(result.messages) == 2

    m_sticker, m_voice = result.messages
    assert m_sticker.channel == "sticker"
    assert m_sticker.text == ""
    assert m_sticker.attachment is True

    assert m_voice.channel == "voice"
    assert m_voice.text == "既有语音又有贴纸的气泡。"
    assert m_voice.attachment is False
    assert m_voice.quote_message_id == 55555

    # 验证底层调用：先发送贴纸，再发送语音；且 sticker 从语音 options 中被安全剔除
    assert len(sender.all_calls) == 2
    assert sender.all_calls[0]["type"] == "sticker"
    assert sender.all_calls[0]["sticker"] == "candy_combo"
    assert sender.all_calls[1]["type"] == "voice"
    assert sender.all_calls[1]["options"] == {"voice": "soft", "pitch": "+2"}
    assert "sticker" not in sender.all_calls[1]["options"]


# =========================================================================
# 7. 部分成功后 sticker 失败不重发
# =========================================================================
@async_test
async def test_partial_success_then_sticker_failure_does_not_resend():
    draft = create_reply_draft("第一句纯文字顺利成功。\n\n第二句带了坏表情。")
    assert len(draft.bubbles) == 2
    b0, b1 = draft.bubbles

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(
                bubble_id=b1.bubble_id,
                channel="sticker",
                extra={"sticker": "err_sticker"},
            ),
        ),
    )

    sender = FakeSender(fail_sticker_on={"err_sticker"})
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "degraded"
    assert len(result.messages) == 2

    m0, m1 = result.messages
    assert m0.bubble_id == b0.bubble_id
    assert m0.channel == "text"
    assert m0.text == "第一句纯文字顺利成功。"

    assert m1.bubble_id == b1.bubble_id
    assert m1.channel == "text"
    assert m1.text == "第二句带了坏表情。"
    assert m1.fallback_from == "sticker"

    # 第一句绝不重发：总共只发了 2 条 text，第一句 1 次，第二句 1 次
    assert len(sender.text_calls) == 2
    assert sender.text_calls[0]["text"] == "第一句纯文字顺利成功。"
    assert sender.text_calls[1]["text"] == "第二句带了坏表情。"


# =========================================================================
# 8. 显式沉默
# =========================================================================
@async_test
async def test_explicit_silent():
    draft = create_reply_draft("")
    assert len(draft.bubbles) == 0

    plan = DeliveryPlan(draft_id=draft.draft_id, actions=(), silent=True)
    validate_action_delivery_plan(plan, draft)

    sender = FakeSender()
    recorder = FakeRecorder()
    executor = DeliveryExecutor(sender=sender, recorder=recorder)
    result = await executor.execute(draft, plan)

    assert result.status == "silent"
    assert result.messages == ()
    assert result.errors == ()
    assert len(sender.all_calls) == 0
    assert len(recorder.calls) == 0


# =========================================================================
# 9. 非空原稿 silent 拒绝
# =========================================================================
def test_non_empty_draft_silent_rejected():
    draft = create_reply_draft("这里有正文内容")
    plan = DeliveryPlan(draft_id=draft.draft_id, actions=(), silent=True)
    with pytest.raises(ValueError, match="非空原稿或动作计划不得被后置判为沉默"):
        validate_action_delivery_plan(plan, draft)

    # 空草稿但计划携带了 actions
    empty_draft = create_reply_draft("")
    plan_with_actions = DeliveryPlan(
        draft_id=empty_draft.draft_id,
        actions=(DeliveryAction(bubble_id="any", channel="text"),),
        silent=True,
    )
    with pytest.raises(ValueError, match="非空原稿或动作计划不得被后置判为沉默"):
        validate_action_delivery_plan(plan_with_actions, empty_draft)

    # 沉默计划携带了 quote_message_id
    plan_with_quote = DeliveryPlan(
        draft_id=empty_draft.draft_id,
        actions=(),
        quote_message_id=12345,
        silent=True,
    )
    with pytest.raises(ValueError, match="沉默计划不得携带显示引用"):
        validate_action_delivery_plan(plan_with_quote, empty_draft)


# =========================================================================
# 10. 计划缺失/重复/未知/非法
# =========================================================================
def test_plan_validation_comprehensive():
    draft = create_reply_draft("第一句\n\n第二句")
    assert len(draft.bubbles) == 2
    b0, b1 = draft.bubbles

    # a. draft_id 不匹配
    p_mismatch = DeliveryPlan(
        draft_id="other_draft_id",
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="不匹配"):
        validate_action_delivery_plan(p_mismatch, draft)

    # b. quote_message_id 非整数
    p_bool_quote = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
        quote_message_id=True,  # type: ignore
    )
    with pytest.raises(ValueError, match="真实整数"):
        validate_action_delivery_plan(p_bool_quote, draft)

    p_str_quote = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
        quote_message_id="123",  # type: ignore
    )
    with pytest.raises(ValueError, match="真实整数"):
        validate_action_delivery_plan(p_str_quote, draft)

    # c. 非 silent 草稿无气泡
    empty_draft = create_reply_draft("")
    p_non_silent_empty = DeliveryPlan(draft_id=empty_draft.draft_id, actions=(), silent=False)
    with pytest.raises(ValueError, match="至少需要一个气泡"):
        validate_action_delivery_plan(p_non_silent_empty, empty_draft)

    # d. 计划缺失气泡
    p_missing = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(bubble_id=b0.bubble_id, channel="text"),),
    )
    with pytest.raises(ValueError, match="遗漏了部分气泡"):
        validate_action_delivery_plan(p_missing, draft)

    # e. 计划重复气泡
    p_dup = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="重复出现"):
        validate_action_delivery_plan(p_dup, draft)

    # f. 计划未知气泡
    p_unknown = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id="unknown_bubble_999", channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="未知 bubble_id"):
        validate_action_delivery_plan(p_unknown, draft)

    # g. 非法 channel
    p_bad_channel = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="video"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="不支持的投递动作"):
        validate_action_delivery_plan(p_bad_channel, draft)

    # h. extra 尝试覆盖正文 (text/content/bubble_id)
    for bad_key in ["text", "content", "bubble_id"]:
        p_override = DeliveryPlan(
            draft_id=draft.draft_id,
            actions=(
                DeliveryAction(
                    bubble_id=b0.bubble_id, channel="text", extra={bad_key: "hack"}
                ),
                DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
            ),
        )
        with pytest.raises(ValueError, match="不得复制或覆盖"):
            validate_action_delivery_plan(p_override, draft)

    # i. sticker 动作缺失 sticker 参数
    p_no_sticker_res = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="sticker", extra={}),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="sticker 动作必须提供附加资源"):
        validate_action_delivery_plan(p_no_sticker_res, draft)

    # j. text 动作携带了 sticker 以外的 extra 参数
    p_text_extra = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id, channel="text", extra={"voice": "soft"}
            ),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="text 动作只允许 sticker 附加参数"):
        validate_action_delivery_plan(p_text_extra, draft)


# =========================================================================
# 11. recorder 只见实际 (仅 messages 非空调用，支持异步)
# =========================================================================
@async_test
async def test_recorder_records_only_actual_messages():
    draft = create_reply_draft("句子一\n\n句子二")
    b0, b1 = draft.bubbles
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
    )

    # 1. 成功发送时被调用
    sender = FakeSender()
    recorder = FakeRecorder()
    executor = DeliveryExecutor(sender=sender, recorder=recorder)
    res1 = await executor.execute(draft, plan)
    assert res1.status == "sent"
    assert len(recorder.calls) == 1
    assert recorder.calls[0] is res1

    # 2. 全败时 messages 为空，recorder 不得被调用
    draft_fail = create_reply_draft("注定全败的句子")
    plan_fail = DeliveryPlan(
        draft_id=draft_fail.draft_id,
        actions=(DeliveryAction(bubble_id=draft_fail.bubbles[0].bubble_id, channel="text"),),
    )
    sender_fail = FakeSender(fail_all_text=True)
    recorder_fail = FakeRecorder()
    executor_fail = DeliveryExecutor(sender=sender_fail, recorder=recorder_fail)
    res2 = await executor_fail.execute(draft_fail, plan_fail)
    assert res2.status == "failed"
    assert len(res2.messages) == 0
    assert len(recorder_fail.calls) == 0

    # 3. 部分成功时，recorder 只收到实际成功的部分
    draft_part = create_reply_draft("首句成功\n\n次句失败")
    pb0, pb1 = draft_part.bubbles
    plan_part = DeliveryPlan(
        draft_id=draft_part.draft_id,
        actions=(
            DeliveryAction(bubble_id=pb0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=pb1.bubble_id, channel="text"),
        ),
    )
    sender_part = FakeSender(fail_text_on={"次句失败"})
    recorder_part = FakeRecorder()
    executor_part = DeliveryExecutor(sender=sender_part, recorder=recorder_part)
    res3 = await executor_part.execute(draft_part, plan_part)
    assert res3.status == "failed"
    assert len(res3.messages) == 1
    assert len(recorder_part.calls) == 1
    assert recorder_part.calls[0].messages == res3.messages

    # 4. 异步 recorder 正常支持
    recorder_async = FakeRecorder(async_mode=True)
    draft_async = create_reply_draft("异步记录测试")
    plan_async = DeliveryPlan(
        draft_id=draft_async.draft_id,
        actions=(DeliveryAction(bubble_id=draft_async.bubbles[0].bubble_id, channel="text"),),
    )
    executor_async = DeliveryExecutor(sender=FakeSender(), recorder=recorder_async)
    res4 = await executor_async.execute(draft_async, plan_async)
    assert res4.status == "sent"
    assert len(recorder_async.calls) == 1


# =========================================================================
# 12. 重复执行幂等
# =========================================================================
@async_test
async def test_idempotent_execution_no_duplicate_sending():
    draft = create_reply_draft("幂等测试气泡")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(bubble_id=draft.bubbles[0].bubble_id, channel="text"),),
    )

    sender = FakeSender()
    recorder = FakeRecorder()
    executor = DeliveryExecutor(sender=sender, recorder=recorder)

    # 第一次执行
    res1 = await executor.execute(draft, plan)
    assert res1.status == "sent"
    assert len(sender.text_calls) == 1
    assert len(recorder.calls) == 1

    # 第二次执行同一个 draft
    res2 = await executor.execute(draft, plan)
    assert res2 is res1
    # 真实 sender 与 recorder 调用次数均不增加
    assert len(sender.text_calls) == 1
    assert len(recorder.calls) == 1

    # 部分成功情况下的幂等性验证
    draft_part = create_reply_draft("前一段发成功\n\n后一段发失败")
    b0, b1 = draft_part.bubbles
    plan_part = DeliveryPlan(
        draft_id=draft_part.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
        ),
    )
    sender_part = FakeSender(fail_text_on={"后一段发失败"})
    executor_part = DeliveryExecutor(sender=sender_part)
    res_p1 = await executor_part.execute(draft_part, plan_part)
    assert res_p1.status == "failed"
    assert len(res_p1.messages) == 1
    assert len(sender_part.text_calls) == 1

    # 再次执行该草稿，防止重复补发前一段已成功的消息
    res_p2 = await executor_part.execute(draft_part, plan_part)
    assert res_p2 is res_p1
    assert len(sender_part.text_calls) == 1


# =========================================================================
# 13. trace 有计划和结果
# =========================================================================
@async_test
async def test_trace_recorder_records_plan_and_result(tmp_path):
    tracer = JsonlTraceRecorder(directory=tmp_path / "traces")
    trace_id = "test_tr_delivery_001"

    draft = create_reply_draft("追踪测试气泡")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(bubble_id=draft.bubbles[0].bubble_id, channel="text"),),
    )

    sender = FakeSender()
    executor = DeliveryExecutor(sender=sender, tracer=tracer)
    result = await executor.execute(draft, plan, trace_id=trace_id)
    assert result.status == "sent"

    # 读取并验证写入的 trace 事件
    events = tracer.events(trace_id=trace_id)
    steps = [e["step"] for e in events]
    assert "delivery_plan" in steps
    assert "delivery_result" in steps

    plan_ev = next(e for e in events if e["step"] == "delivery_plan")
    assert plan_ev["payload"]["draft"]["draft_id"] == draft.draft_id
    assert plan_ev["payload"]["plan"]["draft_id"] == draft.draft_id

    result_ev = next(e for e in events if e["step"] == "delivery_result")
    assert result_ev["payload"]["draft_id"] == draft.draft_id
    assert result_ev["payload"]["status"] == "sent"
    assert len(result_ev["payload"]["messages"]) == 1

    # 重复执行，验证记录了 delivery_replay
    await executor.execute(draft, plan, trace_id=trace_id)
    replay_events = tracer.events(trace_id=trace_id)
    replay_steps = [e["step"] for e in replay_events]
    assert "delivery_replay" in replay_steps


# =========================================================================
# 14. text 失败停止不盲重试
# =========================================================================
@async_test
async def test_text_failure_stops_without_blind_retry():
    draft = create_reply_draft("第一句正常\n\n第二句文本报错\n\n第三句不应被发送")
    assert len(draft.bubbles) == 3
    b0, b1, b2 = draft.bubbles

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b1.bubble_id, channel="text"),
            DeliveryAction(bubble_id=b2.bubble_id, channel="text"),
        ),
    )

    sender = FakeSender(fail_text_on={"第二句文本报错"})
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "failed"
    assert len(result.messages) == 1
    assert result.messages[0].bubble_id == b0.bubble_id
    assert len(result.errors) == 1
    assert "第二句文本报错" in result.errors[0]

    # 第三句绝不被调用，第二句失败后立即停止且不盲目重试
    assert len(sender.text_calls) == 1
    assert sender.text_calls[0]["text"] == "第一句正常"


# =========================================================================
# 15. voice+sticker 组合中 voice 失败降级
# =========================================================================
@async_test
async def test_voice_and_sticker_combination_voice_failure():
    draft = create_reply_draft("组合动作但语音失败。")
    b0 = draft.bubbles[0]

    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(
                bubble_id=b0.bubble_id,
                channel="voice",
                extra={"sticker": "sweet_candy", "voice": "soft"},
            ),
        ),
        quote_message_id=98765,
    )

    sender = FakeSender(fail_voice=True)
    executor = DeliveryExecutor(sender=sender)
    result = await executor.execute(draft, plan)

    assert result.status == "degraded"
    assert len(result.errors) == 1
    assert len(result.messages) == 2

    m_sticker, m_text = result.messages
    assert m_sticker.channel == "sticker"
    assert m_sticker.attachment is True
    assert m_sticker.quote_message_id is None

    # 语音失败后，原位降级为 text，继承原 quote_message_id
    assert m_text.channel == "text"
    assert m_text.text == "组合动作但语音失败。"
    assert m_text.attachment is False
    assert m_text.fallback_from == "voice"
    assert m_text.quote_message_id == 98765
