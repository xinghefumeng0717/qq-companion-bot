"""测试对话内核上下文构建器 (tests/test_kernel_context.py)

覆盖验收要求：
1. filter_action_tags 正则清洗动作与标记，保留纯文本
2. build_system_prompt：
   - 包含角色核心与身份 (name, core_description)
   - 包含重要记忆与生活习惯 (personal_memories, habits)
   - 包含世界书/世界观设定 (lorebook.always)
   - 包含精简说话风格示范与聊天准则
   - 包含当前关系阶段 (stage) 与心理提示 (snapshot.prompt_hint)
   - 严格不包含 voice/face/quote/sticker/沉默协议或完整动作字典
3. build_messages：
   - 严格组装系统提示词与历史记录及当前输入
   - 历史消息通过 semantic_history_view 自然语义转换
   - 引用前置：当存在 turn_input.quoted_text 时，以自然语言“他引用之前的“...”时说：\n...”前置呈现
   - 当前轮采用 turn_input 自然语义，不含协议或动作编号
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional

from companion.kernel.context import (
    ContextBuilder,
    filter_action_tags,
    filter_style_rule,
)
from companion.kernel.contracts import (
    IncomingMessage,
    MemoryPromptBlocks,
    PsychologicalSnapshot,
    StandardizedTurnInput,
)


@dataclass
class FakeMemoryItem:
    title: str
    content: str


@dataclass
class FakeLorebook:
    always: str = "塔卫二是一颗荒凉而充满矿石的星球。终末地工业在这里建立据点。"


@dataclass
class FakeChatStyle:
    rules: List[str] = field(
        default_factory=lambda: [
            "说话保持干练，不拖泥带水",
            "不要输出 [voice:xxx] 标签",
            "不要输出 [sticker:xxx] 表情包",
            "有事说事，直接回答",
        ]
    )
    good_examples: List[str] = field(
        default_factory=lambda: [
            "【起】准备出发了吗？[sticker:123]（递上外套）",
            "今天的工作报告我已经整理好了。",
        ]
    )
    plain_examples: List[str] = field(
        default_factory=lambda: [
            "路上注意安全。",
        ]
    )


@dataclass
class FakeStage:
    name: str = "初识期"
    tone: str = "礼貌客气、稍带距离感"
    instructions: List[str] = field(
        default_factory=lambda: [
            "保持专业工作态度",
            "不要使用 [face:1] 小黄脸",
        ]
    )
    examples: List[str] = field(
        default_factory=lambda: [
            "收到，请管理员指示。（敬礼）",
        ]
    )


@dataclass
class FakeWorld:
    """角色卡的世界观开关（相对日期、节假日口径）。"""

    relative_dates: bool = False
    holiday_note_short: str = "，今天是法定节假日（学校放假，不上课）"
    holiday_note_long: str = "，今天是法定节假日（放长假，她不在学校）"


@dataclass
class FakePersona:
    name: str = "测试员"
    core_description: str = "终末地技术员，性格冷静严谨。"
    personal_memories: List[FakeMemoryItem] = field(
        default_factory=lambda: [
            FakeMemoryItem(title="佩丽卡的手表", content="入职时总管赠予的纪念品"),
        ]
    )
    habits: List[str] = field(
        default_factory=lambda: [
            "清晨检查设备仪表",
        ]
    )
    lorebook: FakeLorebook = field(default_factory=FakeLorebook)
    chat_style: FakeChatStyle = field(default_factory=FakeChatStyle)
    stages: List[FakeStage] = field(default_factory=lambda: [FakeStage()])
    world: Optional[FakeWorld] = None


def test_filter_action_tags():
    """测试动作描写与协议标签的过滤。"""
    raw = "【起】好的，我明白了。（叹了一口气）[sticker:123][face:2][沉默]*递过咖啡*\n【收】再见。"
    cleaned = filter_action_tags(raw)
    assert "【起】" not in cleaned
    assert "【收】" not in cleaned
    assert "[sticker:123]" not in cleaned
    assert "[face:2]" not in cleaned
    assert "[沉默]" not in cleaned
    assert "（叹了一口气）" not in cleaned
    assert "*递过咖啡*" not in cleaned
    assert "好的，我明白了。" in cleaned
    assert "再见。" in cleaned


def test_build_system_prompt_structure_and_no_protocol_leak():
    """测试 system_prompt 包含角色与世界设定，心理提示，且不包含协议字典。"""
    persona = FakePersona()
    snapshot = PsychologicalSnapshot(
        turn_id="turn_test_01",
        frozen_at=datetime.now(),
        state_version=1,
        stage_index=0,
        prompt_hint="此刻你正专注地与他交流，相处分寸保持礼貌客气。内心感受：情绪平静，信任中等。",
        action_context={},
        affection={},
        mood={},
    )
    builder = ContextBuilder(max_examples=3)
    sys_prompt = builder.build_system_prompt(persona, snapshot)

    # 必须保留角色核心、记忆、习惯与世界设定
    assert "测试员" in sys_prompt
    assert "终末地技术员，性格冷静严谨。" in sys_prompt
    assert "佩丽卡的手表" in sys_prompt
    assert "清晨检查设备仪表" in sys_prompt
    assert "塔卫二是一颗荒凉而充满矿石的星球" in sys_prompt

    # 必须保留当前关系阶段与心理提示
    assert "初识期" in sys_prompt
    assert "礼貌客气、稍带距离感" in sys_prompt
    assert "此刻你正专注地与他交流" in sys_prompt

    # 严格检验动作协议与机器字典不泄漏
    for forbidden in ["[voice", "[face", "[sticker", "[quote", "[沉默", "表情包", "小黄脸"]:
        assert forbidden not in sys_prompt, f"发现泄漏协议关键字: {forbidden}"

    # 严格检验示例中的动作描写被剥离
    assert "（递上外套）" not in sys_prompt
    assert "（敬礼）" not in sys_prompt
    assert "准备出发了吗？" in sys_prompt


def test_build_messages_with_quote_and_history():
    """测试 build_messages 的引用前置、历史清洗与结构。"""
    persona = FakePersona()
    snapshot = PsychologicalSnapshot(
        turn_id="turn_test_02",
        frozen_at=datetime.now(),
        state_version=1,
        stage_index=0,
        prompt_hint="测试心理状态",
        action_context={},
        affection={},
        mood={},
    )

    # 历史记录中混杂旧协议
    history = [
        {"role": "user", "content": "今天晚上吃什么？[face:馋]"},
        {"role": "assistant", "content": "[voice:1]食堂今天有土豆烧牛肉。[/voice]"},
    ]

    # 当前轮带引用
    turn_input = StandardizedTurnInput(
        session_id="sess_context_01",
        messages=(IncomingMessage(message_id=901, text="那一份够吃吗？"),),
        text="那一份够吃吗？",
        received_at=datetime.now(),
        reply_to_message_id=801,
        quoted_text="食堂今天有土豆烧牛肉。",
    )

    builder = ContextBuilder(max_history_turns=5)
    messages = builder.build_messages(persona, snapshot, history, turn_input)

    assert len(messages) >= 3
    assert messages[0]["role"] == "system"

    # 检查历史清洗
    user_hist = messages[1]
    assert user_hist["role"] == "user"
    assert "[face:" not in user_hist["content"]
    assert "发了一个“馋”表情" in user_hist["content"]

    asst_hist = messages[2]
    assert asst_hist["role"] == "assistant"
    assert "[voice:" not in asst_hist["content"]
    assert "[/voice]" not in asst_hist["content"]

    # 检查当前用户输入与引用前置
    current_user_msg = messages[-1]
    assert current_user_msg["role"] == "user"
    expected_quote_prefix = "他引用之前的“食堂今天有土豆烧牛肉。”时说：\n那一份够吃吗？"
    assert current_user_msg["content"] == expected_quote_prefix

def test_filter_action_tags_preserves_arithmetic_and_explanations():
    """测试 filter_action_tags 保留算术星号、数字/英文括号与普通解释说明括号，仅清洗明确动作与协议。"""
    # 1. 算术星号场景（2*3*4, 5 * 6 * 7）
    math_text = "请计算 2*3*4 的乘积，或者 5 * 6 * 7。"
    cleaned_math = filter_action_tags(math_text)
    assert "2*3*4" in cleaned_math
    assert "5 * 6 * 7" in cleaned_math

    # 2. 普通数字与英文括号场景
    num_eng_text = "参考条款(1)与第（2）条细则，版本更新为(v1.0)，支持(PDF)格式。"
    cleaned_num_eng = filter_action_tags(num_eng_text)
    assert "(1)" in cleaned_num_eng
    assert "（2）" in cleaned_num_eng
    assert "(v1.0)" in cleaned_num_eng
    assert "(PDF)" in cleaned_num_eng

    # 3. 普通解释说明括号场景
    expl_text = "好的（普通解释括号），这里有一个特例（例如苹果），详情参考（注：这是测试说明）。"
    cleaned_expl = filter_action_tags(expl_text)
    assert "好的（普通解释括号）" in cleaned_expl
    assert "（例如苹果）" in cleaned_expl
    assert "（注：这是测试说明）" in cleaned_expl

    # 4. 混合场景：明确动作描写与协议被剔除，解释与算术保留
    mixed_raw = (
        "【起】好的，我明白了。（叹了一口气）[sticker:123] 结果是 2*3*4。（递上外套）"
        "[face:2] 详细说明（普通解释括号）*递过咖啡* [沉默] 记得查阅(v1.0)文档。（敬礼）\n【收】完毕。"
    )
    cleaned_mixed = filter_action_tags(mixed_raw)
    # 动作与协议被过滤
    assert "【起】" not in cleaned_mixed
    assert "【收】" not in cleaned_mixed
    assert "（叹了一口气）" not in cleaned_mixed
    assert "（递上外套）" not in cleaned_mixed
    assert "（敬礼）" not in cleaned_mixed
    assert "*递过咖啡*" not in cleaned_mixed
    assert "[sticker:123]" not in cleaned_mixed
    assert "[face:2]" not in cleaned_mixed
    assert "[沉默]" not in cleaned_mixed
    # 算术与解释被完整保留
    assert "2*3*4" in cleaned_mixed
    assert "（普通解释括号）" in cleaned_mixed
    assert "(v1.0)" in cleaned_mixed
    assert "好的，我明白了。" in cleaned_mixed


def test_dynamic_lorebook_hits_from_turn_input_and_history():
    """测试世界书动态词条检索：支持当前输入与近期历史动态命中，角色卡完全可替换。"""
    from companion.lorebook import Lorebook, LoreEntry

    # 设定可替换角色（例如虚构学者）及其世界书词条
    dynamic_lore = Lorebook(
        always="星港是文明交汇的中立空港枢纽。",
        entries=[
            LoreEntry(keys=["光晶石", "源石"], content="光晶石是一种蕴含高能量的自然结晶。", priority=10),
            LoreEntry(keys=["远航协议", "巡航舰"], content="远航协议由星区开拓联盟订立。", priority=5),
            LoreEntry(keys=["未提及词条"], content="该词条无对应输入，绝对不应被命中。", priority=1),
        ],
        scan_turns=3,
    )

    persona = FakePersona(
        name="领航员",
        core_description="星港领航员，冷静敏锐。",
        lorebook=dynamic_lore,
    )
    snapshot = PsychologicalSnapshot(
        turn_id="turn_lore_01",
        frozen_at=datetime.now(),
        state_version=1,
        stage_index=0,
        prompt_hint="专注工作中",
        action_context={},
        affection={},
        mood={},
    )
    builder = ContextBuilder()

    # 1. 命中场景 A：当前输入触发关键词 "光晶石"
    turn_input_a = StandardizedTurnInput(
        session_id="sess_lore_01",
        messages=(IncomingMessage(message_id=1, text="分析一下刚才采集的光晶石。"),),
        text="分析一下刚才采集的光晶石。",
        received_at=datetime.now(),
    )
    messages_a = builder.build_messages(persona, snapshot, history=[], turn_input=turn_input_a)
    sys_prompt_a = messages_a[0]["content"]

    # 常驻设定与命中的动态词条均在提示词中
    assert "星港是文明交汇的中立空港枢纽。" in sys_prompt_a
    assert "【世界书】" in sys_prompt_a
    assert "光晶石是一种蕴含高能量的自然结晶。" in sys_prompt_a
    # 未提及词条不在提示词中
    assert "该词条无对应输入" not in sys_prompt_a

    # 2. 命中场景 B：当前输入未命中，但近期历史记录中提到了 "巡航舰"
    history_b = [
        {"role": "user", "content": "港口刚停靠了一艘重型巡航舰。"},
        {"role": "assistant", "content": "收到，正在核对入境身份。"},
    ]
    turn_input_b = StandardizedTurnInput(
        session_id="sess_lore_02",
        messages=(IncomingMessage(message_id=2, text="他们有合法许可吗？"),),
        text="他们有合法许可吗？",
        received_at=datetime.now(),
    )
    messages_b = builder.build_messages(persona, snapshot, history=history_b, turn_input=turn_input_b)
    sys_prompt_b = messages_b[0]["content"]

    assert "远航协议由星区开拓联盟订立。" in sys_prompt_b
    assert "该词条无对应输入" not in sys_prompt_b

    # 3. 未命中场景 C：当前输入与历史均无相关词条
    turn_input_c = StandardizedTurnInput(
        session_id="sess_lore_03",
        messages=(IncomingMessage(message_id=3, text="今天午餐吃了份三明治。"),),
        text="今天午餐吃了份三明治。",
        received_at=datetime.now(),
    )
    messages_c = builder.build_messages(persona, snapshot, history=[], turn_input=turn_input_c)
    sys_prompt_c = messages_c[0]["content"]

    assert "星港是文明交汇的中立空港枢纽。" in sys_prompt_c
    assert "【世界书】" not in sys_prompt_c
    assert "光晶石" not in sys_prompt_c
    assert "远航协议" not in sys_prompt_c


def _snapshot_for_context():
    return PsychologicalSnapshot(
        turn_id="context-stage4", frozen_at=datetime.now(), state_version=1,
        stage_index=0, prompt_hint="平静", action_context={}, affection={}, mood={},
    )


def test_full_style_assets_are_separate_and_unlimited_by_default():
    persona = FakePersona()
    persona.chat_style.good_examples = [f"自然回答{i}" for i in range(7)]
    persona.chat_style.plain_examples = ["嗯", "知道了"]
    persona.chat_style.bad_examples = ["（敬礼）收到，请指示", "[voice:x]机械收尾"]
    prompt = ContextBuilder().build_system_prompt(persona, _snapshot_for_context())
    assert "【正确示范】" in prompt
    assert all(f"自然回答{i}" in prompt for i in range(7))
    assert "【日常废话流基线】" in prompt
    assert "不必每条都接话、提问或表达点什么" in prompt
    assert "【错误示范（禁止）】" in prompt
    assert "收到，请指示" in prompt and "机械收尾" in prompt
    assert "（敬礼）" not in prompt and "[voice" not in prompt


def test_style_rule_only_drops_action_clauses():
    assert filter_style_rule("别发语音，保持自然，别用祈使句") == "保持自然，别用祈使句"
    assert filter_style_rule("不要输出 [sticker:x] 表情包") == ""
    assert filter_style_rule("说话保持干练，不拖泥带水") == "说话保持干练，不拖泥带水"


def test_memory_blocks_insert_in_order_and_empty_is_compatible():
    persona = FakePersona()
    snapshot = _snapshot_for_context()
    builder = ContextBuilder()
    empty = builder.build_system_prompt(persona, snapshot, memory_blocks=MemoryPromptBlocks())
    assert empty == builder.build_system_prompt(persona, snapshot)
    blocks = MemoryPromptBlocks(
        facts="【关于他】\n事实", impressions="【你对他的看法】\n印象",
        self_facts="【你说过的关于自己的事】\n自我", diaries="【她的记忆】\n日记",
        followups="【待跟进】\n跟进", meetups="【约好的见面】\n约定", desires="【欲言又止】\n念头",
        arcs="【生活剧本】\n主线", safety="【安全边界】\n安全",
    )
    prompt = builder.build_system_prompt(persona, snapshot, memory_blocks=blocks)
    expected = ["【关于他】", "【你对他的看法】", "【你说过的关于自己的事】", "【她的记忆】", "【待跟进】", "【约好的见面】", "【欲言又止】", "【生活剧本】", "【安全边界】"]
    positions = [prompt.index(title) for title in expected]
    assert positions == sorted(positions)


def test_dynamic_style_block_stays_before_safety_boundary():
    """真人示例只是参考，不能排在安全边界之后稀释最终安全约束。"""
    from companion.kernel.contracts import StyleExampleSelection

    selection = StyleExampleSelection(
        query="今天好累", seed=1, indices=(0,),
        items=(("今天好累", "先休息"),),
        block="【别人的聊天参考】\n- 他：今天好累 → 她：先休息",
    )
    prompt = ContextBuilder().build_system_prompt(
        FakePersona(),
        _snapshot_for_context(),
        memory_blocks=MemoryPromptBlocks(safety="【安全边界】\n安全"),
        style_examples=selection,
    )

    assert prompt.index("【别人的聊天参考】") < prompt.index("【安全边界】")
    assert prompt.rstrip().endswith("【安全边界】\n安全")


# ==================================================================
# 10. 本轮时间事实块：独立成块，只进 system prompt，不污染 user 正文
# ==================================================================

_NOW = datetime(2026, 10, 26, 15, 4, 0)  # 星期一


def _snapshot_with(
    action_context: dict,
    frozen_at: Optional[datetime] = None,
    prompt_hint: str = "此刻你正专注地与他交流。",
) -> PsychologicalSnapshot:
    return PsychologicalSnapshot(
        turn_id="turn_time_block",
        frozen_at=frozen_at or _NOW,
        state_version=1,
        stage_index=0,
        prompt_hint=prompt_hint,
        action_context=action_context,
        affection={},
        mood={},
    )


def _turn_input(at: Optional[datetime] = None, text: str = "今天设备还顺利吗？") -> StandardizedTurnInput:
    return StandardizedTurnInput(
        session_id="sess_time_block",
        messages=(IncomingMessage(message_id=1, text=text),),
        text=text,
        received_at=at or _NOW,
    )


def test_time_block_is_in_system_prompt_only():
    """当前时间、星期、间隔与作息背景必须出现在 system prompt，且绝不能进 user 正文。"""
    persona = FakePersona()
    snapshot = _snapshot_with({
        "hours_since_chat": 5.6,
        "routine_activity": "在工坊维护设备",
        "effective_activity": "在与他聊天（日常状态：在工坊维护设备）",
        "is_sleeping_routine": False,
        "is_chatting": True,
        "holiday_span": 0,
    })
    builder = ContextBuilder()
    messages = builder.build_messages(persona, snapshot, history=[], turn_input=_turn_input())

    system_prompt = messages[0]["content"]
    user_content = messages[-1]["content"]

    # 独立成块
    assert "【本轮时间】" in system_prompt
    # 日期 / 时刻 / 星期
    assert "2026-10-26" in system_prompt
    assert "15:04" in system_prompt
    assert "星期一" in system_prompt
    # >=3 小时的间隔（旧 gap_note 的向下取整口径）
    assert "距上次聊天已过 5 小时" in system_prompt
    # 活动状态：展示快照已仲裁好的 effective_activity（聊天时把日常作息带在里面）
    assert "活动状态：在与他聊天（日常状态：在工坊维护设备）" in system_prompt
    assert "在工坊维护设备" in system_prompt

    # 这些事实绝不进用户正文
    for token in ("【本轮时间】", "当前日期", "距上次聊天", "活动状态", "2026-10-26", "15:04"):
        assert token not in user_content, token
    assert user_content == "今天设备还顺利吗？"


def test_time_block_uses_snapshot_frozen_at_without_turn_input():
    """直接调 build_system_prompt（没有输入层）时，时间基准取 snapshot.frozen_at。"""
    persona = FakePersona()
    snapshot = _snapshot_with(
        {"routine_activity": "在工坊维护设备", "hours_since_chat": 0.0, "holiday_span": 0},
        frozen_at=datetime(2026, 3, 2, 8, 30, 0),
    )
    prompt = ContextBuilder().build_system_prompt(persona, snapshot)
    assert "2026-03-02" in prompt
    assert "08:30" in prompt
    assert "星期一" in prompt  # 2026-03-02 是星期一


def test_gap_below_three_hours_is_not_shown():
    """不足 3 小时不显示间隔，也不显示任何秒级噪音。"""
    persona = FakePersona()
    for hours in (0.0, 0.5, 1.0, 2.9):
        snapshot = _snapshot_with({
            "hours_since_chat": hours,
            "routine_activity": "在工坊维护设备",
            "holiday_span": 0,
        })
        prompt = ContextBuilder().build_system_prompt(persona, snapshot)
        assert "距上次聊天" not in prompt, hours
        assert str(hours) not in prompt, hours


def test_gap_nan_inf_and_negative_are_safely_ignored():
    """NaN / inf / 负数的间隔必须被安全忽略，绝不泄漏原始浮点值，也不得崩掉组装。"""
    persona = FakePersona()
    for hours in (float("nan"), float("inf"), float("-inf"), -1.0, -100.0, None, "不是数字"):
        snapshot = _snapshot_with({
            "hours_since_chat": hours,
            "routine_activity": "在工坊维护设备",
            "holiday_span": 0,
        })
        prompt = ContextBuilder().build_system_prompt(persona, snapshot)
        assert "距上次聊天" not in prompt, hours
        lowered = prompt.lower()
        for leaked in ("nan", "inf", "-1.0", "-100.0"):
            assert leaked not in lowered, (hours, leaked)


def test_holiday_note_reuses_prompts_holiday_prompt_note():
    """holiday_span>0 时必须复用角色卡节假日口径（短假/长假两句）。"""
    from companion.prompts import (
        LONG_HOLIDAY_PROMPT_NOTE,
        SHORT_HOLIDAY_PROMPT_NOTE,
    )

    persona = FakePersona()
    short_snapshot = _snapshot_with({"holiday_span": 1, "routine_activity": "休息"})
    short_prompt = ContextBuilder().build_system_prompt(persona, short_snapshot)
    assert SHORT_HOLIDAY_PROMPT_NOTE in short_prompt
    assert LONG_HOLIDAY_PROMPT_NOTE not in short_prompt

    long_snapshot = _snapshot_with({"holiday_span": 6, "routine_activity": "放长假中"})
    long_prompt = ContextBuilder().build_system_prompt(persona, long_snapshot)
    assert LONG_HOLIDAY_PROMPT_NOTE in long_prompt

    # 非假期没有节假日附注
    none_snapshot = _snapshot_with({"holiday_span": 0, "routine_activity": "休息"})
    none_prompt = ContextBuilder().build_system_prompt(persona, none_snapshot)
    assert SHORT_HOLIDAY_PROMPT_NOTE not in none_prompt
    assert LONG_HOLIDAY_PROMPT_NOTE not in none_prompt


def test_sleep_routine_with_chatting_does_not_contradict_itself():
    """作息写睡觉、但此刻在聊天时，时间块不得写成"正在睡着又聊天"。"""
    persona = FakePersona()
    snapshot = _snapshot_with(
        {
            "hours_since_chat": 4.0,
            "routine_activity": "睡觉中",
            "effective_activity": "刚被消息吵醒，仍然很困，但正在回应他",
            "is_sleeping_routine": True,
            "is_chatting": True,
            "holiday_span": 0,
        },
        prompt_hint="刚被消息吵醒，仍然很困，但正在回应他。",
    )
    prompt = ContextBuilder().build_system_prompt(persona, snapshot)
    assert "活动状态：刚被消息吵醒，仍然很困，但正在回应他" in prompt
    # 不得出现"正在睡着"这类与当前聊天冲突的写法
    for claim in ("正在熟睡", "正在睡觉", "睡着（其实醒着）"):
        assert claim not in prompt, claim


def test_context_only_displays_snapshot_activity_without_re_arbitrating():
    """ContextBuilder 只展示快照的仲裁结论，不自己判断聊天/睡觉。

    - effective_activity 有值就原样展示（不论 is_chatting / is_sleeping_routine 是什么）；
    - effective_activity 为空才回退 routine_activity；
    - 两者都空就不出现活动状态这一行。
    """
    persona = FakePersona()

    # 1. 快照说醒着在聊天（哪怕作息表写着睡觉），展示的就是快照结论，不做二次特判
    chatting = _snapshot_with({
        "hours_since_chat": 4.0,
        "routine_activity": "睡觉中",
        "effective_activity": "刚被消息吵醒，仍然很困，但正在回应他",
        "is_sleeping_routine": True,
        "is_chatting": True,
        "holiday_span": 0,
    })
    prompt = ContextBuilder().build_system_prompt(persona, chatting)
    assert "活动状态：刚被消息吵醒，仍然很困，但正在回应他" in prompt
    assert "活动状态：睡觉中" not in prompt

    # 2. 非聊天轮：快照结论就是作息表本身，照样展示（不能因为 is_chatting=False 就丢掉）
    idle = _snapshot_with({
        "hours_since_chat": 0.2,
        "routine_activity": "睡觉中",
        "effective_activity": "睡觉中",
        "is_sleeping_routine": True,
        "is_chatting": False,
        "holiday_span": 0,
    })
    prompt = ContextBuilder().build_system_prompt(persona, idle)
    assert "活动状态：睡觉中" in prompt

    # 3. effective_activity 为空才回退 routine_activity
    fallback = _snapshot_with({
        "hours_since_chat": 0.2,
        "routine_activity": "在工坊维护设备",
        "effective_activity": "",
        "holiday_span": 0,
    })
    prompt = ContextBuilder().build_system_prompt(persona, fallback)
    assert "活动状态：在工坊维护设备" in prompt

    # 4. 两者都空就不出现这一行
    empty = _snapshot_with({"hours_since_chat": 0.2, "holiday_span": 0})
    prompt = ContextBuilder().build_system_prompt(persona, empty)
    assert "活动状态" not in prompt


def test_relative_dates_relativizes_current_date_and_memory_dates():
    """world.relative_dates 为真时：当前日期显示为"今天"，记忆块日期按同一参照转换。"""
    persona = FakePersona(world=FakeWorld(relative_dates=True))
    snapshot = _snapshot_with({"holiday_span": 0, "routine_activity": "在工坊维护设备"})
    blocks = MemoryPromptBlocks(
        facts="【关于他】\n  - 他在 2026-10-25 说过要检修设备"
    )
    prompt = ContextBuilder().build_system_prompt(persona, snapshot, memory_blocks=blocks)

    time_block = prompt.split("【本轮时间】", 1)[1].split("\n\n", 1)[0]
    assert "今天" in time_block
    assert "2026-10-26" not in prompt
    assert "昨天" in prompt
    assert "2026-10-25" not in prompt


def test_absolute_dates_mode_keeps_concrete_date():
    """绝对日期模式保持具体日期，不做相对化。"""
    persona = FakePersona(world=FakeWorld(relative_dates=False))
    snapshot = _snapshot_with({"holiday_span": 0, "routine_activity": "在工坊维护设备"})
    blocks = MemoryPromptBlocks(
        facts="【关于他】\n  - 他在 2026-10-25 说过要检修设备"
    )
    prompt = ContextBuilder().build_system_prompt(persona, snapshot, memory_blocks=blocks)

    time_block = prompt.split("【本轮时间】", 1)[1].split("\n\n", 1)[0]
    assert "2026-10-26" in time_block
    assert "2026-10-25" in prompt
    assert "今天" not in time_block
