"""第三阶段 A/B 沙箱纯函数模块的单元测试 (tests/test_kernel_ab.py)

覆盖验收要求：
1. estimate_tokens：纯文本估算规则 + 图片 / 非字符串 content 拒绝；
2. render_blind_markdown：每题都完整给出 A/B 两份回答；
3. 同一 seed 可复现；
4. Markdown 不泄漏 old/new、旧链路、新内核、原稿、费用与 token；
5. 空投递按 status 显示占位文案；
6. outcomes_to_json / outcomes_from_json 往返一致；
7. 非法 side、缺边、缺题报错。

本文件只依赖标准库与 pytest，不依赖项目其它模块。
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

import pytest

# 保证从仓库根目录即可导入 companion.kernel.ab（不依赖任何旧模块）。
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from companion.kernel.ab import (  # noqa: E402
    ABCase,
    FORMAT_FAIL_MARK,
    HistoryTurn,
    NO_TEXT_MARK,
    SILENCE_MARK,
    SideOutcome,
    estimate_tokens,
    outcomes_from_json,
    outcomes_to_json,
    render_blind_markdown,
)


def _case(index: int, constructed: bool = False) -> ABCase:
    return ABCase(
        case_id=f"case-{index}",
        title=f"题目 {index}",
        user_text=f"第 {index} 句用户输入",
        received_at=datetime(2024, 5, 1, 12, 0, 0),
        history=(
            HistoryTurn(role="user", content="你今天在做什么", created_at="2024-05-01 11:50:00"),
            HistoryTurn(role="assistant", content="在等你先开口", created_at="2024-05-01 11:51:00"),
        ),
        source="internal",
        constructed=constructed,
        source_turn_id=100 + index,
    )


def _outcome(side: str, display: str, status: str = "ok", raw: str = "") -> SideOutcome:
    return SideOutcome(
        side=side,
        raw_text=raw or f"RAW-{side}-SECRET",
        display_text=display,
        status=status,
        errors=(),
        cost=0.37,
        prompt_tokens=4242,
        completion_tokens=1357,
        trace_id=f"trace-{side}",
    )


def _outcomes(count: int) -> dict:
    return {
        f"case-{i}": {
            "old": _outcome("old", f"回答正文 {i}"),
            "new": _outcome("new", f"另一份正文 {i}"),
        }
        for i in range(count)
    }


# ---------------------------------------------------------------- 1. token 估算

def test_estimate_tokens_empty_messages_is_zero():
    assert estimate_tokens([]) == 0


def test_estimate_tokens_ascii_and_chinese_and_overhead():
    # "hello" -> 5 ASCII 字符 -> ceil(5/4)=2，加每条 4 的开销 => 6
    assert estimate_tokens([{"role": "user", "content": "hello"}]) == 6
    # 中文每字 1 token -> 2 + 4 = 6
    assert estimate_tokens([{"role": "user", "content": "你好"}]) == 6
    # 两条消息：每条 2 个 ASCII 字符 -> 1 token，加每条 4 的开销 => 5 + 5 = 10
    assert estimate_tokens(
        [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}]
    ) == 10


def test_estimate_tokens_is_monotonic_and_int():
    short = estimate_tokens([{"role": "user", "content": "a"}])
    long = estimate_tokens([{"role": "user", "content": "a" * 400}])
    assert isinstance(long, int)
    assert long > short


def test_estimate_tokens_rejects_image_content():
    with pytest.raises(ValueError) as excinfo:
        estimate_tokens([{"role": "user", "content": [{"type": "image_url", "url": "x"}]}])
    message = str(excinfo.value)
    assert "content" in message
    assert "图片" in message or "多模态" in message


def test_estimate_tokens_rejects_none_content_and_missing_content():
    with pytest.raises(ValueError):
        estimate_tokens([{"role": "user", "content": None}])
    with pytest.raises(ValueError):
        estimate_tokens([{"role": "user"}])


# ------------------------------------------------------------ 2. 报告结构完整性

def test_every_case_gets_two_distinct_sides():
    cases = [_case(i, constructed=(i % 2 == 0)) for i in range(6)]
    markdown, private = render_blind_markdown(cases, _outcomes(6), seed=7)
    assert private["seed"] == 7
    assert set(private["cases"]) == {c.case_id for c in cases}
    for case in cases:
        mapping = private["cases"][case.case_id]
        assert set(mapping) == {"A", "B"}
        assert mapping["A"] != mapping["B"]
        assert {mapping["A"], mapping["B"]} == {"old", "new"}
    for index, case in enumerate(cases, start=1):
        assert f"## 第 {index} 题：{case.title}" in markdown
        assert "### 回答 A" in markdown
        assert "### 回答 B" in markdown
        assert f"第 {case.case_id[-1]} 句用户输入" in markdown
    assert "构造对抗" in markdown
    assert "真实历史" in markdown


def test_all_sides_are_randomly_assigned_across_cases():
    cases = [_case(i) for i in range(40)]
    _, private = render_blind_markdown(cases, _outcomes(40), seed=3)
    assigned_a = {v["A"] for v in private["cases"].values()}
    assigned_b = {v["B"] for v in private["cases"].values()}
    assert assigned_a == {"old", "new"}
    assert assigned_b == {"old", "new"}


def test_same_seed_is_reproducible():
    cases = [_case(i) for i in range(8)]
    first_md, first_private = render_blind_markdown(cases, _outcomes(8), seed=99)
    second_md, second_private = render_blind_markdown(cases, _outcomes(8), seed=99)
    assert first_md == second_md
    assert first_private == second_private


def test_different_seed_can_change_assignment():
    cases = [_case(i) for i in range(20)]
    _, private_a = render_blind_markdown(cases, _outcomes(20), seed=1)
    _, private_b = render_blind_markdown(cases, _outcomes(20), seed=2)
    assert private_a != private_b


# ------------------------------------------------------------ 3. 不泄漏任何内部信息

def test_markdown_does_not_leak_side_names_or_internal_terms():
    cases = [_case(i) for i in range(5)]
    markdown, _ = render_blind_markdown(cases, _outcomes(5), seed=11)
    lowered = markdown.lower()
    assert "old" not in lowered
    assert "new" not in lowered
    assert "旧链路" not in markdown
    assert "新内核" not in markdown
    assert "旧" not in markdown
    assert "新" not in markdown


def test_markdown_does_not_leak_raw_text_cost_or_tokens():
    cases = [_case(i) for i in range(3)]
    outcomes = _outcomes(3)
    markdown, _ = render_blind_markdown(cases, outcomes, seed=5)
    for side_outcomes in outcomes.values():
        for outcome in side_outcomes.values():
            assert outcome.raw_text not in markdown
    assert "4242" not in markdown
    assert "1357" not in markdown
    assert "0.37" not in markdown
    assert "trace-" not in markdown


def test_markdown_shows_only_display_text_not_raw_text():
    case = _case(1)
    outcomes = {
        "case-1": {
            "old": _outcome("old", "展示用正文", raw="原稿正文不应出现"),
            "new": _outcome("new", "另一份展示正文", raw="另一份原稿"),
        }
    }
    markdown, _ = render_blind_markdown([case], outcomes, seed=2)
    assert "展示用正文" in markdown
    assert "另一份展示正文" in markdown
    assert "原稿正文不应出现" not in markdown
    assert "另一份原稿" not in markdown


def test_markdown_shows_history_and_user_input():
    case = _case(1, constructed=True)
    outcomes = {"case-1": {"old": _outcome("old", "甲"), "new": _outcome("new", "乙")}}
    markdown, _ = render_blind_markdown([case], outcomes, seed=4)
    assert "必要历史" in markdown
    assert "在等你先开口" in markdown
    assert "第 1 句用户输入" in markdown


# ------------------------------------------------------------ 4. 空投递占位

def test_empty_display_uses_status_specific_marks():
    case = _case(1)
    outcomes = {
        "case-1": {
            "old": _outcome("old", "", status="silence"),
            "new": _outcome("new", "", status="format_failed"),
        }
    }
    markdown, _ = render_blind_markdown([case], outcomes, seed=1)
    assert SILENCE_MARK in markdown
    assert FORMAT_FAIL_MARK in markdown


def test_empty_display_without_special_status_uses_generic_mark():
    case = _case(1)
    outcomes = {"case-1": {"old": _outcome("old", "", status="ok"), "new": _outcome("new", "有内容")}}
    markdown, _ = render_blind_markdown([case], outcomes, seed=1)
    assert NO_TEXT_MARK in markdown


def test_empty_display_does_not_leak_raw_text():
    case = _case(1)
    outcomes = {
        "case-1": {
            "old": _outcome("old", "", status="silence", raw="静默前的原稿"),
            "new": _outcome("new", "", status="silence", raw="另一份原稿"),
        }
    }
    markdown, _ = render_blind_markdown([case], outcomes, seed=1)
    assert "静默前的原稿" not in markdown
    assert "另一份原稿" not in markdown


# ------------------------------------------------------------ 5. JSON 往返

def test_outcomes_json_roundtrip_single_and_multi():
    multi = _outcomes(3)
    payload = outcomes_to_json(multi)
    text = json.dumps(payload, ensure_ascii=False)
    restored = outcomes_from_json(json.loads(text))
    assert restored == multi


def test_outcomes_json_roundtrip_flat_mapping():
    flat = {"old": _outcome("old", "甲"), "new": _outcome("new", "乙")}
    payload = outcomes_to_json(flat)
    restored = outcomes_from_json(json.loads(json.dumps(payload, ensure_ascii=False)))
    assert restored == flat


def test_outcomes_to_json_uses_dataclass_fields():
    payload = outcomes_to_json({"old": _outcome("old", "甲")})
    assert payload["old"]["side"] == "old"
    assert payload["old"]["display_text"] == "甲"
    assert payload["old"]["prompt_tokens"] == 4242
    assert isinstance(payload["old"]["errors"], list)


# ------------------------------------------------------------ 6. 非法输入报错

def test_outcomes_from_json_rejects_invalid_side():
    payload = {"old": {"side": "wrong", "raw_text": "", "display_text": "x", "status": "ok"}}
    with pytest.raises(ValueError):
        outcomes_from_json(payload)


def test_outcomes_from_json_rejects_side_key_mismatch():
    payload = {"old": {"side": "new", "raw_text": "", "display_text": "x", "status": "ok"}}
    with pytest.raises(ValueError):
        outcomes_from_json(payload)


def test_outcomes_from_json_rejects_unknown_field():
    payload = {"old": {"side": "old", "raw_text": "", "display_text": "x", "status": "ok", "extra": 1}}
    with pytest.raises(ValueError):
        outcomes_from_json(payload)


def test_render_requires_both_sides():
    case = _case(1)
    with pytest.raises(ValueError):
        render_blind_markdown([case], {"case-1": {"old": _outcome("old", "甲")}}, seed=1)


def test_render_rejects_unknown_side():
    case = _case(1)
    outcomes = {
        "case-1": {
            "old": _outcome("old", "甲"),
            "new": _outcome("new", "乙"),
            "extra": _outcome("new", "丙"),
        }
    }
    with pytest.raises(ValueError):
        render_blind_markdown([case], outcomes, seed=1)


def test_render_rejects_missing_case_results():
    with pytest.raises(ValueError):
        render_blind_markdown([_case(1)], {}, seed=1)


def test_render_rejects_side_field_mismatch():
    case = _case(1)
    outcomes = {
        "case-1": {
            "old": _outcome("old", "甲"),
            "new": _outcome("old", "乙"),
        }
    }
    with pytest.raises(ValueError):
        render_blind_markdown([case], outcomes, seed=1)


def test_render_rejects_duplicate_case_id():
    outcomes = _outcomes(1)
    with pytest.raises(ValueError):
        render_blind_markdown([_case(1), _case(1)], outcomes, seed=1)
