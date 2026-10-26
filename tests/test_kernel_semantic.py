"""测试内核语义抽象层 (tests/test_kernel_semantic.py)

覆盖验收要求：
1. format_incoming_messages / build_turn_input:
   - 吃糖表情保留自然语义，无数字编号，无动作协议
   - 多条消息聚合自然语言，单条消息直接返回自然语义
   - 回复目标 ID (reply_to_message_id) 取当前用户消息，quoted_text 独立承担历史引用理解
   - 真实 message_id 保留不丢失
2. semantic_history_view:
   - 历史记录只读语义视图
   - 历史语音只剥离标记并保留正文，不生成“用语音说”模板
   - 历史中的 [face:xxx]、[sticker:xxx] 转换为自然表述「发了一个“xxx”表情」
   - 历史视图不再出现（语音）/（表情：）等括号记号，自然内容完整保留
   - 剥离 [quote:N]、[沉默]、[SILENT] 等内部机器协议
   - 不修改数据库原始数据，不向模型输出协议教学
3. create_reply_draft 与 ReplyBubble:
   - 气泡段落切分、空白过滤
   - 气泡 ID 稳定且幂等
4. create_delivery_plan 与 validate_delivery_plan:
   - 纯文字 text 动作
   - 气泡投递幂等
   - 拒绝重复 bubble_id
   - 拒绝缺失 bubble_id
   - 拒绝未知 bubble_id
   - 拒绝非 text channel
   - 拒绝空草稿（非沉默草稿至少需要一个气泡）
   - 拒绝 draft_id 不匹配
"""

import pytest
from datetime import datetime
from companion.kernel.contracts import (
    DeliveryAction,
    DeliveryPlan,
    IncomingMessage,
    ReplyBubble,
    ReplyDraft,
)
from companion.kernel.semantic import (
    build_turn_input,
    create_delivery_plan,
    create_reply_draft,
    format_incoming_messages,
    semantic_history_view,
    validate_delivery_plan,
)


def test_format_incoming_messages_single_and_sugar_face():
    """测试吃糖表情保留自然语义且无数字编号与协议。"""
    msg1 = IncomingMessage(message_id=101, text="睡了啊那肯定", kind="text")
    assert format_incoming_messages([msg1]) == "睡了啊那肯定"

    msg2 = IncomingMessage(
        message_id=102,
        text="吃糖",
        kind="face",
        semantic_text="发了一个“吃糖”表情",
    )
    res2 = format_incoming_messages([msg2])
    assert "吃糖" in res2
    assert "发了一个“吃糖”表情" in res2
    assert "[face:" not in res2
    assert "[1]" not in res2
    assert "1." not in res2

    # 文本 + 表情恰好两条时，合成一个自然整体，不再拆成两行
    combined = format_incoming_messages([msg1, msg2])
    assert combined == "对方说：“睡了啊那肯定”，同时附上一个小表情“吃糖”"
    assert "对方连着发来了以下内容：" not in combined
    assert "吃糖" in combined
    assert "1." not in combined
    assert "[1]" not in combined
    assert "【1】" not in combined
    assert "[face:" not in combined


def test_format_incoming_messages_merges_text_and_face_in_both_orders():
    """测试文本+表情（两种顺序）合成一个自然整体，表情不成为独立话题。"""
    text_msg = IncomingMessage(message_id=201, text="睡了啊那肯定", kind="text")
    face_msg = IncomingMessage(
        message_id=202,
        text="吃糖",
        kind="face",
        semantic_text="发了一个“吃糖”表情",
    )

    # 1. 文本在前
    res_text_first = format_incoming_messages([text_msg, face_msg])
    assert res_text_first == "对方说：“睡了啊那肯定”，同时附上一个小表情“吃糖”"
    assert "\n" not in res_text_first
    assert "对方连着发来了以下内容：" not in res_text_first
    assert "吃糖" in res_text_first
    assert "[face:" not in res_text_first
    assert "发了一个" not in res_text_first

    # 2. 表情在前
    res_face_first = format_incoming_messages([face_msg, text_msg])
    assert res_face_first == "对方先附上一个小表情“吃糖”，接着说：“睡了啊那肯定”"
    assert "\n" not in res_face_first
    assert "对方连着发来了以下内容：" not in res_face_first
    assert "吃糖" in res_face_first
    assert "[face:" not in res_face_first
    assert "发了一个" not in res_face_first

    # 3. 顺序不同，输出形态不同，但都保留原始标签、不编造含义
    assert res_text_first != res_face_first
    for res in (res_text_first, res_face_first):
        assert "睡了啊那肯定" in res
        assert "吃糖" in res
        assert "开心" not in res
        assert "糖" in res and "吃糖" in res


def test_format_incoming_messages_keeps_normal_batches_unchanged():
    """测试普通多消息批次行为保持不变：仍是“对方连着发来了以下内容”聚合。"""
    msg_a = IncomingMessage(message_id=301, text="在吗", kind="text")
    msg_b = IncomingMessage(message_id=302, text="刚洗完澡", kind="text")

    two_texts = format_incoming_messages([msg_a, msg_b])
    assert two_texts == "对方连着发来了以下内容：\n在吗\n刚洗完澡"

    # 混入表情的批次走“合成一个自然整体”这条路（见下方专门用例）
    msg_c = IncomingMessage(message_id=303, text="吃糖", kind="face")
    three_mixed = format_incoming_messages([msg_a, msg_b, msg_c])
    assert "对方连着发来了以下内容：" not in three_mixed
    assert "在吗" in three_mixed
    assert "刚洗完澡" in three_mixed
    assert "吃糖" in three_mixed

    # 两条表情仍然走普通聚合
    two_faces = format_incoming_messages(
        [
            IncomingMessage(message_id=304, text="吃糖", kind="face"),
            IncomingMessage(message_id=305, text="点赞", kind="sticker"),
        ]
    )
    assert two_faces.startswith("对方连着发来了以下内容：\n")
    assert "吃糖" in two_faces
    assert "点赞" in two_faces

    # 单条仍然直接返回自然语义
    assert format_incoming_messages([msg_a]) == "在吗"
    assert format_incoming_messages([]) == ""


def test_build_turn_input_preserves_message_ids_and_quotes():
    """测试真实 message_id 保留；回复目标取当前用户消息，引用历史由 quoted_text 承担。"""
    msg_normal = IncomingMessage(
        message_id=5001,
        text="好的",
        kind="text",
    )
    msg_quote = IncomingMessage(
        message_id=5002,
        text="确实是这样",
        kind="text",
        quoted_message_id=4001,
        payload={
            "quoted_text": "今天天气真好",
            "quoted_semantic_text": "今天天气真好",
        },
    )

    now = datetime(2026, 10, 26, 12, 0, 0)
    turn_input = build_turn_input(
        session_id="session_test_01",
        messages=[msg_normal, msg_quote],
        received_at=now,
    )

    assert turn_input.session_id == "session_test_01"
    assert turn_input.received_at == now
    assert len(turn_input.messages) == 2
    assert turn_input.messages[0].message_id == 5001
    assert turn_input.messages[1].message_id == 5002
    # 回复目标是当前用户消息 5002，而不是用户引用的历史消息 4001
    assert turn_input.reply_to_message_id == 5002
    assert turn_input.reply_to_message_id != 4001
    # 引用的历史消息语义由 quoted_text 独立承担
    assert turn_input.quoted_text == "今天天气真好"

    turn_input_override = build_turn_input(
        session_id="session_test_02",
        messages=[msg_normal, msg_quote],
        reply_to_message_id=9999,
    )
    assert turn_input_override.reply_to_message_id == 9999
    assert turn_input_override.quoted_text == "今天天气真好"


def test_build_turn_input_reply_target_ignores_quoted_history():
    """测试回复目标绝不会被用户引用的历史消息 ID 污染。

    场景：旧消息 A=4001，当前用户消息 B=5002 引用了 A。
    回复计划必须引用 B，而不是 A。
    """
    old_msg = IncomingMessage(message_id=4001, text="今天天气真好", kind="text")
    current_msg = IncomingMessage(
        message_id=5002,
        text="确实是这样",
        kind="text",
        quoted_message_id=4001,
        payload={"quoted_text": "今天天气真好"},
    )

    turn = build_turn_input("s", [current_msg])
    assert turn.reply_to_message_id == 5002
    assert turn.quoted_text == "今天天气真好"

    # 批次包含多条消息时，回复目标取最后一条真实 message_id，不受历史引用影响
    turn_mixed = build_turn_input("s", [old_msg, current_msg])
    assert turn_mixed.reply_to_message_id == 5002


def test_build_turn_input_reply_target_defaults_to_current_message():
    """测试普通发言（无引用）同样引用当前这条用户消息。"""
    msg = IncomingMessage(message_id=6001, text="在吗", kind="text")
    turn = build_turn_input("s", [msg])
    assert turn.reply_to_message_id == 6001
    assert turn.quoted_text == ""


def test_build_turn_input_explicit_reply_target_wins():
    """测试显式给出的回复目标优先于批次推导结果。"""
    msg = IncomingMessage(message_id=7001, text="在吗", kind="text")
    turn = build_turn_input("s", [msg], reply_to_message_id=8888)
    assert turn.reply_to_message_id == 8888


def test_build_turn_input_without_real_message_ids_has_no_reply_target():
    """测试批次消息没有真实 message_id 时，不臆造回复目标。"""
    msg = IncomingMessage(message_id=None, text="在吗", kind="text")
    turn = build_turn_input("s", [msg])
    assert turn.reply_to_message_id is None


def test_semantic_history_view_removes_machine_protocols():
    """测试历史语音/表情/协议转为自然语义，不保留机器协议。"""
    # 1. 历史语音只保留正文，不保留协议或“用语音说”模板
    raw_history_voice = "[voice:]早点休息，明天见[/voice]\n(语音消息)晚安啦\n[voice: 路上小心 ]"
    view_voice = semantic_history_view(raw_history_voice)
    assert "[voice:" not in view_voice
    assert "[/voice]" not in view_voice
    assert "(语音消息)" not in view_voice
    assert view_voice.splitlines() == ["早点休息，明天见", "晚安啦", "路上小心"]
    assert "用语音说" not in view_voice
    # 不得再出现括号记号
    assert "（语音）" not in view_voice
    assert "(语音)" not in view_voice
    # 原始文字段保留
    assert "早点休息，明天见" in view_voice
    assert "晚安啦" in view_voice
    assert "路上小心" in view_voice

    # 2. 表情与贴纸：发了一个“xxx”表情
    raw_history_face = "你看这个[face:点赞]还有这个[sticker:猫猫探头]"
    view_face = semantic_history_view(raw_history_face)
    assert "[face:" not in view_face
    assert "[sticker:" not in view_face
    assert "发了一个“点赞”表情" in view_face
    assert "发了一个“猫猫探头”表情" in view_face
    assert "（表情：点赞）" not in view_face
    assert "（表情：猫猫探头）" not in view_face
    assert "表情：" not in view_face
    # 原始文字段保留
    assert "点赞" in view_face
    assert "猫猫探头" in view_face

    # 3. 引用与沉默协议
    raw_history_protocol = "[quote:8848] 你刚才说的那个事情\n[沉默]\n[SILENT]\n我赞成"
    view_protocol = semantic_history_view(raw_history_protocol)
    assert "[quote:" not in view_protocol
    assert "[8848]" not in view_protocol
    assert "[沉默]" not in view_protocol
    assert "[SILENT]" not in view_protocol
    assert "你刚才说的那个事情" in view_protocol
    assert "我赞成" in view_protocol


def test_semantic_history_view_has_no_bracket_markers():
    """测试历史视图不再输出（语音）/（表情：）等括号记号，自然内容与顺序完整保留。"""
    raw_history = (
        "[voice:]今天好累[/voice]\n"
        "(语音消息)早点睡\n"
        "[voice: 明天再说 ]\n"
        "[face:吃糖][sticker:猫猫探头]\n"
        "[quote:1234] 一句正文\n"
        "最后一句"
    )
    view = semantic_history_view(raw_history)

    # 括号记号彻底消失
    assert "（语音）" not in view
    assert "(语音)" not in view
    assert "（表情：" not in view
    assert "(表情：" not in view
    assert "表情：" not in view

    # 协议标记不泄漏
    assert "[voice:" not in view
    assert "[/voice]" not in view
    assert "[face:" not in view
    assert "[sticker:" not in view
    assert "[quote:" not in view

    # 历史语音只保留正文，表情仍沿用原有语义，不扩大本阶段范围
    assert "用语音说" not in view
    assert "发了一个“吃糖”表情" in view
    assert "发了一个“猫猫探头”表情" in view

    # 自然内容与顺序保留
    assert "今天好累" in view
    assert "早点睡" in view
    assert "明天再说" in view
    assert "吃糖" in view
    assert "猫猫探头" in view
    assert "一句正文" in view
    assert "最后一句" in view
    lines = view.splitlines()
    assert lines.index("今天好累") < lines.index("早点睡")
    face_line_idx = next(i for i, line in enumerate(lines) if "吃糖" in line)
    assert face_line_idx < lines.index("最后一句")


def test_create_reply_draft_idempotent_bubbles():
    """测试创建原稿及气泡的幂等性与切分逻辑。"""
    text = "第一行气泡\n\n第二行气泡\n  第三行气泡  \n"
    fixed_time = datetime(2026, 10, 26, 10, 0, 0)
    draft1 = create_reply_draft(text, draft_id="draft_fixed_01", created_at=fixed_time)
    draft2 = create_reply_draft(text, draft_id="draft_fixed_01", created_at=fixed_time)

    assert draft1.draft_id == "draft_fixed_01"
    assert len(draft1.bubbles) == 3
    assert draft1.bubbles[0].text == "第一行气泡"
    assert draft1.bubbles[1].text == "第二行气泡"
    assert draft1.bubbles[2].text == "第三行气泡"
    assert [b.bubble_id for b in draft1.bubbles] == [b.bubble_id for b in draft2.bubbles]


def test_create_and_validate_delivery_plan_success():
    """测试合法纯文字投递计划创建与校验。"""
    draft = create_reply_draft("好的。\n明天见！", draft_id="draft_valid_01")
    plan = create_delivery_plan(draft, quote_message_id=12345)

    assert plan.draft_id == "draft_valid_01"
    assert plan.quote_message_id == 12345
    assert len(plan.actions) == 2
    assert plan.actions[0].bubble_id == draft.bubbles[0].bubble_id
    assert plan.actions[0].channel == "text"
    assert plan.actions[1].bubble_id == draft.bubbles[1].bubble_id
    assert plan.actions[1].channel == "text"
    validate_delivery_plan(plan, draft)


def test_validate_delivery_plan_rejections():
    """测试投递计划在各种非法情形下的严格拒绝。"""
    draft = create_reply_draft("段落A\n段落B", draft_id="draft_reject_01")
    b0_id = draft.bubbles[0].bubble_id
    b1_id = draft.bubbles[1].bubble_id

    empty_draft = ReplyDraft(
        draft_id="empty_draft",
        text="",
        bubbles=(),
        created_at=datetime.now(),
    )
    with pytest.raises(ValueError, match="非沉默草稿至少需要一个气泡"):
        validate_delivery_plan(DeliveryPlan(draft_id="empty_draft", actions=()), empty_draft)

    mismatched_plan = DeliveryPlan(
        draft_id="other_draft_id",
        actions=(DeliveryAction(bubble_id=b0_id), DeliveryAction(bubble_id=b1_id)),
    )
    with pytest.raises(ValueError, match="不匹配"):
        validate_delivery_plan(mismatched_plan, draft)

    non_text_plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0_id, channel="text"),
            DeliveryAction(bubble_id=b1_id, channel="voice"),
        ),
    )
    with pytest.raises(ValueError, match="仅支持纯文字投递.*非 text 动作"):
        validate_delivery_plan(non_text_plan, draft)

    duplicate_plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0_id, channel="text"),
            DeliveryAction(bubble_id=b0_id, channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="重复出现"):
        validate_delivery_plan(duplicate_plan, draft)

    missing_plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(DeliveryAction(bubble_id=b0_id, channel="text"),),
    )
    with pytest.raises(ValueError, match="遗漏了部分气泡"):
        validate_delivery_plan(missing_plan, draft)

    unknown_plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=(
            DeliveryAction(bubble_id=b0_id, channel="text"),
            DeliveryAction(bubble_id=b1_id, channel="text"),
            DeliveryAction(bubble_id="unknown_bubble_999", channel="text"),
        ),
    )
    with pytest.raises(ValueError, match="未在 ReplyDraft 中定义的未知 bubble_id"):
        validate_delivery_plan(unknown_plan, draft)


def test_create_reply_draft_preserves_code_blocks():
    """测试 Markdown 代码块内部换行与缩进完整保护为一个气泡，不被拆碎。"""
    # 1. 包含前后短句与一个完整代码块（含内部缩进与空行）
    text = (
        "我帮你写了个函数：\n"
        "```python\n"
        "def greet(name: str) -> str:\n"
        "    # 打招呼逻辑\n"
        "    msg = f'Hello, {name}'\n"
        "\n"
        "    return msg\n"
        "```\n"
        "你可以直接调用试试。"
    )
    draft = create_reply_draft(text, draft_id="draft_code_01")
    assert len(draft.bubbles) == 3
    assert draft.bubbles[0].text == "我帮你写了个函数："
    expected_code = (
        "```python\n"
        "def greet(name: str) -> str:\n"
        "    # 打招呼逻辑\n"
        "    msg = f'Hello, {name}'\n"
        "\n"
        "    return msg\n"
        "```"
    )
    assert draft.bubbles[1].text == expected_code
    assert draft.bubbles[2].text == "你可以直接调用试试。"

    # 2. 波浪线 ~~~ 代码块与未闭合代码块保护
    unclosed_text = (
        "看这段未闭合的配置：\n"
        "~~~yaml\n"
        "server:\n"
        "  port: 8080\n"
        "  host: 0.0.0.0"
    )
    draft_unclosed = create_reply_draft(unclosed_text, draft_id="draft_code_02")
    assert len(draft_unclosed.bubbles) == 2
    assert draft_unclosed.bubbles[0].text == "看这段未闭合的配置："
    assert "port: 8080" in draft_unclosed.bubbles[1].text
    assert draft_unclosed.bubbles[1].text.startswith("~~~yaml")


def test_create_reply_draft_merges_numbered_lists_and_continuations():
    """测试连续编号列表与缩进续行聚合成气泡，避免逐行刷屏。"""
    # 1. 连续编号列表（点号、顿号、括号）与前后短句
    text = (
        "推荐去以下几个地方玩：\n"
        "1. 西湖，漫步苏堤很舒服\n"
        "2. 灵隐寺，香火很旺\n"
        "3. 浙大玉泉校区，风景典雅\n"
        "挑一个晴天去正合适。"
    )
    draft = create_reply_draft(text, draft_id="draft_list_01")
    assert len(draft.bubbles) == 3
    assert draft.bubbles[0].text == "推荐去以下几个地方玩："
    expected_list = (
        "1. 西湖，漫步苏堤很舒服\n"
        "2. 灵隐寺，香火很旺\n"
        "3. 浙大玉泉校区，风景典雅"
    )
    assert draft.bubbles[1].text == expected_list
    assert draft.bubbles[2].text == "挑一个晴天去正合适。"

    # 2. 列表项带有缩进续行（多空格、Tab、全角空格）
    text_with_indent = (
        "步骤安排：\n"
        "1、前期准备\n"
        "   准备好所有必需材料与工具\n"
        "   提前确认环境配置\n"
        "2、正式执行\n"
        "\t启动主任务\n"
        "\u3000保持网络畅通\n"
        "完成之后告诉我一声。"
    )
    draft_indent = create_reply_draft(text_with_indent, draft_id="draft_list_02")
    assert len(draft_indent.bubbles) == 3
    assert draft_indent.bubbles[0].text == "步骤安排："
    assert "准备好所有必需材料与工具" in draft_indent.bubbles[1].text
    assert "保持网络畅通" in draft_indent.bubbles[1].text
    assert draft_indent.bubbles[1].text.startswith("1、前期准备")
    assert draft_indent.bubbles[2].text == "完成之后告诉我一声。"

    # 3. 括号编号格式 (1)/(2) 以及带空行间隔的连续列表
    text_paren = (
        "(1) 方案一：坐高铁\n"
        "\n"
        "(2) 方案二：自驾走高速"
    )
    draft_paren = create_reply_draft(text_paren, draft_id="draft_list_03")
    assert len(draft_paren.bubbles) == 1
    assert "(1) 方案一：坐高铁" in draft_paren.bubbles[0].text
    assert "(2) 方案二：自驾走高速" in draft_paren.bubbles[0].text


def test_create_reply_draft_normal_sentences_and_decimals():
    """测试普通短句换行独立切分行为保持，且小数不会误判为列表。"""
    # 普通短句换行逐行切分为气泡
    text_normal = "好的。\n我知道了。\n稍等我一下。\n马上来。"
    draft_normal = create_reply_draft(text_normal, draft_id="draft_normal_01")
    assert len(draft_normal.bubbles) == 4
    assert [b.text for b in draft_normal.bubbles] == ["好的。", "我知道了。", "稍等我一下。", "马上来。"]

    # 小数与普通带空格句子不误判为列表
    text_decimal = "圆周率近似值是 3.14 大概就是这样。\n自然常数是 2.718 左右。\n  普通带缩进短句  "
    draft_decimal = create_reply_draft(text_decimal, draft_id="draft_decimal_01")
    assert len(draft_decimal.bubbles) == 3
    assert "3.14" in draft_decimal.bubbles[0].text
    assert "2.718" in draft_decimal.bubbles[1].text
    assert draft_decimal.bubbles[2].text == "普通带缩进短句"


def test_semantic_history_view_strips_label_protocol():
    """测试历史记录中的 [label:N] 内部机器标签连同引用被彻底剥离，不进入模型上下文。"""
    raw_history = "[label:1] 上一次回复内容\n[quote:10086] 用户说的话\n正常聊天内容"
    view = semantic_history_view(raw_history)
    assert "[label:" not in view
    assert "[1]" not in view
    assert "[quote:" not in view
    assert "上一次回复内容" in view
    assert "用户说的话" in view
    assert "正常聊天内容" in view


def test_format_incoming_messages_merges_multi_text_multi_face_batch():
    """多条文本 + 多个表情合成一个自然整体，表情是附属信息而非新话题。"""
    msgs = [
        IncomingMessage(message_id=401, text="今天好累", kind="text"),
        IncomingMessage(
            message_id=402,
            text="吃糖",
            kind="face",
            semantic_text="发了一个“吃糖”表情",
        ),
        IncomingMessage(message_id=403, text="刚开完会", kind="text"),
        IncomingMessage(
            message_id=404,
            text="猫猫探头",
            kind="sticker",
            semantic_text="发了一个“猫猫探头”表情",
        ),
        IncomingMessage(message_id=405, text="睡了", kind="text"),
    ]
    res = format_incoming_messages(msgs)

    # 一个整体，不再拆成"对方连着发来了以下内容"
    assert "对方连着发来了以下内容：" not in res
    assert res.startswith("对方")

    # 原始先后顺序保留
    assert res.index("今天好累") < res.index("吃糖")
    assert res.index("吃糖") < res.index("刚开完会")
    assert res.index("刚开完会") < res.index("猫猫探头")
    assert res.index("猫猫探头") < res.index("睡了")

    # 全部正文与全部原始表情标签都在
    for token in ("今天好累", "刚开完会", "睡了", "吃糖", "猫猫探头"):
        assert token in res

    # 表情写成附属信息，而不是需要分别评论的新话题
    assert "附上一个小表情" in res
    assert "发了一个" not in res
    assert "[face:" not in res and "[sticker:" not in res
    assert "1." not in res and "[1]" not in res and "【1】" not in res

    # 不解释、不编造表情含义
    assert "开心" not in res
    assert "糖" in res


def test_format_incoming_messages_face_position_in_batch():
    """表情在文本前 / 中 / 后都合成一个整体，顺序不变。"""
    text_a = IncomingMessage(message_id=411, text="在吗", kind="text")
    text_b = IncomingMessage(message_id=412, text="刚洗完澡", kind="text")
    face_a = IncomingMessage(
        message_id=413,
        text="点赞",
        kind="emoji",
        semantic_text="发了一个“点赞”表情",
    )

    face_first = format_incoming_messages([face_a, text_a, text_b])
    assert face_first.startswith("对方先附上一个小表情“点赞”")
    assert face_first.index("点赞") < face_first.index("在吗") < face_first.index("刚洗完澡")

    face_middle = format_incoming_messages([text_a, face_a, text_b])
    assert face_middle.index("在吗") < face_middle.index("点赞") < face_middle.index("刚洗完澡")

    face_last = format_incoming_messages([text_a, text_b, face_a])
    assert face_last.index("在吗") < face_last.index("刚洗完澡") < face_last.index("点赞")

    for res in (face_first, face_middle, face_last):
        assert "对方连着发来了以下内容：" not in res
        assert "附上一个小表情“点赞”" in res
        assert "发了一个" not in res
        assert "在吗" in res and "刚洗完澡" in res and "点赞" in res


def test_format_incoming_messages_falls_back_when_body_or_label_missing():
    """正文或表情标签为空时无法安全合并，退回原格式且不丢内容。"""
    empty_text = IncomingMessage(message_id=421, text="", kind="text")
    face = IncomingMessage(
        message_id=422,
        text="吃糖",
        kind="face",
        semantic_text="发了一个“吃糖”表情",
    )
    res = format_incoming_messages([empty_text, face])
    assert res.startswith("对方连着发来了以下内容：")
    assert "吃糖" in res

    empty_face = IncomingMessage(message_id=423, text="", kind="face", semantic_text="")
    text = IncomingMessage(message_id=424, text="睡了啊", kind="text")
    res2 = format_incoming_messages([text, empty_face])
    assert res2.startswith("对方连着发来了以下内容：")
    assert "睡了啊" in res2


def test_format_incoming_messages_other_kind_keeps_original_behavior():
    """批次里混有其它 kind（语音）时保持原格式，语音语义不被改写。"""
    text = IncomingMessage(message_id=431, text="在吗", kind="text")
    face = IncomingMessage(
        message_id=432,
        text="吃糖",
        kind="face",
        semantic_text="发了一个“吃糖”表情",
    )
    voice = IncomingMessage(
        message_id=433,
        text="明天见",
        kind="voice",
        semantic_text="发来一条语音，内容是：“明天见”",
    )
    res = format_incoming_messages([text, face, voice])
    assert res.startswith("对方连着发来了以下内容：")
    assert "在吗" in res
    assert "吃糖" in res
    assert "明天见" in res


def test_format_incoming_messages_pure_batches_still_listed():
    """纯文本、纯表情、其它 kind 的既有行为不变。"""
    text_a = IncomingMessage(message_id=441, text="在吗", kind="text")
    text_b = IncomingMessage(message_id=442, text="刚洗完澡", kind="text")
    assert format_incoming_messages([text_a, text_b]) == "对方连着发来了以下内容：\n在吗\n刚洗完澡"

    face_a = IncomingMessage(message_id=443, text="吃糖", kind="face")
    face_b = IncomingMessage(message_id=444, text="点赞", kind="sticker")
    faces = format_incoming_messages([face_a, face_b])
    assert faces.startswith("对方连着发来了以下内容：\n")
    assert "吃糖" in faces and "点赞" in faces

    # 单条仍然直接返回自然语义
    assert format_incoming_messages([text_a]) == "在吗"
    assert format_incoming_messages([]) == ""
