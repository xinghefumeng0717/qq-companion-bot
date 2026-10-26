"""离线对话内核：纯文本主聊、心理快照和同源追踪。"""

from companion.kernel.contracts import (
    DeliveryAction,
    DeliveryPlan,
    DeliveryResult,
    IncomingMessage,
    KernelResult,
    MemoryPromptBlocks,
    PsychologicalSnapshot,
    ReplyBubble,
    ReplyDraft,
    SendReceipt,
    SentMessage,
    StandardizedTurnInput,
)

from companion.kernel.context import ContextBuilder
from companion.kernel.delivery import (
    DeliveryExecutor,
    UnconfirmedDeliveryError,
    validate_action_delivery_plan,
)
from companion.kernel.kernel import DialogueKernel, replay_pure_text
from companion.kernel.memory_view import collect_memory_blocks
from companion.kernel.psychology import PsychologicalSnapshotBuilder
from companion.kernel.tracing import JsonlTraceRecorder

__all__ = [
    "ContextBuilder", "DialogueKernel", "DeliveryExecutor", "JsonlTraceRecorder",
    "PsychologicalSnapshotBuilder", "UnconfirmedDeliveryError", "replay_pure_text",
    "collect_memory_blocks", "MemoryPromptBlocks",
    "validate_action_delivery_plan",
    "DeliveryAction", "DeliveryPlan", "DeliveryResult", "IncomingMessage", "KernelResult",
    "PsychologicalSnapshot", "ReplyBubble", "ReplyDraft", "SendReceipt", "SentMessage",
    "StandardizedTurnInput",
]
