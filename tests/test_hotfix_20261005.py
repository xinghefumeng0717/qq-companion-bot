"""2026-10-05 生产热修（总部署当天实测暴露）：

1. **语音存档格式串台**：她把落库格式"（语音消息）内容"当发送语法写
   （历史里她读到的语音都长这样，有样学样），括号滤网把前缀当旁白剥掉，
   语音意图静默降级成文字（生产日志：4 次想发语音、3 次写错格式）。
   修复双管齐下：提示词明说"（语音消息）是存档格式不许自己写" +
   replier 兜底把行首（语音消息）翻译回 [voice:] 标记（语音可用）或剥前缀留正文。
2. **看板"她此刻"没接节假日信号**：admin 调 get_current_activity 不传
   holiday_span，长假期间看板显示的是在校作息（聊天主链路是对的，
   卡里的长假口径已经生效）。修复：看板与主链路共用 assembler.get_holidays 同一数据源。
3. **撤回事件没人接**：机主打错字后撤回重发（2026-10-04 18:01 生产日志），
   撤回通知（`friend_recall`）过去被静默丢弃，被撤回的那句照常留在聚合缓冲里
   送给了模型。修复：onebot 识别机主私聊撤回 → 聚合器按 message_id 摘出缓冲。
4. **超编引用被降级成乱码**：模型一轮输出两条 `[quote:N]`，`keep_first_quote`
   把第二条降级成字面文字 `"[quote:2]"` 原样发上了 QQ。修复：多余的引用段
   直接丢弃（标记不是内容，她要说的在后面的 text 段里）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

import companion.aggregator as aggmod
import companion.main as companion_main
import companion.onebot as obmod
from companion.admin import AdminServer
from companion.aggregator import MessageAggregator
from companion.config import OneBotConfig, ReplyConfig
from companion.onebot import OneBotClient
from companion.persona import Persona
from companion.prompts import VOICE_USAGE_RULES
from companion.replier import Replier, keep_first_quote, rewrite_voice_record_prefix
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


OWNER_QQ = 123456789  # 占位号（隐私：真号不入库）


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# 聚合窗口缩到亚秒级（真实 6.0 / 15.0 / 30.0），比例保持一致
_AGG_PATCH = {"SILENCE_WINDOW": 0.15, "HARD_LIMIT": 0.60, "TYPING_ABSOLUTE_LIMIT": 0.90}


def _recall_event(
    message_id: Any = 1234,
    user_id: int = OWNER_QQ,
    notice_type: str = "friend_recall",
    **over: Any,
) -> Dict[str, Any]:
    """按 OneBot v11 私聊撤回事件结构造报文（群撤回带 group_id/operator_id）。"""
    ev: Dict[str, Any] = {
        "time": 1759574400,
        "self_id": 10000,
        "post_type": "notice",
        "notice_type": notice_type,
        "user_id": user_id,
        "message_id": message_id,
    }
    ev.update(over)
    return ev


class TestRecallRemoval(unittest.IsolatedAsyncioTestCase):
    """撤回的消息从聚合缓冲里摘掉（2026-10-04 18:01 生产实锤修复）。"""

    async def asyncSetUp(self):
        self._real = {k: getattr(aggmod, k) for k in _AGG_PATCH}
        for k, v in _AGG_PATCH.items():
            setattr(aggmod, k, v)
        self.turns: List[Any] = []

        async def handler(text, image_path, batch=None):
            self.turns.append((text, image_path, batch))

        self.agg = MessageAggregator(turn_handler=handler)
        self.agg.start()

    async def asyncTearDown(self):
        self.agg.stop()
        for k, v in self._real.items():
            setattr(aggmod, k, v)

    async def test_删中间条后编号连续(self):
        await self.agg.push_message("第一句", None, 1001)
        await self.agg.push_message("打错的", None, 1002)
        await self.agg.push_message("第三句", None, 1003)

        self.agg.remove_message(1002)

        self.assertEqual([b["message_id"] for b in self.agg._batch], [1001, 1003])
        self.assertEqual([b["index"] for b in self.agg._batch], [1, 2], "编号必须从 1 连续")
        self.assertEqual(self.agg._text_buffer, ["第一句", "第三句"], "文本缓冲从 _batch 重建")

        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=2.0))
        text, _img, batch = self.turns[0]
        self.assertEqual(text, "第一句\n第三句")
        self.assertEqual([b["message_id"] for b in batch], [1001, 1003])
        self.assertEqual([b["index"] for b in batch], [1, 2])

    async def test_删带图条清掉图片(self):
        await self.agg.push_message("你看这个", None, 2001)
        await self.agg.push_message("", "data/x.jpg", 2002)
        self.assertEqual(self.agg._image_index, 2)

        self.agg.remove_message(2002)

        self.assertEqual([b["message_id"] for b in self.agg._batch], [2001])
        self.assertEqual([b["index"] for b in self.agg._batch], [1])
        self.assertIsNone(self.agg._image_buffer, "被撤的就是带图那条，图也要清")
        self.assertIsNone(self.agg._image_index)

        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=2.0))
        _text, image_path, batch = self.turns[0]
        self.assertIsNone(image_path)
        self.assertEqual([b["message_id"] for b in batch], [2001])

    async def test_删空缓冲撤销计时器(self):
        await self.agg.push_message("发出去又撤回了", None, 3001)
        self.assertIsNotNone(self.agg._debounce_task)
        self.agg._peer_typing = True  # 假装他此刻还在打字
        self.agg.remove_message(3001)

        self.assertEqual(self.agg._batch, [])
        self.assertEqual(self.agg._text_buffer, [])
        self.assertIsNone(self.agg._debounce_task, "计时器必须取消")
        self.assertEqual(self.agg._first_msg_time, 0.0)
        self.assertFalse(self.agg._peer_typing)

        # 静默窗早就该到了，但这一轮当没发生过：不许开口
        await asyncio.sleep(_AGG_PATCH["SILENCE_WINDOW"] * 2)
        self.assertEqual(self.turns, [], "全撤回的一轮不得触发回复")

    async def test_删不存在的id是空操作(self):
        await self.agg.push_message("在吗", None, 4001)

        self.agg.remove_message(9999)

        self.assertEqual([b["message_id"] for b in self.agg._batch], [4001])
        self.assertEqual([b["index"] for b in self.agg._batch], [1])
        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=2.0))
        self.assertEqual(self.turns[0][0], "在吗")

    async def test_空message_id不误删(self):
        """没带 id 的消息（历史/异常）不许被 message_id=None 的撤回事件误伤。"""
        await self.agg.push_message("无 id 的一句", None, None)
        self.agg.remove_message(None)
        self.assertEqual(len(self.agg._batch), 1)


class TestRecallEventDispatch(unittest.IsolatedAsyncioTestCase):
    """协议层：只认机主本人私聊撤回，其余一律忽略。"""

    async def asyncSetUp(self):
        self.seen: List[Any] = []
        self._tmp = tempfile.mkdtemp(prefix="hotfix_recall_")
        self.client = OneBotClient(
            config=OneBotConfig(),
            allowed_user_id=OWNER_QQ,
            image_save_dir=self._tmp,
            on_recall_callback=self.seen.append,
        )
        self.client._running = True

    async def asyncTearDown(self):
        await self.client.stop()
        try:
            os.rmdir(self._tmp)
        except OSError:
            pass

    async def feed(self, payload: Dict[str, Any]) -> None:
        await self.client._handle_raw_message(json.dumps(payload))

    async def test_私聊撤回触发回调(self):
        await self.feed(_recall_event(1234))
        self.assertEqual(self.seen, [1234])

    async def test_别人的撤回忽略(self):
        await self.feed(_recall_event(1234, user_id=999999999))
        self.assertEqual(self.seen, [], "别人的撤回跟我们无关")

    async def test_群撤回忽略(self):
        await self.feed(
            _recall_event(1234, notice_type="group_recall", group_id=555, operator_id=OWNER_QQ)
        )
        self.assertEqual(self.seen, [], "群撤回不进私聊链路")

    async def test_没有message_id忽略(self):
        await self.feed(_recall_event(None))
        self.assertEqual(self.seen, [])

    async def test_stop后不处理在途撤回帧(self):
        await self.client.stop()
        await self.feed(_recall_event(1234))
        self.assertEqual(self.seen, [], "stop() 之后必须丢弃在途帧")

    async def test_回调抛异常不影响消息链(self):
        got: List[Any] = []

        async def on_message(text, image_path, message_id=None):
            got.append((text, message_id))

        self.client.on_recall_callback = lambda mid: (_ for _ in ()).throw(
            RuntimeError("聚合器炸了")
        )
        self.client.on_message_callback = on_message

        await self.feed(_recall_event(1234))
        await self.feed(
            {
                "post_type": "message",
                "message_type": "private",
                "user_id": OWNER_QQ,
                "message": "在吗",
                "message_id": 777,
            }
        )
        self.assertTrue(
            await _wait_for(lambda: len(got) == 1, timeout=2.0),
            "撤回回调抛异常不得影响消息事件处理",
        )
        self.assertEqual(got[0], ("在吗", 777))

    def test_撤回常量就是onebot协议字面值(self):
        self.assertEqual(obmod.RECALL_NOTICE_TYPE, "friend_recall")


class TestQuoteOverflowDropped(unittest.TestCase):
    """超编引用段直接丢弃，不得降级成字面 `[quote:N]` 发上屏。"""

    def test_两条引用只留第一条(self):
        out = keep_first_quote([
            {"type": "quote", "index": 1, "message_id": 1001},
            {"type": "text", "content": "甲"},
            {"type": "quote", "index": 2, "message_id": 1002},
            {"type": "text", "content": "乙"},
        ])
        quotes = [c for c in out if c["type"] == "quote"]
        self.assertEqual(len(quotes), 1)
        self.assertEqual(quotes[0]["index"], 1)
        self.assertEqual([c["content"] for c in out if c["type"] == "text"], ["甲", "乙"])

    def test_多余的不产生任何text段(self):
        out = keep_first_quote([
            {"type": "quote", "index": 1, "message_id": 1001},
            {"type": "text", "content": "甲"},
            {"type": "quote", "index": 2, "message_id": 1002},
        ])
        for c in out:
            self.assertNotIn("[quote:2]", c.get("content", ""))

    def test_整轮解析后记录里没有残余标记(self):
        chunks, record = _replier().parse_reply(
            "[quote:1] 甲\n[quote:2] 乙", quote_targets=[
                {"index": 1, "text": "甲", "message_id": 1001, "has_image": False},
                {"index": 2, "text": "乙", "message_id": 1002, "has_image": False},
            ]
        )
        self.assertEqual(len([c for c in chunks if c.get("_quote")]), 1)
        self.assertNotIn("[quote:2]", record)
        self.assertIn("乙", record)


if __name__ == "__main__":
    unittest.main()
