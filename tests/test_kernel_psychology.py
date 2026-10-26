"""心理状态快照构建器单元测试 (tests/test_kernel_psychology.py)。

覆盖目标：
1. 状态只读不推进：不调用 update_mood、不修改底层好感度或情绪引擎状态
2. 快照副本与版本/阶段正确性：深拷贝隔离、不可变性 MappingProxyType、state_version 与 stage_index 透传
3. prompt_hint 安全性：不含原始数值、不泄露动作协议（如 [sticker:...]、[沉默]、[voice]、[label:] 等）
4. 睡眠作息与当前用户消息仲裁：
   - 用户有消息输入时：判定为清醒交流，不自称正在熟睡/睡着
   - 用户无消息输入（主动消息等）且作息在睡觉时：维持就寝状态描写
5. 阶段提示不携带动作指令：仅包含关系分寸与语气，不渗入动作协议指令
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import MappingProxyType, SimpleNamespace
from typing import Any, Dict, List, Optional
import pytest

from companion.kernel.contracts import PsychologicalSnapshot
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.persona import Persona, Stage


class FakeAffectionReader:
    """轻量只读好感度 Reader Fake，用于验证只读性与方法调用。"""

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self._state = state or {
            "stage": 3,
            "composite": 45.5,
            "dimensions": {"warmth": 50.0, "trust": 40.0},
        }
        self.get_state_calls = 0

    async def get_state(self) -> Dict[str, Any]:
        self.get_state_calls += 1
        return self._state

    def update_mood(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("PsychologicalSnapshotBuilder 绝不能调用 update_mood！")


class FakeMoodReader:
    """轻量只读情绪 Reader Fake，用于验证只读性与不推进状态。"""

    def __init__(
        self,
        state: Optional[Dict[str, Any]] = None,
        hours_since_last_chat: float = 2.5,
    ) -> None:
        self._state = state or {
            "v": 0.5,
            "a": 0.2,
            "t": 7.5,
            "frustration": 0.0,
        }
        self.hours_since_last_chat = hours_since_last_chat
        self.get_state_calls = 0
        self.get_hours_calls = 0

    async def get_state(self) -> Dict[str, Any]:
        self.get_state_calls += 1
        return self._state

    async def get_hours_since_last_chat(self, now: Optional[datetime] = None) -> float:
        self.get_hours_calls += 1
        return self.hours_since_last_chat

    def update_mood(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("PsychologicalSnapshotBuilder 绝不能调用 update_mood！")

    def decay_mood(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("PsychologicalSnapshotBuilder 绝不能调用 decay_mood 推进状态！")


class FakePersona:
    """轻量 Persona Fake，支持配置作息和阶段。"""

    def __init__(
        self,
        activity: str = "工作",
        stage: Optional[Stage] = None,
    ) -> None:
        self.activity = activity
        self.stage = stage or Stage(
            name="熟悉",
            tone="自然温和",
            instructions=["多分享日常琐事", "不用过于拘谨"],
        )
        # 记录每次作息查询的入参，供节假日链路断言使用。
        self.activity_calls: List[Dict[str, Any]] = []

    def get_current_activity(self, hour: int, weekday: int, holiday_span: int = 0) -> str:
        self.activity_calls.append(
            {"hour": hour, "weekday": weekday, "holiday_span": holiday_span}
        )
        return self.activity

    def get_stage(self, stage_idx: int) -> Optional[Stage]:
        return self.stage


# ---------------------------------------------------------------------------
# 1. 状态只读不推进测试
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_snapshot_readonly_does_not_mutate_or_advance_state():
    """验证快照构建纯只读，绝不推进或改变底层 state。"""
    initial_aff = {"stage": 2, "composite": 30.0, "nested": {"counter": 1}}
    initial_mood = {"v": 0.1, "a": 0.2, "t": 6.0, "frustration": 0.0}

    aff_reader = FakeAffectionReader(state=initial_aff)
    mood_reader = FakeMoodReader(state=initial_mood)

    builder = PsychologicalSnapshotBuilder(
        affection_engine=aff_reader,
        mood_engine=mood_reader,
        persona=FakePersona(),
    )

    snapshot = await builder.build_snapshot(
        turn_id="turn-readonly-1",
        user_message="你好呀",
    )

    assert aff_reader.get_state_calls == 1
    assert mood_reader.get_state_calls == 1
    # 状态数据无变动
    assert aff_reader._state["composite"] == 30.0
    assert mood_reader._state["v"] == 0.1


# ---------------------------------------------------------------------------
# 2. 快照副本与版本/阶段正确性测试
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_snapshot_copy_isolation_and_versioning():
    """验证返回的快照包含深拷贝副本，外部修改源数据或修改快照不影响彼此。"""
    raw_aff = {"stage": 4, "composite": 60.0, "nested": {"tag": "orig"}}
    raw_mood = {"v": 0.6, "a": -0.1, "t": 8.0, "frustration": 0.1}

    aff_reader = FakeAffectionReader(state=raw_aff)
    mood_reader = FakeMoodReader(state=raw_mood)

    builder = PsychologicalSnapshotBuilder(
        affection_engine=aff_reader,
        mood_engine=mood_reader,
    )

    snapshot = await builder.build_snapshot(
        turn_id="turn-isolation-1",
        user_message="测试隔离",
        state_version=42,
    )

    # 验证元数据
    assert snapshot.turn_id == "turn-isolation-1"
    assert snapshot.state_version == 42
    assert snapshot.stage_index == 4
    assert isinstance(snapshot.affection, MappingProxyType)
    assert isinstance(snapshot.mood, MappingProxyType)
    assert isinstance(snapshot.action_context, MappingProxyType)

    # 修改原始字典，不应污染快照
    raw_aff["composite"] = 999.0
    raw_aff["nested"]["tag"] = "mutated"
    raw_mood["v"] = -0.9

    assert snapshot.affection["composite"] == 60.0
    assert snapshot.affection["nested"]["tag"] == "orig"
    assert snapshot.mood["v"] == 0.6

    # 快照自身映射为只读
    with pytest.raises(TypeError):
        snapshot.affection["composite"] = 100.0  # type: ignore

    with pytest.raises(TypeError):
        snapshot.mood["v"] = 1.0  # type: ignore


# ---------------------------------------------------------------------------
# 3. prompt_hint 安全性：不含原始数值与动作协议
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_prompt_hint_does_not_contain_raw_numbers_or_action_protocols():
    """验证 prompt_hint 中绝无 raw numbers 或动作协议语法。"""
    raw_aff = {"stage": 2, "composite": 45.678}
    raw_mood = {"v": 0.8123, "a": -0.456, "t": 7.89, "frustration": 0.321}

    aff_reader = FakeAffectionReader(state=raw_aff)
    mood_reader = FakeMoodReader(state=raw_mood, hours_since_last_chat=12.34)

    # 构造携带潜在动作指令的 persona
    persona = FakePersona(
        activity="阅读",
        stage=Stage(
            name="进阶",
            tone="温和且真诚",
            instructions=["[sticker:cat_smile] 展现微笑", "[沉默] 偶尔停顿"],
        ),
    )

    builder = PsychologicalSnapshotBuilder(
        affection_engine=aff_reader,
        mood_engine=mood_reader,
        persona=persona,
    )

    snapshot = await builder.build_snapshot(
        turn_id="turn-safe-1",
        user_message="在忙什么呢？",
    )

    hint = snapshot.prompt_hint

    # 1. 不含动作协议标签
    banned_protocols = [
        "[sticker",
        "[voice",
        "[沉默",
        "[label:",
        "[quote",
        "[face",
        "sticker:",
        "voice:",
    ]
    for protocol in banned_protocols:
        assert protocol not in hint, f"prompt_hint 泄漏动作协议: {protocol} in '{hint}'"

    # 2. 不含底层裸浮点数值
    banned_numbers = ["45.678", "0.8123", "-0.456", "7.89", "0.321", "12.34"]
    for num in banned_numbers:
        assert num not in hint, f"prompt_hint 泄漏原始浮点数值: {num} in '{hint}'"

    # 3. 也不得出现 composite / valence / arousal 内部英文字段
    for field in ["composite", "valence", "arousal", "frustration"]:
        assert field not in hint.lower(), f"prompt_hint 出现内部状态键名: {field}"


# ---------------------------------------------------------------------------
# 4. 睡眠作息与当前用户消息仲裁测试
# ---------------------------------------------------------------------------

@pytest.mark.anyio
@pytest.mark.parametrize(
    "sleep_activity",
    ["睡觉", "睡着了", "睡眠中", "深睡", "熟睡中", "已入睡", "进入梦乡", "歇息"],
)
async def test_sleep_routine_with_user_message_arbitration(sleep_activity: str):
    """当睡眠作息与用户当前消息冲突时：
    客观当前聊天优先，提示词必须表明此刻醒着在对话，绝不自称正在熟睡/睡着。
    """
    persona = FakePersona(activity=sleep_activity)
    builder = PsychologicalSnapshotBuilder(
        persona=persona,
    )

    # 场景 A: 有用户消息
    snapshot_chatting = await builder.build_snapshot(
        turn_id="turn-sleep-chat",
        user_message="睡了吗？快醒醒",
        now=datetime(2026, 10, 26, 2, 30, 0),
    )

    hint_chat = snapshot_chatting.prompt_hint
    ctx_chat = snapshot_chatting.action_context

    assert ctx_chat["is_chatting"] is True
    assert ctx_chat["is_sleeping_routine"] is True

    # 绝对不能自称正在熟睡或睡着（但允许体现夜深/刚醒等客观状态）
    forbidden_claims = ["正在熟睡", "处于熟睡", "正在睡觉", "睡着（其实醒着）", "目前在深睡", "处于睡觉"]
    for claim in forbidden_claims:
        assert claim not in hint_chat, f"提示词自相矛盾自称熟睡: '{claim}' in '{hint_chat}'"

    # 场景 B: 无用户消息（例如主动触发或后台定时巡检）
    snapshot_idle = await builder.build_snapshot(
        turn_id="turn-sleep-idle",
        user_message="",
        now=datetime(2026, 10, 26, 2, 30, 0),
    )

    ctx_idle = snapshot_idle.action_context
    assert ctx_idle["is_chatting"] is False
    assert ctx_idle["is_sleeping_routine"] is True


# ---------------------------------------------------------------------------
# 5. 阶段提示不携带动作指令
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_stage_hint_does_not_carry_action_directives():
    """验证阶段提示仅作为分寸/语气参考，不携带任何动作指令。"""
    stage_with_action = Stage(
        name="信任",
        tone="亲近依赖",
        instructions=[
            "主动倾诉烦恼",
            "发送 [sticker:hug] 拥抱对方",
            "若被问及秘密使用 [沉默] 避而不谈",
        ],
    )
    persona = FakePersona(activity="闲聊", stage=stage_with_action)
    builder = PsychologicalSnapshotBuilder(persona=persona)

    snapshot = await builder.build_snapshot(
        turn_id="turn-stage-1",
        user_message="今天过得怎么样？",
    )

    hint = snapshot.prompt_hint
    assert "[sticker" not in hint
    assert "[沉默" not in hint
    assert "保持亲近依赖" in hint or "亲近依赖" in hint


# ---------------------------------------------------------------------------
# 6. 使用公开 example 卡集成测试
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_snapshot_builder_with_example_persona():
    """使用 characters/example 公开示例角色卡验证真实 Persona 兼容性。"""
    import os

    example_path = "characters/example"
    if not os.path.exists(example_path):
        pytest.skip("characters/example 路径不存在，跳过卡集成测试")

    persona = Persona.load(example_path)
    aff_reader = FakeAffectionReader()
    mood_reader = FakeMoodReader()

    builder = PsychologicalSnapshotBuilder(
        affection_engine=aff_reader,
        mood_engine=mood_reader,
        persona=persona,
    )

    # 模拟凌晨 3 点有消息
    dt_night = datetime(2026, 10, 26, 3, 0, 0)
    snapshot = await builder.build_snapshot(
        turn_id="turn-example-night",
        user_message="还没睡呢？",
        now=dt_night,
        state_version=1,
    )

    assert isinstance(snapshot, PsychologicalSnapshot)
    assert snapshot.state_version == 1
    assert snapshot.prompt_hint != ""
    assert "[sticker:" not in snapshot.prompt_hint
    assert "睡着（其实醒着）" not in snapshot.prompt_hint


# ---------------------------------------------------------------------------
# 7. 节假日：只读 provider -> holiday_span -> 作息查询入参
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_holidays_provider_drives_holiday_span_and_activity_argument():
    """短假：provider 里的当天日期必须真的算出 holiday_span，并传进作息查询。"""
    persona = FakePersona(activity="周六作息")
    calls: List[List[str]] = []

    def _provider() -> List[str]:
        calls.append(["read"])
        return ["2026-10-24", "2026-10-25"]

    builder = PsychologicalSnapshotBuilder(persona=persona, holidays_provider=_provider)

    snapshot = await builder.build_snapshot(
        turn_id="turn-holiday-short",
        user_message="今天放假诶",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )

    # 短假：段长 2（10-24 与 10-25 相连）
    assert snapshot.action_context["holiday_span"] == 2
    # 作息查询真的拿到了这个段长
    assert persona.activity_calls[-1]["holiday_span"] == 2
    # 每轮只读一次节假日表
    assert len(calls) == 1


@pytest.mark.anyio
async def test_holidays_provider_long_holiday_changes_span_and_activity_argument():
    """长假：连续段长 >=4 必须算出来，并原样传进 persona.get_current_activity。"""
    persona = FakePersona(activity="放长假中，回老家，不在学校")

    builder = PsychologicalSnapshotBuilder(
        persona=persona,
        holidays_provider=lambda: [
            "2026-10-23", "2026-10-24", "2026-10-25",
            "2026-10-26", "2026-10-27", "2026-10-28",
        ],
    )

    snapshot = await builder.build_snapshot(
        turn_id="turn-holiday-long",
        user_message="假期过半啦",
        now=datetime(2026, 10, 26, 10, 0, 0),
    )

    assert snapshot.action_context["holiday_span"] == 6
    assert persona.activity_calls[-1]["holiday_span"] == 6


@pytest.mark.anyio
async def test_holidays_provider_missing_or_raising_falls_back_to_non_holiday():
    """缺省 provider 或 provider 抛异常都必须回退成"非节假日"，且不得炸掉整轮。"""
    # 缺省
    persona = FakePersona()
    builder = PsychologicalSnapshotBuilder(persona=persona)
    snapshot = await builder.build_snapshot(
        turn_id="turn-holiday-none",
        user_message="在吗",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )
    assert snapshot.action_context["holiday_span"] == 0
    assert persona.activity_calls[-1]["holiday_span"] == 0

    # 抛异常
    def _boom() -> List[str]:
        raise RuntimeError("节假日表读取炸了")

    persona2 = FakePersona()
    builder2 = PsychologicalSnapshotBuilder(persona=persona2, holidays_provider=_boom)
    snapshot2 = await builder2.build_snapshot(
        turn_id="turn-holiday-boom",
        user_message="在吗",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )
    assert snapshot2.action_context["holiday_span"] == 0
    assert persona2.activity_calls[-1]["holiday_span"] == 0
    # 状态依然照常产出
    assert snapshot2.prompt_hint != ""


@pytest.mark.anyio
async def test_public_holidays_opt_out_skips_provider():
    """架空世界明确关闭现实节假日时，快照自身也必须挡住 provider。"""
    persona = FakePersona()
    persona.world = SimpleNamespace(follow_public_holidays=False)
    reads = 0

    def _provider() -> List[str]:
        nonlocal reads
        reads += 1
        return ["2026-10-25"]

    builder = PsychologicalSnapshotBuilder(persona=persona, holidays_provider=_provider)
    snapshot = await builder.build_snapshot(
        turn_id="turn-holiday-opt-out",
        user_message="在吗",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )

    assert reads == 0
    assert snapshot.action_context["holiday_span"] == 0
    assert persona.activity_calls[-1]["holiday_span"] == 0


@pytest.mark.anyio
async def test_follow_public_holidays_gate_lives_only_inside_snapshot_builder():
    """follow_public_holidays=False 的闸门只在快照内部生效。

    调用方（脚本/装配层）把只读取值函数原样传进来即可，
    快照内部必须自己挡住 provider —— provider 一次都不能被调用，span 恒为 0。
    """
    persona = FakePersona()
    persona.world = SimpleNamespace(follow_public_holidays=False)
    reads = 0

    def _provider() -> List[str]:
        nonlocal reads
        reads += 1
        return ["2026-10-25"]

    # 调用方不做任何判断：直接把 provider 递进去
    builder = PsychologicalSnapshotBuilder(persona=persona, holidays_provider=_provider)
    snapshot = await builder.build_snapshot(
        turn_id="turn-gate-inside",
        user_message="在吗",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )

    assert reads == 0, "闸门必须留在快照内部，provider 不得被调用"
    assert snapshot.action_context["holiday_span"] == 0
    assert persona.activity_calls[-1]["holiday_span"] == 0


@pytest.mark.anyio
async def test_holidays_provider_is_read_only_and_called_once_per_snapshot():
    """provider 是只读函数：每轮 build_snapshot 恰好读一次，且不得写配置或状态。"""
    class _Config:
        def __init__(self) -> None:
            self.reads = 0
            self.writes = 0

        def get_holidays(self) -> List[str]:
            self.reads += 1
            return ["2026-10-25"]

        def set_holidays(self, _value: List[str]) -> None:
            self.writes += 1

    config = _Config()
    persona = FakePersona()
    builder = PsychologicalSnapshotBuilder(persona=persona, holidays_provider=config.get_holidays)

    await builder.build_snapshot(
        turn_id="turn-holiday-ro",
        user_message="在吗",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )
    assert config.reads == 1
    assert config.writes == 0

    await builder.build_snapshot(
        turn_id="turn-holiday-ro-2",
        user_message="还在吗",
        now=datetime(2026, 10, 25, 11, 0, 0),
    )
    assert config.reads == 2
    assert config.writes == 0


@pytest.mark.anyio
async def test_action_context_keeps_all_existing_keys_plus_holiday_span():
    """action_context 必须保留全部既有键，只新增 holiday_span，不得改动任何数学。"""
    persona = FakePersona()
    builder = PsychologicalSnapshotBuilder(
        affection_engine=FakeAffectionReader(state={"stage": 3, "composite": 45.5}),
        mood_engine=FakeMoodReader(state={"v": 0.5, "a": 0.2, "t": 7.5, "frustration": 0.0}),
        persona=persona,
    )

    snapshot = await builder.build_snapshot(
        turn_id="turn-ctx-keys",
        user_message="你好呀",
        now=datetime(2026, 10, 25, 10, 0, 0),
    )

    context = snapshot.action_context
    for key in (
        "hours_since_chat", "composite_affection", "stage_index", "routine_activity",
        "effective_activity", "is_sleeping_routine", "is_chatting", "valence",
        "arousal", "trust", "frustration", "frozen_at", "holiday_span",
    ):
        assert key in context, key

    # 六维与 PAD 数值原样保留（不得因为新增键而改动数学）
    assert context["composite_affection"] == 45.5
    assert context["valence"] == 0.5
    assert context["arousal"] == 0.2
    assert context["trust"] == 7.5
