"""统一投递的重复发送与未决状态守卫；全部使用离线假发送器。"""

import asyncio
from dataclasses import replace

import pytest

from companion.kernel.contracts import DeliveryAction, DeliveryPlan, SendReceipt
from companion.kernel.delivery import DeliveryExecutor, UnconfirmedDeliveryError
from companion.kernel.semantic import (
    create_delivery_plan,
    create_reply_draft,
    validate_delivery_plan,
)


class GuardSender:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    async def send_text(self, text, quote_message_id=None):
        self.calls.append(("text", text, quote_message_id))
        if self.error:
            raise self.error
        return SendReceipt(message_id=100)

    async def send_voice(self, text, quote_message_id=None, **options):
        self.calls.append(("voice", text, quote_message_id))
        if self.error:
            raise self.error
        return SendReceipt(message_id=101)

    async def send_sticker(self, sticker):
        self.calls.append(("sticker", sticker))
        return SendReceipt(message_id=102)


@pytest.mark.anyio
async def test_unconfirmed_voice_never_falls_back_or_retries():
    draft = create_reply_draft("嗯，晚安", draft_id="unconfirmed-voice")
    sender = GuardSender(UnconfirmedDeliveryError("timeout; delivery unknown"))
    executor = DeliveryExecutor(sender)
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(draft.bubbles[0].bubble_id, "voice"),),
    )

    first = await executor.execute(draft, plan)
    second = await executor.execute(draft, plan)

    assert first is second
    assert first.status == "failed"
    assert first.messages == ()
    assert "voice_unconfirmed" in first.errors[0]
    assert [call[0] for call in sender.calls] == ["voice"]


@pytest.mark.anyio
async def test_concurrent_replay_sends_each_bubble_only_once():
    draft = create_reply_draft("第一句\n第二句", draft_id="concurrent-replay")
    sender = GuardSender()
    executor = DeliveryExecutor(sender)
    plan = create_delivery_plan(draft, quote_message_id=12345)

    first, second = await asyncio.gather(
        executor.execute(draft, plan), executor.execute(draft, plan)
    )

    assert first is second
    assert len([call for call in sender.calls if call[0] == "text"]) == 2
    assert [call[2] for call in sender.calls] == [12345, None]


@pytest.mark.anyio
async def test_recorder_failure_cannot_trigger_physical_replay():
    class BrokenRecorder:
        def __init__(self):
            self.calls = 0

        async def record_delivery(self, result):
            self.calls += 1
            raise RuntimeError("database locked")

    draft = create_reply_draft("已经发出", draft_id="recorder-failure")
    sender = GuardSender()
    recorder = BrokenRecorder()
    executor = DeliveryExecutor(sender, recorder=recorder)
    plan = create_delivery_plan(draft)

    with pytest.raises(RuntimeError, match="database locked"):
        await executor.execute(draft, plan)
    result = await executor.execute(draft, plan)

    assert result.status == "sent"
    assert len(sender.calls) == 1
    assert recorder.calls == 1


@pytest.mark.anyio
async def test_same_draft_id_cannot_reuse_cached_result_for_changed_text():
    sender = GuardSender()
    executor = DeliveryExecutor(sender)
    original = create_reply_draft("原稿", draft_id="same-id")
    original_plan = create_delivery_plan(original)
    await executor.execute(original, original_plan)

    changed = create_reply_draft("被替换的正文", draft_id="same-id")
    changed_plan = create_delivery_plan(changed)
    with pytest.raises(ValueError, match="不得替换原稿"):
        await executor.execute(changed, changed_plan)
    assert len(sender.calls) == 1


def test_action_plan_cannot_reorder_frozen_bubbles():
    from companion.kernel.delivery import validate_action_delivery_plan

    draft = create_reply_draft("第一句\n第二句", draft_id="frozen-order")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=tuple(DeliveryAction(b.bubble_id) for b in reversed(draft.bubbles)),
    )
    with pytest.raises(ValueError, match="不得重排"):
        validate_action_delivery_plan(plan, draft)


@pytest.mark.anyio
async def test_nonempty_raw_draft_cannot_silent_even_without_bubbles():
    empty = create_reply_draft("", draft_id="raw-nonempty")
    malformed = replace(empty, text="不能丢掉的正文")
    executor = DeliveryExecutor(GuardSender())
    with pytest.raises(ValueError, match="不得被后置判为沉默"):
        await executor.execute(
            malformed,
            DeliveryPlan(draft_id=malformed.draft_id, actions=(), silent=True),
        )


def test_stage1_validator_still_rejects_silent_plan():
    draft = create_reply_draft("不能沉默", draft_id="stage1-silent")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=tuple(DeliveryAction(b.bubble_id) for b in draft.bubbles),
        silent=True,
    )
    with pytest.raises(ValueError, match="第一阶段"):
        validate_delivery_plan(plan, draft)
