"""离线生产 sender：假 OneBot/TTS，不连接真实 QQ，不接生产主链。"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from companion.config import ReplyConfig
from companion.kernel.contracts import DeliveryAction, DeliveryPlan, SendReceipt
from companion.kernel.delivery import (
    DeliveryExecutor,
    DeliveryRejectedError,
    UnconfirmedDeliveryError,
)
from companion.kernel.semantic import create_reply_draft
from companion.main import CompanionBot
from companion.replier import Replier


class FakeOneBot:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = []
        self.next_id = 100

    async def send_private_msg_receipt(self, user_id, segments):
        self.calls.append(segments)
        if self.mode == "unconfirmed":
            raise UnconfirmedDeliveryError("offline unknown")
        if self.mode == "reject_quote" and any(s.get("type") == "reply" for s in segments):
            raise DeliveryRejectedError("quote rejected")
        self.next_id += 1
        return SendReceipt(message_id=self.next_id)


class FakeTTS:
    def __init__(self, *, synth_error=None, bump_error=None):
        self.synth_error = synth_error
        self.bump_error = bump_error
        self.synth_calls = []
        self.bump_calls = 0
        self.cleaned = []

    async def synthesize(self, text):
        self.synth_calls.append(text)
        if self.synth_error:
            raise self.synth_error
        return "offline.mp3"

    async def bump_daily(self):
        self.bump_calls += 1
        if self.bump_error:
            raise self.bump_error

    def cleanup(self, path):
        self.cleaned.append(path)


def make_bot(mode="ok", tts=None):
    bot = object.__new__(CompanionBot)
    bot.config = SimpleNamespace(account=SimpleNamespace(allowed_user_id=1))
    bot.onebot = FakeOneBot(mode)
    bot.tts = tts or FakeTTS()
    bot.replier = Replier(ReplyConfig(chunk_delay_min=0, chunk_delay_max=0), None)
    return bot


@pytest.mark.anyio
async def test_text_quote_rejected_retries_plain_text_once():
    bot = make_bot("reject_quote")
    receipt = await bot.send_text("正文不变", 123)
    assert receipt.actual_quote_message_id is None
    assert len(bot.onebot.calls) == 2
    assert bot.onebot.calls[0][0]["type"] == "reply"
    assert bot.onebot.calls[1][0] == {"type": "text", "data": {"text": "正文不变"}}


@pytest.mark.anyio
async def test_text_unconfirmed_never_retries_without_quote():
    bot = make_bot("unconfirmed")
    with pytest.raises(UnconfirmedDeliveryError):
        await bot.send_text("正文不变", 123)
    assert len(bot.onebot.calls) == 1


@pytest.mark.anyio
async def test_voice_is_record_only_and_cleans_after_confirmation(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("companion.main.asyncio.sleep", no_sleep)
    bot = make_bot()
    receipt = await bot.send_voice("晚安", 123)
    assert receipt.actual_quote_message_id is None
    assert bot.onebot.calls[0][0]["type"] == "record"
    assert bot.tts.bump_calls == 1
    assert bot.tts.cleaned == ["offline.mp3"]


@pytest.mark.anyio
async def test_voice_confirmed_bump_failure_does_not_resend(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("companion.main.asyncio.sleep", no_sleep)
    bot = make_bot(tts=FakeTTS(bump_error=RuntimeError("ledger")))
    receipt = await bot.send_voice("晚安")
    assert receipt.message_id is not None
    assert len(bot.onebot.calls) == 1
    assert bot.tts.cleaned == ["offline.mp3"]


@pytest.mark.anyio
async def test_cancel_after_voice_receipt_preserves_actual_message(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("companion.main.asyncio.sleep", no_sleep)
    entered = asyncio.Event()

    class BlockingBumpTTS(FakeTTS):
        async def bump_daily(self):
            entered.set()
            await asyncio.Event().wait()

    bot = make_bot(tts=BlockingBumpTTS())
    draft = create_reply_draft("晚安", draft_id="sender-cancel-after-receipt")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(draft.bubbles[0].bubble_id, "voice"),),
    )
    executor = DeliveryExecutor(bot)
    task = asyncio.create_task(executor.execute(draft, plan))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cached = executor._results[draft.draft_id]
    assert cached.status == "failed"
    assert [(m.channel, m.text) for m in cached.messages] == [("voice", "晚安")]
    assert cached.messages[0].message_id == 101
    assert "voice_confirmed_cancelled" in cached.errors[0]
    replay = await executor.execute(draft, plan)
    assert replay is cached
    assert len(bot.onebot.calls) == 1
    assert bot.tts.cleaned == ["offline.mp3"]


@pytest.mark.anyio
async def test_voice_unconfirmed_never_falls_back_to_text(monkeypatch):
    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr("companion.main.asyncio.sleep", no_sleep)
    bot = make_bot("unconfirmed")
    draft = create_reply_draft("晚安", draft_id="sender-voice-unconfirmed")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(draft.bubbles[0].bubble_id, "voice"),),
        quote_message_id=123,
    )
    result = await DeliveryExecutor(bot).execute(draft, plan)
    assert result.status == "failed"
    assert result.messages == ()
    assert len(bot.onebot.calls) == 1
    assert bot.onebot.calls[0][0]["type"] == "record"
    assert bot.tts.bump_calls == 0


@pytest.mark.anyio
async def test_voice_rejection_executor_sends_original_text_with_quote():
    bot = make_bot(tts=FakeTTS(synth_error=DeliveryRejectedError("no tts")))
    draft = create_reply_draft("晚安", draft_id="sender-voice-fallback")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(draft.bubbles[0].bubble_id, "voice"),),
        quote_message_id=123,
    )
    result = await DeliveryExecutor(bot).execute(draft, plan)
    assert result.status == "degraded"
    assert result.messages[0].text == "晚安"
    assert result.messages[0].channel == "text"
    assert result.messages[0].fallback_from == "voice"
    assert result.messages[0].quote_message_id == 123


@pytest.mark.anyio
async def test_voice_fallback_uses_only_one_replier_delay():
    bot = make_bot(tts=FakeTTS(synth_error=DeliveryRejectedError("no tts")))
    delays = []

    async def record_delay(chunk, index):
        delays.append((chunk["type"], index))

    bot.replier.wait_before_chunk = record_delay
    draft = create_reply_draft("晚安", draft_id="sender-one-delay")
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(draft.bubbles[0].bubble_id, "voice"),),
    )
    await DeliveryExecutor(bot, before_send=bot.before_delivery_send).execute(draft, plan)
    assert delays == [("voice", 0)]


@pytest.mark.anyio
async def test_replier_delay_formula_shared_and_first_chunk_halved(monkeypatch):
    monkeypatch.setattr("companion.replier.random.uniform", lambda _a, _b: 1.0)
    waits = []

    async def record_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("companion.replier.asyncio.sleep", record_sleep)
    replier = Replier(ReplyConfig(), None)
    await replier.wait_before_chunk({"type": "text", "content": "十个字十个字十个"}, 0)
    await replier.wait_before_chunk({"type": "voice", "content": "语音正文长度不影响权重"}, 1)
    assert waits == [pytest.approx(0.62), pytest.approx(1.15)]


@pytest.mark.anyio
async def test_sticker_uses_real_file_and_no_quote():
    bot = make_bot()
    receipt = await bot.send_sticker({"file": "stickers/si.png"})
    assert receipt.actual_quote_message_id is None
    assert bot.onebot.calls[0][0]["type"] == "image"
    assert "stickers/si.png" in str(bot.onebot.calls[0][0])


@pytest.mark.anyio
async def test_cancel_second_action_preserves_first_and_replay_never_resends():
    draft = create_reply_draft("第一句\n第二句", draft_id="sender-cancel")
    calls = []

    class Sender:
        async def send_text(self, text, quote_message_id=None):
            calls.append(text)
            return SendReceipt(
                message_id=len(calls), actual_quote_message_id=quote_message_id
            )

    async def before_send(_bubble, _action, index):
        if index == 1:
            await asyncio.Event().wait()

    executor = DeliveryExecutor(Sender(), before_send=before_send)
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=tuple(DeliveryAction(b.bubble_id, "text") for b in draft.bubbles),
    )
    task = asyncio.create_task(executor.execute(draft, plan))
    for _ in range(20):
        if calls == ["第一句"]:
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    cached = executor._results[draft.draft_id]
    assert cached.status == "failed"
    assert [m.text for m in cached.messages] == ["第一句"]
    assert "cancelled" in cached.errors
    replay = await executor.execute(draft, plan)
    assert replay is cached
    assert calls == ["第一句"]
