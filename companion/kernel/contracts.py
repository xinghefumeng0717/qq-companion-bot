"""对话内核的公共数据契约。

只承载数据，不依赖旧回复管道，也不包含任何角色或世界观硬编码。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional, Tuple


@dataclass(frozen=True)
class IncomingMessage:
    """一条已保留原始 ID 的输入；semantic_text 是给模型看的自然语义。"""

    message_id: Optional[int]
    text: str
    kind: str = "text"
    semantic_text: str = ""
    quoted_message_id: Optional[int] = None
    # 原始载荷（如 face_id、图片路径）；不直接喂给主模型。
    payload: Mapping[str, Any] = field(default_factory=dict)

    @property
    def readable_text(self) -> str:
        return self.semantic_text or self.text


@dataclass(frozen=True)
class StandardizedTurnInput:
    session_id: str
    messages: Tuple[IncomingMessage, ...]
    text: str
    received_at: datetime
    # 机器人回复应引用的当前用户消息 ID（投递层引用目标）。
    # 与"用户引用的历史消息"无关：后者由 IncomingMessage.quoted_message_id 与 quoted_text 承担。
    reply_to_message_id: Optional[int] = None
    # 用户引用的原消息语义（若有），引用理解在生成前完成。
    quoted_text: str = ""


@dataclass(frozen=True)
class PsychologicalSnapshot:
    """一轮内唯一心理快照；原始状态仅供内部，prompt_hint 必须短小。"""

    turn_id: str
    frozen_at: datetime
    state_version: int
    stage_index: int
    prompt_hint: str
    action_context: Mapping[str, Any]
    affection: Mapping[str, Any]
    mood: Mapping[str, Any]


@dataclass(frozen=True)
class MemoryPromptBlocks:
    """一轮内只读采集的记忆与安全上下文；空字符串表示整块省略。"""

    facts: str = ""
    impressions: str = ""
    self_facts: str = ""
    diaries: str = ""
    followups: str = ""
    meetups: str = ""
    desires: str = ""
    arcs: str = ""
    safety: str = ""


@dataclass(frozen=True)
class StyleExampleSelection:
    """一轮动态示例选择；条目和块均已冻结，可用 seed 与原始序号回放。"""

    query: str
    seed: int
    indices: Tuple[int, ...]
    items: Tuple[Tuple[str, str], ...]
    block: str


@dataclass(frozen=True)
class PreparedTurn:
    """一轮只读准备结果；准备与生成共享同一快照、上下文和 trace。

    history 与 messages 使用元组固定本轮顺序。无 memory 时 history 为空元组、
    memory_blocks 为 None；准备过程不调用主模型，也不推进任何心理状态。
    """

    trace_id: str
    turn_input: StandardizedTurnInput
    snapshot: PsychologicalSnapshot
    history: Tuple[Any, ...]
    memory_blocks: Optional[MemoryPromptBlocks]
    messages: Tuple[Mapping[str, Any], ...]
    style_examples: Optional[StyleExampleSelection] = None


@dataclass(frozen=True)
class ReplyBubble:
    bubble_id: str
    text: str


@dataclass(frozen=True)
class ReplyDraft:
    """主模型原稿；气泡创建后不再被投递层改写。"""

    draft_id: str
    # text 是原始响应存档；实际投递只以 bubbles 为真相。
    text: str
    bubbles: Tuple[ReplyBubble, ...]
    created_at: datetime


@dataclass(frozen=True)
class DeliveryAction:
    """只引用气泡，不复制正文；第一阶段 channel 只有 text。"""

    bubble_id: str
    channel: str = "text"
    # 后续语音/表情只放附加参数，第一阶段保持为空。
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryPlan:
    draft_id: str
    actions: Tuple[DeliveryAction, ...]
    quote_message_id: Optional[int] = None
    # 沉默是独立的生成前决策；第一阶段校验器始终拒绝它。
    silent: bool = False


@dataclass(frozen=True)
class SendReceipt:
    """发送器对一条真实消息的确认；未确认必须抛错，不能伪造成功。"""

    message_id: Optional[int] = None


@dataclass(frozen=True)
class SentMessage:
    """实际发送结果。attachment=True 表示不消耗原稿气泡的附加动作。"""

    bubble_id: str
    channel: str
    text: str
    message_id: Optional[int] = None
    quote_message_id: Optional[int] = None
    attachment: bool = False
    fallback_from: Optional[str] = None


@dataclass(frozen=True)
class DeliveryResult:
    """一次动作计划的实际结果；messages 不包含规划但未成功的动作。"""

    draft_id: str
    status: str
    messages: Tuple[SentMessage, ...] = ()
    errors: Tuple[str, ...] = ()


@dataclass(frozen=True)
class KernelResult:
    trace_id: str
    turn_input: StandardizedTurnInput
    snapshot: PsychologicalSnapshot
    draft: ReplyDraft
    plan: DeliveryPlan
    messages: Tuple[Mapping[str, Any], ...] = ()
    validation_passed: bool = True
    validation_errors: Tuple[str, ...] = ()
    latency_ms: Optional[float] = None
