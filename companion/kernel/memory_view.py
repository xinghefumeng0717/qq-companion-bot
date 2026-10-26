"""每轮只读记忆上下文采集，不推进状态、不调用模型、不写数据库。"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Awaitable, Optional

from companion.kernel.contracts import MemoryPromptBlocks
from companion.db import parse_dt
from companion.safety import SafetyChecker

logger = logging.getLogger(__name__)


async def _safe(block: Awaitable[str]) -> str:
    try:
        return await block
    except Exception as exc:
        logger.warning("只读记忆块采集失败，整块省略: %s", exc)
        return ""


def _lines(title: str, note: str, items: list[str]) -> str:
    if not items:
        return ""
    body = "\n".join(f"  - {item}" for item in items)
    return f"【{title}】{note}\n{body}"


async def collect_memory_blocks(
    memory: Any,
    *,
    arcs: Optional[Any] = None,
    query: str = "",
    valence: float = 2.0,
    now: Optional[datetime] = None,
    self_memory: bool = False,
    impressions: bool = False,
    meetups: Optional[Any] = None,
) -> MemoryPromptBlocks:
    """复用既有记忆读取与遗忘曲线，组成一轮冻结的补充提示词。"""
    now = now or datetime.now()
    facts_task = _safe(_facts(memory, query))
    impressions_task = _safe(_impressions(memory)) if impressions else None
    self_task = _safe(_self_facts(memory, query)) if self_memory else None
    diaries_task = _safe(_diaries(memory, valence, now))
    followups_task = _safe(_followups(memory, now))
    desires_task = _safe(_desires(memory, now))
    meetups_task = _safe(_meetups(meetups, now)) if meetups is not None else None
    arcs_task = _safe(arcs.build_prompt_block(as_script=True, now=now)) if arcs is not None else None

    facts = await facts_task
    impression_block = await impressions_task if impressions_task is not None else ""
    self_block = await self_task if self_task is not None else ""
    diaries = await diaries_task
    followups = await followups_task
    desires = await desires_task
    meetup_block = await meetups_task if meetups_task is not None else ""
    arc_block = await arcs_task if arcs_task is not None else ""
    try:
        safety_text = SafetyChecker.check_message(query)
        safety = f"【安全边界】\n{safety_text}" if safety_text else ""
    except Exception as exc:
        logger.warning("安全边界块采集失败，整块省略: %s", exc)
        safety = ""
    return MemoryPromptBlocks(
        facts=facts,
        impressions=impression_block,
        self_facts=self_block,
        diaries=diaries,
        followups=followups,
        meetups=meetup_block,
        desires=desires,
        arcs=arc_block,
        safety=safety,
    )


async def _facts(memory: Any, query: str) -> str:
    items = await memory.get_facts_for_prompt(query=query)
    return _lines("关于他", "（括号里是记下的日期，里面的「明天/今天」按那天算）", items)


async def _impressions(memory: Any) -> str:
    rows = await memory.get_impressions()
    words = {1: "有点感觉", 2: "比较确定", 3: "很确定"}
    items = [
        f"{row['content']}（{words.get(row['confidence'], '有点感觉')}）"
        for row in rows
    ]
    return _lines(
        "你对他的看法",
        "（你自己的感觉，可能看错。用来决定怎么接他的话；不要说出口，也不要分析他）",
        items,
    )


async def _self_facts(memory: Any, query: str) -> str:
    items = await memory.get_self_facts_for_prompt(query=query)
    # 字段本身就是来源契约：这是聊天中的自我陈述，不是角色设定。
    return _lines("你的历史自述", "", items)


async def _diaries(memory: Any, valence: float, now: datetime) -> str:
    items = await memory.get_active_diaries(current_valence=valence, now=now)
    return _lines("她的记忆｜历史聊天摘要", "", items)


async def _followups(memory: Any, now: datetime) -> str:
    rows = await memory.get_open_followups_readonly(due_only=True, limit=5, now=now)
    items = [memory.format_followup(row) for row in rows]
    return _lines("待跟进", "", items)


async def _desires(memory: Any, now: datetime) -> str:
    rows = await memory.get_live_desires_readonly(limit=5, now=now)
    items = [row["content"] for row in rows]
    return _lines("欲言又止｜历史念头", "", items)


async def _meetups(meetups: Any, now: datetime) -> str:
    """只读地整理待办见面：按时间分成“还没到的”和“已经过了的”。

    - when_at 解析不了的脏数据整条跳过：既不归入“还没到的”，也不当成“已经过了的”；
    - 有效条目的原有顺序与说明文案保持不变；
    - 一条有效条目都没有时整块返回空串，不能只剩一个空标题。
    """
    rows = await meetups.pending()
    if not rows:
        return ""
    upcoming, passed = [], []
    for row in rows:
        when = parse_dt(row.get("when_at"))
        if when is None:
            logger.warning("见面时间解析不了，已跳过这条脏数据: %r", row.get("when_at"))
            continue
        (passed if when < now else upcoming).append(row)
    if not upcoming and not passed:
        return ""
    lines = []
    if upcoming:
        lines.append("  还没到的（他提起时按将来的事说）：")
        lines.extend(f"    - {meetups.describe(row)}" for row in upcoming)
    if passed:
        lines.append("  已经过了的（见面已经发生，提起来用过去时，不要再当成还没到的事）：")
        lines.extend(f"    - {meetups.describe(row)}" for row in passed)
    return "【约好的见面】（见面本身发生在聊天之外）\n" + "\n".join(lines)


__all__ = ["collect_memory_blocks"]
