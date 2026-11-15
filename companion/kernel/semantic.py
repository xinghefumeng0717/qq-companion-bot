"""内核语义抽象层 (companion/kernel/semantic.py)

提供第一阶段离线对话内核的自然语义转换与数据编排能力：
1. 从 contracts.IncomingMessage 序列构建 StandardizedTurnInput，聚合为自然语言，保留真实 message_id；
   （文本与表情混合的批次合成一个自然整体，表情是附属信息而不是新话题）；
2. 历史记录只读语义视图，把已有语音/表情/协议记录转为自然语义描述，不修改数据库，不向模型输出协议教学；
   助手自己的历史只剥离动作标记，不把系统动作改写成“发了一个……表情”回灌正文；
3. 从主模型文本创建不可变 ReplyDraft 与具有稳定 ID 的 ReplyBubble 序列；
4. 创建并严格校验第一阶段纯文字 DeliveryPlan（每个气泡恰好投递一次，拒绝重复、缺失或非纯文本）。

本模块完全通用，不硬编码任何具体角色名、人设或世界观。
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from typing import Any, List, Mapping, Optional, Sequence, Set, Tuple

from companion.kernel.contracts import (
    DeliveryAction,
    DeliveryPlan,
    IncomingMessage,
    ReplyBubble,
    ReplyDraft,
    StandardizedTurnInput,
)

# 历史记录中的协议与标记正则
_VOICE_RECORD_PREFIX_RE = re.compile(r"^[（(]\s*语音消息\s*[）)]\s*(.*)$", re.MULTILINE)
_VOICE_TAG_PAIR_RE = re.compile(
    r"\[voice[:：]\s*\]?\s*(.*?)\s*\[/voice\]",
    re.IGNORECASE | re.DOTALL,
)
_VOICE_LINE_SHORTHAND_RE = re.compile(r"^\[voice[:：]\s*([^\]]+)\]$", re.MULTILINE | re.IGNORECASE)
_VOICE_BARE_TAGS_RE = re.compile(r"\[/?voice[:：]?[^\]]*\]", re.IGNORECASE)
_FACE_TAG_RE = re.compile(r"\[face[:：]([^\]]+)\]", re.IGNORECASE)
_STICKER_TAG_RE = re.compile(r"\[(?:sticker|表情)[:：]([^\]]+)\]", re.IGNORECASE)
_QUOTE_TAG_RE = re.compile(r"^[ \t]*\[quote[:：](\d+)\]\s*", re.MULTILINE | re.IGNORECASE)
_SILENCE_TAG_RE = re.compile(r"^[ \t]*\[(?:沉默|SILENT|SILENCE)\][ \t]*$", re.MULTILINE | re.IGNORECASE)
_LABEL_TAG_RE = re.compile(r"^[ \t]*\[label[:：](\d+)\]\s*", re.MULTILINE | re.IGNORECASE)
_USER_IMAGE_DESC_RE = re.compile(r"\[(?:发来一张照片|发来一张图片)[:：]([^\]]+)\]")
_USER_IMAGE_UNCLEAR_RE = re.compile(r"\[(?:对方)?发来一张图片[，,]但没能看清\]")
_USER_IMAGE_UNSEEN_RE = re.compile(r"\[对方发来一张图片[，,]你看不到内容\]")

# 气泡切分：代码块与列表匹配正则
_CODE_FENCE_START_RE = re.compile(r"^[ \t]*(`{3,}|~{3,})(.*)$")
_LIST_ITEM_START_RE = re.compile(
    r"^[ \t]*(?:"
    r"\d+\.(?:[ \t]+|[^\d\s])"  # 1. xxx or 1.xxx (排除 3.14 小数)
    r"|\d+[、\)）][ \t]*"       # 1、xxx or 1) xxx or 1）xxx
    r"|[（(]\d+[）)][ \t]*"     # (1) xxx or （1）xxx
    r")"
)
_INDENTED_LINE_RE = re.compile(r"^(?:[ \t]{2,}|\t|[\u3000]+)\S")


def _incoming_semantic(msg: IncomingMessage) -> str:
    """把一条消息变成自然语义；没有 semantic_text 时也保留媒体事实。"""
    if msg.semantic_text.strip():
        return msg.semantic_text.strip()
    raw = msg.text.strip()
    kind = msg.kind.lower()
    label = raw.strip("[]【】") or kind
    if kind in {"face", "emoji", "sticker"}:
        return f"发了一个{_face_view(label)}表情"
    if kind == "voice":
        return f"发来一条语音，内容是：“{raw}”" if raw else "发来一条语音"
    if kind == "image":
        if msg.payload.get("media_role") == "sticker":
            return f"附带表情的语气：{raw}" if raw else "附带表情的语气没有识别出来"
        return f"发来一张图片：{raw}" if raw else "发来一张图片"
    return raw


_FACE_KINDS = {"face", "emoji", "sticker"}


def _face_view(label: str) -> str:
    """表情名后面带一句它在聊天里的意思；只有名字时模型会按字面去读（把“吃糖”当成真在吃糖）。

    含义取自既有的小黄脸词表，只在这个表情真的出现时带上，不整表常驻提示词。
    """
    from companion.prompts import READ_FACE_MEANINGS

    meaning = READ_FACE_MEANINGS.get(label, "")
    return f"“{label}”（{meaning}）" if meaning else f"“{label}”"


def _face_label(msg: IncomingMessage) -> str:
    """取表情消息的原始标签；不解释、不补充含义。"""
    raw = (msg.text or "").strip().strip("[]【】").strip()
    if raw:
        return raw
    match = re.search(r"[“\"](.+?)[”\"]", msg.semantic_text or "")
    if match:
        return match.group(1).strip()
    return ""


def _text_body(msg: IncomingMessage) -> str:
    """取文本消息的正文。"""
    semantic = (msg.semantic_text or "").strip()
    if semantic:
        return semantic
    return (msg.text or "").strip()


def _msg_class(msg: IncomingMessage) -> str:
    """把一条消息归到三类：text / face / other（语音、图片等其它类型）。"""
    kind = (msg.kind or "").lower()
    if kind == "text":
        return "text"
    if kind in _FACE_KINDS:
        return "face"
    return "other"


def _merge_text_and_faces(
    messages: Sequence[IncomingMessage],
) -> Optional[str]:
    """把“文本 + 表情”混合批次合成一个自然整体（任意条数）。

    这是一条通用数据规则，不针对任何具体句子、角色或作品：

    - 批次里必须同时有文本消息与表情类消息（face / emoji / sticker）；
    - 批次里若混有其它 kind（语音、图片等），不合并，保持既有行为；
    - 任一文本正文为空、或任一表情标签为空时无法安全合并，返回 None 退回原格式；
    - 合并后按消息原始先后顺序叙述：全部正文逐条保留，全部原始表情标签逐个保留，
      表情以“附上的小表情”出现，是附属信息而不是需要分别评论的新话题；
    - 只做结构减负：不解释、不编造表情含义。
    """
    classes = [_msg_class(msg) for msg in messages]
    if "other" in classes:
        return None
    if "text" not in classes or "face" not in classes:
        return None

    parts: List[str] = []
    prev: Optional[str] = None
    for msg, cls in zip(messages, classes):
        if cls == "text":
            body = _text_body(msg)
            if not body:
                return None
            if prev is None:
                parts.append(f"说：“{body}”")
            elif prev == "face":
                parts.append(f"接着说：“{body}”")
            else:
                parts.append(f"又说：“{body}”")
        else:
            label = _face_label(msg)
            if not label:
                return None
            if prev is None:
                parts.append(f"先附上一个小表情{_face_view(label)}")
            elif prev == "text":
                parts.append(f"同时附上一个小表情{_face_view(label)}")
            else:
                parts.append(f"又附上一个小表情{_face_view(label)}")
        prev = cls

    return "对方" + "，".join(parts)


def format_incoming_messages(
    messages: Sequence[IncomingMessage],
) -> str:
    """将输入整理为自然语义，不引入数字编号或动作协议。"""
    if not messages:
        return ""
    merged = _merge_text_and_faces(messages)
    if merged is not None:
        return merged
    contents = [_incoming_semantic(msg) for msg in messages]
    if len(contents) == 1:
        return contents[0]
    return "对方连着发来了以下内容：\n" + "\n".join(contents)


def build_turn_input(
    session_id: str,
    messages: Sequence[IncomingMessage],
    received_at: Optional[datetime] = None,
    reply_to_message_id: Optional[int] = None,
) -> StandardizedTurnInput:
    """从 IncomingMessage 序列构建 StandardizedTurnInput。

    - 聚合自然语言文本；
    - reply_to_message_id 只表示"机器人回复应引用的当前用户消息"；
      未显式给出时，取当前批次中最后一个带真实 message_id 的消息；
      绝不从 msg.quoted_message_id（用户引用的历史消息）推导；
    - quoted_text 独立承担"用户引用了哪条历史消息"的语义理解。
    """
    if received_at is None:
        received_at = datetime.now()

    resolved_reply_to_id = reply_to_message_id
    if resolved_reply_to_id is None:
        for msg in reversed(messages):
            if msg.message_id is not None:
                resolved_reply_to_id = msg.message_id
                break

    aggregated_text = format_incoming_messages(messages)
    quoted_text = ""
    for msg in reversed(messages):
        if msg.quoted_message_id is not None:
            quoted_text = str(
                msg.payload.get("quoted_semantic_text")
                or msg.payload.get("quoted_text")
                or ""
            )
            if quoted_text:
                break

    return StandardizedTurnInput(
        session_id=session_id,
        messages=tuple(messages),
        text=aggregated_text,
        received_at=received_at,
        reply_to_message_id=resolved_reply_to_id,
        quoted_text=quoted_text,
    )


def build_proactive_turn_input(
    material: str,
    received_at: Optional[datetime] = None,
) -> StandardizedTurnInput:
    """构造系统主动事件；它没有用户消息 ID，也不产生显示引用目标。"""
    event_text = (material or "").strip()
    event = IncomingMessage(
        message_id=None,
        text=event_text,
        kind="proactive_event",
        semantic_text=event_text,
    )
    return StandardizedTurnInput(
        session_id="production-proactive",
        messages=(event,),
        text=event_text,
        received_at=received_at or datetime.now(),
        reply_to_message_id=None,
        source="proactive",
    )


def _current_message_scope(msg: IncomingMessage, position: int) -> Tuple[Any, ...]:
    """优先使用生产批次序位；旧数据只按真实消息 ID 收拢连续段。"""
    scope = msg.payload.get("source_scope")
    if scope is not None:
        return ("source", scope)
    if msg.message_id is not None:
        return ("message", msg.message_id)
    return ("position", position)


def _quoted_group_view(group: Sequence[IncomingMessage]) -> str:
    """一条原消息只展示一次引用；组内各段仍复用现有自然语义。"""
    merged = _merge_text_and_faces(group)
    body = merged if merged is not None else format_incoming_messages(group)
    first = group[0]
    if first.quoted_message_id is None or first.payload.get("quote_prefix_in_text"):
        return body

    quoted_text = str(
        first.payload.get("quoted_semantic_text")
        or first.payload.get("quoted_text")
        or ""
    ).strip()
    if not quoted_text:
        return body

    role = str(first.payload.get("quoted_role") or "unknown")
    if role == "user":
        prefix = f"他引用自己之前说的“{quoted_text}”"
    elif role == "assistant":
        prefix = f"他引用你之前说的“{quoted_text}”"
    else:
        prefix = f"他引用之前的“{quoted_text}”"

    if not body:
        return prefix
    if merged is not None:
        # “对方”是上面 _merge_text_and_faces 刚生成的结构外壳，不是用户原文。
        return f"{prefix}，{merged[len('对方'):]}"
    if len(group) > 1:
        # 多段外壳同样由结构决定，不能靠正文前缀猜测。
        contents = [_incoming_semantic(msg) for msg in group]
        return prefix + "；这条消息的内容是：\n" + "\n".join(contents)
    if first.kind.lower() in _FACE_KINDS | {"image", "voice"}:
        return f"{prefix}，同时{body}"
    return f"{prefix}时说：\n{body}"


def format_current_user_content(turn_input: StandardizedTurnInput) -> str:
    """生成主模型唯一需要阅读的当前输入，不回写正文或历史。

    新结构化数据按原消息逐条展示引用；旧数据只有顶层 quoted_text 时，
    保留原有整批前置作为兼容 fallback。scope、消息 ID 和角色枚举不外泄。
    """
    messages = list(turn_input.messages)
    if not any(msg.quoted_message_id is not None for msg in messages):
        if turn_input.quoted_text:
            return f"他引用之前的“{turn_input.quoted_text}”时说：\n{turn_input.text}"
        return turn_input.text

    groups: List[List[IncomingMessage]] = []
    previous_scope: Optional[Tuple[Any, ...]] = None
    for position, msg in enumerate(messages):
        scope = _current_message_scope(msg, position)
        if not groups or scope != previous_scope:
            groups.append([msg])
        else:
            groups[-1].append(msg)
        previous_scope = scope

    views = [_quoted_group_view(group) for group in groups]
    views = [view for view in views if view.strip()]
    if not views:
        return ""
    if len(views) == 1:
        return views[0]
    return "对方连着发来了以下内容：\n" + "\n".join(views)


def _is_assistant_role(role: str) -> bool:
    """判断这条历史是否来自助手自己。

    助手自己的历史里，[face:x]、[sticker:x]、[表情:x] 是系统动作而不是助手说的话；
    对方（用户）发的表情则一直是自然语义的一部分。
    """
    return str(role or "").strip().lower() == "assistant"


def semantic_history_view(raw_content: str, role: str = "assistant") -> str:
    """历史记录只读语义视图：将已有的语音/表情/协议标记转化为自然语义描述。

    - 严禁修改数据库原始数据；
    - 历史里的语音标记只剥离外壳并保留正文，不生成“用语音说”模板；
    - role="assistant"：只读剥离 [face:xxx]、[sticker:xxx]、[表情:xxx] 等动作标记并保留自然正文，
      绝不把系统动作改写成「发了一个“xxx”表情」回灌成助手自己的话（动作事实另有历史）；
    - role="user"（及其它非助手角色）：沿用既有表情语义「发了一个“xxx”表情」；
    - 去除协议性行首引用标记 [quote:N] 或转为自然语境；
    - 绝不在模型上下文里露出协议标记，避免模型模仿学习协议语法。
    """
    if not raw_content:
        return ""

    text = raw_content

    # 1. 过滤单独一行的沉默标记
    text = _SILENCE_TAG_RE.sub("", text)

    # 2-3. 历史语音只保留正文；投递方式由系统负责，不教模型复述“用语音说”。
    text = _VOICE_TAG_PAIR_RE.sub(lambda match: match.group(1).strip(), text)
    text = _VOICE_LINE_SHORTHAND_RE.sub(lambda match: match.group(1).strip(), text)

    # 4. 清理残留或孤立 voice 标记
    text = _VOICE_BARE_TAGS_RE.sub("", text)

    # 5. 历史存档前缀同样只去掉外壳，保留正文。
    text = _VOICE_RECORD_PREFIX_RE.sub(lambda match: match.group(1).strip(), text)

    # 6. 表情与表情包按角色处理，任何情况下都不留下机器协议外形。
    #    助手自己的历史：只剥离动作标记，动作事实由动作历史承担，不回灌成助手正文；
    #    对方（用户等）发来的表情：沿用既有自然语义。
    if _is_assistant_role(role):
        text = _FACE_TAG_RE.sub("", text)
        text = _STICKER_TAG_RE.sub("", text)
    else:
        text = _FACE_TAG_RE.sub(lambda m: f"发了一个{_face_view(m.group(1))}表情", text)
        text = _STICKER_TAG_RE.sub(lambda m: f"发了一个{_face_view(m.group(1))}表情", text)
        # 既有用户图片历史记录只读语义转换（去除方括号并保留自然语义）
        text = _USER_IMAGE_DESC_RE.sub(lambda m: f"发来一张图片：{m.group(1).strip()}", text)
        text = _USER_IMAGE_UNCLEAR_RE.sub("发来一张图片，但没能看清", text)
        text = _USER_IMAGE_UNSEEN_RE.sub("对方发来一张图片，你看不到内容", text)

    # 7. 剥离行首协议引用 [quote:N] 与 [label:N]
    text = _QUOTE_TAG_RE.sub("", text)
    text = _LABEL_TAG_RE.sub("", text)

    # 8. 规范化空白换行
    lines = [line.strip() for line in text.splitlines()]
    non_empty_lines = [line for line in lines if line]
    return "\n".join(non_empty_lines)


def _split_into_bubbles(text: str) -> List[str]:
    """将主模型原稿文本切分为气泡段落。

    切分规则：
    1. 保护 Markdown 代码块内部换行（``` 或 ~~~ 包裹的块作为一个完整气泡，内部换行与缩进完整保留）；
    2. 连续编号列表及缩进续行聚合成同一气泡，避免逐行刷屏；
    3. 普通短句与段落换行保持原有独立气泡行为；
    4. 过滤空行与空白气泡。
    """
    lines = text.splitlines()
    bubbles: List[str] = []

    in_code_block = False
    fence_char = ""
    fence_len = 0
    code_block_lines: List[str] = []

    current_list_lines: List[str] = []
    pending_empty_lines: List[str] = []

    def flush_list() -> None:
        nonlocal current_list_lines, pending_empty_lines
        if current_list_lines:
            bubbles.append("\n".join(current_list_lines).strip())
            current_list_lines = []
            pending_empty_lines = []

    for line in lines:
        if in_code_block:
            code_block_lines.append(line)
            close_match = re.match(r"^[ \t]*(`{3,}|~{3,})[ \t]*$", line)
            if close_match:
                c_char = close_match.group(1)[0]
                c_len = len(close_match.group(1))
                if c_char == fence_char and c_len >= fence_len:
                    bubbles.append("\n".join(code_block_lines).strip())
                    in_code_block = False
                    code_block_lines = []
            continue

        fence_match = _CODE_FENCE_START_RE.match(line)
        if fence_match:
            flush_list()
            in_code_block = True
            fence_char = fence_match.group(1)[0]
            fence_len = len(fence_match.group(1))
            code_block_lines = [line]
            continue

        cleaned = line.strip()
        if not cleaned:
            if current_list_lines:
                pending_empty_lines.append(line)
            continue

        if _LIST_ITEM_START_RE.match(line):
            if current_list_lines:
                if pending_empty_lines:
                    current_list_lines.extend(pending_empty_lines)
                    pending_empty_lines = []
                current_list_lines.append(line.rstrip())
            else:
                current_list_lines.append(line.rstrip())
                pending_empty_lines = []
        elif current_list_lines and _INDENTED_LINE_RE.match(line):
            if pending_empty_lines:
                current_list_lines.extend(pending_empty_lines)
                pending_empty_lines = []
            current_list_lines.append(line.rstrip())
        else:
            flush_list()
            bubbles.append(cleaned)

    if in_code_block and code_block_lines:
        bubbles.append("\n".join(code_block_lines).strip())
    flush_list()

    return bubbles


def create_reply_draft(
    text: str,
    draft_id: Optional[str] = None,
    created_at: Optional[datetime] = None,
) -> ReplyDraft:
    """从主模型原始文本构建 ReplyDraft，并生成稳定 ID 的 ReplyBubble。

    气泡 ID 由 draft_id 与段落序号/哈希确定，保证在整个生命周期中幂等稳定。
    """
    if created_at is None:
        created_at = datetime.now()

    if draft_id is None:
        text_digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]
        ts_str = created_at.strftime("%Y%m%d_%H%M%S")
        draft_id = f"draft_{ts_str}_{text_digest}"

    raw_bubbles = _split_into_bubbles(text)
    bubble_objs: List[ReplyBubble] = []

    for idx, b_text in enumerate(raw_bubbles):
        b_hash = hashlib.sha256(f"{idx}:{b_text}".encode("utf-8")).hexdigest()[:6]
        b_id = f"{draft_id}_b{idx}_{b_hash}"
        bubble_objs.append(ReplyBubble(bubble_id=b_id, text=b_text))

    return ReplyDraft(
        draft_id=draft_id,
        text=text,
        bubbles=tuple(bubble_objs),
        created_at=created_at,
    )


def create_delivery_plan(
    draft: ReplyDraft,
    quote_message_id: Optional[int] = None,
) -> DeliveryPlan:
    """创建第一阶段纯文字 DeliveryPlan。

    为 draft 中的每个 bubble 创建对应的纯文本 DeliveryAction，保证每段恰好一次。
    """
    actions: List[DeliveryAction] = [
        DeliveryAction(bubble_id=b.bubble_id, channel="text")
        for b in draft.bubbles
    ]
    plan = DeliveryPlan(
        draft_id=draft.draft_id,
        actions=tuple(actions),
        quote_message_id=quote_message_id,
    )
    validate_delivery_plan(plan, draft)
    return plan


def validate_delivery_plan(plan: DeliveryPlan, draft: ReplyDraft) -> None:
    """校验第一阶段 DeliveryPlan 的合法性。

    严格约束：
    1. plan.draft_id 必须与 draft.draft_id 一致；
    2. 第一阶段 channel 必须且只能为 "text"；
    3. draft 中的每个 bubble_id 必须在 actions 中恰好出现一次，不允许遗漏；
    4. 不允许重复投递同一个 bubble_id；
    5. actions 中不得包含 draft 中不存在的未知 bubble_id。
    """
    if plan.silent:
        raise ValueError("第一阶段仅支持非沉默纯文字投递")
    if not draft.bubbles:
        raise ValueError("非沉默草稿至少需要一个气泡；沉默必须在生成前决定")
    if plan.draft_id != draft.draft_id:
        raise ValueError(
            f"DeliveryPlan draft_id ({plan.draft_id}) 与 ReplyDraft draft_id ({draft.draft_id}) 不匹配"
        )

    draft_bubble_ids = [b.bubble_id for b in draft.bubbles]
    draft_bubble_id_set = set(draft_bubble_ids)

    seen_actions: Set[str] = set()

    for idx, action in enumerate(plan.actions):
        if action.channel != "text":
            raise ValueError(
                f"第一阶段仅支持纯文字投递，发现非 text 动作 channel='{action.channel}' (bubble_id={action.bubble_id})"
            )
        if action.bubble_id not in draft_bubble_id_set:
            raise ValueError(
                f"DeliveryPlan 包含未在 ReplyDraft 中定义的未知 bubble_id='{action.bubble_id}'"
            )
        if action.bubble_id in seen_actions:
            raise ValueError(
                f"DeliveryPlan 中 bubble_id='{action.bubble_id}' 重复出现，违反每段恰好一次原则"
            )
        seen_actions.add(action.bubble_id)

    missing = draft_bubble_id_set - seen_actions
    if missing:
        raise ValueError(f"DeliveryPlan 遗漏了部分气泡: {missing}")

    if len(seen_actions) != len(draft_bubble_ids):
        raise ValueError(
            f"DeliveryPlan 气泡数量 ({len(seen_actions)}) 与 ReplyDraft 气泡数量 ({len(draft_bubble_ids)}) 不一致"
        )
