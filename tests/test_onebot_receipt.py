"""单次 OneBot 真实回执入口：全部使用假 WS，不连接真实 QQ。"""
from __future__ import annotations

import asyncio
import json

import pytest

from companion.kernel.delivery import UnconfirmedDeliveryError
from companion.onebot import OneBotClient


class FakeWS:
    def __init__(self, *, send_error: Exception | None = None):
        self.closed = False
        self.sent: list[str] = []
        self.send_error = send_error

    async def send_str(self, payload: str):
        self.sent.append(payload)
        if self.send_error is not None:
            raise self.send_error


def make_client(ws: FakeWS | None = None) -> OneBotClient:
    client = object.__new__(OneBotClient)
    client._ws = ws
    client._pending_echoes = {}
    return client


async def answer(client: OneBotClient, response: dict):
    """等待真实 echo 生成，再走现有回包分发入口。"""
    task = asyncio.create_task(client.send_private_msg_receipt(1, [{"type": "text", "data": {"text": "hi"}}]))
    for _ in range(20):
        if client._pending_echoes:
            break
        await asyncio.sleep(0)
    assert len(client._pending_echoes) == 1
    echo = next(iter(client._pending_echoes))
    await client._handle_raw_message(json.dumps({"echo": echo, **response}))
    return task


@pytest.mark.anyio
async def test_receipt_success_returns_real_message_id():
    client = make_client(FakeWS())
    task = await answer(client, {"status": "ok", "retcode": 0, "data": {"message_id": 7788}})
    receipt = await task
    assert receipt.message_id == 7788
    assert len(client._ws.sent) == 1
    assert client._pending_echoes == {}


@pytest.mark.anyio
async def test_receipt_success_allows_missing_id():
    client = make_client(FakeWS())
    task = await answer(client, {"status": "ok", "retcode": 0, "data": {}})
    receipt = await task
    assert receipt.message_id is None
    assert len(client._ws.sent) == 1


@pytest.mark.anyio
@pytest.mark.parametrize("raw_id", [42.9, 42.0, True, "42.9", "not-an-id", "²"])
async def test_receipt_invalid_message_id_is_none_not_fabricated(raw_id):
    client = make_client(FakeWS())
    task = await answer(client, {"status": "ok", "retcode": 0, "data": {"message_id": raw_id}})
    receipt = await task
    assert receipt.message_id is None


@pytest.mark.anyio
async def test_receipt_integer_string_message_id_is_accepted():
    client = make_client(FakeWS())
    task = await answer(client, {"status": "ok", "retcode": 0, "data": {"message_id": "42"}})
    receipt = await task
    assert receipt.message_id == 42


@pytest.mark.anyio
async def test_receipt_explicit_rejection_is_not_success():
    client = make_client(FakeWS())
    task = await answer(client, {"status": "failed", "retcode": 1200, "data": None})
    with pytest.raises(RuntimeError, match="明确拒绝"):
        await task
    assert len(client._ws.sent) == 1
    assert client._pending_echoes == {}


@pytest.mark.anyio
@pytest.mark.parametrize(
    "response",
    [
        {"status": "ok", "retcode": 1200, "data": {"message_id": 1}},
        {"data": {"message_id": 1}},
        {"status": "ok", "retcode": 0, "data": ["not-a-dict"]},
        {"status": "ok", "retcode": 0, "data": []},
        {"status": "async", "retcode": 1, "data": None},
        {"status": "unknown", "data": None},
        {"retcode": [], "data": None},
        {"retcode": False, "data": {}},
    ],
)
async def test_receipt_contradictory_or_malformed_response_is_unconfirmed(response):
    client = make_client(FakeWS())
    task = await answer(client, response)
    with pytest.raises(UnconfirmedDeliveryError):
        await task
    assert len(client._ws.sent) == 1
    assert client._pending_echoes == {}


@pytest.mark.anyio
async def test_receipt_disconnected_fails_before_send():
    client = make_client(None)
    with pytest.raises(RuntimeError, match="尚未提交"):
        await client.send_private_msg_receipt(1, [])
    assert client._pending_echoes == {}


@pytest.mark.anyio
async def test_receipt_send_error_is_unconfirmed_and_never_retried():
    ws = FakeWS(send_error=ConnectionError("offline after submit"))
    client = make_client(ws)
    with pytest.raises(UnconfirmedDeliveryError):
        await client.send_private_msg_receipt(1, [])
    assert len(ws.sent) == 1
    assert client._pending_echoes == {}


@pytest.mark.anyio
async def test_receipt_timeout_cleans_pending_and_late_echo_does_not_resend(monkeypatch):
    async def timeout(_future, timeout):
        raise asyncio.TimeoutError

    monkeypatch.setattr("companion.onebot.asyncio.wait_for", timeout)
    client = make_client(FakeWS())
    with pytest.raises(UnconfirmedDeliveryError):
        await client.send_private_msg_receipt(1, [])
    assert len(client._ws.sent) == 1
    assert client._pending_echoes == {}

    sent_before = list(client._ws.sent)
    await client._handle_raw_message(json.dumps({
        "echo": "late-unknown-echo", "status": "ok", "retcode": 0, "data": {"message_id": 9},
    }))
    assert client._ws.sent == sent_before


@pytest.mark.anyio
async def test_receipt_cancellation_cleans_pending_without_retry():
    client = make_client(FakeWS())
    task = asyncio.create_task(client.send_private_msg_receipt(1, []))
    for _ in range(20):
        if client._pending_echoes:
            break
        await asyncio.sleep(0)
    assert len(client._pending_echoes) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(client._ws.sent) == 1
    assert client._pending_echoes == {}
