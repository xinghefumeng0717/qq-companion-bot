"""FIXES23 他打字她等——输入状态联动的聚合窗延长 测试集 (tests/test_fixes23.py)

聚合器原本只看"静默 6 秒"：他打字慢、句间停顿超过 6 秒，她就抢话把一段话劈成两截。
NapCat 会上报对方输入状态，这个信号让聚合窗"看见他在打字"。本测试集盯住：

1. **事件解析**：`notice/input_status` 认得出来、取值宽容、陌生取值一律忽略
   （任务1）。真事件样本的字段名与默认值取自 NapCat 源码
   `packages/napcat-onebot/event/notice/OB11InputStatusEvent.ts`（2026-10-04 核对）
2. **聚合窗联动**：打字时静默窗被撤、停手后重新计时、30 秒绝对上限强制 flush、
   空缓冲忽略、乱序/重复不炸（任务2）
3. **状态泄漏自愈**：开始输入后 15 秒没等到结束事件，按停手处理（任务1）
4. **纯增强**：收不到输入状态事件时全路径与改动前一致（所有者拍板决策 1）

**反向对照纪律**：本文件里凡"断言没 flush"的用例，都配一条"同样输入但不发输入状态
事件就会 flush"的对照用例。否则"没触发"和"代码根本没跑起来"长得一模一样，
是一次自动成立的空断言。

全部本地构造事件，零真实 API 调用。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import companion.aggregator as aggmod
import companion.onebot as obmod
from companion.aggregator import MessageAggregator
from companion.config import OneBotConfig
from companion.onebot import OneBotClient, parse_input_status_event

OWNER_QQ = 123456789


def _typing_event(event_type: Any, user_id: int = OWNER_QQ, **over) -> Dict[str, Any]:
    """按 NapCat 真事件结构造一条 notice 报文（私聊：group_id 恒为 0）。"""
    ev = {
        "time": 1759574400,
        "self_id": 10000,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "input_status",
        "status_text": "对方正在输入...",
        "event_type": event_type,
        "user_id": user_id,
        "group_id": 0,
    }
    ev.update(over)
    return ev


async def _wait_for(predicate, timeout: float = 3.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


# 打桩窗口：真实值 6.0 / 15.0 / 30.0 秒，测试里全部缩到亚秒级。
# 比例保持一致（静默 < 硬上限 < 打字绝对上限），这样"延长有没有真的生效"可判。
_PATCH = {"SILENCE_WINDOW": 0.15, "HARD_LIMIT": 0.60, "TYPING_ABSOLUTE_LIMIT": 0.90}


class _TypingWindowCase(unittest.IsolatedAsyncioTestCase):
    """聚合窗联动用例的公共脚手架：真实窗口值留档，测试期间缩到亚秒级。"""

    async def asyncSetUp(self):
        self._real = {k: getattr(aggmod, k) for k in _PATCH}
        for k, v in _PATCH.items():
            setattr(aggmod, k, v)

        self.turns: List[Any] = []
        self.t0 = 0.0

        async def handler(text, image_path, batch=None):
            self.turns.append((text, image_path, batch))

        self.agg = MessageAggregator(turn_handler=handler)
        self.agg.start()
        self.t0 = asyncio.get_running_loop().time()

    async def asyncTearDown(self):
        self.agg.stop()
        for k, v in self._real.items():
            setattr(aggmod, k, v)

    def elapsed(self) -> float:
        return asyncio.get_running_loop().time() - self.t0

    async def say(self, text: str, message_id: int | None = None) -> None:
        await self.agg.push_message(text, None, message_id)


# ---------------------------------------------------------------------------
# 1. 事件解析层
# ---------------------------------------------------------------------------
class TestParseInputStatusEvent(unittest.TestCase):
    def test_真事件_开始输入(self):
        self.assertIs(parse_input_status_event(_typing_event(1)), True)

    def test_真事件_停手(self):
        self.assertIs(parse_input_status_event(_typing_event(0)), False)

    def test_取值宽容_字符串与布尔(self):
        """不同实现可能把状态码发成字符串或 bool，都得认。"""
        self.assertIs(parse_input_status_event(_typing_event("1")), True)
        self.assertIs(parse_input_status_event(_typing_event("0")), False)
        self.assertIs(parse_input_status_event(_typing_event(True)), True)
        self.assertIs(parse_input_status_event(_typing_event(False)), False)

    def test_陌生取值一律忽略(self):
        """其他状态码不猜：宁可当作没这回事，也不能把未知值当成"他在打字"。"""
        for bad in (2, -1, 99, "typing", "abc", "", None, [], {}):
            with self.subTest(bad=bad):
                self.assertIsNone(parse_input_status_event(_typing_event(bad)))

    def test_缺字段忽略(self):
        ev = _typing_event(1)
        del ev["event_type"]
        self.assertIsNone(parse_input_status_event(ev))

    def test_非输入状态事件忽略(self):
        """别的 notice（戳一戳/点赞/群名片）与消息事件都不得被误认。"""
        cases = [
            _typing_event(1, notice_type="group_recall", sub_type="input_status"),
            _typing_event(1, notice_type="notify", sub_type="poke"),
            _typing_event(1, notice_type="notify", sub_type="profile_like"),
            _typing_event(1, post_type="message", notice_type=None, sub_type=None),
            _typing_event(1, notice_type="input_status", sub_type="input_status"),
        ]
        for ev in cases:
            with self.subTest(ev=ev.get("sub_type")):
                self.assertIsNone(parse_input_status_event(ev))

    def test_非字典输入不炸(self):
        self.assertIsNone(parse_input_status_event(None))
        self.assertIsNone(parse_input_status_event("input_status"))


# ---------------------------------------------------------------------------
# 2. 聚合窗联动
# ---------------------------------------------------------------------------
class TestTypingExtendsWindow(_TypingWindowCase):
    async def test_反向对照_没有输入状态时静默窗照常到期(self):
        """对照组：同样两条消息，**不发**输入状态事件，窗口期内必须 flush。

        没有这条，上面"打字时不 flush"就可能只是因为代码压根没跑起来。
        """
        await self.say("今天做完实验了", 1001)
        await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 0.4)
        await self.say("晚上吃啥", 1002)

        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=2.0),
            "无输入状态事件时必须在静默窗到期后 flush（对照组没生效）",
        )
        self.assertLess(
            self.elapsed(),
            _PATCH["HARD_LIMIT"],
            "对照组应在静默窗附近 flush，而不是拖到硬上限",
        )

    async def test_他在打字_静默窗到期不插话(self):
        await self.say("今天做完实验了", 1001)
        self.agg.notify_peer_typing(True)

        # 等过 3 倍静默窗：机制若没生效，此时早该 flush 了
        await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 3)
        self.assertEqual(len(self.turns), 0, "他在打字就不该插话（静默窗没被撤）")

    async def test_他停手_重新起满静默窗才回(self):
        """打字 → 停手 → 静默窗重新计时 → 才 flush，且三条消息合成一轮。"""
        await self.say("今天做完实验了", 1001)
        self.agg.notify_peer_typing(True)
        await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 3)
        self.assertEqual(len(self.turns), 0)

        self.agg.notify_peer_typing(False)
        self.assertEqual(len(self.turns), 0, "刚停手就该立刻回（静默窗没重新计时）")

        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=2.0),
            "他停手后静默窗到期必须 flush",
        )
        text, _img, batch = self.turns[0]
        self.assertEqual(text, "今天做完实验了")
        self.assertEqual([b["index"] for b in batch], [1])
        self.assertGreaterEqual(
            self.elapsed(),
            _PATCH["SILENCE_WINDOW"] * 3,
            "flush 时刻必须晚于被撤掉的静默窗（否则等于没等）",
        )

    async def test_打字中连发消息也不插话(self):
        """他一边打字一边连发：每条新消息都该续成"等停手"，而不是重开静默窗。"""
        await self.say("我跟你说个事", 1001)
        self.agg.notify_peer_typing(True)
        await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 1.5)
        await self.say("今天那个会", 1002)
        await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 1.5)
        self.assertEqual(len(self.turns), 0, "打字中连发不该把她逼出来插话")

        self.agg.notify_peer_typing(False)
        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=2.0))
        text, _img, batch = self.turns[0]
        self.assertEqual(text, "我跟你说个事\n今天那个会")
        self.assertEqual([b["index"] for b in batch], [1, 2])
        self.assertEqual([b["message_id"] for b in batch], [1001, 1002])


class TestTypingAbsoluteLimit(_TypingWindowCase):
    async def test_持续打字到绝对上限强制flush(self):
        """他一直打字不放：到 30 秒（打桩 0.9s）必须强制 flush，谁还在打字都不好使。"""
        await self.say("我跟你说个事，特别长那种", 1001)
        self.agg.notify_peer_typing(True)

        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=3.0),
            "打字不能无限续命，绝对上限到点必须 flush",
        )
        waited = self.elapsed()
        # 关键断言：确实等过了"硬上限"，证明打字真的把天花板抬高了；
        # 同时没有等到超出绝对上限太多（没卡住、没漏 flush）。
        self.assertGreater(
            waited,
            _PATCH["HARD_LIMIT"],
            "等待时间必须超过硬上限，否则输入状态没起到延长作用",
        )
        self.assertLess(
            waited,
            _PATCH["TYPING_ABSOLUTE_LIMIT"] + 0.6,
            "到绝对上限就该 flush，不能继续等",
        )
        self.assertEqual(self.turns[0][0], "我跟你说个事，特别长那种")

    async def test_打字不续命_连发消息也不推迟绝对上限(self):
        """绝对上限从**本轮第一条**起算：打字中反复发消息不能把 deadline 往后推。"""
        await self.say("第一句", 1001)
        self.agg.notify_peer_typing(True)
        await asyncio.sleep(_PATCH["HARD_LIMIT"] * 0.8)
        await self.say("第二句", 1002)
        await asyncio.sleep(_PATCH["HARD_LIMIT"] * 0.8)

        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=3.0),
            "打字中连发也必须被绝对上限兜住",
        )
        self.assertLess(
            self.elapsed(),
            _PATCH["TYPING_ABSOLUTE_LIMIT"] + 0.6,
            "连发消息不得把绝对上限往后推（否则等于可无限续命）",
        )

    async def test_他停手但已过硬上限_立刻回(self):
        """打字拖过 15 秒线才停手：不能因为"刚停手"又给一个满额静默窗。"""
        await self.say("我跟你说个事", 1001)
        self.agg.notify_peer_typing(True)
        await asyncio.sleep(_PATCH["HARD_LIMIT"] + 0.15)
        self.assertEqual(len(self.turns), 0, "还没到绝对上限，不该提前回")

        self.agg.notify_peer_typing(False)
        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=1.0),
            "停手时已过硬上限，应立刻回",
        )
        # 立刻：不能又拖一个完整静默窗
        self.assertLess(self.elapsed(), _PATCH["HARD_LIMIT"] + 0.45)


class TestTypingEdgeCases(_TypingWindowCase):
    async def test_空缓冲只记状态不起表(self):
        """他没说话光打字：不能凭空等一轮（不挂任何计时器），但状态必须记住。

        真机上提示几乎总是**先于**消息到达（消息走内部队列异步消费，
        输入状态在读循环里同步处理）。照"缓冲为空直接忽略"实现的话，
        这条信号会被系统性丢掉，整个功能等于死的——所以只记状态、不起表。
        """
        self.agg.notify_peer_typing(True)
        self.assertIsNone(self.agg._debounce_task, "空缓冲不该挂任何计时器")
        self.assertTrue(self.agg._peer_typing, "他已在打字这件事必须被记住")

        # 他开口了：这一轮就该按"他在打字"处理，而不是按静默窗
        await self.say("我跟你说个事", 1001)
        await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 3)
        self.assertEqual(len(self.turns), 0, "他先打字后发言，这轮就该等他停手")

        self.agg.notify_peer_typing(False)
        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=2.0))

    async def test_空缓冲忽略后正常消息仍走静默窗(self):
        """反向对照：他没在打字时说话，仍按原窗口走（记状态没搞坏普通路径）。"""
        await self.say("刚看到个东西", 1001)
        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=2.0),
            "空缓冲被忽略后，正常消息仍要按静默窗 flush",
        )
        self.assertLess(self.elapsed(), _PATCH["HARD_LIMIT"])

    async def test_结束先于开始不炸(self):
        """乱序：先来一个"停手"。此时没在打字，忽略即可，绝不重启静默窗。"""
        await self.say("在吗", 1001)
        before = self.agg._debounce_task
        self.agg.notify_peer_typing(False)
        self.assertIs(self.agg._debounce_task, before, "无处可收的停手不该重挂静默窗")
        self.assertEqual(len(self.turns), 0)

        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=2.0))

    async def test_重复开始不续命(self):
        """连发心跳式"开始输入"：不能把绝对上限往后推（否则等待可无限延长）。"""
        await self.say("我跟你说个事", 1001)
        self.agg.notify_peer_typing(True)
        first = self.agg._debounce_task
        for _ in range(5):
            self.agg.notify_peer_typing(True)
            self.assertIs(
                self.agg._debounce_task, first, "重复开始输入不得重挂计时器"
            )

        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 1, timeout=3.0),
            "重复上报不得让绝对上限失效",
        )
        self.assertLess(self.elapsed(), _PATCH["TYPING_ABSOLUTE_LIMIT"] + 0.6)

    async def test_一轮结束后打字状态归零(self):
        """flush 必须把打字状态清干净，否则下一轮会莫名接着等。"""
        await self.say("我跟你说个事", 1001)
        self.agg.notify_peer_typing(True)
        self.assertTrue(await _wait_for(lambda: len(self.turns) == 1, timeout=3.0))
        self.assertFalse(
            self.agg._peer_typing, "一轮结束后 _peer_typing 必须归零"
        )

        await self.say("然后呢", 1002)
        self.assertTrue(
            await _wait_for(lambda: len(self.turns) == 2, timeout=2.0),
            "下一轮必须按普通静默窗走",
        )

    async def test_stop后不重挂计时器(self):
        """stop() 之后再来在途输入状态帧：不得把已收尾的计时器重新拉起来。"""
        await self.say("我跟你说个事", 1001)
        self.agg.notify_peer_typing(True)
        self.agg.stop()
        stopped_task = self.agg._debounce_task

        self.agg.notify_peer_typing(False)
        self.assertIs(
            self.agg._debounce_task, stopped_task, "stop() 后不得重挂计时器"
        )
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.turns), 0, "stop() 之后不得再 flush")


class TestNoTypingEventUnchanged(unittest.IsolatedAsyncioTestCase):
    """所有者拍板决策 1：纯增强——收不到输入状态事件时行为与改动前一致。

    必须是 IsolatedAsyncioTestCase：写成普通 TestCase 的话 async 用例会被
    静默跳过、报 OK——那是一次"什么都没验就通过"的假绿。
    """

    def test_常量未被改动(self):
        """SILENCE_WINDOW / HARD_LIMIT 的既有数值与语义不许动。"""
        self.assertEqual(aggmod.SILENCE_WINDOW, 6.0)
        self.assertEqual(aggmod.HARD_LIMIT, 15.0)
        self.assertEqual(aggmod.TYPING_ABSOLUTE_LIMIT, 30.0)

    def test_默认不认为他在打字(self):
        agg = MessageAggregator(turn_handler=None)
        self.assertFalse(agg._peer_typing)

    async def test_无输入状态时两句话仍合成一轮(self):
        real = {k: getattr(aggmod, k) for k in _PATCH}
        for k, v in _PATCH.items():
            setattr(aggmod, k, v)
        turns: List[Any] = []

        async def handler(text, image_path, batch=None):
            turns.append(text)

        agg = MessageAggregator(turn_handler=handler)
        agg.start()
        try:
            await agg.push_message("今天做完实验了", None, 1001)
            await asyncio.sleep(_PATCH["SILENCE_WINDOW"] * 0.4)
            await agg.push_message("晚上吃啥", None, 1002)
            self.assertTrue(await _wait_for(lambda: len(turns) == 1, timeout=2.0))
            self.assertEqual(turns[0], "今天做完实验了\n晚上吃啥")
        finally:
            agg.stop()
            for k, v in real.items():
                setattr(aggmod, k, v)


# ---------------------------------------------------------------------------
# 3. 协议层：OneBot 事件分发 + 状态泄漏自愈
# ---------------------------------------------------------------------------
class TestOneBotInputStatus(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._real_stale = obmod.INPUT_STATUS_STALE_LIMIT
        obmod.INPUT_STATUS_STALE_LIMIT = 0.20
        self.seen: List[bool] = []
        self._tmp = tempfile.mkdtemp(prefix="fixes23_")
        self.client = OneBotClient(
            config=OneBotConfig(),
            allowed_user_id=OWNER_QQ,
            image_save_dir=self._tmp,
            on_typing_callback=self.seen.append,
        )
        self.client._running = True

    async def asyncTearDown(self):
        await self.client.stop()
        obmod.INPUT_STATUS_STALE_LIMIT = self._real_stale
        try:
            os.rmdir(self._tmp)
        except OSError:
            pass

    async def feed(self, payload: Dict[str, Any]) -> None:
        """走真实读循环入口，顺带验一遍 post_type 路由。"""
        await self.client._handle_raw_message(json.dumps(payload))

    async def test_真事件触发回调(self):
        await self.feed(_typing_event(1))
        self.assertEqual(self.seen, [True])
        self.assertTrue(self.client._peer_typing)

        await self.feed(_typing_event(0))
        self.assertEqual(self.seen, [True, False])
        self.assertFalse(self.client._peer_typing)

    async def test_非机主的输入状态忽略(self):
        await self.feed(_typing_event(1, user_id=999999999))
        self.assertEqual(self.seen, [], "别人的输入状态跟我们无关")
        self.assertFalse(self.client._peer_typing)

    async def test_陌生事件不进这条链(self):
        await self.feed(_typing_event(1, sub_type="poke"))
        await self.feed(_typing_event(7))
        self.assertEqual(self.seen, [])

    async def test_状态泄漏自愈(self):
        """开始输入后一直没等到结束事件 → 按停手处理（防"他早停了、她还傻等"）。"""
        await self.feed(_typing_event(1))
        self.assertEqual(self.seen, [True])

        self.assertTrue(
            await _wait_for(lambda: self.seen == [True, False], timeout=2.0),
            f"{obmod.INPUT_STATUS_STALE_LIMIT} 秒没等到结束事件，必须自愈为已停手",
        )
        self.assertFalse(self.client._peer_typing)
        self.assertIsNone(self.client._typing_watchdog)

    async def test_结束事件先到则取消自愈(self):
        """正常收到结束事件后，不该再冒出一次迟到的自愈回调。"""
        await self.feed(_typing_event(1))
        await asyncio.sleep(obmod.INPUT_STATUS_STALE_LIMIT / 3)
        await self.feed(_typing_event(0))
        self.assertEqual(self.seen, [True, False])

        await asyncio.sleep(obmod.INPUT_STATUS_STALE_LIMIT * 2)
        self.assertEqual(self.seen, [True, False], "自愈计时器没被取消，多回调了一次")

    async def test_重复开始不重置自愈表(self):
        """连发心跳：重复的"开始输入"不能把自愈窗口一直往后推。"""
        await self.feed(_typing_event(1))
        for _ in range(4):
            await asyncio.sleep(obmod.INPUT_STATUS_STALE_LIMIT * 0.2)
            await self.feed(_typing_event(1))
        self.assertEqual(self.seen, [True], "重复上报不该反复回调")

        self.assertTrue(
            await _wait_for(lambda: self.seen == [True, False], timeout=2.0),
            "自愈必须按第一次开始输入起算，不能被心跳续命",
        )

    async def test_stop后不再处理在途输入状态帧(self):
        await self.client.stop()
        await self.feed(_typing_event(1))
        self.assertEqual(self.seen, [], "stop() 之后必须丢弃在途帧")
        self.assertIsNone(self.client._typing_watchdog)

    async def test_无回调时也不炸(self):
        """没注入 on_typing_callback（比如旧调用方）时，事件照收不误。"""
        self.client.on_typing_callback = None
        await self.feed(_typing_event(1))
        self.assertTrue(self.client._peer_typing)

    async def test_回调抛异常不影响消息链(self):
        """聚合器一时出错不能把读循环带崩——这条链路只是"让回复晚一点"。"""
        got_messages: List[Any] = []

        async def on_message(text, image_path, message_id=None):
            got_messages.append((text, message_id))

        self.client.on_typing_callback = lambda is_typing: (_ for _ in ()).throw(
            RuntimeError("聚合器炸了")
        )
        self.client.on_message_callback = on_message

        await self.feed(_typing_event(1))
        self.assertTrue(self.client._peer_typing, "回调炸了也要把状态记下来")

        # 消息事件照常走完整条链
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
            await _wait_for(lambda: len(got_messages) == 1, timeout=2.0),
            "输入状态回调抛异常不得影响消息事件处理",
        )
        self.assertEqual(got_messages[0], ("在吗", 777))


if __name__ == "__main__":
    unittest.main(verbosity=2)
