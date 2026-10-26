"""动态真人示例的只读、可回放选择。

检索与渲染复用 StyleExampleBank；这里只负责用本轮稳定种子抽样，
不读文件、不写数据库、不调用模型，也不触碰 bank 的共享随机状态。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from companion.kernel.contracts import StandardizedTurnInput, StyleExampleSelection
from companion.style_examples import PICK, TOP_K

logger = logging.getLogger(__name__)


def _turn_seed(turn_input: StandardizedTurnInput) -> int:
    identity = "".join((
        turn_input.session_id,
        turn_input.received_at.isoformat(),
        str(turn_input.reply_to_message_id or ""),
        turn_input.text,
    ))
    digest = hashlib.sha256(identity.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def collect_style_examples(
    bank: Any,
    turn_input: StandardizedTurnInput,
) -> StyleExampleSelection:
    """每轮选择一次；相同输入得到相同条目，bank 的共享 RNG 状态不变。"""
    seed = _turn_seed(turn_input)
    try:
        candidates = bank.search_entries(
            turn_input.text, top_k=max(TOP_K, PICK),
        )
        # 纯哈希排序不依赖 Python RNG 算法版本，跨进程/解释器也可复现。
        ranked = sorted(
            candidates,
            key=lambda entry: hashlib.sha256(
                f"{seed}:{entry[0]}".encode("utf-8"),
            ).digest(),
        )
        # 哈希决定“选哪些”，语料原顺序决定“如何展示”。
        selected = sorted(ranked[:PICK], key=lambda entry: entry[0])
        indices = tuple(index for index, _item in selected)
        raw_items = [item for _index, item in selected]
        items = tuple((str(item["his"]), str(item["her"])) for item in raw_items)
        block = bank.render(raw_items)
    except Exception as exc:
        logger.error("[StyleExamples] 内核只读选择异常，按无示例处理: %s", exc)
        indices, items, block = (), (), ""
    return StyleExampleSelection(
        query=turn_input.text,
        seed=seed,
        indices=indices,
        items=items,
        block=block,
    )
