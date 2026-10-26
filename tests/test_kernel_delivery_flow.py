"""第二阶段对话事故回放：断言实际投递，而非仅检查计划。"""

import pytest

from companion.kernel.contracts import DeliveryAction, DeliveryPlan, SendReceipt
from companion.kernel.delivery import DeliveryExecutor
from companion.kernel.semantic import create_reply_draft


class FlowSender:
    def __init__(self, *, fail_voice=False, fail_sticker=False):
        self.fail_voice = fail_voice
        self.fail_sticker = fail_sticker
        self.calls = []
        self.next_id = 9000

    def _receipt(self):
        self.next_id += 1
        return SendReceipt(message_id=self.next_id)

    async def send_text(self, text, quote_message_id=None):
        self.calls.append(("text", text, quote_message_id))
        return self._receipt()

    async def send_voice(self, text, quote_message_id=None, **options):
        self.calls.append(("voice", text, quote_message_id, options))
        if self.fail_voice and text == "嗯，晚安":
            raise RuntimeError("offline TTS rejected")
        return self._receipt()

    async def send_sticker(self, sticker):
        self.calls.append(("sticker", sticker))
        if self.fail_sticker:
            raise RuntimeError("missing sticker asset")
        return self._receipt()


class FlowRecorder:
    def __init__(self):
        self.results = []

    async def record_delivery(self, result):
        self.results.append(result)


def action_plan(draft, channels, *, quote=None):
    return DeliveryPlan(
        draft_id=draft.draft_id,
        actions=tuple(
            DeliveryAction(bubble.bubble_id, channel, extra)
            for bubble, (channel, extra) in zip(draft.bubbles, channels)
        ),
        quote_message_id=quote,
    )


@pytest.mark.anyio
async def test_goodnight_voice_failure_returns_original_text_in_place_with_quote():
    draft = create_reply_draft("嗯，晚安\n明天见", draft_id="flow-goodnight")
    sender = FlowSender(fail_voice=True)
    executor = DeliveryExecutor(sender)
    plan = action_plan(draft, [("voice", {}), ("voice", {})], quote=1001)

    result = await executor.execute(draft, plan)

    assert [call[0] for call in sender.calls] == ["voice", "text", "voice"]
    assert [message.text for message in result.messages] == ["嗯，晚安", "明天见"]
    assert [message.channel for message in result.messages] == ["text", "voice"]
    assert result.messages[0].fallback_from == "voice"
    assert result.messages[0].quote_message_id == 1001
    assert result.messages[1].quote_message_id is None
    assert result.status == "degraded"


@pytest.mark.anyio
async def test_voice_and_sticker_combo_preserves_whole_bubble_and_records_actual():
    draft = create_reply_draft("今天也辛苦你了", draft_id="flow-combo")
    sender = FlowSender()
    recorder = FlowRecorder()
    executor = DeliveryExecutor(sender, recorder=recorder)
    plan = action_plan(
        draft, [("voice", {"sticker": "candy", "voice": "soft"})], quote=1002
    )

    result = await executor.execute(draft, plan)

    assert sender.calls[0] == ("sticker", "candy")
    assert sender.calls[1] == ("voice", "今天也辛苦你了", 1002, {"voice": "soft"})
    assert [(m.channel, m.attachment) for m in result.messages] == [
        ("sticker", True), ("voice", False)
    ]
    assert recorder.results == [result]
    assert all(m.message_id is not None for m in recorder.results[0].messages)


@pytest.mark.anyio
async def test_sticker_failure_falls_back_without_repeating_previous_bubble():
    draft = create_reply_draft("第一句\n第二句\n第三句", draft_id="flow-partial")
    sender = FlowSender(fail_sticker=True)
    executor = DeliveryExecutor(sender)
    plan = action_plan(
        draft,
        [("text", {}), ("sticker", {"sticker": "candy"}), ("voice", {})],
    )

    result = await executor.execute(draft, plan)

    original_calls = [call for call in sender.calls if call[0] in {"text", "voice"}]
    assert original_calls == [("text", "第一句", None), ("text", "第二句", None), ("text", "第三句", None)]
    assert [m.text for m in result.messages] == ["第一句", "第二句", "第三句"]
    assert len({m.bubble_id for m in result.messages}) == 3
    assert result.status == "degraded"


@pytest.mark.anyio
async def test_first_sticker_failure_turns_entire_plan_into_plain_text():
    draft = create_reply_draft("保留全部原文", draft_id="flow-full-fallback")
    sender = FlowSender(fail_sticker=True)
    executor = DeliveryExecutor(sender)
    plan = action_plan(draft, [("voice", {"sticker": "candy"})])

    result = await executor.execute(draft, plan)

    assert [call[0] for call in sender.calls] == ["sticker", "text"]
    assert len(result.messages) == 1
    assert result.messages[0].text == "保留全部原文"
    assert result.messages[0].channel == "text"
    assert result.status == "degraded"


@pytest.mark.anyio
async def test_explicit_silence_sends_and_records_nothing_but_nonempty_cannot_silence():
    empty = create_reply_draft("", draft_id="flow-silent")
    sender = FlowSender()
    recorder = FlowRecorder()
    executor = DeliveryExecutor(sender, recorder=recorder)
    result = await executor.execute(
        empty, DeliveryPlan(draft_id=empty.draft_id, actions=(), silent=True)
    )

    assert result.status == "silent"
    assert result.messages == ()
    assert sender.calls == []
    assert recorder.results == []

    nonempty = create_reply_draft("嗯，晚安", draft_id="flow-not-silent")
    with pytest.raises(ValueError, match="不得被后置判为沉默"):
        await executor.execute(
            nonempty,
            DeliveryPlan(draft_id=nonempty.draft_id, actions=(), silent=True),
        )
