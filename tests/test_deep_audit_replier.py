"""DEEP_AUDIT 面 B-4/A-8（行内标记劈行绕过整行滤网）与 B-6（纯语音轮 typing 兜底）
的回归测试集 (tests/test_deep_audit_replier.py)

B-4 / A-8：两道整行滤网（图片占位符 / 内心旁白）的判据是"整行"，可第 4 步的
标记拆分（[face:] / [sticker:]）会把一行劈成多个文字段。逐段判定时：
  - 尾段丢掉"含'你'就放行"的保险丝 → 误杀她的正常句（"你怎么知道的[face:吃瓜]
    我看他朋友圈了" 后半句蒸发）；
  - 整句旁白被劈开后每半各缺一个条件 → 整句旁白照发
    （"这人嘴硬[face:流泪]我折回去看看。"）。
修法：按段上的行号把同一原始行的文字段拼回整行再判（见 classify_dropped_lines），
命中即这一行的**文字**整行消失；行内的 face/sticker 不是旁白也不是占位符，
原样保留（"只剩脸"，与句尾挂脸同一形态）。

B-6：`typing_text_from_chunks(chunks) or 记录文本` 的裸 or 兜底在"本轮只有语音段"
时会把语音正文当打字（实测 10.0 秒）。修法见 typing_text_from_chunks_or_record：
只有"既没有 text/combo 段、也没有 voice 段"时才回退记录文本。

全部本地构造，零真实 API 调用。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

from companion.config import ProactiveConfig, ReplyConfig, TimingConfig
from companion.replier import (
    Replier,
    classify_dropped_lines,
    drop_silence_marker_lines,
    keep_face_cap,
    normalize_face_markers,
    typing_text_from_chunks_or_record,
)
from helpers import close_db, make_db, make_engine_stack

# 打补丁前存下真的 asyncio.sleep：假 sleep 里要用它让出事件循环
_REAL_SLEEP = asyncio.sleep


class _StubStickers:
    """假表情包管理器：按描述词直接回一个假路径（不落盘）"""

    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫", "狗头"]


def _replier() -> Replier:
    return Replier(ReplyConfig(), _StubStickers())


def _texts(chunks: List[Dict[str, Any]]) -> str:
    """把实发段里的文字拼起来（combo 里的文字也算），用于"漏没漏出去"断言。"""
    out: List[str] = []
    for c in chunks:
        if c["type"] == "text":
            out.append(c.get("content", ""))
        elif c["type"] == "combo":
            out.append(
                "".join(p.get("content", "") for p in c.get("parts", [])
                        if p.get("type") == "text")
            )
    return "".join(out)


# ==========================================
# 1. B-4 / A-8：行内标记不再劈开整行判定
# ==========================================

# A-8 沙箱里的三条实锤句（真实自然会话，不是生造）
MISKILL_CASE = "你怎么知道的[face:吃瓜]我看他朋友圈了"     # 有「你」，必须整行放行
NARRATION_CASE = "这人嘴硬[face:流泪]我折回去看看。"       # 整句旁白，必须拦下
SAFE_TAIL_CASE = "这人嘴硬，我折回去看看。[face:流泪]"     # 句尾挂脸：文字拦下、只剩脸


class TestAuditB4InlineMarkerLines(unittest.TestCase):
    def test_行中插脸不再误杀后半句(self):
        """A-8 实锤①：整行含「你」= 在对他说话，后半句一个字都不许少。"""
        chunks, record = _replier().parse_reply(MISKILL_CASE)
        self.assertIn("我看他朋友圈了", _texts(chunks), "后半句被误杀了")
        self.assertIn("你怎么知道的", _texts(chunks))
        self.assertIn("我看他朋友圈了", record)
        self.assertIn("[face:吃瓜]", record, "脸也要照发")
        self.assertEqual(len(chunks), 2, f"应是 combo + 后半句两条，实际：{chunks}")
        self.assertEqual([c["type"] for c in chunks], ["combo", "text"])

    def test_行中插表情包不再误杀后半句(self):
        """同一机制在 sticker 上早已存在（A-8 实锤③的变体），一并钉死。"""
        chunks, record = _replier().parse_reply("你等着[sticker:猫猫]我看他怎么收场")
        self.assertIn("我看他怎么收场", _texts(chunks), "表情包劈行不该吞掉后半句")
        self.assertEqual(
            [c["type"] for c in chunks], ["text", "sticker", "text"]
        )
        self.assertIn("[表情:猫猫]", record)
        self.assertIn("我看他怎么收场", record)

    def test_整句旁白插脸整行文字被拦只剩脸(self):
        """A-8 实锤②：行内插脸不再让整句旁白漏出去；文字全丢、脸不是旁白所以留下。"""
        chunks, record = _replier().parse_reply(NARRATION_CASE)
        self.assertNotIn("这人嘴硬", _texts(chunks), "旁白照发了")
        self.assertNotIn("折回去", _texts(chunks))
        self.assertNotIn("这人嘴硬", record)
        self.assertNotIn("折回去", record)
        self.assertEqual([c["type"] for c in chunks], ["face"])
        self.assertEqual(record, "[face:流泪]")

    def test_句尾挂脸仍然安全只剩脸(self):
        """A-8 的对照形态：句尾挂脸一直是安全的，改动后不许把它弄坏。"""
        chunks, record = _replier().parse_reply(SAFE_TAIL_CASE)
        self.assertNotIn("折回去", record)
        self.assertEqual([c["type"] for c in chunks], ["face"])
        self.assertEqual(record, "[face:流泪]")

    def test_去掉标记后整行旁白照旧被拦(self):
        """不带标记的病句一个字都不放松（对照 A-8 的"不加脸 → 记录为空"）。"""
        chunks, record = _replier().parse_reply("这人嘴硬，我折回去看看。")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_跨行形态不受影响(self):
        """脸在上一行、旁白在下一行：既有行为（test_fixes20）不许被改动牵连。"""
        chunks, record = _replier().parse_reply(
            "在呢[face:贴贴]\n这人嘴硬，我折回去看看。"
        )
        self.assertEqual([c["type"] for c in chunks], ["combo"])
        self.assertEqual(record, "在呢[face:贴贴]")

    def test_行内夹杂占位符照旧不删(self):
        """两道滤网共用一次拼回判定，图片滤网的"行内夹杂不删"契约不许被挤坏。"""
        chunks, record = _replier().parse_reply("今天[图片]里那只猫[face:doge]")
        self.assertIn("[图片]", record)
        self.assertIn("[face:doge]", record)

    def test_占位符行内挂脸只丢占位符文字(self):
        """[图片] 单独占一行时照丢；同一行的脸不是占位符，留下。"""
        chunks, record = _replier().parse_reply("[图片][face:doge]")
        self.assertNotIn("[图片]", record)
        self.assertEqual([c["type"] for c in chunks], ["face"])

    def test_classify把同行文字段拼回再判(self):
        """段级单测：拼回后的整行含「你」→ 不丢；整句旁白 → 丢掉这一行。"""
        miskill = [
            {"type": "text", "content": "你怎么知道的", "_end_line": 0},
            {"type": "face", "tag": "吃瓜", "id": 6, "_line": 0, "_end_line": 0},
            {"type": "text", "content": "我看他朋友圈了", "_end_line": 0},
        ]
        self.assertEqual(classify_dropped_lines(miskill), set())

        narration = [
            {"type": "text", "content": "这人嘴硬", "_end_line": 0},
            {"type": "face", "tag": "流泪", "id": 5, "_line": 0, "_end_line": 0},
            {"type": "text", "content": "我折回去看看。", "_end_line": 0},
        ]
        self.assertEqual(classify_dropped_lines(narration), {0})

    def test_多行文本只有命中行被拿掉(self):
        """跨行文字段：命中的一行消失，其余行逐字保留（不整段连坐）。"""
        raw = "在呢\n这人嘴硬，我折回去看看。\n你吃了没"
        chunks, record = _replier().parse_reply(raw)
        self.assertEqual(record, "在呢\n你吃了没")
        self.assertNotIn("折回去", _texts(chunks))

    def test_拼回判定记INFO日志(self):
        """丢弃仍要留痕（沿用两道滤网既有的日志口径，见 test_fixes19/12）。"""
        with self.assertLogs("companion.replier", level="INFO") as cm:
            _replier().parse_reply(NARRATION_CASE, source="reply")
        joined = "\n".join(cm.output)
        self.assertIn("内心旁白兜底", joined)
        self.assertIn("折回去", joined)


# ==========================================
# 2. B-6：纯语音轮的 typing 兜底
# ==========================================

VOICE_CHUNKS = [
    {"type": "voice", "content": "刚练完 手指都快断了，今天合练加练到九点半，你也早点睡"}
]
VOICE_RECORD = "（语音消息）刚练完 手指都快断了，今天合练加练到九点半，你也早点睡"


class TestAuditB6PureVoiceTyping(unittest.TestCase):
    def test_纯语音轮不拿记录文本兜底(self):
        """B-6 的原病：裸 or 兜底把语音正文当打字（实测 10.0 秒）。"""
        self.assertEqual(
            typing_text_from_chunks_or_record(VOICE_CHUNKS, VOICE_RECORD), ""
        )

    def test_有文字段时取文字段(self):
        chunks = [
            {"type": "text", "content": "在呢"},
            {"type": "voice", "content": "刚练完"},
        ]
        self.assertEqual(
            typing_text_from_chunks_or_record(chunks, "在呢\n（语音消息）刚练完"),
            "在呢",
        )

    def test_只有表情包段时才回退记录文本(self):
        """旧口径里真正需要兜底的场景（整轮只有图/脸）不许一起丢掉。"""
        chunks = [{"type": "sticker", "file": "/x.png", "desc": "猫猫"}]
        self.assertEqual(
            typing_text_from_chunks_or_record(chunks, "[表情:猫猫]"), "[表情:猫猫]"
        )
        self.assertEqual(
            typing_text_from_chunks_or_record(
                [{"type": "face", "tag": "晕", "id": 34}], "[face:晕]"
            ),
            "[face:晕]",
        )

    def test_段为空时才走记录文本(self):
        """完全空段（老调用方的默认路径）兜底语义一字不动。"""
        self.assertEqual(typing_text_from_chunks_or_record([], "在呢"), "在呢")


class _FakeTTS:
    """语音闸门恒开 + 上限 60（只借 config.max_chars 这一处）。"""

    class config:  # noqa: N801 - 对齐 TTSManager.config 的鸭子类型
        max_chars = 60

    async def check_gate(self, activity: str):
        return True, "测试桩：闸门恒开"


class _TypingEvents:
    """替换 asyncio.sleep：记下每次请求的秒数后立即返回。

    真 sleep(0) 用**打补丁前**存下来的那个函数，否则会递归调用自己。
    """

    def __init__(self, events: List[tuple]):
        self.events = events

    async def __call__(self, delay, *args, **kwargs):
        self.events.append(("sleep", float(delay)))
        await _REAL_SLEEP(0)


def _typing_sleep(events: List[tuple]) -> float:
    """取 typing 开→关之间的那一段 sleep（就是 T_typing 本身）。"""
    idx = next(i for i, e in enumerate(events) if e == ("typing", True))
    return next(d for k, d in events[idx + 1:] if k == "sleep")


class TestAuditB6TypingCallSites(unittest.TestCase):
    """两个调用点（turn_handler / proactive）都必须按 B-6 的口径走。"""

    def test_主聊纯语音轮打字时长落在最短档(self):
        from test_fixes15 import _handler, _Recorder

        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                timing = TimingConfig()
                handler, _ = _handler(
                    db,
                    pieces=["[voice:刚练完 手指都快断了，今天合练加练到九点半，你也早点睡[/voice]"],
                    timing=timing,
                    events=events,
                )
                handler.tts = _FakeTTS()
                with patch("asyncio.sleep", new=_Recorder(events)):
                    await handler.handle_turn("在吗", None)
                return events, timing
            finally:
                await close_db(db)

        events, timing = asyncio.run(_run())
        duration = _typing_sleep(events)
        # 语音正文 26 字 → 旧口径 5.2 秒；正确口径 = 没有可打的字 = typing_min
        self.assertAlmostEqual(duration, timing.typing_min, places=6)
        self.assertNotIn("（语音消息）", [c for k, c in events if k == "send"])
        self.assertIn(("typing", True), events)

    def test_主动消息纯语音轮打字时长落在最短档(self):
        from companion.proactive import ProactiveScheduler

        async def _run():
            db = await make_db()
            try:
                events: List[tuple] = []
                timing = TimingConfig()
                stack = make_engine_stack(
                    db, gateway=None, reply_config=ReplyConfig(5, 0.0, 0.0)
                )
                gateway = MagicMock()
                gateway.config.text_model = "deepseek-chat"
                gateway.config.observer_model = "deepseek-chat"
                gateway.chat = AsyncMock(
                    return_value=(
                        "[voice:刚练完 手指都快断了，今天合练加练到九点半，你也早点睡[/voice]"
                    )
                )

                async def _send(chunk):
                    events.append(("send", chunk.get("type", "")))

                async def _set_typing(flag: bool) -> bool:
                    events.append(("typing", bool(flag)))
                    return True

                sched = ProactiveScheduler(
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
                    set_typing_fn=_set_typing,
                    timing_config=timing,
                )
                sched.tts = _FakeTTS()
                with patch("asyncio.sleep", new=_TypingEvents(events)):
                    ok = await sched._generate_and_send("期末读书报告", "2026-10-05 22:00")
                return events, timing, ok
            finally:
                await close_db(db)

        events, timing, ok = asyncio.run(_run())
        self.assertTrue(ok, "主动消息应当发出去")
        self.assertAlmostEqual(_typing_sleep(events), timing.typing_min, places=6)
        self.assertEqual(
            [c for k, c in events if k == "send"], ["voice"], "实发的应当是语音段"
        )


# ==========================================
# 3. B-5：清单外 face 标签不再以字面量上屏
# ==========================================

MOON_CASE = "晚安啦\n[face:月亮]"


class TestAuditB5FaceLiteralDowngrade(unittest.TestCase):
    def test_清单外标签只发正文不发字面量(self):
        """B-5 真实案例（仿真 S2 第 9 轮）：`月亮` 能发但不在 26 项清单里。"""
        chunks, record = _replier().parse_reply(MOON_CASE)
        self.assertEqual(record, "晚安啦")
        self.assertEqual([c["content"] for c in chunks], ["晚安啦"])
        self.assertNotIn("[face:", record)
        self.assertNotIn("月亮", record)

    def test_句尾挂清单外标签同样只发正文(self):
        chunks, record = _replier().parse_reply("晚点聊[face:玫瑰]")
        self.assertEqual(record, "晚点聊")
        self.assertNotIn("玫瑰", record)

    def test_合法标记一个字节都不动(self):
        chunks, record = _replier().parse_reply("你真棒[face:doge]")
        self.assertEqual([c["type"] for c in chunks], ["combo"])
        self.assertEqual(record, "你真棒[face:doge]")

    def test_超限额的脸整段丢弃且不造空气泡(self):
        chunks, record = _replier().parse_reply(
            "你[face:doge][face:流泪][face:晕]"
        )
        faces = [
            p
            for c in chunks
            for p in ([c] if c["type"] == "face" else c.get("parts", []))
            if p["type"] == "face"
        ]
        self.assertEqual(len(faces), 2, "整轮上限 2 个")
        self.assertNotIn("[face:晕]", record)
        self.assertTrue(all(c.get("content", "x").strip() for c in chunks if c["type"] == "text"),
                        "不许造出空文字段（空气泡）")

    def test_降级记INFO日志(self):
        with self.assertLogs("companion.replier", level="INFO") as cm:
            _replier().parse_reply("晚安啦\n[face:月亮]")
        self.assertIn("已剥掉标记只发正文", "\n".join(cm.output))

    def test_normalize函数级行为(self):
        self.assertEqual(normalize_face_markers("晚安啦[face:月亮]"), "晚安啦")
        self.assertEqual(normalize_face_markers("你[face:doge]好"), "你[face:doge]好")
        self.assertEqual(normalize_face_markers("真棒[face:微笑]"), "真棒")


# ==========================================
# 4. B-7：单独占行的 [沉默] 不再上屏
# ==========================================


class TestAuditB7SilenceMarkerLine(unittest.TestCase):
    def test_单独占行的沉默标记被剥掉其余照发(self):
        chunks, record = _replier().parse_reply("嗯\n[沉默]")
        self.assertEqual([c["content"] for c in chunks], ["嗯"])
        self.assertEqual(record, "嗯")
        self.assertNotIn("[沉默]", record)

    def test_语音与沉默同现(self):
        """B-7 的真实形态：发条语音晚安 + 沉默收尾。"""
        chunks, record = _replier().parse_reply(
            "[voice:晚安 明天聊[/voice]\n[沉默]", voice_allowed=True
        )
        self.assertEqual([c["type"] for c in chunks], ["voice"])
        self.assertEqual(record, "（语音消息）晚安 明天聊")
        self.assertNotIn("[沉默]", record)

    def test_整条恰为沉默时沉默权一字不动(self):
        for raw in ("[沉默]", "  \n[沉默] \n", "[quote:2]\n[沉默]"):
            with self.subTest(raw=raw):
                chunks, record = _replier().parse_reply(raw)
                self.assertEqual(chunks, [])
                self.assertEqual(record, "")

    def test_行内夹杂的沉默照旧不动(self):
        """FIXES13 防滥用条款：串在文字里的 [沉默] 一个字节都不动。"""
        chunks, record = _replier().parse_reply("[沉默] 哈哈")
        self.assertEqual(record, "[沉默] 哈哈")
        self.assertTrue(chunks)

    def test_辅助函数只剥单独占行的标记(self):
        self.assertEqual(drop_silence_marker_lines("嗯\n[沉默]"), "嗯")
        self.assertEqual(drop_silence_marker_lines("[沉默]"), "[沉默]")
        self.assertEqual(drop_silence_marker_lines("[沉默] 哈哈"), "[沉默] 哈哈")
        self.assertEqual(drop_silence_marker_lines("在呢"), "在呢")


if __name__ == "__main__":
    unittest.main()
