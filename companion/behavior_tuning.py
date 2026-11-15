"""心理状态到程序旋钮的唯一映射；不含提示词，也不改变明确用户请求。"""
from __future__ import annotations

from typing import Any, Mapping


def _number(state: Mapping[str, Any] | None, key: str, default: float) -> float:
    try:
        return float((state or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def first_reply_free_bounds(timing: Any, affection: Mapping[str, Any] | None) -> tuple[float, float]:
    """只让好感微调闲时首条等待；忙时作息不动，心情不用于晾人。"""
    low = float(timing.first_reply_free_delay_min)
    high = float(timing.first_reply_free_delay_max)
    value = _number(affection, "composite", 50.0)
    scale = 0.8 if value >= 70.0 else 1.2 if value <= 30.0 else 1.0
    return low * scale, high * scale


def proactive_interval_bounds(
    config: Any,
    affection: Mapping[str, Any] | None,
    mood: Mapping[str, Any] | None,
) -> tuple[float, float]:
    """亲近可略多主动，坏心情只降频；外围未回复与安静时段闸门不变。"""
    value = _number(affection, "composite", 50.0)
    scale = 0.85 if value >= 70.0 else 1.25 if value <= 30.0 else 1.0
    if _number(mood, "v", 2.0) < 0.0:
        scale *= 1.25
    scale = min(1.5, max(0.75, scale))
    return config.wake_interval_min * scale, config.wake_interval_max * scale


def expression_cooldown_turns(mood: Mapping[str, Any] | None, base: int = 5) -> int:
    """心情好且较有精神时略多表情，心情差时减少；默认仍约六轮一次。"""
    v = _number(mood, "v", 2.0)
    a = _number(mood, "a", 1.0)
    if v < 0.0:
        return max(base, 7)
    if v >= 5.0 and a >= 2.0:
        return min(base, 4)
    return base


def allow_content_voice(mood: Mapping[str, Any] | None) -> bool:
    """只减少坏心情下的自动语音；明确请求不经过这里。"""
    return _number(mood, "v", 2.0) >= 0.0 and _number(mood, "frustration", 0.0) <= 6.0


__all__ = [
    "allow_content_voice",
    "expression_cooldown_turns",
    "first_reply_free_bounds",
    "proactive_interval_bounds",
]
