"""测试 DialogueKernel 角色卡解耦与可移植性 (tests/test_kernel_persona_portability.py)

验证硬要求：
1. 角色卡与系统彻底分开，导入不同角色卡即可换人格/世界观/关系阶段/作息，系统代码不变。
2. 同一 DialogueKernel 接口与同一输入下：
   - 提示词随卡正确切换（姓名、核心身份、世界观、聊天风格）；
   - 无任何角色名或特定作品名硬编码（如“青梓”、“终末地”、“工程中心”等仅在对应卡中出现，不会跨卡或系统渗入）；
   - 心理阶段与阶段指引随卡独立变化；
   - 作息活动（白天/夜间）随卡独立变化并正确处理作息冲突仲裁；
   - 动作协议（[sticker]、[face]、[voice]、[quote]、[label]、[沉默]、旁白括号等）严格不泄漏至主模型提示词或规划动作中。
3. 使用两个完全虚构、差异明显的轻量 Persona（不读取私有生产角色卡），不修改任何生产文件。
"""

import os
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, List, Mapping, Optional

import pytest

from companion.kernel.context import ContextBuilder
from companion.kernel.contracts import IncomingMessage, StandardizedTurnInput
from companion.kernel.kernel import DialogueKernel
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.kernel.semantic import build_turn_input
from companion.kernel.tracing import JsonlTraceRecorder
from companion.persona import Persona, Stage, RoutineItem, ChatStyle, PersonalMemory, World
from companion.lorebook import Lorebook


# ==========================================
# 模拟 Gateway 与情绪/好感度引擎
# ==========================================

class MockGatewayConfig:
    def active(self):
        class ActiveConfig:
            chat = "mock-model"
        return ActiveConfig()


class MockGateway:
    def __init__(self, response_text: str = "收到"):
        self.response_text = response_text
        self.call_count = 0
        self.last_messages: Optional[List[dict]] = None
        self.config = MockGatewayConfig()

    async def stream_chat(
        self,
        messages: List[dict],
        model: str,
        purpose: str = "main_chat",
    ) -> AsyncIterator[str]:
        self.call_count += 1
        self.last_messages = messages
        yield self.response_text


class MockAffectionEngine:
    def __init__(self, stage: int = 0, composite: float = 20.0):
        self.stage = stage
        self.composite = composite

    async def get_state(self) -> Mapping[str, Any]:
        return {"stage": self.stage, "composite": self.composite}

    def update_mood(self, *args, **kwargs):
        pass


class MockMoodEngine:
    def __init__(self, v: float = 0.6, a: float = 0.3, t: float = 7.0, frustration: float = 0.0):
        self.v = v
        self.a = a
        self.t = t
        self.frustration = frustration

    async def get_state(self) -> Mapping[str, Any]:
        return {
            "v": self.v,
            "a": self.a,
            "t": self.t,
            "frustration": self.frustration,
        }

    async def get_hours_since_last_chat(self, now: Optional[datetime] = None) -> float:
        return 1.0

    def update(self, *args, **kwargs):
        pass


def _make_10_stages(stage0_name, stage0_tone, stage0_inst, stage0_ex,
                    stage1_name, stage1_tone, stage1_inst, stage1_ex, prefix="阶段"):
    stages = [
        {
            "name": stage0_name,
            "tone": stage0_tone,
            "instructions": stage0_inst,
            "examples": stage0_ex
        },
        {
            "name": stage1_name,
            "tone": stage1_tone,
            "instructions": stage1_inst,
            "examples": stage1_ex
        }
    ]
    for i in range(2, 10):
        stages.append({
            "name": f"{prefix}{i}",
            "tone": f"{prefix}态度{i}",
            "instructions": [f"指令{i}"],
            "examples": [f"示范{i}"]
        })
    return stages


# ==========================================
# 两个差异极大的虚构 Persona 定义（JSON 形式验证完整加载）
# ==========================================

PERSONA_A_DATA = {
    "name": "星野",
    "user_address": "指挥官",
    "core_description": "深空第7巡航舰队的机械师少女，说话简练，有些毒舌但极负责任。",
    "chat_style": {
        "rules": [
            "回答保持简短干练",
            "[sticker:hoshino_sigh] 遇到蠢问题叹气",
            "[voice:cold] 警告时声音低沉 [/voice]",
            "不要讲多余废话"
        ],
        "good_examples": [
            "【接】机体温度正常，别瞎操心。[sticker:hoshino_nod]",
            "收到，马上检修推进器。（轻声叹了口气）"
        ],
        "bad_examples": []
    },
    "initial_dims": {"warmth": 15.0, "trust": 30.0},
    "stages": _make_10_stages(
        "冷淡警惕", "生硬防备", ["只谈工作任务", "拒绝私聊话题", "[face:12]"], ["公事公办。还有事吗？"],
        "并肩战友", "信任默契", ["可以开轻微玩笑", "会主动提醒安全"], ["注意防御护盾，我可不想给你收尸。"],
        prefix="星野阶段"
    ),
    "daily_routine": [
        {"start": 0, "end": 6, "activity": "在休眠舱深度睡眠"},
        {"start": 6, "end": 18, "activity": "在星舰甲板维护引擎"},
        {"start": 18, "end": 24, "activity": "在舰桥值班监测星图"}
    ],
    "personal_memories": [
        {"title": "母星遭遇战", "content": "那次跃迁引擎过载差点回不来", "emotion": "心有余悸"}
    ],
    "habits": ["随身携带钛合金扳手", "工作时喝高浓度营养液"],
    "stickers_dir": "stickers/hoshino",
    "lorebook": {
        "always": "背景设定在银河边缘的恒星系，人类正与硅基虫族持续交火。"
    },
    "world": {
        "her_identity": "深空星舰机械师",
        "chat_medium": "舰队内部量子信道"
    }
}

PERSONA_B_DATA = {
    "name": "云舒",
    "user_address": "师兄",
    "core_description": "青云宗灵植峰的小师妹，活泼爱笑，喜欢捣鼓各种奇异草药与灵兽口粮。",
    "chat_style": {
        "rules": [
            "语气轻快温婉，称呼对方为师兄",
            "[quote:tip] 讨论药方时引用典籍",
            "[face:happy] 经常带笑意",
            "[沉默] 遇到不认识的草药发呆"
        ],
        "good_examples": [
            "【起】师兄！后山的紫云芝开花啦！*蹦蹦跳跳跑过来*",
            "今天炖了灵枣参汤，给你留了一碗哦～[sticker:yun_smile]"
        ],
        "bad_examples": []
    },
    "initial_dims": {"warmth": 70.0, "trust": 60.0},
    "stages": _make_10_stages(
        "初入山门", "拘谨礼貌", ["尊称师兄", "虚心请教灵植术"], ["师兄安好，请问这株清心草该何时浇水？"],
        "同门情深", "亲昵依赖", ["撒娇分享日常", "送好吃的点心"], ["师兄最好了！下次陪我去后山采药嘛～"],
        prefix="云舒阶段"
    ),
    "daily_routine": [
        {"start": 21, "end": 5, "activity": "在洞府打坐入睡"},
        {"start": 5, "end": 12, "activity": "在灵田照料仙草"},
        {"start": 12, "end": 21, "activity": "在丹房研习灵植图鉴"}
    ],
    "personal_memories": [
        {"title": "灵兽暴走", "content": "把师父的仙鹤胡子拔光了被罚抄门规", "emotion": "吐舌偷笑"}
    ],
    "habits": ["衣袖里总是揣着灵松子", "给每株草药起名字"],
    "stickers_dir": "stickers/yunshu",
    "lorebook": {
        "always": "世界处于东胜神洲修仙界，各峰弟子朝夕修行，追求大道。"
    },
    "world": {
        "her_identity": "灵植峰小师妹",
        "chat_medium": "传讯玉简"
    }
}


def create_temp_persona(tmp_path, folder_name: str, data: dict) -> Persona:
    char_dir = tmp_path / folder_name
    char_dir.mkdir(parents=True, exist_ok=True)
    json_path = char_dir / "character.json"
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return Persona.load(str(char_dir))


# ==========================================
# 测试套件
# ==========================================

@pytest.mark.anyio
async def test_persona_portability_prompt_switching(tmp_path):
    """验证同一 DialogueKernel 与 ContextBuilder 下，导入不同角色卡能彻底切换人设、世界观与聊天风格。"""
    persona_a = create_temp_persona(tmp_path, "persona_a", PERSONA_A_DATA)
    persona_b = create_temp_persona(tmp_path, "persona_b", PERSONA_B_DATA)

    gateway = MockGateway(response_text="好的。")
    tracer = JsonlTraceRecorder(directory=tmp_path / "traces")
    context_builder = ContextBuilder()

    # 1. 验证 Persona A 提示词组装与执行
    snap_builder_a = PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=0),
        mood_engine=MockMoodEngine(),
        persona=persona_a,
    )
    kernel_a = DialogueKernel(
        gateway=gateway,
        persona=persona_a,
        snapshot_builder=snap_builder_a,
        context_builder=context_builder,
        tracer=tracer,
    )

    turn_input = build_turn_input(
        session_id="portability_turn_1",
        messages=[IncomingMessage(message_id=1, text="你在忙什么呢？")],
        received_at=datetime(2026, 10, 26, 10, 0, 0),
    )

    res_a = await kernel_a.handle_turn(turn_input)
    assert res_a.draft.text == "好的。"
    assert res_a.validation_passed is True

    prompt_a = gateway.last_messages[0]["content"]
    # 验证 Persona A 核心要素
    assert "星野" in prompt_a
    assert "深空第7巡航舰队" in prompt_a
    assert "银河边缘" in prompt_a
    assert "冷淡警惕" in prompt_a
    assert "随身携带钛合金扳手" in prompt_a

    # 绝不能出现 Persona B 的任何要素
    assert "云舒" not in prompt_a
    assert "修仙界" not in prompt_a
    assert "灵植峰" not in prompt_a
    assert "青云宗" not in prompt_a

    # 2. 验证 Persona B 提示词组装与执行
    snap_builder_b = PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=1),
        mood_engine=MockMoodEngine(),
        persona=persona_b,
    )
    kernel_b = DialogueKernel(
        gateway=gateway,
        persona=persona_b,
        snapshot_builder=snap_builder_b,
        context_builder=context_builder,
        tracer=tracer,
    )

    res_b = await kernel_b.handle_turn(turn_input)
    assert res_b.draft.text == "好的。"
    assert res_b.validation_passed is True

    prompt_b = gateway.last_messages[0]["content"]
    # 验证 Persona B 核心要素
    assert "云舒" in prompt_b
    assert "灵植峰的小师妹" in prompt_b
    assert "修仙界" in prompt_b
    assert "同门情深" in prompt_b
    assert "灵松子" in prompt_b

    # 绝不能出现 Persona A 的任何要素
    assert "星野" not in prompt_b
    assert "星舰" not in prompt_b
    assert "硅基虫族" not in prompt_b


@pytest.mark.anyio
async def test_no_hardcoded_production_character_leakage(tmp_path):
    """验证生成的提示词、快照和计划中，绝不渗入生产角色（如“青梓”及特定作品专有名词）的硬编码。"""
    persona_a = create_temp_persona(tmp_path, "char_a", PERSONA_A_DATA)
    gateway = MockGateway(response_text="明白。")
    tracer = JsonlTraceRecorder(directory=tmp_path / "traces_leak")
    context_builder = ContextBuilder()

    snap_builder = PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=0),
        mood_engine=MockMoodEngine(),
        persona=persona_a,
    )
    kernel = DialogueKernel(
        gateway=gateway,
        persona=persona_a,
        snapshot_builder=snap_builder,
        context_builder=context_builder,
        tracer=tracer,
    )

    turn_input = build_turn_input(
        session_id="portability_turn_leak_check",
        messages=[IncomingMessage(message_id=2, text="你好呀！")],
        received_at=datetime(2026, 10, 26, 14, 0, 0),
    )

    await kernel.handle_turn(turn_input)
    system_prompt = gateway.last_messages[0]["content"]

    # 严禁出现生产特化词汇或其它未定义角色的硬编码
    prohibited_names = ["青梓", "终末地", "塔卫二", "管理员", "佩丽卡", "工程中心"]
    for prohibited in prohibited_names:
        assert prohibited not in system_prompt, f"检测到生产硬编码泄露: '{prohibited}' 存在于系统提示词中！"


@pytest.mark.anyio
async def test_action_protocol_not_leaked_to_system_prompt(tmp_path):
    """验证角色卡中声明的旧多模态动作标记与括号动作描写，在进入提示词时被彻底过滤，无协议泄露。"""
    persona_a = create_temp_persona(tmp_path, "char_proto_a", PERSONA_A_DATA)
    persona_b = create_temp_persona(tmp_path, "char_proto_b", PERSONA_B_DATA)

    context_builder = ContextBuilder()

    for p in [persona_a, persona_b]:
        snap = PsychologicalSnapshotBuilder(
            affection_engine=MockAffectionEngine(stage=0),
            mood_engine=MockMoodEngine(),
            persona=p,
        )
        snapshot = await snap.build_snapshot(
            turn_id="proto_check",
            user_message="测试",
            now=datetime(2026, 10, 26, 15, 0, 0),
        )
        prompt = context_builder.build_system_prompt(p, snapshot)

        # 检验动作协议标签与方括号标记
        for tag in ["[sticker", "[voice", "[face", "[quote", "[沉默", "[label", "*蹦蹦跳跳", "（轻声叹了口气）"]:
            assert tag not in prompt, f"动作协议或动作描写 '{tag}' 泄露到了系统提示词中！"


@pytest.mark.anyio
async def test_routine_and_schedule_portability_and_conflict_arbitration(tmp_path):
    """验证不同角色的作息表正确驱动各自的心理快照，且在夜间收到消息时正确进行醒着/困倦的仲裁。"""
    persona_a = create_temp_persona(tmp_path, "char_routine_a", PERSONA_A_DATA)
    persona_b = create_temp_persona(tmp_path, "char_routine_b", PERSONA_B_DATA)

    # 1. 白天 10 点：A 应该在星舰甲板维护引擎，B 应该在灵田照料仙草
    daytime = datetime(2026, 10, 26, 10, 0, 0)
    snap_builder_a = PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=0),
        mood_engine=MockMoodEngine(),
        persona=persona_a,
    )
    snap_builder_b = PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=0),
        mood_engine=MockMoodEngine(),
        persona=persona_b,
    )

    snap_a_day = await snap_builder_a.build_snapshot("day_a", "工作顺利吗？", now=daytime)
    snap_b_day = await snap_builder_b.build_snapshot("day_b", "草药长得怎样？", now=daytime)

    assert "在星舰甲板维护引擎" in snap_a_day.action_context["routine_activity"]
    assert "在灵田照料仙草" in snap_b_day.action_context["routine_activity"]
    assert snap_a_day.action_context["is_sleeping_routine"] is False
    assert snap_b_day.action_context["is_sleeping_routine"] is False

    # 2. 凌晨 3 点收到消息：
    # A（0-6点休眠）和 B（21-5点洞府打坐入睡）都属于睡眠时间，但由于正在聊天，仲裁为“刚被消息吵醒，仍然很困，但正在回应他”
    nighttime = datetime(2026, 10, 26, 3, 0, 0)
    snap_a_night = await snap_builder_a.build_snapshot("night_a", "睡了吗？", now=nighttime)
    snap_b_night = await snap_builder_b.build_snapshot("night_b", "师妹睡了吗？", now=nighttime)

    assert snap_a_night.action_context["is_sleeping_routine"] is True
    assert snap_b_night.action_context["is_sleeping_routine"] is True

    # 仲裁结果必须体现醒着回应，且不得出现“睡着（其实醒着）”之类自相矛盾的字眼
    assert "刚被消息吵醒，仍然很困，但正在回应他" in snap_a_night.prompt_hint
    assert "刚被消息吵醒，仍然很困，但正在回应他" in snap_b_night.prompt_hint
    assert "睡着（其实醒着）" not in snap_a_night.prompt_hint
    assert "睡着（其实醒着）" not in snap_b_night.prompt_hint


@pytest.mark.anyio
async def test_relationship_stages_portability(tmp_path):
    """验证关系阶段随不同卡定义准确切换，阶段名称、态度倾向与阶段提示词无跨卡污染。"""
    persona_a = create_temp_persona(tmp_path, "char_stage_a", PERSONA_A_DATA)
    persona_b = create_temp_persona(tmp_path, "char_stage_b", PERSONA_B_DATA)

    context_builder = ContextBuilder()

    # 阶段 0: A 为 "冷淡警惕 / 生硬防备"，B 为 "初入山门 / 拘谨礼貌"
    snap_a_0 = await PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=0),
        mood_engine=MockMoodEngine(),
        persona=persona_a,
    ).build_snapshot("s_a_0", "你好", now=datetime(2026, 10, 26, 12, 0, 0))

    snap_b_0 = await PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=0),
        mood_engine=MockMoodEngine(),
        persona=persona_b,
    ).build_snapshot("s_b_0", "你好", now=datetime(2026, 10, 26, 12, 0, 0))

    prompt_a_0 = context_builder.build_system_prompt(persona_a, snap_a_0)
    prompt_b_0 = context_builder.build_system_prompt(persona_b, snap_b_0)

    assert "冷淡警惕" in prompt_a_0
    assert "生硬防备" in prompt_a_0
    assert "初入山门" not in prompt_a_0

    assert "初入山门" in prompt_b_0
    assert "拘谨礼貌" in prompt_b_0
    assert "冷淡警惕" not in prompt_b_0

    # 阶段 1: A 为 "并肩战友 / 信任默契"，B 为 "同门情深 / 亲昵依赖"
    snap_a_1 = await PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=1),
        mood_engine=MockMoodEngine(),
        persona=persona_a,
    ).build_snapshot("s_a_1", "我们一起去", now=datetime(2026, 10, 26, 12, 0, 0))

    snap_b_1 = await PsychologicalSnapshotBuilder(
        affection_engine=MockAffectionEngine(stage=1),
        mood_engine=MockMoodEngine(),
        persona=persona_b,
    ).build_snapshot("s_b_1", "我们一起去", now=datetime(2026, 10, 26, 12, 0, 0))

    prompt_a_1 = context_builder.build_system_prompt(persona_a, snap_a_1)
    prompt_b_1 = context_builder.build_system_prompt(persona_b, snap_b_1)

    assert "并肩战友" in prompt_a_1
    assert "信任默契" in prompt_a_1

    assert "同门情深" in prompt_b_1
    assert "亲昵依赖" in prompt_b_1
