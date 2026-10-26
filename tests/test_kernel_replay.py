from datetime import datetime

import pytest

from companion.kernel.kernel import _INTERNAL_PROTOCOL_RE, replay_pure_text
from companion.kernel.semantic import create_reply_draft
from companion.kernel.tracing import JsonlTraceRecorder


@pytest.mark.anyio
async def test_replay_trace_id_stays_explicit_after_other_turn(tmp_path):
    tracer = JsonlTraceRecorder(tmp_path)
    draft = create_reply_draft("嗯，晚安")
    tracer.new_trace("other-turn")
    await replay_pure_text(draft, tracer=tracer, trace_id="original-turn")
    events = tracer.read_trace("original-turn")
    assert [event["step"] for event in events] == ["replay_draft", "replay_plan"]
    assert tracer.read_trace("other-turn") == []


@pytest.mark.parametrize("text", ["（语音）我到家了", "（语音消息）晚安", "（表情：微笑）好呀"])
def test_history_style_protocol_is_rejected(text):
    match = _INTERNAL_PROTOCOL_RE.search(text)
    assert match is not None
    assert match.group(0).startswith("（")
