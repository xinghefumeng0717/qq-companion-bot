"""新内核生产桥接：纯文字单模型、原稿投递、特殊输入事前回退。"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from companion.kernel.contracts import (
    DeliveryPlan,
    IncomingMessage,
    KernelResult,
    PsychologicalSnapshot,
    StandardizedTurnInput,
)
from companion.kernel.delivery import plan_explicit_voice, validate_action_delivery_plan
from companion.kernel.production import KernelTurnHandler, can_use_kernel_text, chunks_from_plan
from companion.kernel.semantic import build_turn_input, create_delivery_plan, create_reply_draft


def _snapshot():
    return PsychologicalSnapshot(
        turn_id="turn-production", frozen_at=datetime(2026, 10, 26, 12, 0),
        state_version=1, stage_index=1, prompt_hint="平静",
        action_context={}, affection={}, mood={},
    )


def _result(turn_input, *, passed=True):
    draft = create_reply_draft("我在这里的\n你也不看看几点了") if passed else create_reply_draft(" ")
    plan = create_delivery_plan(draft, turn_input.reply_to_message_id) if passed else DeliveryPlan(draft_id=draft.draft_id, actions=())
    return KernelResult(
        trace_id="trace-production", turn_input=turn_input, snapshot=_snapshot(),
        draft=draft, plan=plan, validation_passed=passed,
        validation_errors=() if passed else ("empty_draft",),
    )


class FakeKernel:
    def __init__(self):
        self.calls = []

    async def handle_turn(self, turn_input):
        self.calls.append(turn_input)
        return _result(turn_input, passed=bool(turn_input.text.strip()))


class FakeLegacy:
    def __init__(self):
        self.calls = []

    async def handle_turn(self, user_text, image_path, quote_targets=None):
        self.calls.append((user_text, image_path, quote_targets))


class FakeMemory:
    def __init__(self):
        self.saved = []
        self.reinforced = []

    async def save_turn_pair(self, **kwargs):
        self.saved.append(kwargs)

    async def reinforce_memories(self, text):
        self.reinforced.append(text)

    async def get_recent_turns(self, limit=10):
        return []


class FakeObserver:
    def __init__(self):
        self.calls = []

    async def settle_turn(self, **kwargs):
        self.calls.append(kwargs)


class FakeProactive:
    def __init__(self):
        self.reset_count = 0

    async def reset_unanswered_count(self):
        self.reset_count += 1


class FakeReplier:
    def __init__(self):
        self.chunks = []

    async def send_reply_chunks(self, chunks, send_chunk_fn):
        self.chunks.append(chunks)
        for chunk in chunks:
            await send_chunk_fn(chunk)


def _handler(kernel, legacy):
    return KernelTurnHandler(
        kernel=kernel,
        legacy_handler=legacy,
        memory=FakeMemory(),
        observer=FakeObserver(),
        proactive=FakeProactive(),
        replier=FakeReplier(),
        send_chunk_fn=lambda chunk: _send(chunk),
    )


async def _send(chunk):
    return None


@pytest.mark.anyio
async def test_plain_text_uses_kernel_once_and_preserves_draft_and_quote():
    kernel, legacy = FakeKernel(), FakeLegacy()
    meetup_calls = []

    async def process_due():
        meetup_calls.append(1)

    legacy.assembler = SimpleNamespace(meetups=SimpleNamespace(process_due=process_due))
    memory = FakeMemory()
    observer = FakeObserver()
    replier = FakeReplier()
    sent = []

    async def send(chunk):
        sent.append(chunk)

    handler = KernelTurnHandler(
        kernel=kernel, legacy_handler=legacy, memory=memory,
        observer=observer, proactive=FakeProactive(), replier=replier,
        send_chunk_fn=send,
    )
    batch = [
        {"index": 1, "text": "在吗", "message_id": 4001, "has_image": False},
        {"index": 2, "text": "睡了吗", "message_id": 5002, "has_image": False},
    ]
    await handler.handle_turn("在吗\n睡了吗", None, batch)
    await asyncio.sleep(0)

    assert len(kernel.calls) == 1
    assert kernel.calls[0].reply_to_message_id == 5002
    assert legacy.calls == []
    assert [chunk["content"] for chunk in sent] == ["我在这里的", "你也不看看几点了"]
    assert sent[0]["_quote"] == {"message_id": 5002}
    assert memory.saved[-1]["bot_msg"] == "我在这里的\n你也不看看几点了"
    assert len(observer.calls) == 1
    assert meetup_calls == [1]


@pytest.mark.anyio
async def test_invalid_draft_never_falls_back_to_second_model():
    class EmptyKernel(FakeKernel):
        async def handle_turn(self, turn_input):
            self.calls.append(turn_input)
            return _result(turn_input, passed=False)

    kernel, legacy = EmptyKernel(), FakeLegacy()
    memory = FakeMemory()
    sent = []

    async def send(chunk):
        sent.append(chunk)

    handler = KernelTurnHandler(
        kernel=kernel, legacy_handler=legacy, memory=memory,
        observer=FakeObserver(), proactive=FakeProactive(), replier=FakeReplier(),
        send_chunk_fn=send,
    )
    await handler.handle_turn("在吗", None)

    assert len(kernel.calls) == 1
    assert legacy.calls == []
    assert sent == []
    assert memory.saved[-1]["bot_msg"] is None


@pytest.mark.parametrize(
    "text,image",
    [
        ("看看这张图", "image.jpg"),
        ("[face:吃糖]", None),
        ("[吃糖][吃糖]", None),
        ("（他引用了你之前说的「旧话」）新话", None),
    ],
)
@pytest.mark.anyio
async def test_special_inputs_use_legacy_before_any_model_call(text, image):
    kernel, legacy = FakeKernel(), FakeLegacy()
    handler = _handler(kernel, legacy)

    assert can_use_kernel_text(text, image, None) is False
    await handler.handle_turn(text, image, None)
    assert kernel.calls == []
    assert len(legacy.calls) == 1


def test_explicit_voice_plan_changes_only_channel_not_text_or_order():
    turn = build_turn_input("s", [IncomingMessage(message_id=1, text="发条语音")])
    draft = create_reply_draft("第一句台词\n第二句")
    original = create_delivery_plan(draft, 1)

    planned = plan_explicit_voice(draft, original, requested=True, allowed=True, max_chars=60)

    assert [action.channel for action in original.actions] == ["text", "text"]
    assert [action.channel for action in planned.actions] == ["voice", "text"]
    assert [action.bubble_id for action in planned.actions] == [bubble.bubble_id for bubble in draft.bubbles]
    assert [bubble.text for bubble in draft.bubbles] == ["第一句台词", "第二句"]
    assert planned.quote_message_id == 1
    validate_action_delivery_plan(planned, draft)


def test_too_short_bubble_is_skipped_without_cutting_or_reordering():
    draft = create_reply_draft("嗯\n这么晚了还想听声音，睡不着？")
    original = create_delivery_plan(draft, None)

    planned = plan_explicit_voice(draft, original, requested=True, allowed=True, max_chars=60)

    assert [action.channel for action in planned.actions] == ["text", "voice"]
    assert [bubble.text for bubble in draft.bubbles] == ["嗯", "这么晚了还想听声音，睡不着？"]


def test_delivery_chunk_matrix_never_combines_reply_with_record():
    draft = create_reply_draft("嗯\n第二段语音\n第三段文字")
    original = create_delivery_plan(draft, 5002)
    planned = plan_explicit_voice(draft, original, requested=True, allowed=True, max_chars=60)

    chunks = chunks_from_plan(draft, planned)

    assert [chunk["type"] for chunk in chunks] == ["text", "voice", "text"]
    assert [chunk["content"] for chunk in chunks] == ["嗯", "第二段语音", "第三段文字"]
    assert chunks[0]["_quote"] == {"message_id": 5002}
    assert "_quote" not in chunks[1]
    assert "_quote" not in chunks[2]
    assert sum("_quote" in chunk for chunk in chunks) == 1

    voice_only = plan_explicit_voice(
        create_reply_draft("完整一句话"),
        create_delivery_plan(create_reply_draft("完整一句话"), 5002),
        requested=True,
        allowed=True,
        max_chars=60,
    )
    only_chunks = chunks_from_plan(create_reply_draft("完整一句话"), voice_only)
    assert len(only_chunks) == 1
    assert "_quote" not in only_chunks[0]


def test_overlong_bubble_never_gets_truncated_for_voice():
    draft = create_reply_draft("这是一段明显超过语音字数限制的完整气泡，绝不为合成而截断。")
    original = create_delivery_plan(draft, None)

    planned = plan_explicit_voice(draft, original, requested=True, allowed=True, max_chars=10)

    assert planned == original
    assert [action.channel for action in planned.actions] == ["text"]
    assert draft.bubbles[0].text == "这是一段明显超过语音字数限制的完整气泡，绝不为合成而截断。"


def _voice_legacy(allowed=True):
    legacy = FakeLegacy()

    class Planner:
        frequency = "explicit"

        @staticmethod
        def allows(text):
            return True

    legacy.voice_planner = Planner()

    async def check_gate(text):
        return allowed, "test gate"

    legacy._check_voice_gate = check_gate
    legacy._voice_max_chars = lambda: 60
    return legacy


@pytest.mark.anyio
async def test_explicit_voice_uses_kernel_once_and_complete_bubble():
    kernel, legacy = FakeKernel(), _voice_legacy(allowed=True)
    sent = []

    async def send(chunk):
        if chunk["type"] == "voice":
            chunk["_voice_delivery"] = "sent"
        sent.append(chunk)

    handler = KernelTurnHandler(
        kernel=kernel, legacy_handler=legacy, memory=FakeMemory(),
        observer=FakeObserver(), proactive=FakeProactive(), replier=FakeReplier(),
        send_chunk_fn=send,
    )
    assert can_use_kernel_text("发条语音听听[吃糖]", None, None) is True
    await handler.handle_turn("发条语音听听[吃糖]", None)

    assert len(kernel.calls) == 1
    assert legacy.calls == []
    assert [chunk["type"] for chunk in sent] == ["voice", "text"]
    assert sent[0]["content"] == "我在这里的"
    assert sent[1]["content"] == "你也不看看几点了"
    assert handler._last_voice_sent is True
    assert handler._last_voice_failed is False


@pytest.mark.anyio
async def test_voice_gate_denial_keeps_all_original_text_without_second_model():
    kernel, legacy = FakeKernel(), _voice_legacy(allowed=False)
    sent = []

    async def send(chunk):
        sent.append(chunk)

    handler = KernelTurnHandler(
        kernel=kernel, legacy_handler=legacy, memory=FakeMemory(),
        observer=FakeObserver(), proactive=FakeProactive(), replier=FakeReplier(),
        send_chunk_fn=send,
    )
    await handler.handle_turn("发条语音听听", None)

    assert len(kernel.calls) == 1
    assert legacy.calls == []
    assert [chunk["type"] for chunk in sent] == ["text", "text"]
    assert [chunk["content"] for chunk in sent] == ["我在这里的", "你也不看看几点了"]
    assert handler._last_voice_sent is False
    assert handler._last_voice_failed is True


@pytest.mark.anyio
async def test_voice_failure_recovery_returns_original_without_model():
    kernel, legacy = FakeKernel(), _voice_legacy(allowed=True)
    handler = _handler(kernel, legacy)

    recovered = await handler.recover_voice_failure("同一段原文", "合成失败")

    assert recovered == "同一段原文"
    assert legacy.calls == []


def test_kernel_switch_is_opt_in():
    from companion.config import ReplyConfig

    assert ReplyConfig().use_kernel is False
