"""新主链复用的生产运行时能力。

这里只保留语音闸门、作息与跨轮语音状态；不持有模型出口，也不生成或改写回复。
"""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Callable, Coroutine, List, Optional, Tuple

from companion.assembler import PromptAssembler
from companion.config import Config
from companion.persona import holiday_span
from companion.voice_planner import VoicePlanner, is_voice_request

logger = logging.getLogger("companion")


# 对话进行中（她刚回过话）"正在输入"的上限：这时她打字是快的，
# 真按字数算会出现"聊着聊着突然卡 20 秒"的假人感。节奏标定常量，不是部署参数。
TYPING_INLINE_MAX = 8.0


class TurnRuntime:
    """向 KernelTurnHandler 提供共享运行时状态；没有 handle_turn 或生成入口。"""

    def __init__(
        self,
        *,
        config: Config,
        assembler: PromptAssembler,
        tts: Any = None,
        set_typing_fn: Optional[Callable[[bool], Coroutine[Any, Any, bool]]] = None,
    ):
        self.config = config
        self.assembler = assembler
        self.tts = tts
        # 回复时机：她拿起手机的第一条先晾一会儿；发送前演"正在输入"。两者都只是等待，不碰正文。
        self.timing = getattr(config, "timing", None)
        self.set_typing_fn = set_typing_fn
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

    def _busy_now(self) -> Tuple[str, bool]:
        """此刻她忙不忙：命中作息表里的结构化条目算忙，回退文案与长假算闲。"""
        persona = getattr(self.assembler, "persona", None)
        if persona is None or not hasattr(persona, "get_current_activity_detail"):
            return "未知（取不到作息表）", False
        now = datetime.now()
        span = holiday_span(now.strftime("%Y-%m-%d"), self._holidays())
        return persona.get_current_activity_detail(now.hour, now.weekday(), holiday_span=span)

    async def wait_first_reply(self, memory: Any) -> bool:
        """她"拿起手机"的第一条先等一会儿，返回本轮是不是第一条。

        第一条 = 她上一次说话已经过了 active_conversation_window；聊开之后不再等。
        等待发生在生成之前，相当于她还没看到消息；等完再取时间和状态，提示词里的"现在"才准。
        """
        timing = self.timing
        if timing is None:
            return False
        last = await memory.get_last_assistant_turn_time()
        is_first = (
            last is None
            or (datetime.now() - last).total_seconds() >= timing.active_conversation_window
        )
        if not (timing.timing_enabled and is_first):
            return is_first
        activity, is_busy = self._busy_now()
        if is_busy:
            delay = random.uniform(timing.first_reply_busy_delay_min, timing.first_reply_busy_delay_max)
        else:
            delay = random.uniform(timing.first_reply_free_delay_min, timing.first_reply_free_delay_max)
        logger.info(
            "[Timing] 首条延迟 %.1f 秒（作息：%s，%s）", delay, activity, "忙" if is_busy else "闲"
        )
        await asyncio.sleep(delay)
        return is_first

    async def play_typing(self, text: str, is_first_reply: bool) -> None:
        """发送前演"正在输入"：开 → 按字数等 → 关。只算她打出来的字，语音那条不算。

        任何异常都降级成"不演了，照发"，不会因为打字状态丢掉一条消息。
        """
        timing = self.timing
        if timing is None or not timing.typing_indicator_enabled or self.set_typing_fn is None:
            return
        raw = len(text or "") / 10.0 * timing.typing_seconds_per_10chars
        duration = max(timing.typing_min, min(timing.typing_max, raw))
        if not is_first_reply:
            duration = min(duration, TYPING_INLINE_MAX)
        if duration <= 0:
            return
        try:
            await self.set_typing_fn(True)
            logger.info(
                "[Timing] 正在输入 %.1f 秒（%s，%d 字）",
                duration, "首条" if is_first_reply else "对话中", len(text or ""),
            )
            await asyncio.sleep(duration)
        except Exception as exc:
            logger.warning("[Timing] 正在输入展示异常，降级为直接发送: %s", exc)
        finally:
            try:
                await self.set_typing_fn(False)
            except Exception as exc:
                logger.warning("[Timing] 关闭正在输入异常，忽略: %s", exc)

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
