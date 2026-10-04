"""FIXES15 真实 API 冒烟验证 (scripts/smoke/smoke_fixes15.py)

沿用 smoke_fixes12/14 的路子：生产库副本 → 沙箱 → 本地 config.toml 的真实 key，
零服务器副作用。本轮要验的不是文笔，而是**回复时机的行为序列**：

A. 忙时首条：完整序列 = 静默期延迟 D（日志带作息）→ typing 开 → typing 关 → 逐段发送
B. 连续第二条：同一通对话里的下一条，D=0，只走短 typing 展示
C. 强制定序对照：同一套 TurnHandler + 固定回复文本，证明序列来自代码结构，
   不是"模型这次刚好没沉默"
D. 主动消息 typing：直调 proactive 的表演函数，验 wiring 与时长计算（不赌决策层选 A）

关于等待：忙时首条的 D 是 60~600 秒，冒烟不能真等。做法是替换 asyncio.sleep：
把请求的秒数原样记进事件流，再只真睡 20ms。报告里 requested/waited 两列都留，
谁都能看出哪些是真等、哪些是记账。

报告落 data/smoke_fixes15_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.chat import ChatSession
from companion.config import ReplyConfig
from companion.db import TIME_FORMAT
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.turn_handler import TurnHandler

logging.basicConfig(level=logging.WARNING)

REAL_DB = r"C:\Users\user\AppData\Local\Temp\qingzi_real.db"
FALLBACK_DB = "data/companion.db"
SANDBOX_DB = "data/smoke_fixes15_sandbox.db"
REPORT_FILE = "data/smoke_fixes15_report.json"

# 真 sleep 先存一份。冒烟会 patch 全局的 asyncio.sleep，记录器自己必须调这一份，
# 否则它会 patch 出的自己 —— 无限递归。
_REAL_SLEEP = asyncio.sleep

# 冒烟里每次 asyncio.sleep 的实际上限（秒）。请求值原样记账，只真等这么多。
REAL_SLEEP_CAP = 0.02

# 种子对话：把"她最近一次发言"放到 2 小时前，确保 A/C 段是真首条。
# 踩过的坑（FIXES12/14 教训）：种子时间戳打在"现在"会被 5 分钟激活窗口零成本拦掉整条链，
# 断言自动成立。这里必须往前挪够。
SEED_OFFSET = timedelta(hours=2)
SEED_USER = "晚上有空吗，想找你聊两句"
SEED_BOT = "在呢，你说"

# C 段钉死的回复：不能是 [沉默]，否则又变成"模型自由意志"
C_FIXED_REPLY = "在呢，刚到寝室。今天是真的累。"
# D 段直调用文本（9 字 -> 1.8s -> 钳到 typing_min=3.0）
D_FIXED_TEXT = "刚下课，人还活着。"

SEGMENTS = ("A_busy_first_reply", "B_followup_no_delay", "C_forced_sequence", "D_proactive_typing")


class _SleepRecorder:
    """替换 asyncio.sleep：记录请求秒数，只真等 REAL_SLEEP_CAP。
    仍然是真的 await（await 真 sleep），保持协程顺序语义。
    """

    def __init__(self, events: List[Dict[str, Any]]):
        self.events = events

    async def __call__(self, delay, *args, **kwargs):
        d = float(delay)
        self.events.append(
            {"type": "sleep", "requested": round(d, 3), "waited": round(min(d, REAL_SLEEP_CAP), 3)}
        )
        await _REAL_SLEEP(min(d, REAL_SLEEP_CAP))


def _fmt_events(events: List[Dict[str, Any]]) -> str:
    """把事件流渲染成一行行可读日志（贴进报告当证据）"""
    out = []
    for e in events:
        t = e["type"]
        if t == "sleep":
            out.append(f"      等待 {e['requested']} 秒（实际只等 {e['waited']}s）")
        elif t == "typing":
            out.append(f"      正在输入 {'开' if e['typing'] else '关'}")
        elif t == "send":
            out.append(f"      发送气泡: {e['text']}")
    return "\n".join(out)


def _idx(events: List[Dict[str, Any]], pred: Callable[[Dict[str, Any]], bool]) -> int:
    for i, e in enumerate(events):
        if pred(e):
            return i
    return -1


def _is_sleep(e: Dict[str, Any]) -> bool:
    return e["type"] == "sleep"


def _is_typing_on(e: Dict[str, Any]) -> bool:
    return e["type"] == "typing" and e["typing"]


def _is_typing_off(e: Dict[str, Any]) -> bool:
    return e["type"] == "typing" and not e["typing"]


def _is_send(e: Dict[str, Any]) -> bool:
    return e["type"] == "send"


def _fixed_stream(text: str):
    """固定回复的流式假件：绕开模型自由意志，单独验序列结构"""

    async def _stream(**kwargs):
        yield text

    return _stream


async def _seed_history(db, at: datetime) -> None:
    """清空沙箱 turns 并种一对 SEED_OFFSET 之前的对话（保证本段是真首条）"""
    await db.execute("DELETE FROM turns")
    ts = at.strftime(TIME_FORMAT)
    await db.execute(
        "INSERT INTO turns (role, content, proactive, has_image, created_at)"
        " VALUES ('user', ?, 0, 0, ?)",
        (SEED_USER, ts),
    )
    await db.execute(
        "INSERT INTO turns (role, content, proactive, has_image, created_at)"
        " VALUES ('assistant', ?, 0, 0, ?)",
        (SEED_BOT, ts),
    )


class Harness:
    """给某一段冒烟造一套独立的 TurnHandler/ProactiveScheduler + 事件流。

    事件流必须与 handler 同生共死：collect/typing_fake 是闭包，捕获的是建栈那一刻的
    list 对象。每段各建一套，段与段之间才不会互相串事件。
    """

    def __init__(self, session: ChatSession, fixed_reply: Optional[str] = None):
        self.session = session
        self.events: List[Dict[str, Any]] = []
        self.fixed_reply = fixed_reply
        self.llm_calls: List[Dict[str, Any]] = []
        self._build()

    def _build(self) -> None:
        s = self.session
        events = self.events

        async def collect(chunk):
            text = chunk.get("content", chunk.get("file", ""))
            events.append({"type": "send", "text": text})

        async def typing_fake(typing: bool) -> bool:
            events.append({"type": "typing", "typing": bool(typing)})
            # 真的调沙箱假实现：它会打印"[沙箱] 正在输入 开/关"，且零外呼
            return await s.set_typing_sandbox(typing)

        # 气泡段间延迟不是本轮验收对象（负面清单第 2 条），置 0 让冒烟跑得快
        replier = Replier(ReplyConfig(5, 0.0, 0.0), s.stickers)
        self.scheduler = ProactiveScheduler(
            config=s.config.proactive,
            persona=s.persona,
            affection=s.affection,
            mood=s.mood,
            memory=s.memory,
            stickers=s.stickers,
            replier=replier,
            gateway=s.gateway,
            db=s.db,
            send_msg_fn=collect,
            assembler=s.assembler,
            holidays_provider=s.config.get_holidays,
            set_typing_fn=typing_fake,
            timing_config=s.config.timing,
        )
        self.handler = TurnHandler(
            config=s.config,
            gateway=s.gateway,
            assembler=s.assembler,
            replier=replier,
            memory=s.memory,
            observer=s.observer,
            proactive=self.scheduler,
            send_chunk_fn=collect,
            set_typing_fn=typing_fake,
            timing_config=s.config.timing,
        )

    async def turn(self, user_text: str) -> float:
        """跑一轮真实 handle_turn，返回墙钟耗时（秒）"""
        real_stream = self.session.gateway.stream_chat
        spy = self.llm_calls

        async def _spy(**kwargs):
            spy.append({"purpose": kwargs.get("purpose"), "model": kwargs.get("model")})
            async for piece in real_stream(**kwargs):
                yield piece

        if self.fixed_reply is not None:
            self.handler.gateway.stream_chat = _fixed_stream(self.fixed_reply)
        else:
            self.handler.gateway.stream_chat = _spy

        t0 = time.monotonic()
        with patch("asyncio.sleep", new=_SleepRecorder(self.events)):
            await self.handler.handle_turn(user_text, None)
        wall = time.monotonic() - t0
        # 还原真实流式通道：固定流式只在 C 段用，不能漏给下一段
        self.handler.gateway.stream_chat = real_stream
        # 让观察者结算任务跑完（真 sleep，已出 patch 作用域）
        await asyncio.sleep(0.05)
        return wall


def _busy_state_report(session: ChatSession) -> Dict[str, Any]:
    """如实记下"此刻她算什么作息、算忙还是闲"——D 的区间由此决定"""
    now_dt = datetime.now()
    holidays = session.config.get_holidays()
    activity, is_busy = session.persona.get_current_activity_detail(
        now_dt.hour, now_dt.weekday()
    )
    t = session.config.timing
    lo, hi = (
        (t.first_reply_busy_delay_min, t.first_reply_busy_delay_max)
        if is_busy
        else (t.first_reply_free_delay_min, t.first_reply_free_delay_max)
    )
    return {
        "now": now_dt.strftime(TIME_FORMAT),
        "weekday": now_dt.weekday(),
        "hour": now_dt.hour,
        "activity": activity,
        "is_busy": is_busy,
        "delay_range": [lo, hi],
        "holidays": holidays,
    }


async def run_smoke_test() -> dict:
    prod_db = REAL_DB if os.path.exists(REAL_DB) else FALLBACK_DB
    print("=" * 70)
    print(f"   FIXES15 真实 API 冒烟验证 (基准库: {prod_db})")
    print("=" * 70)

    session = ChatSession(prod_db_path=prod_db, sandbox_db_path=SANDBOX_DB)
    await session.initialize()
    print("✓ 沙箱初始化完成（真实 key，无服务器副作用）")

    busy = _busy_state_report(session)
    report: Dict[str, Any] = {
        "base_db": prod_db,
        "wait_policy": {
            "real_sleep_cap_seconds": REAL_SLEEP_CAP,
            "why": "忙时首条的 D 是 60~600 秒，冒烟不能真等；请求秒数原样记账，只真等 20ms",
        },
        "busy_state": busy,
        "verdict": "PENDING",
    }
    for k in SEGMENTS:
        report[k] = {}
    print(
        f"✓ 此刻作息: {busy['activity']}（{'忙' if busy['is_busy'] else '闲'}），"
        f"首条延迟区间 {busy['delay_range']} 秒"
    )

    try:
        # ================= A 段：忙时首条 =================
        print("\n" + "-" * 70)
        print("A 段：忙时首条（真实 API，验完整序列）")
        print("-" * 70)
        seed_at = datetime.now() - SEED_OFFSET
        await _seed_history(session.db, seed_at)
        print(f"  种子历史: {SEED_USER} / {SEED_BOT}（{seed_at:{TIME_FORMAT}}）")
        print("  说明：种子放到 2 小时前，否则会被 5 分钟激活窗口判成非首条，整段验不到")

        h = Harness(session)
        wall = await h.turn("刚吃完饭回宿舍了，今天累死了")
        ev = h.events
        print(_fmt_events(ev))

        i_d, i_on, i_off, i_send = _idx(ev, _is_sleep), _idx(ev, _is_typing_on), _idx(ev, _is_typing_off), _idx(ev, _is_send)
        n_typing = len([e for e in ev if e["type"] == "typing"])
        lo, hi = busy["delay_range"]
        a_pass = bool(i_send >= 0) and i_d == 0 and i_d < i_on < i_off < i_send and lo <= ev[i_d]["requested"] <= hi and n_typing == 2
        a_silence = i_send < 0
        print(f"  墙钟耗时: {wall:.2f} 秒（含真实 API 生成；等待均为记账）")
        print(f"  判定: {'PASS（延迟 -> typing开 -> typing关 -> 发送，四步齐全）' if a_pass else 'FAIL'}"
              f"{'｜⚠ 模型本轮选择沉默，未产生发送' if a_silence else ''}")

        report["A_busy_first_reply"] = {
            "seed_at": seed_at.strftime(TIME_FORMAT),
            "activity": busy["activity"],
            "is_busy": busy["is_busy"],
            "delay_range": [lo, hi],
            "first_delay_requested": ev[i_d]["requested"] if i_d >= 0 else None,
            "llm_calls": h.llm_calls,
            "wall_seconds": round(wall, 2),
            "event_kinds": [e["type"] for e in ev],
            "typing_call_count": n_typing,
            "sent": [e["text"] for e in ev if _is_send(e)],
            "silence": a_silence,
            "events": ev,
            "pass": a_pass,
        }

        # ================= B 段：连续第二条 =================
        print("\n" + "-" * 70)
        print("B 段：连续第二条（同一通对话，D=0）")
        print("-" * 70)
        if a_silence:
            print("  ⚠ A 段沉默未落 assistant 记录，显式补一条当前时刻的发言作为激活基线")
            await session.db.execute(
                "INSERT INTO turns (role, content, proactive, has_image, created_at)"
                " VALUES ('assistant', ?, 0, 0, ?)",
                ("刚刚回过话", datetime.now().strftime(TIME_FORMAT)),
            )

        h = Harness(session)
        await h.turn("那你说说今天累在哪了")
        ev = h.events
        print(_fmt_events(ev))
        long_delays = [e["requested"] for e in ev if _is_sleep(e) and e["requested"] >= 60.0]
        b_on = _idx(ev, _is_typing_on)
        typings = [e for e in ev if e["type"] == "typing"]
        b_pass = b_on == 0 and not long_delays and typings[:1] == [{"type": "typing", "typing": True}]
        print(f"  60 秒以上的等待: {long_delays or '无'}")
        print(f"  判定: {'PASS（首事件即 typing 开，无长延迟）' if b_pass else 'FAIL'}"
              f"{'｜本轮沉默未发送' if _idx(ev, _is_send) < 0 else ''}")

        report["B_followup_no_delay"] = {
            "long_delays": long_delays,
            "event_kinds": [e["type"] for e in ev],
            "typing_calls": typings,
            "sent": [e["text"] for e in ev if _is_send(e)],
            "silence": _idx(ev, _is_send) < 0,
            "events": ev,
            "pass": b_pass,
        }

        # ================= C 段：强制定序对照 =================
        print("\n" + "-" * 70)
        print("C 段：强制定序对照（固定回复，绕开模型自由意志）")
        print("-" * 70)
        print("  理由：A/B 段若碰上模型沉默，'没发送'会被误读成机制生效。")
        print("        这里把流式输出钉死成一句话，单独验序列来自代码结构。")
        print(f"        固定回复: {C_FIXED_REPLY}")
        await _seed_history(session.db, datetime.now() - SEED_OFFSET)
        h = Harness(session, fixed_reply=C_FIXED_REPLY)
        await h.turn("刚吃完饭回宿舍了")
        ev = h.events
        print(_fmt_events(ev))
        c_d, c_on, c_off, c_send = _idx(ev, _is_sleep), _idx(ev, _is_typing_on), _idx(ev, _is_typing_off), _idx(ev, _is_send)
        c_pass = c_d == 0 and c_d < c_on < c_off < c_send
        print(f"  判定: {'PASS（序列由代码保证）' if c_pass else 'FAIL'}")

        report["C_forced_sequence"] = {
            "note": "固定流式输出 = 一句话，零模型自由意志；证明序列来自 turn_handler 的代码结构",
            "fixed_reply": C_FIXED_REPLY,
            "event_kinds": [e["type"] for e in ev],
            "first_delay_requested": ev[c_d]["requested"] if c_d >= 0 else None,
            "typing_seconds": ev[c_on + 1]["requested"] if c_on >= 0 and c_on + 1 < len(ev) else None,
            "sent": [e["text"] for e in ev if _is_send(e)],
            "events": ev,
            "pass": c_pass,
        }

        # ================= D 段：主动消息 typing =================
        print("\n" + "-" * 70)
        print("D 段：主动消息 typing（直调表演函数）")
        print("-" * 70)
        print("  理由：主动消息要过决策层（A/B/C 自由意志），不赌它选 A。")
        print("        这里直调 _play_typing_indicator，验 wiring + 时长计算。")
        h = Harness(session)
        ev = h.events
        with patch("asyncio.sleep", new=_SleepRecorder(ev)):
            await h.scheduler._play_typing_indicator(D_FIXED_TEXT)
        print(_fmt_events(ev))
        d_typing = [e for e in ev if e["type"] == "typing"]
        d_sleeps = [e["requested"] for e in ev if _is_sleep(e)]
        d_pass = (
            d_typing == [{"type": "typing", "typing": True}, {"type": "typing", "typing": False}]
            and d_sleeps == [3.0]
        )
        print(f"  typing 时长: {d_sleeps} 秒"
              f"（'{D_FIXED_TEXT}' {len(D_FIXED_TEXT)} 字 -> {len(D_FIXED_TEXT) / 10 * 2:.1f}s -> 钳到下限 3.0s）")
        print(f"  判定: {'PASS' if d_pass else 'FAIL'}")

        report["D_proactive_typing"] = {
            "note": "直调 proactive._play_typing_indicator；主动消息恒定 D=0，只演 typing",
            "text": D_FIXED_TEXT,
            "text_len": len(D_FIXED_TEXT),
            "typing_calls": d_typing,
            "sleeps": d_sleeps,
            "expected_sleep": 3.0,
            "events": ev,
            "pass": d_pass,
        }
    finally:
        await session.close()
        print("\n✓ 沙箱资源已清理")

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    passed = all(report[k].get("pass") for k in SEGMENTS)
    report["verdict"] = "PASS" if passed else "PARTIAL/FAIL"
    report["segments_passed"] = {k: bool(report[k].get("pass")) for k in SEGMENTS}
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"✓ 完整报告: {REPORT_FILE}")
    print(f"✓ 总判定: {report['verdict']}")
    for k in SEGMENTS:
        print(f"    {k}: {'PASS' if report[k].get('pass') else 'FAIL'}")
    return report


if __name__ == "__main__":
    asyncio.run(run_smoke_test())
