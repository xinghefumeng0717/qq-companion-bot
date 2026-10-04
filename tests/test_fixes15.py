"""FIXES15 迭代测试集 (tests/test_fixes15.py)

按任务书 §四分组：
1. 忙/闲判定（persona.get_current_activity_detail）
   - 结构化作息条目时段=忙、回退文案=闲、长假=闲、文案与 get_current_activity 逐格一致
2. 首条判定（memory.get_last_assistant_turn_time + TurnHandler._is_first_reply）
3. 延迟选择：忙首条落 busy 区间、闲首条落 free 区间、非首条 D=0、开关关闭不等待
4. typing 时长：按字数计算 + 上下限钳制 + 非首条上限收紧
5. 端到端时序：静默期 -> typing 开 -> typing 关 -> 发送的完整序列
6. 静默降级与熔断：typing 回调抛异常/返回 False、延迟环节抛异常，消息都不许丢
7. 沉默权优先：[沉默] 时 typing 被收掉、无发送
8. 既有调用方零改动：timing_config 不传 = 行为与改动前一致
9. 配置兜底：config.toml 无 [timing] 节也能跑（全部走代码默认值）

所有等待都用 mock asyncio.sleep 记录秒数，**全程真睡 0 秒**。
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

from companion.config import (
    AccountConfig,
    Config,
    ReplyConfig,
    TimingConfig,
)
from companion.db import TIME_FORMAT
from companion.persona import (
    FREE_ACTIVITY_FALLBACK,
    LONG_HOLIDAY_ACTIVITY,
    LONG_HOLIDAY_MIN_SPAN,
    ChatStyle,
    Persona,
    RoutineItem,
    Stage,
)
from companion.turn_handler import TYPING_INLINE_MAX, TurnHandler
from helpers import close_db, make_db, make_engine_stack

# 真 sleep 先存一份：mock 之后仍要靠它让出事件循环，保持真实 await 语义
_REAL_SLEEP = asyncio.sleep


# ==========================================
# 公共脚手架
# ==========================================


def _persona(routine: List[RoutineItem]) -> Persona:
    """最小可用 Persona（只需作息表，测试不碰人设渲染）"""
    return Persona(
        name="青梓",
        user_address="你",
        core_description="",
        chat_style=ChatStyle(),
        initial_dims={},
        stages=[Stage(name="初识", tone="初识")],
        daily_routine=routine,
        personal_memories=[],
        habits=[],
        stickers_dir="stickers",
        base_dir=".",
    )


# 带空档的作息表：9~17 点在琴房（忙），20 点落在回退文案（闲），23 点~7 点睡觉（忙）
_GAP_ROUTINE = [
    RoutineItem(start=9, end=17, activity="在琴房练琴", days=None),
    RoutineItem(start=23, end=7, activity="已经睡下了，在安静的梦乡中", days=None),
]


class _FakeDatetime(datetime):
    """只改 now()，strftime/strptime 照常——供 patch('companion.turn_handler.datetime') 使用"""

    at: datetime = datetime(2026, 10, 5, 10, 0)  # 周一 10:00，作息命中"在琴房练琴"=忙

    @classmethod
    def now(cls, tz=None):
        return cls.at if tz is None else cls.at.astimezone(tz)


class _FreeFakeDatetime(_FakeDatetime):
    """把时钟挪到 20:00：作息表没覆盖，落到回退文案=闲"""

    at = datetime(2026, 10, 5, 20, 0)


def _config(timing: TimingConfig) -> Config:
    return Config(
        account=AccountConfig(allowed_user_id=123456789),
        reply=ReplyConfig(max_chunks=5, chunk_delay_min=0.0, chunk_delay_max=0.0),
        timing=timing,
    )


class _Recorder:
    """替换 asyncio.sleep：记录请求的秒数后立即返回，一个真睡都没有。

    仍 await 真 sleep(0) 让出事件循环：保持"这是一个真的 await"，
    顺序断言才有意义（否则所有协程一口气跑完，测不出时序）。
    """

    def __init__(self, events: List[tuple]):
        self.events = events

    async def __call__(self, delay, *args, **kwargs):
        self.events.append(("sleep", float(delay)))
        await _REAL_SLEEP(0)


def _typing_recorder(events: List[tuple], *, raises: bool = False, returns: bool = True):
    async def _set_typing(typing: bool) -> bool:
        events.append(("typing", bool(typing)))
        if raises:
            raise RuntimeError("NapCat 未连接")
        return returns

    return _set_typing


def _handler(
    db,
    *,
    pieces: List[str],
    timing: Optional[TimingConfig],
    events: List[tuple],
    persona: Optional[Persona] = None,
    typing_raises: bool = False,
    typing_returns: bool = True,
    pass_typing_fn: bool = True,
) -> Tuple[TurnHandler, Any]:
    """造一个走真实 memory/replier 的 TurnHandler；返回 (handler, stack)"""
    gateway = MagicMock()

    def _stream(**kwargs):
        async def _gen():
            for piece in pieces:
                yield piece

        return _gen()

    gateway.stream_chat = _stream
    gateway.config.observer_model = "deepseek-chat"
    gateway.chat = AsyncMock(return_value="{}")

    stack = make_engine_stack(
        db,
        gateway=None,  # 不给 gateway：save_turn_pair 派生的日记归档任务直接 return，不发异步请求
        reply_config=ReplyConfig(max_chunks=5, chunk_delay_min=0.0, chunk_delay_max=0.0),
    )

    assembler = MagicMock()
    assembler.assemble_messages = AsyncMock(return_value=([], "sys"))
    assembler.persona = persona if persona is not None else _persona(_GAP_ROUTINE)

    observer = MagicMock()
    observer.settle_turn = AsyncMock()
    proactive = MagicMock()
    proactive.reset_unanswered_count = AsyncMock()

    async def _send(chunk):
        events.append(("send", chunk.get("content", chunk.get("file", ""))))

    handler = TurnHandler(
        config=_config(timing) if timing is not None else _config(TimingConfig()),
        gateway=gateway,
        assembler=assembler,
        replier=stack.replier,
        memory=stack.memory,
        observer=observer,
        proactive=proactive,
        send_chunk_fn=_send,
        set_typing_fn=(
            _typing_recorder(events, raises=typing_raises, returns=typing_returns)
            if pass_typing_fn
            else None
        ),
        timing_config=timing,
    )
    return handler, stack


async def _seed_assistant_async(db, when: datetime, content: str = "在练琴呢") -> None:
    await db.execute(
        "INSERT INTO turns (role, content, proactive, has_image, created_at)"
        " VALUES ('assistant', ?, 0, 0, ?)",
        (content, when.strftime(TIME_FORMAT)),
    )


def _first_index(events: List[tuple], kind: str) -> int:
    for i, e in enumerate(events):
        if e[0] == kind:
            return i
    return -1


# ==========================================
# 1. 忙/闲判定
# ==========================================


class TestBusyIdleDetection(unittest.TestCase):
    def setUp(self):
        self.persona = _persona(_GAP_ROUTINE)

    def test_structured_routine_hour_is_busy(self):
        activity, is_busy = self.persona.get_current_activity_detail(10, 0)
        self.assertEqual(activity, "在琴房练琴")
        self.assertTrue(is_busy, "命中 daily_routine 结构化条目 = 她人在忙")

    def test_sleeping_hour_is_busy(self):
        _, is_busy = self.persona.get_current_activity_detail(2, 0)
        self.assertTrue(is_busy, "睡觉也是结构化作息条目，回消息要慢")

    def test_gap_hour_is_free(self):
        activity, is_busy = self.persona.get_current_activity_detail(20, 0)
        self.assertEqual(activity, FREE_ACTIVITY_FALLBACK)
        self.assertFalse(is_busy, '回退文案"在度过属于自己的时间" = 她人在闲')

    def test_long_holiday_is_free(self):
        """任务书明写：LONG_HOLIDAY_ACTIVITY 是回退类，长假=闲"""
        for span in (LONG_HOLIDAY_MIN_SPAN, 5, 8):
            activity, is_busy = self.persona.get_current_activity_detail(10, 0, holiday_span=span)
            self.assertEqual(activity, LONG_HOLIDAY_ACTIVITY)
            self.assertFalse(is_busy, f"段长 {span} 属长假，应归为闲")

    def test_short_holiday_keeps_saturday_routine_and_busy_flag(self):
        """短假仍命中周六作息条目（FIXES11 行为），忙闲判定跟着走"""
        persona = _persona(
            [
                RoutineItem(start=10, end=12, activity="周末在宿舍睡懒觉", days=[5, 6]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )
        activity, is_busy = persona.get_current_activity_detail(10, 3, holiday_span=1)
        self.assertEqual(activity, "周末在宿舍睡懒觉")
        self.assertTrue(is_busy)

    def test_weekday_matched_routine_is_busy(self):
        persona = _persona(
            [
                RoutineItem(start=9, end=12, activity="专业课", days=[0, 1, 2, 3, 4]),
                RoutineItem(start=23, end=7, activity="睡眠休息", days=None),
            ]
        )
        _, is_busy = persona.get_current_activity_detail(10, 3)
        self.assertTrue(is_busy)

    def test_get_current_activity_text_unchanged(self):
        """重构只是把返回值多带一位，文案必须与改动前逐格一致（防回归）"""
        cases = [
            (10, 0, 0, False),
            (20, 0, 0, False),
            (10, 0, 1, False),
            (10, 0, 8, False),
            (10, 0, 0, True),
            (2, 3, 0, False),
        ]
        for hour, wd, span, is_hol in cases:
            detail_text = self.persona.get_current_activity_detail(hour, wd, span, is_hol)[0]
            legacy_text = self.persona.get_current_activity(hour, wd, span, is_hol)
            self.assertEqual(detail_text, legacy_text, f"hour={hour} span={span}")


# ==========================================
# 2. 首条判定
# ==========================================


class TestFirstReplyDetection(unittest.TestCase):
    def _is_first(
        self, *, timing=None, seed_at: Optional[datetime] = None, proactive: bool = False
    ) -> bool:
        """在伪时钟下问一句"本轮是不是首条"。
        伪时钟是必须的：turns.created_at 只存到分钟，种子时间与读取时间
        必须同源，否则会出现"种子在未来"的假象。
        """

        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["好呀"], timing=timing or TimingConfig(), events=events
                )
                if seed_at is not None:
                    await db.execute(
                        "INSERT INTO turns (role, content, proactive, has_image, created_at)"
                        " VALUES ('assistant', ?, ?, 0, ?)",
                        (
                            "她之前发过话",
                            1 if proactive else 0,
                            seed_at.strftime(TIME_FORMAT),
                        ),
                    )
                with patch("companion.turn_handler.datetime", _FakeDatetime):
                    return await handler._is_first_reply()
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_no_assistant_record_counts_as_first(self):
        self.assertTrue(self._is_first(), "从没回过话 = 拿起手机的第一条")

    def test_assistant_two_minutes_ago_is_not_first(self):
        self.assertFalse(
            self._is_first(seed_at=_FakeDatetime.at - timedelta(minutes=2)),
            "5 分钟内回过话 = 对话激活中 = 非首条",
        )

    def test_assistant_ten_minutes_ago_is_first(self):
        self.assertTrue(
            self._is_first(seed_at=_FakeDatetime.at - timedelta(minutes=10)),
            "超过激活窗口 = 又一条新会话 = 首条",
        )

    def test_window_boundary_is_first(self):
        """恰好等于 active_conversation_window 视为首条（判据是 < 窗口才非首条）"""
        self.assertTrue(
            self._is_first(seed_at=_FakeDatetime.at - timedelta(seconds=300))
        )

    def test_custom_window_respected(self):
        self.assertFalse(
            self._is_first(
                timing=TimingConfig(active_conversation_window=1800.0),
                seed_at=_FakeDatetime.at - timedelta(minutes=10),
            ),
            "窗口调成 30 分钟后，10 分钟前的发言仍在激活区内",
        )

    def test_proactive_turn_also_keeps_conversation_active(self):
        """主动消息同样算"她回过话"：不该让首条延迟在主动消息后立刻重来"""
        self.assertFalse(
            self._is_first(
                seed_at=_FakeDatetime.at - timedelta(minutes=1), proactive=True
            )
        )


# ==========================================
# 3. 延迟选择
# ==========================================


class TestDelaySelection(unittest.TestCase):
    def _delays(self, *, clock, timing=None, seed_rows=None) -> List[float]:
        """跑 20 次 prepare_reply_timing，把每次请求的静默期秒数收回来"""

        async def _run():
            db = await make_db()
            try:
                out: List[float] = []
                cfg = timing if timing is not None else TimingConfig()
                for _ in range(20):
                    events: List[tuple] = []
                    handler, _ = _handler(
                        db, pieces=["好呀"], timing=cfg, events=events
                    )
                    if seed_rows is not None:
                        await _seed_assistant_async(db, seed_rows)
                    rec = _Recorder(events)
                    with patch("companion.turn_handler.datetime", clock), patch(
                        "asyncio.sleep", new=rec
                    ):
                        await handler.prepare_reply_timing()
                    out.extend(d for k, d in events if k == "sleep")
                return out
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_busy_first_reply_lands_in_busy_range(self):
        """10:00 命中"在琴房练琴"=忙 -> D 落在 60~600 秒"""
        delays = self._delays(clock=_FakeDatetime)
        self.assertEqual(len(delays), 20, "每轮都该有一段静默期")
        for d in delays:
            self.assertGreaterEqual(d, 60.0, f"{d} 低于忙时下限")
            self.assertLessEqual(d, 600.0, f"{d} 高于忙时上限")

    def test_free_first_reply_lands_in_free_range(self):
        """20:00 落到回退文案=闲 -> D 落在 5~30 秒"""
        delays = self._delays(clock=_FreeFakeDatetime)
        self.assertEqual(len(delays), 20)
        for d in delays:
            self.assertGreaterEqual(d, 5.0, f"{d} 低于闲时下限")
            self.assertLessEqual(d, 30.0, f"{d} 高于闲时上限")

    def test_non_first_reply_has_zero_delay(self):
        """对话激活中：一条延迟都不许有（D=0）"""
        delays = self._delays(
            clock=_FakeDatetime, seed_rows=_FakeDatetime.at - timedelta(minutes=1)
        )
        self.assertEqual(delays, [], "非首条一次都不许 sleep")

    def test_timing_disabled_has_zero_delay(self):
        """timing_enabled=False：她还是秒回，但 typing 表演照旧（两个开关独立）"""
        delays = self._delays(clock=_FakeDatetime, timing=TimingConfig(timing_enabled=False))
        self.assertEqual(delays, [])

    def test_delay_actually_varies(self):
        """随机性：不能 20 次全一样（否则是常量而不是"看心情"）"""
        delays = self._delays(clock=_FakeDatetime)
        self.assertGreater(len(set(delays)), 1, "20 次首条延迟应当有分布")


# ==========================================
# 4. typing 时长计算
# ==========================================


class TestTypingDuration(unittest.TestCase):
    def _calc(self, text: str, is_first: bool, timing: Optional[TimingConfig] = None) -> float:
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["x"], timing=timing or TimingConfig(), events=events
                )
                return handler.calc_typing_duration(text, is_first)
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_scales_with_length(self):
        """每 10 字 2 秒：50 字 = 10.0，100 字 = 20.0"""
        self.assertAlmostEqual(self._calc("字" * 50, True), 10.0, places=6)
        self.assertAlmostEqual(self._calc("字" * 100, True), 20.0, places=6)

    def test_clamped_to_min(self):
        """5 字只有 1.0 秒，钳到 typing_min=3.0"""
        self.assertAlmostEqual(self._calc("字" * 5, True), 3.0, places=6)

    def test_clamped_to_max(self):
        """400 字 80.0 秒，钳到 typing_max=25.0"""
        self.assertAlmostEqual(self._calc("字" * 400, True), 25.0, places=6)

    def test_inline_upper_bound(self):
        """非首条：25.0 收紧到 TYPING_INLINE_MAX=8.0（对话中她的打字是快的）"""
        self.assertAlmostEqual(self._calc("字" * 400, False), TYPING_INLINE_MAX, places=6)

    def test_inline_leaves_short_replies_alone(self):
        """非首条但内容很短：4.0 < 8.0，不该被拉低"""
        self.assertAlmostEqual(self._calc("字" * 20, False), 4.0, places=6)

    def test_custom_per_10chars(self):
        timing = TimingConfig(typing_seconds_per_10chars=1.0, typing_min=0.5, typing_max=99.0)
        self.assertAlmostEqual(
            self._calc("字" * 30, True, timing), 3.0, places=6
        )


# ==========================================
# 5. 端到端时序
# ==========================================


class TestTypingSequence(unittest.TestCase):
    def _run_turn(
        self,
        *,
        pieces: List[str],
        timing: Optional[TimingConfig] = None,
        clock=_FakeDatetime,
        persona: Optional[Persona] = None,
        seed_rows: Optional[datetime] = None,
        typing_raises: bool = False,
        typing_returns: bool = True,
        pass_typing_fn: bool = True,
        pass_timing: bool = True,
    ) -> List[tuple]:
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db,
                    pieces=pieces,
                    timing=(
                        (timing if timing is not None else TimingConfig())
                        if pass_timing
                        else None
                    ),
                    events=events,
                    persona=persona,
                    typing_raises=typing_raises,
                    typing_returns=typing_returns,
                    pass_typing_fn=pass_typing_fn,
                )
                if seed_rows is not None:
                    await _seed_assistant_async(db, seed_rows)
                rec = _Recorder(events)
                with patch("companion.turn_handler.datetime", clock), patch(
                    "asyncio.sleep", new=rec
                ):
                    await handler.handle_turn("在吗", None)
                return events
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_busy_first_reply_full_sequence(self):
        """完整序列：静默期 D -> typing 开 -> typing 关 -> 发送"""
        events = self._run_turn(pieces=["刚练完琴，手有点酸。你吃了没？"])

        kinds = [e[0] for e in events]
        self.assertEqual(kinds[0], "sleep", "第一步必须是静默期等待（她还没看到）")
        self.assertEqual(kinds[1], "typing", "生成完才开 typing")
        self.assertEqual(kinds[2], "sleep", "typing 期间要等 T_typing")
        self.assertEqual(kinds[3], "typing", "发送前必须收掉 typing")
        self.assertIn("send", kinds)
        self.assertLess(
            _first_index(events, "typing"),
            _first_index(events, "send"),
            "typing 必须在发送之前演完",
        )

    def test_sequence_payloads(self):
        events = self._run_turn(pieces=["刚练完琴，手有点酸。你吃了没？"])
        delay = events[0][1]
        self.assertGreaterEqual(delay, 60.0)
        self.assertLessEqual(delay, 600.0)
        self.assertEqual(events[1], ("typing", True))
        # 落库纯文本 13 字 -> 13/10*2 = 2.6 -> 钳到 typing_min=3.0
        self.assertAlmostEqual(events[2][1], 3.0, places=6)
        self.assertEqual(events[3], ("typing", False))

    def test_typing_never_opens_during_silent_first_period(self):
        """整个静默期里她"还没看到"，一个 typing 都不许发"""
        events = self._run_turn(pieces=["好"])
        first_typing = _first_index(events, "typing")
        self.assertGreater(first_typing, 0, "typing 必须发生在静默期之后")

    def test_typing_opened_exactly_once(self):
        """负面清单第 4 条：只开/关一次，不做"打了又删"的反复表演"""
        events = self._run_turn(pieces=["一句。第二句。第三句。"])
        typings = [e for e in events if e[0] == "typing"]
        self.assertEqual(typings, [("typing", True), ("typing", False)])

    def test_followup_turn_no_long_delay(self):
        """连续第二条：无长延迟，只走短 typing 展示"""
        events = self._run_turn(
            pieces=["还在呢，你说"],
            seed_rows=_FakeDatetime.at - timedelta(seconds=30),
        )
        # 非首条：首个事件就应该是 typing 开，中间没有 60~600 秒的等待
        self.assertEqual(events[0], ("typing", True))
        long_delays = [
            d
            for k, d in events
            if k == "sleep" and d >= 60.0
        ]
        self.assertEqual(long_delays, [], "对话激活中不该出现长延迟")
        self.assertLessEqual(
            events[1][1], TYPING_INLINE_MAX, "非首条 typing 必须收紧上限"
        )

    def test_typing_disabled_still_sends(self):
        """typing_indicator_enabled=False：照常发，只是不演 typing"""
        events = self._run_turn(
            pieces=["好呀"],
            timing=TimingConfig(typing_indicator_enabled=False),
        )
        self.assertEqual([e for e in events if e[0] == "typing"], [])
        self.assertIn("send", [e[0] for e in events])

    def test_timing_enabled_false_keeps_typing(self):
        """两个开关独立：关掉延迟不该顺手关掉 typing 表演"""
        events = self._run_turn(
            pieces=["好呀"],
            timing=TimingConfig(timing_enabled=False),
        )
        self.assertEqual([e for e in events if e[0] == "typing"], [("typing", True), ("typing", False)])
        self.assertIn("send", [e[0] for e in events])

    def test_legacy_caller_without_timing_config_unchanged(self):
        """既有调用方（既有测试/冒烟脚本）不传 timing_config = 旧行为：无延迟无 typing"""
        events = self._run_turn(pieces=["好呀，晚安啦"], pass_timing=False)
        self.assertEqual([e for e in events if e[0] == "typing"], [])
        self.assertEqual([e for e in events if e[0] == "sleep" and e[1] > 8.0], [])
        self.assertIn("send", [e[0] for e in events])

    def test_no_typing_fn_means_no_typing_calls(self):
        """没注入 typing 回调时不允许有任何 typing 动作（不能凭空调）"""
        events = self._run_turn(pieces=["好呀"], pass_typing_fn=False)
        self.assertEqual([e for e in events if e[0] == "typing"], [])
        self.assertIn("send", [e[0] for e in events])


# ==========================================
# 6. 静默降级与熔断
# ==========================================


class TestGracefulDegradation(unittest.TestCase):
    def _run_turn(self, **kwargs) -> List[tuple]:
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db,
                    pieces=kwargs.pop("pieces", ["好呀，在的"]),
                    timing=TimingConfig(),
                    events=events,
                    **kwargs,
                )
                rec = _Recorder(events)
                with patch("companion.turn_handler.datetime", _FakeDatetime), patch(
                    "asyncio.sleep", new=rec
                ):
                    await handler.handle_turn("在吗", None)
                return events
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_typing_callback_raising_still_sends(self):
        """NapCat 不支持/断线：typing 抛异常也必须把消息发出去"""
        events = self._run_turn(typing_raises=True)
        self.assertIn("send", [e[0] for e in events], "typing 抛异常不许把消息丢死")

    def test_typing_callback_returning_false_still_sends(self):
        """set_input_status 返回 False（降级）时照发"""
        events = self._run_turn(typing_returns=False)
        self.assertIn("send", [e[0] for e in events])

    def test_typing_wait_failure_still_sends(self):
        """等待 T_typing 时出异常：必须走到发送，且 typing 已被收掉"""
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["好呀，在的"], timing=TimingConfig(), events=events
                )

                calls = {"n": 0}

                async def _boom(delay, *a, **kw):
                    calls["n"] += 1
                    events.append(("sleep", float(delay)))
                    if calls["n"] == 2:
                        # 第一次 sleep 是静默期 D（放行），第二次是 T_typing（炸）
                        raise RuntimeError("sleep 被打断")
                    await _REAL_SLEEP(0)

                with patch("companion.turn_handler.datetime", _FakeDatetime), patch(
                    "asyncio.sleep", new=_boom
                ):
                    await handler.handle_turn("在吗", None)
                return events
            finally:
                await close_db(db)

        events = asyncio.run(_run())
        self.assertIn("send", [e[0] for e in events], "等待失败不许把消息丢死")
        self.assertIn(("typing", False), events, "异常路径也必须收掉 typing")

    def test_typing_cancelled_still_closes_typing(self):
        """停机取消发生在 typing 等待期间：CancelledError 该往上抛（不能吞掉停机），
        但 finally 里的收尾必须已经跑过——否则 NapCat 侧的"正在输入"会永久挂着。"""
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["好呀，在的"], timing=TimingConfig(), events=events
                )

                calls = {"n": 0}

                async def _cancel(delay, *a, **kw):
                    calls["n"] += 1
                    events.append(("sleep", float(delay)))
                    if calls["n"] == 2:
                        raise asyncio.CancelledError
                    await _REAL_SLEEP(0)

                raised = False
                with patch("companion.turn_handler.datetime", _FakeDatetime), patch(
                    "asyncio.sleep", new=_cancel
                ):
                    try:
                        await handler.handle_turn("在吗", None)
                    except asyncio.CancelledError:
                        raised = True
                return events, raised
            finally:
                await close_db(db)

        events, raised = asyncio.run(_run())
        self.assertTrue(raised, "CancelledError 必须继续往上抛（停机语义不能被吞）")
        self.assertIn(("typing", False), events, "取消时也必须收掉 typing")

    def test_timing_stage_exception_degrades_to_immediate_send(self):
        """熔断：延迟环节炸了（查库失败）-> 记 WARNING + 立即生成立即发送"""
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["好呀，在的"], timing=TimingConfig(), events=events
                )

                async def _boom(*a, **kw):
                    raise RuntimeError("数据库炸了")

                handler.memory.get_last_assistant_turn_time = _boom
                rec = _Recorder(events)
                with patch("companion.turn_handler.datetime", _FakeDatetime), patch(
                    "asyncio.sleep", new=rec
                ):
                    await handler.handle_turn("在吗", None)
                return events
            finally:
                await close_db(db)

        events = asyncio.run(_run())
        self.assertIn("send", [e[0] for e in events], "熔断后消息必须发出去")
        self.assertEqual(
            [e for e in events if e[0] == "sleep" and e[1] >= 60.0],
            [],
            "查库炸了不该还去睡首条延迟",
        )

    def test_persona_missing_degrades_to_free(self):
        """取不到 persona（assembler 是 mock）时按闲处理：延迟短，最保守"""
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["好呀"], timing=TimingConfig(), events=events
                )
                del handler.assembler.persona  # 让 getattr 拿不到
                with patch("companion.turn_handler.datetime", _FakeDatetime):
                    activity, is_busy = handler.current_busy_state()
                return activity, is_busy
            finally:
                await close_db(db)

        activity, is_busy = asyncio.run(_run())
        self.assertFalse(is_busy)
        self.assertIn("未知", activity)


# ==========================================
# 7. 沉默权优先
# ==========================================


class TestSilenceBeatsTyping(unittest.TestCase):
    def test_silence_closes_typing_and_sends_nothing(self):
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["[沉默]"], timing=TimingConfig(), events=events
                )
                rec = _Recorder(events)
                with patch("companion.turn_handler.datetime", _FakeDatetime), patch(
                    "asyncio.sleep", new=rec
                ):
                    await handler.handle_turn("在吗", None)
                return events
            finally:
                await close_db(db)

        events = asyncio.run(_run())
        typings = [e for e in events if e[0] == "typing"]
        self.assertEqual(
            typings, [("typing", False)], "沉默时只许收掉 typing，绝不许开"
        )
        self.assertEqual([e for e in events if e[0] == "send"], [], "沉默不许发送")

    def test_silence_does_not_wait_typing_duration(self):
        """沉默不该为了演 typing 白等一场"""
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                handler, _ = _handler(
                    db, pieces=["[沉默]"], timing=TimingConfig(), events=events
                )
                rec = _Recorder(events)
                with patch("companion.turn_handler.datetime", _FakeDatetime), patch(
                    "asyncio.sleep", new=rec
                ):
                    await handler.handle_turn("在吗", None)
                return events
            finally:
                await close_db(db)

        events = asyncio.run(_run())
        self.assertNotIn(("typing", True), events)
        self.assertLessEqual(len([e for e in events if e[0] == "sleep"]), 1)


# ==========================================
# 8. 主动消息 typing
# ==========================================


class TestProactiveTyping(unittest.TestCase):
    def _scheduler(
        self, db, *, events, timing=TimingConfig(), typing_raises=False, with_timing=True
    ):
        from companion.config import ProactiveConfig
        from companion.proactive import ProactiveScheduler

        stack = make_engine_stack(
            db, gateway=None, reply_config=ReplyConfig(5, 0.0, 0.0)
        )

        async def _send(chunk):
            events.append(("send", chunk.get("content", "")))

        async def _set_typing(typing_flag: bool) -> bool:
            events.append(("typing", bool(typing_flag)))
            if typing_raises:
                raise RuntimeError("NapCat not connected")
            return True

        gateway = MagicMock()
        gateway.config.observer_model = "deepseek-chat"
        gateway.config.text_model = "deepseek-chat"
        gateway.chat = AsyncMock(
            side_effect=[json.dumps({"choice": "A", "topic_hint": ""}), "刚下课。"]
        )

        return ProactiveScheduler(
            config=ProactiveConfig(enabled=True, quiet_hours=[], max_unanswered=2),
            persona=stack.persona,
            affection=stack.affection,
            mood=stack.mood,
            memory=stack.memory,
            stickers=stack.stickers,
            replier=stack.replier,
            gateway=gateway,
            db=db,
            send_msg_fn=_send,
            assembler=stack.assembler,
            holidays_provider=lambda: [],
            set_typing_fn=_set_typing if with_timing else None,
            timing_config=timing if with_timing else None,
        )

    def _run(self, **kwargs) -> List[tuple]:
        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                sched = self._scheduler(db, events=events, **kwargs)
                rec = _Recorder(events)
                with patch("asyncio.sleep", new=rec):
                    await sched.trigger_cycle()
                return events
            finally:
                await close_db(db)

        return asyncio.run(_run())

    def test_proactive_plays_typing_before_send(self):
        events = self._run()
        typings = [e for e in events if e[0] == "typing"]
        self.assertEqual(typings, [("typing", True), ("typing", False)])
        self.assertIn("send", [e[0] for e in events])
        self.assertLess(_first_index(events, "typing"), _first_index(events, "send"))

    def test_proactive_has_no_first_reply_delay(self):
        """主动消息 D=0：不该出现首条那种 60~600 秒的静默期"""
        events = self._run()
        self.assertEqual(
            [d for k, d in events if k == "sleep" and d >= 60.0],
            [],
            "主动消息没有'拿起手机'的延迟",
        )

    def test_proactive_typing_failure_still_sends(self):
        events = self._run(typing_raises=True)
        self.assertIn("send", [e[0] for e in events], "typing 抛异常不许把主动消息丢死")
        self.assertIn(("typing", False), events, "异常路径也必须收掉 typing")

    def test_proactive_without_timing_config_sends_plainly(self):
        """既有调用方不传新参数 = 旧行为：无 typing、无延迟"""
        events = self._run(with_timing=False)
        self.assertEqual([e for e in events if e[0] == "typing"], [])
        self.assertIn("send", [e[0] for e in events])


# ==========================================
# 9. NapCat 封装
# ==========================================


class TestSetInputStatusWrapper(unittest.TestCase):
    def _client(self):
        from companion.config import OneBotConfig
        from companion.onebot import OneBotClient

        return OneBotClient(OneBotConfig(), allowed_user_id=1, image_save_dir="data/_t15")

    def test_action_name_and_params(self):
        client = self._client()
        seen: Dict[str, Any] = {}

        async def _fake_call(action, params, timeout=2.0):
            seen.update(action=action, params=params, timeout=timeout)
            return {}

        client._call_action = _fake_call

        async def _run():
            return await client.set_input_status(999, True)

        self.assertTrue(asyncio.run(_run()))
        self.assertEqual(seen["action"], "set_input_status")
        self.assertEqual(seen["params"], {"user_id": 999, "event_type": 1})
        self.assertEqual(seen["timeout"], 2.0)

    def test_typing_false_maps_to_event_type_zero(self):
        client = self._client()
        seen: Dict[str, Any] = {}

        async def _fake_call(action, params, timeout=2.0):
            seen.update(params=params)
            return {}

        client._call_action = _fake_call

        async def _run():
            return await client.set_input_status(999, False)

        asyncio.run(_run())
        self.assertEqual(seen["params"]["event_type"], 0)

    def test_failure_returns_false_without_raising(self):
        """_call_action 返回 None（未连接/超时/报错）-> False，不抛异常"""
        client = self._client()

        async def _fake_call(action, params, timeout=2.0):
            return None

        client._call_action = _fake_call
        self.assertFalse(asyncio.run(client.set_input_status(1, True)))

    def test_real_call_action_returns_none_when_disconnected(self):
        """未连接时 _call_action 静默返回 None（真实实现，不 mock）"""
        client = self._client()
        client._ws = None
        self.assertFalse(asyncio.run(client.set_input_status(1, True)))


# ==========================================
# 10. 配置兜底
# ==========================================


MINIMAL_TOML = """
[account]
allowed_user_id = 123456789

[onebot]
ws_url = "ws://127.0.0.1:3001"
access_token = "token"

[llm]
current = "deepseek"

[models.deepseek]
provider = "deepseek"
base_url = "https://api.deepseek.com"
api_key = "sk-test"
chat = "deepseek-flash"
vision = "deepseek-flash"
tasks = "deepseek-flash"
"""


class TestTimingConfigDefaults(unittest.TestCase):
    def _load(self, toml_text: str) -> Config:
        fd, path = tempfile.mkstemp(suffix=".toml")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(toml_text)
            return Config.load(path)
        finally:
            os.remove(path)

    def test_missing_timing_section_uses_code_defaults(self):
        """所有者拍板决策 4：服务器 config.toml 零改动也能跑"""
        t = self._load(MINIMAL_TOML).timing
        self.assertTrue(t.timing_enabled)
        self.assertTrue(t.typing_indicator_enabled)
        self.assertEqual(t.first_reply_busy_delay_min, 60.0)
        self.assertEqual(t.first_reply_busy_delay_max, 600.0)
        self.assertEqual(t.first_reply_free_delay_min, 5.0)
        self.assertEqual(t.first_reply_free_delay_max, 30.0)
        self.assertEqual(t.active_conversation_window, 300.0)
        self.assertEqual(t.typing_seconds_per_10chars, 2.0)
        self.assertEqual(t.typing_min, 3.0)
        self.assertEqual(t.typing_max, 25.0)

    def test_partial_timing_section_overrides_only_given_keys(self):
        t = self._load(MINIMAL_TOML + '\n[timing]\ntiming_enabled = false\n').timing
        self.assertFalse(t.timing_enabled)
        self.assertTrue(t.typing_indicator_enabled, "没写的键走默认值")
        self.assertEqual(t.first_reply_busy_delay_max, 600.0)

    def test_full_timing_section_parses(self):
        t = self._load(
            MINIMAL_TOML
            + """
[timing]
timing_enabled = true
typing_indicator_enabled = false
first_reply_busy_delay_min = 30.0
first_reply_busy_delay_max = 120.0
first_reply_free_delay_min = 2.0
first_reply_free_delay_max = 10.0
active_conversation_window = 600.0
typing_seconds_per_10chars = 1.0
typing_min = 1.0
typing_max = 9.0
"""
        ).timing
        self.assertFalse(t.typing_indicator_enabled)
        self.assertEqual(t.first_reply_busy_delay_min, 30.0)
        self.assertEqual(t.first_reply_busy_delay_max, 120.0)
        self.assertEqual(t.first_reply_free_delay_min, 2.0)
        self.assertEqual(t.first_reply_free_delay_max, 10.0)
        self.assertEqual(t.active_conversation_window, 600.0)
        self.assertEqual(t.typing_seconds_per_10chars, 1.0)
        self.assertEqual(t.typing_min, 1.0)
        self.assertEqual(t.typing_max, 9.0)


# ==========================================
# 11. 沙箱假 typing 实现
# ==========================================


class TestSandboxTypingFake(unittest.TestCase):
    def test_set_typing_sandbox_returns_true_and_prints(self):
        import io
        from contextlib import redirect_stdout

        from companion.chat import ChatSession

        buf = io.StringIO()
        with redirect_stdout(buf):
            ok = asyncio.run(ChatSession.set_typing_sandbox(None, True))
        self.assertTrue(ok, "沙箱假实现必须返回 True（模拟成功）")
        self.assertIn("正在输入 开", buf.getvalue())

        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            asyncio.run(ChatSession.set_typing_sandbox(None, False))
        self.assertIn("正在输入 关", buf2.getvalue())


if __name__ == "__main__":
    unittest.main()
