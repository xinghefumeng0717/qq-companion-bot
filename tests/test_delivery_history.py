"""DeliveryResult 到干净正文/实际动作的投影与动作记录；不接真实 QQ 或模型。"""
from __future__ import annotations

from datetime import datetime

import pytest

from companion.kernel.contracts import DeliveryAction, DeliveryPlan, DeliveryResult, SentMessage
from companion.kernel.production import (
    body_from_delivery_result,
    delivery_actions_from_result,
    voice_status_from_delivery_result,
)
from companion.kernel.semantic import create_reply_draft
from companion.runtime_state import RuntimeEventStore
from helpers import close_db, make_db


def make_plan(channels):
    draft = create_reply_draft("\n".join(text for text, _ in channels))
    actions = []
    for bubble, (text, channel) in zip(draft.bubbles, channels):
        extra = {"sticker": {"desc": "祀坏笑", "file": "stickers/si.png"}} if channel == "sticker" else {}
        actions.append(DeliveryAction(bubble.bubble_id, "voice" if channel == "voice" else "text", extra))
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=tuple(actions),
        quote_message_id=123,
    )
    return draft, plan


def actual(draft, text, channel="text", *, attachment=False, fallback_from=None, status=None):
    bubble = next(b for b in draft.bubbles if b.text == text)
    message = SentMessage(
        bubble_id=bubble.bubble_id,
        channel=channel,
        text="" if attachment else text,
        message_id=100,
        attachment=attachment,
        fallback_from=fallback_from,
    )
    return DeliveryResult(draft_id=draft.draft_id, status=status or "sent", messages=(message,))


def test_actual_text_projects_plain_history_in_order():
    draft, plan = make_plan([("第一句", "text"), ("第二句", "text")])
    first = actual(draft, "第一句")
    second = actual(draft, "第二句")
    result = DeliveryResult(
        draft_id=draft.draft_id, status="sent", messages=first.messages + second.messages
    )
    assert body_from_delivery_result(result) == "第一句\n第二句"
    assert delivery_actions_from_result(result, plan) == []
    assert voice_status_from_delivery_result(result, plan) is None


def test_actual_voice_keeps_body_without_carrier_prefix_and_records_action():
    draft, plan = make_plan([("晚安", "voice")])
    result = actual(draft, "晚安", "voice")
    assert body_from_delivery_result(result) == "晚安"
    actions = delivery_actions_from_result(result, plan)
    assert len(actions) == 1
    assert actions[0]["channel"] == "voice"
    assert actions[0]["content"] == "晚安"
    assert actions[0]["metadata"]["draft_id"] == draft.draft_id
    assert actions[0]["metadata"]["message_id"] == 100
    assert voice_status_from_delivery_result(result, plan) == "sent"


def test_voice_fallback_projects_plain_text_without_sent_voice_action():
    draft, plan = make_plan([("晚安", "voice")])
    result = actual(draft, "晚安", "text", fallback_from="voice", status="degraded")
    assert body_from_delivery_result(result) == "晚安"
    assert delivery_actions_from_result(result, plan) == []
    assert voice_status_from_delivery_result(result, plan) == "text_fallback"


def test_sticker_attachment_is_action_not_body_and_uses_actual_bubble_resource():
    draft, plan = make_plan([("正文", "sticker")])
    sticker = actual(draft, "正文", "sticker", attachment=True)
    text = actual(draft, "正文", "text")
    result = DeliveryResult(
        draft_id=draft.draft_id, status="sent", messages=sticker.messages + text.messages
    )
    assert body_from_delivery_result(result) == "正文"
    actions = delivery_actions_from_result(result, plan)
    assert len(actions) == 1
    assert actions[0]["channel"] == "sticker"
    assert actions[0]["content"] == "祀坏笑"
    assert actions[0]["metadata"]["sticker_file"] == "stickers/si.png"


def test_action_projection_rejects_mismatched_draft_plan():
    draft, plan = make_plan([("正文", "sticker")])
    result = actual(draft, "正文", "sticker", attachment=True)
    wrong = DeliveryPlan(draft_id="another-draft", actions=plan.actions)
    with pytest.raises(ValueError, match="draft_id 不匹配"):
        delivery_actions_from_result(result, wrong)


@pytest.mark.parametrize("status", ["failed", "silent"])
def test_failed_or_silent_never_falls_back_to_draft(status):
    draft, plan = make_plan([("没有发出去", "voice")])
    result = DeliveryResult(draft_id=draft.draft_id, status=status, messages=())
    assert body_from_delivery_result(result) == ""
    assert delivery_actions_from_result(result, plan) == []
    assert voice_status_from_delivery_result(result, plan) == "failed"


def test_cancel_after_confirmed_voice_still_projects_actual_body_and_action():
    draft, plan = make_plan([("晚安", "voice")])
    result = actual(draft, "晚安", "voice", status="failed")
    assert body_from_delivery_result(result) == "晚安"
    assert len(delivery_actions_from_result(result, plan)) == 1
    assert voice_status_from_delivery_result(result, plan) == "sent"


@pytest.mark.anyio
async def test_delivery_action_store_writes_and_reads_details_without_changing_voice_summary():
    draft, plan = make_plan([("晚安", "voice"), ("正文", "sticker")])
    voice = actual(draft, "晚安", "voice")
    sticker = actual(draft, "正文", "sticker", attachment=True)
    result = DeliveryResult(
        draft_id=draft.draft_id, status="failed", messages=voice.messages + sticker.messages,
        errors=("later text failed",),
    )
    actions = delivery_actions_from_result(result, plan)
    db = await make_db()
    try:
        store = RuntimeEventStore(db)
        await store.init()
        now = datetime(2026, 11, 7, 12, 0)
        assert await store.record_delivery_actions(actions, now=now) == 2

        recent = await store.recent_delivery_actions()
        assert [row["subject"] for row in recent] == ["sticker", "voice"]
        assert all(row["kind"] == "delivery_action" and row["status"] == "sent" for row in recent)
        assert recent[0]["metadata"]["sticker_file"] == "stickers/si.png"
        assert recent[0]["metadata"]["delivery_status"] == "failed"
        assert recent[0]["metadata"]["errors"] == ["later text failed"]
        assert recent[1]["content"] == "晚安"
        assert recent[1]["occurred_at"] == "2026-11-07 12:00"
        assert [row["subject"] for row in await store.recent_delivery_actions(channel="voice")] == ["voice"]
        assert await store.latest_voice() is None

        await store.record_voice("sent", "晚安", now=now)
        assert (await store.latest_voice())["status"] == "sent"
        assert len(await store.recent_delivery_actions()) == 2
        assert await store.record_delivery_actions([], now=now) == 0
    finally:
        await close_db(db)
