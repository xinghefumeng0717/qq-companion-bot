"""2026-10-05 生产热修（总部署当天实测暴露）：

1. **语音存档格式串台**：她把落库格式"（语音消息）内容"当发送语法写
   （历史里她读到的语音都长这样，有样学样），括号滤网把前缀当旁白剥掉，
   语音意图静默降级成文字（生产日志：4 次想发语音、3 次写错格式）。
   修复双管齐下：提示词明说"（语音消息）是存档格式不许自己写" +
   replier 兜底把行首（语音消息）翻译回 [voice:] 标记（语音可用）或剥前缀留正文。
2. **看板"她此刻"没接节假日信号**：admin 调 get_current_activity 不传
   holiday_span，长假期间看板显示的是在校作息（聊天主链路是对的，
   卡里的长假口径已经生效）。修复：看板与主链路共用 assembler.get_holidays 同一数据源。
"""

from __future__ import annotations

import os
import sys
import unittest
from datetime import datetime, timedelta

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

import companion.main as companion_main
from companion.admin import AdminServer
from companion.config import ReplyConfig
from companion.persona import Persona
from companion.prompts import VOICE_USAGE_RULES
from companion.replier import Replier, rewrite_voice_record_prefix
from helpers import card_path


class _StubStickers:
    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫"]


def _replier() -> Replier:
    return Replier(ReplyConfig(), _StubStickers())


class TestVoiceRecordPrefix(unittest.TestCase):
    """她误写存档格式（语音消息）时的兜底（replier 第 2.5 步）。"""

    def test_语音可用时翻译回voice标记(self):
        chunks, record = _replier().parse_reply(
            "（语音消息）刚顺完一段慢板，琴弓还搁在边上呢", voice_allowed=True
        )
        types = [c.get("type") for c in chunks]
        self.assertIn("voice", types)
        voice = next(c for c in chunks if c.get("type") == "voice")
        self.assertEqual(voice["content"], "刚顺完一段慢板，琴弓还搁在边上呢")
        self.assertNotIn("（语音消息）", "".join(
            c.get("content", "") for c in chunks if c.get("type") == "text"
        ))

    def test_语音不可用时剥前缀留正文(self):
        chunks, record = _replier().parse_reply(
            "（语音消息）那送你半句，快去忙", voice_allowed=False
        )
        self.assertTrue(all(c.get("type") != "voice" for c in chunks))
        self.assertIn("那送你半句，快去忙", record)
        self.assertNotIn("语音消息", record)

    def test_前缀不被旁白滤网吃掉(self):
        """核心回归：语音可用时，（语音消息）不得落入括号旁白滤网。"""
        chunks, _ = _replier().parse_reply("（语音消息）晚安，明天聊", voice_allowed=True)
        self.assertIn("voice", [c.get("type") for c in chunks])

    def test_行中的存档字样不误判(self):
        """宁漏勿错：只有行首才算翻译回语音；行中的（语音消息）交给既有括号滤网
        （它会剥掉括号内容——那是老行为，不归本兜底管），绝不产生 voice 段。"""
        chunks, record = _replier().parse_reply(
            "你那条（语音消息）我听了三遍", voice_allowed=True
        )
        self.assertNotIn("voice", [c.get("type") for c in chunks])
        self.assertNotIn("[voice", record)

    def test_空内容行剥掉(self):
        chunks, record = _replier().parse_reply("（语音消息）", voice_allowed=True)
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_提示词明说存档格式不许自己写(self):
        self.assertIn("存档格式", VOICE_USAGE_RULES)
        self.assertIn("[voice:]", VOICE_USAGE_RULES)


class _StubAssembler:
    def __init__(self, holidays):
        self._holidays = holidays

    def get_holidays(self):
        return self._holidays


class TestAdminActivityHoliday(unittest.TestCase):
    """看板"她此刻"必须接节假日信号（与聊天主链路同一数据源）。"""

    def _dashboard(self, holidays):
        dash = AdminServer.__new__(AdminServer)
        dash.persona = Persona.load(card_path())
        dash.assembler = _StubAssembler(holidays)
        return dash

    def test_长假显示卡里的长假文案而不是在校作息(self):
        today = datetime.now()
        holidays = [
            (today + timedelta(days=d)).strftime("%Y-%m-%d") for d in range(-2, 3)
        ]
        dash = self._dashboard(holidays)
        activity = dash._current_activity(today)
        self.assertEqual(activity, dash.persona.long_holiday_activity)

    def test_非节假日照旧走作息表(self):
        dash = self._dashboard([])
        activity = dash._current_activity(datetime.now())
        self.assertNotEqual(activity, dash.persona.long_holiday_activity)
        self.assertTrue(activity)

    def test_与主链路同一数据源(self):
        """看板必须经由 assembler.get_holidays 取节假日，不许另起炉灶。"""
        import inspect

        source = inspect.getsource(AdminServer._current_activity)
        self.assertIn("assembler.get_holidays", source)


class TestSingleCancelWave(unittest.TestCase):
    """停机只能有一个取消波（2026-10-05 两次 30s SIGKILL 的根因）。

    旧结构：信号处理器另起 stop_gracefully 任务 → close() 的取消波干掉主任务
    → main() finally 的第二波取消把正在干活的 close() 打死 → db.close() 跑不到
    → aiosqlite 非守护线程挂住进程。新结构：信号只取消主任务，close() 由
    run() 的 finally 单路执行；main() finally 的清扫必须有界。
    """

    def test_信号处理器只取消主任务(self):
        import inspect

        source = inspect.getsource(companion_main.main)
        self.assertNotIn(
            "create_task(bot.stop_gracefully())",
            source,
            "信号路径不许另起 stop_gracefully 任务（那会制造第二个取消波）",
        )
        self.assertIn("main_task.cancel()", source)

    def test_main收尾清扫有界(self):
        import inspect

        source = inspect.getsource(companion_main.main)
        self.assertNotIn(
            "gather(*pending",
            source,
            "main() 收尾不许无界 gather（无界等待 = 停机卡死同族病根）",
        )
        self.assertIn("asyncio.wait(pending, timeout=SHUTDOWN_SWEEP_TIMEOUT)", source)


if __name__ == "__main__":
    unittest.main()
