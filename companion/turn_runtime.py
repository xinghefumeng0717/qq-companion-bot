"""新主链复用的生产运行时能力。

这里只保留语音闸门、作息与跨轮语音状态；不持有模型出口，也不生成或改写回复。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Tuple

from companion.assembler import PromptAssembler
from companion.config import Config
from companion.persona import holiday_span
from companion.voice_planner import VoicePlanner, is_voice_request

logger = logging.getLogger("companion")


class TurnRuntime:
    """向 KernelTurnHandler 提供共享运行时状态；没有 handle_turn 或生成入口。"""

    def __init__(self, *, config: Config, assembler: PromptAssembler, tts: Any = None):
        self.config = config
        self.assembler = assembler
        self.tts = tts
        self.runtime_events = None
        self.voice_planner = VoicePlanner(
            getattr(config.reply, "voice_frequency", "frequent"),
            max_chars=self._voice_max_chars(),
        )
        self._last_voice_sent = False
        self._last_voice_failed = False
        self._voice_retry_requested = False
        self._voice_retry_requested_at = None
        self._voice_retry_used = False

    def _voice_max_chars(self) -> int:
        """语音只改完整气泡的载体；字数上限仍取共享 TTS 配置。"""
        value = getattr(getattr(self.tts, "config", None), "max_chars", 60)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 60

    def _holidays(self) -> List[str]:
        getter = getattr(self.assembler, "get_holidays", None)
        if not callable(getter):
            return []
        try:
            holidays = getter()
        except Exception as exc:
            logger.warning("[Timing] 读取节假日列表失败，按无节假日处理: %s", exc)
            return []
        if not isinstance(holidays, list):
            return []
        return [item for item in holidays if isinstance(item, str)]

    async def _current_activity_text(self) -> str:
        now = datetime.now()
        span = holiday_span(now.strftime("%Y-%m-%d"), self._holidays())
        return self.assembler.persona.get_current_activity(
            now.hour, now.weekday(), holiday_span=span
        )

    async def _check_voice_gate(self, user_text: str = "") -> Tuple[bool, str]:
        """复用既有开关、日上限与作息闸门；用户正在聊天时不按睡眠作息拦截。"""
        explicit_request = is_voice_request(user_text)
        carry_request = bool(getattr(self, "_voice_retry_requested", False))
        self._voice_retry_used = carry_request and not explicit_request
        requested = explicit_request or carry_request
        if getattr(self, "tts", None) is None:
            return False, "没有装配 TTSManager"
        try:
            activity = await self._current_activity_text()
        except Exception as exc:
            logger.debug("[Timing] 取作息文案失败（语音闸门按允许处理）: %s", exc)
            activity = ""
        return await self.tts.check_gate(
            activity,
            awake_override=True,
            user_requested=requested,
        )
