"""第三阶段 A/B 沙箱的纯函数模块 (companion/kernel/ab.py)

本模块只做三件事，全部是纯函数，不调用真实 API、不连数据库、不读取角色卡或 config：

1. 数据契约：HistoryTurn / ABCase / SideOutcome；
2. token 保守估算：estimate_tokens；
3. 盲测报告渲染：render_blind_markdown（每题独立随机打乱 old/new 到 A/B，
   输出中不出现 old/new、旧链路、新内核、原稿、费用与 token）。

另外提供 outcomes_to_json / outcomes_from_json，供 --from-results 重排报告。

不依赖项目其它模块，可单独导入测试。
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: 参与对比的两条链路标识（仅内部使用，绝不出现在盲测 Markdown 中）。
OLD_SIDE = "old"
NEW_SIDE = "new"
VALID_SIDES: Tuple[str, ...] = (OLD_SIDE, NEW_SIDE)

#: 空投递时的展示占位符。
SILENCE_MARK = "（沉默，无文字投递）"
FORMAT_FAIL_MARK = "（格式失败，原稿未投递）"
NO_TEXT_MARK = "（无文字结果）"

#: 每条消息的固定结构开销（role / 分隔符等）。
PER_MESSAGE_OVERHEAD_TOKENS = 4
#: ASCII 字符按每 4 个字符约 1 token 估算。
ASCII_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class HistoryTurn:
    """历史里的一条真实或构造对话。"""

    role: str
    content: str
    created_at: str


@dataclass(frozen=True)
class ABCase:
    """一道盲测题。"""

    case_id: str
    title: str
    user_text: str
    received_at: datetime
    history: Tuple[HistoryTurn, ...] = ()
    source: str = ""
    constructed: bool = False
    source_turn_id: Optional[int] = None


@dataclass(frozen=True)
class SideOutcome:
    """一条链路在某一题上的产出。"""

    side: str
    raw_text: str
    display_text: str
    status: str
    errors: Tuple[str, ...] = ()
    cost: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    trace_id: str = ""


def estimate_tokens(messages: Sequence[Mapping[str, Any]]) -> int:
    """对纯文本 messages 做保守 token 估算。

    规则：
    - 每条消息固定加 PER_MESSAGE_OVERHEAD_TOKENS 个 token；
    - 非 ASCII 字符每个算 1 个 token；
    - ASCII 字符约每 ASCII_CHARS_PER_TOKEN 个算 1 个 token（向上取整）。

    本阶段只支持纯文本：content 不是字符串时抛出带说明的 ValueError
    （图片 / 多模态内容当前不受支持）。
    """
    if messages is None:
        raise ValueError("estimate_tokens: messages 不能为 None")
    if isinstance(messages, (str, bytes)):
        raise ValueError("estimate_tokens: messages 必须是消息序列，不能是字符串")

    total = 0
    for index, message in enumerate(messages):
        if not isinstance(message, Mapping):
            raise ValueError(
                f"estimate_tokens: 第 {index} 条消息必须是映射（含 role/content），"
                f"实际是 {type(message).__name__}"
            )
        if "content" not in message:
            raise ValueError(
                f"estimate_tokens: 第 {index} 条消息缺少 content 字段；"
                "本阶段只支持纯文本消息"
            )
        content = message["content"]
        if not isinstance(content, str):
            raise ValueError(
                f"estimate_tokens: 第 {index} 条消息的 content 必须是字符串，"
                f"实际是 {type(content).__name__}；"
                "本阶段不支持图片 / 多模态内容（content 列表或非文本对象）"
            )
        total += PER_MESSAGE_OVERHEAD_TOKENS
        ascii_chars = 0
        for char in content:
            if ord(char) < 128:
                ascii_chars += 1
        non_ascii_chars = len(content) - ascii_chars
        total += non_ascii_chars
        total += (ascii_chars + ASCII_CHARS_PER_TOKEN - 1) // ASCII_CHARS_PER_TOKEN
    return total


def _empty_display_mark(status: str) -> str:
    """按 status 决定空投递时的展示文案。"""
    normalized = (status or "").strip().lower()
    if "silen" in normalized:
        return SILENCE_MARK
    if "format" in normalized:
        return FORMAT_FAIL_MARK
    return NO_TEXT_MARK


def _render_history(case: ABCase) -> Tuple[str, ...]:
    lines = []
    for turn in case.history:
        role = (turn.role or "").strip() or "unknown"
        stamp = (turn.created_at or "").strip()
        prefix = f"[{stamp}] " if stamp else ""
        lines.append(f"- {prefix}{role}: {turn.content}")
    return tuple(lines)


def _side_outcomes_for_case(
    case: ABCase, outcomes: Mapping[str, Mapping[str, SideOutcome]]
) -> Dict[str, SideOutcome]:
    raw = outcomes.get(case.case_id)
    if raw is None:
        raise ValueError(
            f"render_blind_markdown: 题目 {case.case_id!r} 没有任何结果，无法组成 A/B"
        )
    if not isinstance(raw, Mapping):
        raise ValueError(
            f"render_blind_markdown: 题目 {case.case_id!r} 的结果必须是 "
            f"{{side: SideOutcome}} 映射，实际是 {type(raw).__name__}"
        )
    sides = set(raw.keys())
    if sides != set(VALID_SIDES):
        raise ValueError(
            f"render_blind_markdown: 题目 {case.case_id!r} 必须恰好包含 "
            f"{sorted(VALID_SIDES)} 两个 side，实际是 {sorted(sides)}"
        )
    resolved: Dict[str, SideOutcome] = {}
    for side in VALID_SIDES:
        outcome = raw[side]
        if not isinstance(outcome, SideOutcome):
            raise ValueError(
                f"render_blind_markdown: 题目 {case.case_id!r} 的 {side} 结果必须是 "
                f"SideOutcome，实际是 {type(outcome).__name__}"
            )
        if outcome.side != side:
            raise ValueError(
                f"render_blind_markdown: 题目 {case.case_id!r} 的结果 side 字段"
                f"（{outcome.side!r}）与键（{side!r}）不一致"
            )
        resolved[side] = outcome
    return resolved


def render_blind_markdown(
    cases: Sequence[ABCase],
    outcomes: Mapping[str, Mapping[str, SideOutcome]],
    seed: int,
) -> Tuple[str, dict]:
    """渲染盲测 Markdown，并返回只有主人可见的私有映射。

    每题用 random.Random(f"{seed}:{case_id}") 独立打乱两条链路到 A / B，
    因此同一 seed 与同一批题永远得到同样的排布，不同题之间互不影响。

    返回的第二项形如 {"seed": 42, "cases": {"case-1": {"A": "old", "B": "new"}}}。

    Markdown 里不会出现 old / new、旧链路 / 新内核、模型原稿、费用与 token，
    也不会泄漏哪一边是哪一边。
    """
    if not isinstance(outcomes, Mapping):
        raise ValueError(
            "render_blind_markdown: outcomes 必须是 {case_id: {side: SideOutcome}} 映射"
        )

    seen_ids = set()
    chunks = []
    assignments: Dict[str, Dict[str, str]] = {}

    for index, case in enumerate(cases, start=1):
        if not isinstance(case, ABCase):
            raise ValueError(
                f"render_blind_markdown: 第 {index} 道题必须是 ABCase，"
                f"实际是 {type(case).__name__}"
            )
        if case.case_id in seen_ids:
            raise ValueError(
                f"render_blind_markdown: 题号 {case.case_id!r} 重复，无法编号"
            )
        seen_ids.add(case.case_id)

        resolved = _side_outcomes_for_case(case, outcomes)

        rng = random.Random(f"{seed}:{case.case_id}")
        order = [OLD_SIDE, NEW_SIDE]
        rng.shuffle(order)
        side_a, side_b = order

        assignments[case.case_id] = {"A": side_a, "B": side_b}

        origin = "构造对抗" if case.constructed else "真实历史"
        lines = [f"## 第 {index} 题：{case.title}", f"- 题目来源：{origin}"]

        history_lines = _render_history(case)
        if history_lines:
            lines.append("- 必要历史：")
            lines.extend(f"  {item}" for item in history_lines)
        else:
            lines.append("- 必要历史：（无）")

        lines.append("- 用户输入：")
        lines.append(f"  {case.user_text}")

        for label, side in (("A", side_a), ("B", side_b)):
            outcome = resolved[side]
            display = outcome.display_text or ""
            body = display.strip() if display.strip() else _empty_display_mark(outcome.status)
            lines.append("")
            lines.append(f"### 回答 {label}")
            lines.append("")
            lines.append(body)

        chunks.append("\n".join(lines))

    header = "# 对话盲测报告"
    footer = "（报告已随机打乱每题的两份回答，无法从文本判断哪一份来自哪条链路。）"
    body_text = "\n\n".join(chunks)
    if body_text:
        markdown = f"{header}\n\n{body_text}\n\n{footer}\n"
    else:
        markdown = f"{header}\n\n{footer}\n"
    private = {"seed": seed, "cases": assignments}
    return markdown, private


def _outcome_to_json(outcome: SideOutcome) -> dict:
    data = asdict(outcome)
    data["errors"] = list(outcome.errors)
    return data


def _outcome_from_json(side: str, data: Mapping[str, Any]) -> SideOutcome:
    if not isinstance(data, Mapping):
        raise ValueError(
            f"outcomes_from_json: {side!r} 的结果必须是映射，实际是 {type(data).__name__}"
        )
    known = {f.name for f in fields(SideOutcome)}
    unknown = set(data.keys()) - known
    if unknown:
        raise ValueError(
            f"outcomes_from_json: {side!r} 的结果包含未知字段 {sorted(unknown)}"
        )
    side_value = data.get("side")
    if side_value not in VALID_SIDES:
        raise ValueError(
            f"outcomes_from_json: {side!r} 的 side 字段非法（{side_value!r}），"
            f"只能是 {list(VALID_SIDES)}"
        )
    if side_value != side:
        raise ValueError(
            f"outcomes_from_json: side 字段（{side_value!r}）与键（{side!r}）不一致"
        )
    errors = data.get("errors") or ()
    if isinstance(errors, str):
        errors = (errors,)
    return SideOutcome(
        side=side_value,
        raw_text=str(data.get("raw_text", "") or ""),
        display_text=str(data.get("display_text", "") or ""),
        status=str(data.get("status", "") or ""),
        errors=tuple(str(item) for item in errors),
        cost=float(data.get("cost", 0.0) or 0.0),
        prompt_tokens=int(data.get("prompt_tokens", 0) or 0),
        completion_tokens=int(data.get("completion_tokens", 0) or 0),
        trace_id=str(data.get("trace_id", "") or ""),
    )


def outcomes_to_json(outcomes: Mapping[str, Any]) -> dict:
    """把结果转成可 JSON 序列化的字典（支持单题与多题两种结构）。"""
    if not isinstance(outcomes, Mapping):
        raise ValueError(
            f"outcomes_to_json: outcomes 必须是映射，实际是 {type(outcomes).__name__}"
        )
    payload: Dict[str, Any] = {}
    for key, value in outcomes.items():
        if isinstance(value, SideOutcome):
            payload[str(key)] = _outcome_to_json(value)
        elif isinstance(value, Mapping):
            inner: Dict[str, Any] = {}
            for side, outcome in value.items():
                if not isinstance(outcome, SideOutcome):
                    raise ValueError(
                        f"outcomes_to_json: {key!r} 的 {side!r} 结果必须是 SideOutcome，"
                        f"实际是 {type(outcome).__name__}"
                    )
                inner[str(side)] = _outcome_to_json(outcome)
            payload[str(key)] = inner
        else:
            raise ValueError(
                f"outcomes_to_json: {key!r} 的结果必须是 SideOutcome 或其映射，"
                f"实际是 {type(value).__name__}"
            )
    return payload


def outcomes_from_json(data: Mapping[str, Any]) -> dict:
    """从 JSON 字典还原结果对象，并校验 side 合法性。"""
    if not isinstance(data, Mapping):
        raise ValueError(
            f"outcomes_from_json: data 必须是映射，实际是 {type(data).__name__}"
        )
    restored: Dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, Mapping) and "side" not in value:
            inner: Dict[str, SideOutcome] = {}
            for side, outcome in value.items():
                inner[str(side)] = _outcome_from_json(str(side), outcome)
            restored[str(key)] = inner
        else:
            restored[str(key)] = _outcome_from_json(str(key), value)
    return restored
