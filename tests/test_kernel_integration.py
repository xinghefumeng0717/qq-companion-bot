"""测试离线对话内核编排器集成流程 (tests/test_kernel_integration.py)

覆盖验收要求：
1. 普通一轮只调用一次主模型 stream_chat，不调用候选择优或在线改写
2. 心理快照生成不改变好感度或情绪状态，且作息与当前聊天冲突时正确仲裁
3. “嗯，晚安”必须形成一个完整文字气泡，不能被沉默或语音逻辑吞掉
4. [label:1] 等已知内部协议在新原稿中出现时，整轮判为格式失败 (validation_passed=False)，
   且计划 actions 为空，不执行动作，也不碎句修剪后发送
5. 空草稿整轮判为格式失败 (empty_draft)，actions 为空
6. 固定原稿重复回放纯文字投递 (replay_pure_text)，结果必须一致
7. trace 记录完整（turn_input, snapshot, model_context, model_raw, validation, kernel_result）
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, List, Mapping, Optional

import pytest

from companion.kernel.context import ContextBuilder
from companion.kernel.contracts import (
    IncomingMessage,
    MemoryPromptBlocks,
    StandardizedTurnInput,
)
from companion.kernel.kernel import DialogueKernel, replay_pure_text
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.kernel.semantic import build_turn_input, create_reply_draft
from companion.kernel.tracing import JsonlTraceRecorder, read_trace


class FakeGatewayConfig:
    def active(self):
        class ActiveConfig:
            chat = "fake-model-test"
        return ActiveConfig()


class FakeGateway:
    def __init__(self, responses: List[str]):
        self.responses = list(responses)
        self.call_count = 0
        self.config = FakeGatewayConfig()
        self.last_messages: Optional[List[dict]] = None
        self.last_max_retries: Optional[int] = None

    async def stream_chat(
        self,
        messages: List[dict],
        model: str,
        purpose: str = "main_chat",
        max_retries: int = 2,
    ) -> AsyncIterator[str]:
        self.last_max_retries = max_retries
        self.call_count += 1
        self.last_messages = messages
        response_text = self.responses.pop(0) if self.responses else ""
        # 模拟流式输出切片
        yield response_text[:len(response_text) // 2]
        yield response_text[len(response_text) // 2:]


@dataclass
class FakePersona:
    name: str = "青梓"
    core_description: str = "终末地技术员"

    def get_current_activity(self, hour: int, weekday: int, holiday_span: int = 0) -> str:
        # 模拟夜间 2 点处于熟睡状态
        if hour == 2:
            return "睡觉中"
        return "在工坊维护设备"

    def get_stage(self, stage_idx: int):
        class StageInfo:
            tone = "温和熟稔"
        return StageInfo()


class FakeAffectionEngine:
    def __init__(self, stage: int = 1, composite: float = 65.0):
        self.state = {"stage": stage, "composite": composite}
        self.mutate_called = False

    async def get_state(self) -> Mapping[str, Any]:
        return self.state

    def update_mood(self, *args, **kwargs):
        self.mutate_called = True


class FakeMoodEngine:
    def __init__(self, v: float = 0.5, a: float = 0.2, t: float = 8.0, frustration: float = 0.0):
        self.state = {"v": v, "a": a, "t": t, "frustration": frustration}
        self.mutate_called = False

    async def get_state(self) -> Mapping[str, Any]:
        return self.state

    async def get_hours_since_last_chat(self, now: Optional[datetime] = None) -> float:
        return 1.5

    def update(self, *args, **kwargs):
        self.mutate_called = True


@pytest.fixture
def kernel_env(tmp_path):
    """构建集成测试环境，使用临时目录 trace recorder 与 fake 依赖。"""
    trace_dir = tmp_path / "traces"
    tracer = JsonlTraceRecorder(directory=trace_dir)
    persona = FakePersona()
    aff_engine = FakeAffectionEngine()
    mood_engine = FakeMoodEngine()
    snapshot_builder = PsychologicalSnapshotBuilder(
        affection_engine=aff_engine,
        mood_engine=mood_engine,
        persona=persona,
    )
    context_builder = ContextBuilder()
    return {
        "tracer": tracer,
        "trace_dir": trace_dir,
        "persona": persona,
        "aff_engine": aff_engine,
        "mood_engine": mood_engine,
        "snapshot_builder": snapshot_builder,
        "context_builder": context_builder,
    }


@pytest.mark.anyio
async def test_normal_turn_goodnight_single_bubble_and_single_llm_call(kernel_env):
    """测试固定事故回放：“嗯，晚安”形成完整文字气泡，普通一轮只调用一次主模型。"""
    gateway = FakeGateway(responses=["嗯，晚安"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
    )

    # 凌晨 2 点收到消息，验证作息冲突仲裁（醒着在回消息）
    at_night = datetime(2026, 10, 26, 2, 30, 0)
    turn_input = build_turn_input(
        session_id="test_sess_001",
        messages=[IncomingMessage(message_id=1001, text="晚安啦")],
        received_at=at_night,
    )

    res = await kernel.handle_turn(turn_input)

    # 1. 验证普通一轮只调用了一次主模型
    assert gateway.call_count == 1

    # 2. 验证“嗯，晚安”形成一个完整文字气泡，没有被吞掉
    assert res.validation_passed is True
    assert len(res.draft.bubbles) == 1
    assert res.draft.bubbles[0].text == "嗯，晚安"
    assert len(res.plan.actions) == 1
    assert res.plan.actions[0].bubble_id == res.draft.bubbles[0].bubble_id
    assert res.plan.actions[0].channel == "text"

    # 3. 验证心理快照生成不改变好感度和情绪状态
    assert kernel_env["aff_engine"].mutate_called is False
    assert kernel_env["mood_engine"].mutate_called is False

    # 4. 验证夜间睡眠作息与客观聊天冲突时的仲裁（客观醒着在聊，没有奇怪自相矛盾）
    assert "刚被消息吵醒" in res.snapshot.prompt_hint

    # 5. 验证 trace 完整记录
    events = kernel_env["tracer"].read_trace(res.trace_id)
    steps = [e["step"] for e in events]
    assert "turn_input" in steps
    assert "psychological_snapshot" in steps
    assert "model_context" in steps
    assert "model_raw" in steps
    assert "validation" in steps
    assert "kernel_result" in steps


@pytest.mark.anyio
async def test_label_protocol_validation_failed_and_not_executed(kernel_env):
    """测试固定事故回放：模型输出包含 [label:1] 等已知内部协议时，整轮 validation_failed 且不执行动作。"""
    gateway = FakeGateway(responses=["好的，明白了。[label:1]明天见。"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
    )

    turn_input = build_turn_input(
        session_id="test_sess_002",
        messages=[IncomingMessage(message_id=2001, text="明天九点集合")],
    )

    res = await kernel.handle_turn(turn_input)

    # 整轮判为校验失败
    assert res.validation_passed is False
    assert any("internal_protocol" in err for err in res.validation_errors)

    # 动作计划为空，不得成为可执行引用，也不得悄悄改写正文输出
    assert len(res.plan.actions) == 0


@pytest.mark.anyio
async def test_empty_draft_validation_failed(kernel_env):
    """测试空草稿整轮 validation_passed 为 False，actions 为空。"""
    gateway = FakeGateway(responses=["   \n  \t  "])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
    )

    turn_input = build_turn_input(
        session_id="test_sess_003",
        messages=[IncomingMessage(message_id=3001, text="你好")],
    )

    res = await kernel.handle_turn(turn_input)
    assert res.validation_passed is False
    assert "empty_draft" in res.validation_errors
    assert len(res.plan.actions) == 0


@pytest.mark.anyio
async def test_replay_pure_text_consistency(kernel_env):
    """测试固定原稿重复回放纯文字投递，结果必须完全一致。"""
    fixed_draft = create_reply_draft(
        "嗯，晚安\n明天工坊见",
        draft_id="draft_replay_test",
        created_at=datetime(2026, 10, 26, 23, 0, 0),
    )

    # 第一次回放
    plan1 = await replay_pure_text(
        fixed_draft,
        tracer=kernel_env["tracer"],
        quote_message_id=7788,
    )

    # 第二次回放
    plan2 = await replay_pure_text(
        fixed_draft,
        tracer=kernel_env["tracer"],
        quote_message_id=7788,
    )

    # 验证两次回放结构与内容完全一致
    assert plan1.draft_id == plan2.draft_id == "draft_replay_test"
    assert plan1.quote_message_id == plan2.quote_message_id == 7788
    assert len(plan1.actions) == len(plan2.actions) == 2
    for a1, a2 in zip(plan1.actions, plan2.actions):
        assert a1.bubble_id == a2.bubble_id
        assert a1.channel == a2.channel == "text"


@pytest.mark.anyio
async def test_delivery_plan_quotes_current_user_message_not_quoted_history(kernel_env):
    """测试回复计划的引用目标是当前用户消息，而不是用户引用的历史消息。

    场景：旧消息 A=4001，当前用户消息 B=5002 引用了 A。
    投递计划必须引用 B；普通发言引用当前消息；显式目标优先。
    """
    gateway = FakeGateway(responses=["确实，今天很适合出门。", "在呢。", "收到啦"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
    )

    # 1. 当前消息引用了旧消息：计划引用当前消息 5002，而不是旧消息 4001
    turn_quoted = build_turn_input(
        session_id="test_sess_quote_target",
        messages=[
            IncomingMessage(message_id=4001, text="今天天气真好"),
            IncomingMessage(
                message_id=5002,
                text="确实是这样",
                quoted_message_id=4001,
                payload={"quoted_text": "今天天气真好"},
            ),
        ],
    )
    assert turn_quoted.reply_to_message_id == 5002
    assert turn_quoted.quoted_text == "今天天气真好"

    res_quoted = await kernel.handle_turn(turn_quoted)
    assert res_quoted.validation_passed is True
    assert res_quoted.plan.quote_message_id == 5002
    assert res_quoted.plan.quote_message_id != 4001

    # 2. 普通发言（无引用）：计划引用当前这条用户消息
    turn_plain = build_turn_input(
        session_id="test_sess_plain_target",
        messages=[IncomingMessage(message_id=6001, text="在吗")],
    )
    res_plain = await kernel.handle_turn(turn_plain)
    assert res_plain.validation_passed is True
    assert res_plain.plan.quote_message_id == 6001

    # 3. 显式给出的回复目标优先
    turn_explicit = build_turn_input(
        session_id="test_sess_explicit_target",
        messages=[IncomingMessage(message_id=7001, text="在吗")],
        reply_to_message_id=8888,
    )
    res_explicit = await kernel.handle_turn(turn_explicit)
    assert res_explicit.validation_passed is True
    assert res_explicit.plan.quote_message_id == 8888


@pytest.mark.anyio
async def test_kernel_can_disable_gateway_retries_for_ab(kernel_env):
    gateway = FakeGateway(responses=["晚安"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
        max_retries=0,
    )
    turn_input = build_turn_input(
        session_id="test_no_retry",
        messages=[IncomingMessage(message_id=4001, text="晚安")],
    )
    result = await kernel.handle_turn(turn_input)
    assert result.validation_passed is True
    assert gateway.last_max_retries == 0


class FakeReadonlyMemory:
    async def get_recent_turns(self, limit=10):
        return []
    async def get_facts_for_prompt(self, query=""):
        return ["他喜欢甜口（10-01 记下）"]
    async def get_impressions(self):
        return [{"content": "他嘴硬心软", "confidence": 3}]
    async def get_self_facts_for_prompt(self, query=""):
        return ["我怕黑（10-01 说的）"]
    async def get_active_diaries(self, current_valence=2.0, now=None):
        return ["记得: 一起修过终端"]
    async def get_open_followups_readonly(self, due_only=True, limit=5, now=None):
        return [{"topic": "问他终端结果", "created_at": "2026-10-01 10:00"}]
    @staticmethod
    def format_followup(row):
        return row["topic"]
    async def get_live_desires_readonly(self, limit=5, now=None):
        return [{"content": "其实想让他早点睡"}]


class FakeArcs:
    async def build_prompt_block(self, as_script=False, now=None):
        return "【生活剧本】\n- （临近）终端检修"


@pytest.mark.anyio
async def test_memory_blocks_reach_model_and_trace(kernel_env):
    gateway = FakeGateway(responses=["我记得，后来修好了"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
        memory=FakeReadonlyMemory(),
        arcs=FakeArcs(),
        self_memory=True,
        impressions=True,
    )
    turn_input = build_turn_input(
        session_id="test_memory_context",
        messages=[IncomingMessage(message_id=5001, text="终端后来怎么样")],
    )
    result = await kernel.handle_turn(turn_input)
    prompt = result.messages[0]["content"]
    for text in ("他喜欢甜口", "他嘴硬心软", "我怕黑", "一起修过终端", "问他终端结果", "其实想让他早点睡", "终端检修"):
        assert text in prompt
    steps = [event["step"] for event in kernel_env["tracer"].read_trace(result.trace_id)]
    assert "memory_context" in steps
    assert steps.index("memory_context") < steps.index("model_context")
    assert gateway.call_count == 1


# ==================================================================
# 8. 本轮时间事实：进 model_context，但不污染 turn_input 与记忆查询
# ==================================================================


class _GapMoodEngine(FakeMoodEngine):
    """固定"距上次聊天"的小时数，用于时间事实块断言。"""

    async def get_hours_since_last_chat(self, now: Optional[datetime] = None) -> float:
        return 5.0


class _HolidayPersona(FakePersona):
    """带节假日表的角色：只读一次节假日列表。"""

    def __init__(self, holidays: List[str]) -> None:
        super().__init__()
        self.holidays = holidays
        self.holiday_reads = 0

    def get_current_activity(self, hour: int, weekday: int, holiday_span: int = 0) -> str:
        self.last_holiday_span = holiday_span
        return "在工坊维护设备"


@pytest.mark.anyio
async def test_model_context_has_time_facts_without_polluting_user_input(
    kernel_env, monkeypatch,
):
    """集成 trace 的 model_context 必须含本轮时间事实；用户正文与记忆查询原文不受污染。"""
    from companion.kernel import kernel as kernel_module

    captured: dict = {}

    async def _fake_collect(memory, **kwargs):
        captured["query"] = kwargs.get("query", "")
        captured["now"] = kwargs.get("now")
        return MemoryPromptBlocks()

    monkeypatch.setattr(kernel_module, "collect_memory_blocks", _fake_collect)

    persona = _HolidayPersona(["2026-10-26"])
    snapshot_builder = PsychologicalSnapshotBuilder(
        affection_engine=FakeAffectionEngine(),
        mood_engine=_GapMoodEngine(),
        persona=persona,
        holidays_provider=lambda: ["2026-10-26"],
    )
    gateway = FakeGateway(responses=["在的，随时。"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=persona,
        snapshot_builder=snapshot_builder,
        context_builder=ContextBuilder(),
        tracer=kernel_env["tracer"],
        memory=FakeReadonlyMemory(),
    )

    received_at = datetime(2026, 10, 26, 15, 4, 0)
    original_text = "终端后来修好了吗"
    turn_input = build_turn_input(
        session_id="test_time_facts",
        messages=[IncomingMessage(message_id=8001, text=original_text)],
        received_at=received_at,
    )

    result = await kernel.handle_turn(turn_input)

    system_prompt = result.messages[0]["content"]
    user_content = result.messages[-1]["content"]

    # 1. 时间事实都在 system prompt 里
    assert "【本轮时间】" in system_prompt
    assert "2026-10-26" in system_prompt
    assert "15:04" in system_prompt
    assert "星期一" in system_prompt
    assert "距上次聊天已过 5 小时" in system_prompt
    assert "活动状态：在与他聊天（日常状态：在工坊维护设备）" in system_prompt
    # 节假日附注来自角色卡口径
    assert "今天是法定节假日" in system_prompt
    # 快照里也记下了 holiday_span
    assert result.snapshot.action_context["holiday_span"] == 1
    assert persona.last_holiday_span == 1

    # 2. 用户正文不受污染
    assert user_content == original_text
    assert result.turn_input.text == original_text
    for token in ("【本轮时间】", "当前日期", "距上次聊天", "活动状态", "法定节假日"):
        assert token not in user_content, token

    # 3. 记忆查询原文不受污染
    assert captured["query"] == original_text
    assert captured["now"] == received_at

    # 4. trace 的 model_context 里同样能看到这些事实
    events = kernel_env["tracer"].read_trace(result.trace_id)
    model_context = next(e for e in events if e["step"] == "model_context")
    traced_messages = model_context["payload"]["messages"]
    traced_system = traced_messages[0]["content"]
    assert "【本轮时间】" in traced_system
    assert "距上次聊天已过 5 小时" in traced_system
    assert traced_messages[-1]["content"] == original_text


# ==================================================================
# 9. 公共只读准备路径：沙箱与实际内核共用同一份上下文
# ==================================================================


@pytest.mark.anyio
async def test_prepare_turn_without_memory_is_readonly_and_fully_traced(kernel_env):
    """无 memory 时仍可准备；不调主模型、不推进心理，trace 停在模型上下文。"""
    gateway = FakeGateway(responses=[])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
    )
    snapshot_calls = 0
    original_build = kernel.snapshot_builder.build_snapshot

    async def _counting_build(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return await original_build(*args, **kwargs)

    kernel.snapshot_builder.build_snapshot = _counting_build
    original_text = "帝江号今天的终端还稳定吗"
    turn_input = build_turn_input(
        session_id="test_prepare_without_memory",
        messages=[IncomingMessage(message_id=9001, text=original_text)],
        received_at=datetime(2026, 10, 26, 15, 4),
    )

    prepared = await kernel.prepare_turn(turn_input)

    assert prepared.turn_input is turn_input
    assert prepared.turn_input.text == original_text
    assert prepared.history == ()
    assert prepared.memory_blocks is None
    assert prepared.messages[-1]["content"] == original_text
    assert "【本轮时间】" in prepared.messages[0]["content"]
    assert snapshot_calls == 1
    assert gateway.call_count == 0
    assert kernel_env["aff_engine"].mutate_called is False
    assert kernel_env["mood_engine"].mutate_called is False

    events = kernel_env["tracer"].read_trace(prepared.trace_id)
    steps = [event["step"] for event in events]
    assert steps == [
        "turn_input", "runtime", "psychological_snapshot", "model_context",
    ]
    model_context = events[-1]["payload"]["messages"]
    assert model_context[-1]["content"] == original_text


@pytest.mark.anyio
async def test_handle_prepared_turn_reuses_one_snapshot_and_frozen_context(kernel_env):
    """准备后生成必须复用同一快照与消息，不得重复准备或另起 trace。"""
    gateway = FakeGateway(responses=["稳定，我刚看过。"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
    )
    snapshot_calls = 0
    original_build = kernel.snapshot_builder.build_snapshot

    async def _counting_build(*args, **kwargs):
        nonlocal snapshot_calls
        snapshot_calls += 1
        return await original_build(*args, **kwargs)

    kernel.snapshot_builder.build_snapshot = _counting_build
    turn_input = build_turn_input(
        session_id="test_prepare_then_generate",
        messages=[IncomingMessage(message_id=9002, text="终端怎么样")],
    )
    prepared = await kernel.prepare_turn(turn_input)
    assert gateway.call_count == 0

    result = await kernel.handle_prepared_turn(prepared)

    assert snapshot_calls == 1
    assert gateway.call_count == 1
    assert gateway.last_messages == list(prepared.messages)
    assert result.trace_id == prepared.trace_id
    assert result.snapshot is prepared.snapshot
    assert result.turn_input is prepared.turn_input
    assert result.messages == prepared.messages
    assert result.plan.quote_message_id == 9002
    steps = [
        event["step"]
        for event in kernel_env["tracer"].read_trace(prepared.trace_id)
    ]
    assert steps.count("psychological_snapshot") == 1
    assert steps.count("model_context") == 1
    assert steps[-2:] == ["validation", "kernel_result"]


@pytest.mark.anyio
async def test_handle_turn_context_equals_public_prepare_with_memory(
    kernel_env, monkeypatch,
):
    """handle_turn 内部也走公共准备；history、记忆块和原始 query 不复制走偏。"""
    from companion.kernel import kernel as kernel_module

    class _HistoryMemory(FakeReadonlyMemory):
        async def get_recent_turns(self, limit=10):
            return [{"role": "assistant", "content": "昨天在帝江号检修"}]

    captured = {}

    async def _fake_collect(memory, **kwargs):
        captured.update(kwargs)
        return MemoryPromptBlocks(facts="【重要记忆】\n- 管理员偏好原样保留")

    monkeypatch.setattr(kernel_module, "collect_memory_blocks", _fake_collect)
    gateway = FakeGateway(responses=["我记得。"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
        memory=_HistoryMemory(),
    )
    original_text = "后来修得怎么样"
    turn_input = build_turn_input(
        session_id="test_public_prepare_equivalence",
        messages=[IncomingMessage(message_id=9003, text=original_text)],
    )

    expected = await kernel.prepare_turn(turn_input)
    result = await kernel.handle_turn(turn_input)

    assert result.messages == expected.messages
    assert gateway.last_messages == list(expected.messages)
    assert expected.history == ({"role": "assistant", "content": "昨天在帝江号检修"},)
    assert expected.memory_blocks == MemoryPromptBlocks(
        facts="【重要记忆】\n- 管理员偏好原样保留",
    )
    assert result.messages[1] == {"role": "assistant", "content": "昨天在帝江号检修"}
    assert result.messages[-1]["content"] == original_text
    assert captured["query"] == original_text
    assert captured["now"] == turn_input.received_at

    expected_steps = [
        event["step"] for event in kernel_env["tracer"].read_trace(expected.trace_id)
    ]
    result_steps = [
        event["step"] for event in kernel_env["tracer"].read_trace(result.trace_id)
    ]
    assert "memory_context" in expected_steps
    assert "memory_context" in result_steps
    assert expected_steps[:5] == result_steps[:5]


@pytest.mark.anyio
async def test_dynamic_style_examples_are_selected_once_frozen_and_traced(kernel_env):
    """动态示例在准备阶段只选一次；生成复用冻结结果，trace 不重复存块全文。"""
    from companion.style_examples import StyleExampleBank

    bank = StyleExampleBank([
        {"his": f"今天终端检修好累{i}", "her": f"先吃点东西{i}"}
        for i in range(8)
    ])
    gateway = FakeGateway(responses=["先休息一下吧。"])
    kernel = DialogueKernel(
        gateway=gateway,
        persona=kernel_env["persona"],
        snapshot_builder=kernel_env["snapshot_builder"],
        context_builder=kernel_env["context_builder"],
        tracer=kernel_env["tracer"],
        style_examples=bank,
    )
    searches = 0
    original_search = bank.search_entries

    def _counting_search(*args, **kwargs):
        nonlocal searches
        searches += 1
        return original_search(*args, **kwargs)

    bank.search_entries = _counting_search
    turn_input = build_turn_input(
        session_id="test_dynamic_style_examples",
        messages=[IncomingMessage(message_id=9101, text="今天终端检修好累")],
        received_at=datetime(2026, 10, 26, 20, 30),
    )

    prepared = await kernel.prepare_turn(turn_input)
    assert searches == 1
    assert gateway.call_count == 0
    selection = prepared.style_examples
    assert selection is not None
    assert selection.query == turn_input.text
    assert len(selection.items) == 3
    assert selection.block in prepared.messages[0]["content"]
    assert prepared.messages[0]["content"].endswith(selection.block.strip())
    assert prepared.messages[-1]["content"] == turn_input.text

    result = await kernel.handle_prepared_turn(prepared)
    replay = await kernel.prepare_turn(turn_input)

    assert searches == 2  # prepare + 独立重放各一次；生成没有重新选择
    assert gateway.call_count == 1
    assert result.messages == prepared.messages
    assert replay.style_examples == selection
    assert replay.messages == prepared.messages
    assert kernel_env["aff_engine"].mutate_called is False
    assert kernel_env["mood_engine"].mutate_called is False

    events = kernel_env["tracer"].read_trace(prepared.trace_id)
    style_events = [event for event in events if event["step"] == "style_examples"]
    assert len(style_events) == 1
    payload = style_events[0]["payload"]
    assert payload["query"] == turn_input.text
    assert payload["seed"] == selection.seed
    assert payload["indices"] == list(selection.indices)
    import hashlib

    assert payload["system_prompt_sha256"] == hashlib.sha256(
        prepared.messages[0]["content"].encode("utf-8"),
    ).hexdigest()
    assert "block" not in payload  # 全文只在 model_context 中保留，不重复扩散
    steps = [event["step"] for event in events]
    assert steps.index("style_examples") < steps.index("model_context")


@pytest.mark.anyio
async def test_unmatched_dynamic_style_examples_leave_prompt_unchanged(kernel_env):
    """启用了示例库但没命中时，prompt 与未启用逐字节一致，且空选择有 trace。"""
    from companion.style_examples import StyleExampleBank

    def _kernel(bank):
        return DialogueKernel(
            gateway=FakeGateway(responses=[]),
            persona=kernel_env["persona"],
            snapshot_builder=kernel_env["snapshot_builder"],
            context_builder=kernel_env["context_builder"],
            tracer=kernel_env["tracer"],
            style_examples=bank,
        )

    bank = StyleExampleBank([{"his": "明天考试好紧张", "her": "先深呼吸"}])
    turn_input = build_turn_input(
        session_id="test_dynamic_style_no_match",
        messages=[IncomingMessage(message_id=9102, text="嗯")],
    )
    enabled = await _kernel(bank).prepare_turn(turn_input)
    disabled = await _kernel(None).prepare_turn(turn_input)

    assert enabled.style_examples is not None
    assert enabled.style_examples.block == ""
    assert enabled.style_examples.items == ()
    assert enabled.messages == disabled.messages
    assert "【别人的聊天参考】" not in enabled.messages[0]["content"]
    steps = [
        event["step"] for event in kernel_env["tracer"].read_trace(enabled.trace_id)
    ]
    assert "style_examples" in steps
