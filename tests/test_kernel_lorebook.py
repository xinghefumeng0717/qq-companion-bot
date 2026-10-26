from datetime import datetime
from types import SimpleNamespace

from companion.kernel.context import ContextBuilder
from companion.kernel.contracts import IncomingMessage, PsychologicalSnapshot
from companion.kernel.semantic import build_turn_input
from companion.lorebook import Lorebook, LoreEntry
from companion.persona import Persona


def test_dynamic_lorebook_entry_is_included_only_when_relevant():
    persona = Persona.load("characters/example")
    persona.lorebook = Lorebook(
        always="常驻世界",
        entries=[LoreEntry(keys=["墨魉"], content="墨魉是画里化出来的小东西")],
    )
    snapshot = PsychologicalSnapshot(
        turn_id="t1", frozen_at=datetime.now(), state_version=1,
        stage_index=0, prompt_hint="心情平静", action_context={}, affection={}, mood={},
    )
    builder = ContextBuilder()
    hit = build_turn_input("s", [IncomingMessage(message_id=1, text="墨魉去哪了")])
    miss = build_turn_input("s", [IncomingMessage(message_id=1, text="今天吃什么")])
    hit_prompt = builder.build_messages(persona, snapshot, [], hit)[0]["content"]
    miss_prompt = builder.build_messages(persona, snapshot, [], miss)[0]["content"]
    assert "墨魉是画里化出来的小东西" in hit_prompt
    assert "墨魉是画里化出来的小东西" not in miss_prompt
    assert "常驻世界" in miss_prompt


def test_multiple_quotes_use_latest_explicit_focus():
    """测试多条引用时：quoted_text 取最新一条引用语义，回复目标取当前用户消息。"""
    messages = [
        IncomingMessage(message_id=1, text="先引用甲", quoted_message_id=101, payload={"quoted_text": "甲"}),
        IncomingMessage(message_id=2, text="后来引用乙", quoted_message_id=202, payload={"quoted_text": "乙"}),
    ]
    turn = build_turn_input("s", messages)
    # 回复目标是当前用户消息 2，而不是被引用的历史消息 101/202
    assert turn.reply_to_message_id == 2
    assert turn.quoted_text == "乙"
