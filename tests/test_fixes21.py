"""FIXES21 引用回复发送侧测试集 (tests/test_fixes21.py)

收侧引用早就支持（他引用她，她能看到「（他引用了你之前说的「XXX」）」），
但发侧是单向街——她从没引用过他的话。本测试集盯住：

1. **message_id 穿链**：入站事件 → OneBotClient → 聚合器批次 → TurnHandler，
   编号 [1][2][3] 与真实 message_id 一一对应（任务1）
2. **`[quote:N]` 语法**：行首才认、编号越界/非数字降级、每轮上限 1 条、
   引用后无正文丢弃、proactive 通路一律降级（任务2）
3. **发送结构**：reply 段拼在同一条消息的**头部**、与 face 段同条共存、
   reply 段失败时去掉引用重发正文（任务3）
4. **沉默权优先**：`[quote:2]` + `[沉默]` 同时出现时沉默赢（任务书备注要求定义的交互）

全部本地构造文本，零真实 API 调用。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "scripts", "sim") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "sim"))

from companion.aggregator import MessageAggregator
from companion.config import OneBotConfig, ReplyConfig
from companion.main import CompanionBot
from companion.onebot import OneBotClient, build_reply_segment
from companion.prompts import (
    QUOTE_USAGE_EXAMPLES,
    QUOTE_USAGE_RULES,
    SYSTEM_PROMPT_TEMPLATE,
    format_numbered_batch,
)
from companion.replier import (
    Replier,
    keep_first_quote,
    merge_quote_into_next,
    quote_chunk,
    strip_leading_quote,
)


class _StubStickers:
    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫"]


def _replier() -> Replier:
    return Replier(ReplyConfig(), _StubStickers())


# 本轮聚合批次：3 条，编号 1~3 对应 message_id 1001~1003
BATCH3: List[Dict[str, Any]] = [
    {"index": 1, "text": "今天做完实验了", "message_id": 1001, "has_image": False},
    {"index": 2, "text": "晚上吃啥", "message_id": 1002, "has_image": False},
    {"index": 3, "text": "对了周六那个会你还去吗", "message_id": 1003, "has_image": False},
]


def _types(chunks) -> List[str]:
    return [c["type"] for c in chunks]


def _quotes(chunks) -> List[Any]:
    return [c.get("_quote") for c in chunks if c.get("_quote")]


# ==========================================
# 1. message_id 穿链（任务1）
# ==========================================


class _FakeWS:
    def __init__(self, client, responses=None):
        import asyncio as _a

        self._inbound: _a.Queue = _a.Queue()
        self.closed = False
        self.client = client
        self.responses = responses or {}

    def feed(self, frame):
        self._inbound.put_nowait(json.dumps(frame))

    async def receive(self):
        return await self._inbound.get()

    async def close(self):
        self.closed = True

    async def send_str(self, payload: str):
        data = json.loads(payload)
        reply = self.responses.get(data.get("action"))
        if reply is not None:
            self._inbound.put_nowait(json.dumps({"echo": data.get("echo"), **reply}))


def _msg_event(message, message_id=None, user_id=10001):
    return {
        "post_type": "message",
        "message_type": "private",
        "user_id": user_id,
        "message": message,
        "message_id": message_id,
    }


def _text_seg(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


class _ConnectedCase(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _connected(self, responses=None):
        cfg = OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token="")
        got: List[Any] = []

        async def cb(text, img, message_id=None):
            got.append((text, img, message_id))

        c = OneBotClient(
            config=cfg, allowed_user_id=10001, on_message_callback=cb,
            image_save_dir="data/test_fixes21_imgs",
        )
        c._ws = _FakeWS(c, responses)
        c._running = True

        async def reader():
            while True:
                frame = await c._ws.receive()
                await c._handle_raw_message(frame)

        rt = asyncio.create_task(reader())
        try:
            yield c, got, c._ws
        finally:
            rt.cancel()
            await asyncio.wait({rt}, timeout=2.0)
            await c.stop()
            if c._dispatcher_task is not None:
                await asyncio.wait({c._dispatcher_task}, timeout=2.0)


async def _wait_for(predicate, timeout=1.5) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class TestMessageIdChain(_ConnectedCase):
    async def test_入站message_id原样透传到回调(self):
        """入站事件本来就有 message_id，收侧只是以前把它扔了。"""
        async with self._connected() as (c, got, ws):
            ws.feed(_msg_event([_text_seg("在吗")], message_id=778899))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][2], 778899)

    async def test_没有message_id时为None(self):
        async with self._connected() as (c, got, ws):
            ws.feed(_msg_event([_text_seg("在吗")]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertIsNone(got[0][2], "取不到 id 就该是 None，下游据此判不可引用")

    async def test_聚合器批次保留编号与id的映射(self):
        import companion.aggregator as agg_mod

        turns: List[Any] = []

        async def handler(text, img, batch=None):
            turns.append((text, img, batch))

        agg = MessageAggregator(turn_handler=handler)
        agg.start()
        old_w, old_h = agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT
        agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = 0.05, 1.0
        try:
            await agg.push_message("今天做完实验了", None, 1001)
            await agg.push_message("晚上吃啥", None, 1002)
            await agg.push_message("对了周六那个会你还去吗", None, 1003)
            self.assertTrue(await _wait_for(lambda: len(turns) == 1, timeout=8.0))
        finally:
            agg.stop()
            agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = old_w, old_h

        text, _img, batch = turns[0]
        self.assertEqual(text, "今天做完实验了\n晚上吃啥\n对了周六那个会你还去吗")
        self.assertEqual([b["index"] for b in batch], [1, 2, 3])
        self.assertEqual([b["message_id"] for b in batch], [1001, 1002, 1003])
        self.assertEqual([b["text"] for b in batch], [
            "今天做完实验了", "晚上吃啥", "对了周六那个会你还去吗",
        ])

    async def test_纯空消息不进批次(self):
        import companion.aggregator as agg_mod

        turns: List[Any] = []

        async def handler(text, img, batch=None):
            turns.append((text, img, batch))

        agg = MessageAggregator(turn_handler=handler)
        agg.start()
        old_w, old_h = agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT
        agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = 0.05, 1.0
        try:
            # 没有任何内容、只有 id：不该进批次（没有可引用的东西）
            await agg.push_message("", None, 555)
            self.assertFalse(turns)
            self.assertEqual(agg._batch, [])
        finally:
            agg.stop()
            agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = old_w, old_h

    async def test_图片消息也保留编号(self):
        """她也可以引用一张图回话，所以带图那条也要有 id 与编号。"""
        import companion.aggregator as agg_mod

        turns: List[Any] = []

        async def handler(text, img, batch=None):
            turns.append((text, img, batch))

        agg = MessageAggregator(turn_handler=handler)
        agg.start()
        old_w, old_h = agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT
        agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = 0.05, 1.0
        try:
            await agg.push_message("你看这个", None, 2001)
            await agg.push_message("", "data/x.jpg", 2002)
            self.assertTrue(await _wait_for(lambda: len(turns) == 1, timeout=8.0))
        finally:
            agg.stop()
            agg_mod.SILENCE_WINDOW, agg_mod.HARD_LIMIT = old_w, old_h

        _text, img, batch = turns[0]
        self.assertEqual(img, "data/x.jpg")
        self.assertEqual(len(batch), 2)
        self.assertEqual(batch[1]["message_id"], 2002)
        self.assertTrue(batch[1]["has_image"])
        self.assertEqual(batch[1]["index"], 2)

    async def test_历史消息无id时仍能正常处理(self):
        """链路任何一环取不到 id → 那条不可被引用，但**降级不影响主流程**。"""
        chunks, record = _replier().parse_reply(
            "[quote:2] 晚上吃啥",
            quote_targets=[
                {"index": 1, "text": "在吗", "message_id": None},
                {"index": 2, "text": "晚上吃啥", "message_id": None},
            ],
        )
        self.assertTrue(chunks, "没 id 也得把话发出去")
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(record, "[quote:2] 晚上吃啥")


# ==========================================
# 2. 编号块与提示词（任务2 第1/4条）
# ==========================================


class TestNumberedBlock(unittest.TestCase):
    def test_三条渲染成编号块(self):
        block = format_numbered_batch(BATCH3)
        self.assertIn("[1] 今天做完实验了", block)
        self.assertIn("[2] 晚上吃啥", block)
        self.assertIn("[3] 对了周六那个会你还去吗", block)

    def test_单条不渲染编号块(self):
        """他只发一条时"引用哪句"根本不存在，塞个 [1] 只会诱使她无意义地引用。"""
        self.assertEqual(format_numbered_batch(BATCH3[:1]), "")
        self.assertEqual(format_numbered_batch([]), "")
        self.assertEqual(format_numbered_batch(None), "")

    def test_批次内换行被压平(self):
        """批次里每条就是一条消息，不该再有换行去骗模型。"""
        block = format_numbered_batch([
            {"index": 1, "text": "第一行\n第二行", "message_id": 1},
            {"index": 2, "text": "另一条", "message_id": 2},
        ])
        self.assertIn("[1] 第一行 第二行", block)

    def test_空文本条目有占位说明(self):
        block = format_numbered_batch([
            {"index": 1, "text": "", "has_image": True, "message_id": 1},
            {"index": 2, "text": "说点什么", "message_id": 2},
        ])
        self.assertIn("（发来一张图片）", block)

    def test_示范是接新话而不是复读原话(self):
        """引用后复读原话是机械腔（"干巴巴"的同类病灶）。

        第一版示范写的是"[quote:3] 对了周六那个会你还去吗"（引用+复述），
        实测 A 段她当场复读了"晚上吃啥 我请你"。真人引用是用来接新话的。
        这条把"示范不许教复读"钉住：示范里 [quote:N] 后面那句必须不是他的原话。
        """
        import re as _re

        pairs = _re.findall(r"\[quote:(\d)\]「([^」]*)」", QUOTE_USAGE_EXAMPLES)
        self.assertGreaterEqual(len(pairs), 2, "示范里应至少有两个带引号的引用例子")
        his_lines = ["今天做完实验了", "晚上吃啥 我请你", "对了周六那个会你还去吗"]
        for index, reply in pairs:
            with self.subTest(index=index):
                self.assertTrue(reply.strip(), "示范里的回复不能是空的")
                self.assertFalse(
                    any(reply.strip() == h for h in his_lines),
                    f"示范在教复读原话：{reply}",
                )
        self.assertIn("不要把他的原话再抄一遍", QUOTE_USAGE_EXAMPLES)

    def test_模板含引用纪律与示范(self):
        self.assertIn("{quote_block}", SYSTEM_PROMPT_TEMPLATE)
        self.assertIn("[quote:编号]", QUOTE_USAGE_RULES)
        self.assertIn("大部分时候直接回就好", QUOTE_USAGE_RULES)
        self.assertIn("[quote:3]", QUOTE_USAGE_EXAMPLES)
        self.assertIn("主动给他发消息", QUOTE_USAGE_RULES, "主动消息没有可引用对象，得写明")

class TestNumberedBlockAssembler(unittest.IsolatedAsyncioTestCase):
    async def test_单条消息时提示词与改动前一致(self):
        """无批次时不能多出任何编号块。"""
        sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
        from helpers import card_path, close_db, make_db, make_engine_stack

        db = await make_db()
        try:
            stack = make_engine_stack(db, persona_path=card_path())
            messages, _ = await stack.assembler.assemble_messages("在吗", None, [])
            user_msg = messages[-1]["content"]
            self.assertEqual(user_msg, "在吗")
            self.assertNotIn("[1]", user_msg)
        finally:
            await close_db(db)

    async def test_三条消息时提示词含编号块(self):
        sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
        from helpers import card_path, close_db, make_db, make_engine_stack

        db = await make_db()
        try:
            stack = make_engine_stack(db, persona_path=card_path())
            joined = "\n".join(b["text"] for b in BATCH3)
            messages, _ = await stack.assembler.assemble_messages(joined, None, BATCH3)
            user_msg = messages[-1]["content"]
            self.assertIn("[1] 今天做完实验了", user_msg)
            self.assertIn("[3] 对了周六那个会你还去吗", user_msg)
        finally:
            await close_db(db)


# ==========================================
# 3. [quote:N] 语法（任务2 第2/3/5条）
# ==========================================


class TestQuoteParsing(unittest.TestCase):
    def test_行首引用被认出来并翻译成message_id(self):
        chunks, record = _replier().parse_reply("[quote:2] 晚上吃啥", quote_targets=BATCH3)
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(_quotes(chunks), [{"index": 2, "message_id": 1002}])
        self.assertEqual(record, "晚上吃啥", "引用标记不进落库记录（编号只对当轮有效）")

    def test_引用独占一行也算数(self):
        chunks, _ = _replier().parse_reply("[quote:2]\n晚上吃啥", quote_targets=BATCH3)
        self.assertEqual(_quotes(chunks), [{"index": 2, "message_id": 1002}])

    def test_行中引用降级为文字(self):
        """引用必须在一行的开头；写在一句话中间不认。"""
        chunks, record = _replier().parse_reply(
            "对了[quote:2] 晚上吃啥", quote_targets=BATCH3
        )
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(_quotes(chunks), [])
        self.assertEqual(record, "对了[quote:2] 晚上吃啥")

    def test_行尾引用降级为文字(self):
        chunks, record = _replier().parse_reply("晚上吃啥[quote:2]", quote_targets=BATCH3)
        self.assertEqual(_quotes(chunks), [])
        self.assertEqual(record, "晚上吃啥[quote:2]")

    def test_编号越界降级为文字(self):
        chunks, record = _replier().parse_reply("[quote:9] 晚上吃啥", quote_targets=BATCH3)
        self.assertEqual(_quotes(chunks), [])
        self.assertEqual(record, "[quote:9] 晚上吃啥")

    def test_非数字编号降级为文字(self):
        chunks, record = _replier().parse_reply("[quote:abc] 晚上吃啥", quote_targets=BATCH3)
        self.assertEqual(_quotes(chunks), [])
        self.assertEqual(record, "[quote:abc] 晚上吃啥")

    def test_无批次时降级为文字(self):
        """主动消息不传批次 → 引用语法一律按文字。"""
        chunks, record = _replier().parse_reply(
            "[quote:1] 在呢", source="proactive"
        )
        self.assertEqual(_quotes(chunks), [])
        self.assertEqual(record, "[quote:1] 在呢")

    def test_降级标记不独占气泡(self):
        """降级后的标记必须留在同一行那句话里，不能自己占一条气泡。"""
        chunks, _ = _replier().parse_reply("[quote:9] 晚上吃啥", quote_targets=BATCH3)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["content"], "[quote:9] 晚上吃啥")

    def test_每轮只保留第一条引用(self):
        chunks, record = _replier().parse_reply(
            "[quote:1] 甲\n[quote:2] 乙", quote_targets=BATCH3
        )
        self.assertEqual(len(_quotes(chunks)), 1)
        self.assertEqual(_quotes(chunks)[0]["index"], 1)
        # 2026-10-04 口径修正：多余的引用段直接丢弃，不再降级成字面 "[quote:2]"
        # 发上屏（标记不是内容，她会说的话在后面的 text 段里）
        self.assertNotIn("[quote:2]", record, "多余的引用标记不得原样发上屏")
        self.assertIn("乙", record, "引用丢了，那句话本身要留下")

    def test_引用后面没有正文则丢弃(self):
        """模型只输出了 [quote:2]：不空发。"""
        chunks, record = _replier().parse_reply("[quote:2]", quote_targets=BATCH3)
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_引用后面紧跟表情包不合并(self):
        """表情包语义一个字不改；没有正文可并时引用直接丢。"""
        chunks, record = _replier().parse_reply(
            "[quote:2][sticker:猫猫]", quote_targets=BATCH3
        )
        self.assertEqual(_types(chunks), ["sticker"])
        self.assertEqual(_quotes(chunks), [])
        self.assertEqual(record, "[表情:猫猫]")

    def test_中文冒号容错(self):
        chunks, _ = _replier().parse_reply("[quote：2] 晚上吃啥", quote_targets=BATCH3)
        self.assertEqual(_quotes(chunks), [{"index": 2, "message_id": 1002}])

    def test_沉默权优先于引用(self):
        """引用与沉默矛盾时沉默赢：引用不成立、更不该发。"""
        for raw in ("[quote:2]\n[沉默]", "[quote:2] [沉默]"):
            with self.subTest(raw=raw):
                chunks, record = _replier().parse_reply(raw, quote_targets=BATCH3)
                self.assertEqual(chunks, [])
                self.assertEqual(record, "")

    def test_纯沉默不受影响(self):
        chunks, record = _replier().parse_reply("[沉默]")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_引用与face同一条消息(self):
        """DEEP_AUDIT 备注里的 quote × face：一条消息里 reply+text+face 三段。"""
        chunks, record = _replier().parse_reply(
            "[quote:2] 晚上吃啥[face:吃瓜]", quote_targets=BATCH3
        )
        self.assertEqual(_types(chunks), ["combo"])
        self.assertEqual(_quotes(chunks), [{"index": 2, "message_id": 1002}])
        self.assertEqual(record, "晚上吃啥[face:吃瓜]")

    def test_引用与表情包同轮共存(self):
        """引用不与表情包合并，但可以同轮存在（表情包既有语义一个字不改）。"""
        chunks, record = _replier().parse_reply(
            "在呢[sticker:猫猫]\n[quote:2] 晚上吃啥", quote_targets=BATCH3
        )
        # 文字/表情包/带引用的正文 = 三条气泡（FIXES20 起 sticker 就不与文字合并）
        self.assertEqual(_types(chunks), ["text", "sticker", "text"])
        self.assertEqual(_quotes(chunks), [{"index": 2, "message_id": 1002}])
        self.assertEqual(record, "在呢\n[表情:猫猫]\n晚上吃啥")

    def test_引用不占气泡预算(self):
        """max_chunks 限的是气泡数；引用并进正文后仍然只占一条。"""
        raw = "\n".join([f"第{i}句" for i in range(1, 7)])
        chunks, _ = _replier().parse_reply(f"[quote:1]\n{raw}", quote_targets=BATCH3)
        self.assertLessEqual(len(chunks), 5)
        self.assertEqual(_quotes(chunks), [{"index": 1, "message_id": 1001}])


class TestQuoteHelpers(unittest.TestCase):
    def test_quote_chunk四道关(self):
        self.assertEqual(quote_chunk(2, BATCH3), {"type": "quote", "index": 2, "message_id": 1002})
        self.assertIsNone(quote_chunk(9, BATCH3), "编号越界")
        self.assertIsNone(quote_chunk(2, []), "没有批次")
        self.assertIsNone(quote_chunk(2, None), "没有批次")
        self.assertIsNone(
            quote_chunk(1, [{"index": 1, "text": "x", "message_id": None}]), "没有 id"
        )
        self.assertIsNone(
            quote_chunk(1, [{"index": 1, "text": "x", "message_id": "脏值"}]), "id 非法"
        )
        self.assertEqual(quote_chunk(1, [{"index": 1, "text": "x", "message_id": "42"}])["message_id"], 42)

    def test_strip_leading_quote(self):
        self.assertEqual(strip_leading_quote("[quote:2] 你真棒"), ("你真棒", 2))
        self.assertEqual(strip_leading_quote("[quote:2]\n你真棒"), ("你真棒", 2))
        self.assertEqual(strip_leading_quote("你真棒"), ("你真棒", None))
        self.assertEqual(strip_leading_quote("你[quote:2]真棒"), ("你[quote:2]真棒", None))

    def test_keep_first_quote(self):
        out = keep_first_quote([
            {"type": "quote", "index": 1, "message_id": 1},
            {"type": "text", "content": "甲"},
            {"type": "quote", "index": 2, "message_id": 2},
        ])
        self.assertEqual(len([c for c in out if c["type"] == "quote"]), 1)
        # 多余的引用段被丢弃（不再是降级成 "[quote:2]" 的文字段）
        self.assertTrue(all(c.get("content") != "[quote:2]" for c in out))
        self.assertEqual([c for c in out if c["type"] == "text"], [{"type": "text", "content": "甲"}])

    def test_merge_quote_into_next(self):
        out = merge_quote_into_next([
            {"type": "quote", "index": 1, "message_id": 11},
            {"type": "text", "content": "在呢"},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["_quote"], {"index": 1, "message_id": 11})
        self.assertEqual(out[0]["content"], "在呢")

    def test_merge_quote_末尾无正文丢弃(self):
        self.assertEqual(
            merge_quote_into_next([{"type": "quote", "index": 1, "message_id": 11}]), []
        )


# ==========================================
# 4. 发送结构（任务3）
# ==========================================


def _build_reply_segment(message_id):
    return {"type": "reply", "data": {"id": int(message_id)}}


class _RecordingOneBot:
    """记录 send_private_msg 的段数组；fail_quote 用来模拟 reply 段被拒"""

    def __init__(self, fail_quote: bool = False):
        self.sent: List[Dict[str, Any]] = []
        self.fail_quote = fail_quote

    async def send_private_msg(self, user_id, message_segments, max_retries=2):
        has_reply = any(s.get("type") == "reply" for s in message_segments)
        self.sent.append({"user_id": user_id, "message": message_segments})
        if has_reply and self.fail_quote:
            return False   # NapCat 拒了这条（id 过期/不支持）
        return True


class _StubBot(CompanionBot):
    def __init__(self, onebot):  # noqa: D107 - 故意不调 super()，只借发送逻辑
        self.config = type(
            "C", (), {"account": type("A", (), {"allowed_user_id": 10001})()}
        )()
        self.onebot = onebot


class TestSendStructure(unittest.TestCase):
    def test_build_reply_segment(self):
        self.assertEqual(build_reply_segment(1002), {"type": "reply", "data": {"id": 1002}})

    def test_reply段拼在同一条消息头部(self):
        ob = _RecordingOneBot()
        bot = _StubBot(ob)
        chunks, _ = _replier().parse_reply("[quote:2] 晚上吃啥", quote_targets=BATCH3)
        for c in chunks:
            asyncio.run(bot._send_chunk_to_onebot(c))
        self.assertEqual(len(ob.sent), 1, "引用与正文必须是同一条消息")
        self.assertEqual(
            ob.sent[0]["message"],
            [
                {"type": "reply", "data": {"id": 1002}},
                {"type": "text", "data": {"text": "晚上吃啥"}},
            ],
        )

    def test_quote与face同一条消息三段(self):
        ob = _RecordingOneBot()
        bot = _StubBot(ob)
        chunks, _ = _replier().parse_reply(
            "[quote:2] 晚上吃啥[face:吃瓜]", quote_targets=BATCH3
        )
        for c in chunks:
            asyncio.run(bot._send_chunk_to_onebot(c))
        self.assertEqual(len(ob.sent), 1)
        self.assertEqual(
            [s["type"] for s in ob.sent[0]["message"]], ["reply", "text", "face"]
        )

    def test_纯脸气泡带引用(self):
        ob = _RecordingOneBot()
        bot = _StubBot(ob)
        chunks, _ = _replier().parse_reply("[quote:3][face:吃瓜]", quote_targets=BATCH3)
        for c in chunks:
            asyncio.run(bot._send_chunk_to_onebot(c))
        self.assertEqual(len(ob.sent), 1)
        self.assertEqual(
            [s["type"] for s in ob.sent[0]["message"]], ["reply", "face"]
        )

    def test_reply段失败则去掉引用重发正文(self):
        ob = _RecordingOneBot(fail_quote=True)
        bot = _StubBot(ob)
        chunks, _ = _replier().parse_reply("[quote:2] 晚上吃啥", quote_targets=BATCH3)
        for c in chunks:
            asyncio.run(bot._send_chunk_to_onebot(c))
        self.assertEqual(len(ob.sent), 2, "带引用失败后要重发一次")
        self.assertEqual(ob.sent[0]["message"][0]["type"], "reply")
        self.assertEqual(
            ob.sent[1]["message"],
            [{"type": "text", "data": {"text": "晚上吃啥"}}],
            "重发必须没有 reply 段，否则就是死循环",
        )

    def test_无引用时失败不重发(self):
        ob = _RecordingOneBot(fail_quote=True)
        bot = _StubBot(ob)
        chunks, _ = _replier().parse_reply("晚上吃啥", quote_targets=BATCH3)
        for c in chunks:
            asyncio.run(bot._send_chunk_to_onebot(c))
        self.assertEqual(len(ob.sent), 1)

    def test_引用id非法时本条不带引用(self):
        ob = _RecordingOneBot()
        bot = _StubBot(ob)
        asyncio.run(bot._send_chunk_to_onebot(
            {"type": "text", "content": "在呢", "_quote": {"index": 1, "message_id": "脏值"}}
        ))
        self.assertEqual(
            [s["type"] for s in ob.sent[0]["message"]], ["text"],
        )

    def test_typing不算引用(self):
        """引用不进落库记录 → 打字时长天然不含它（不触发长 typing）。"""
        from companion.replier import strip_face_markers

        _chunks, record = _replier().parse_reply(
            "[quote:2] 晚上吃啥", quote_targets=BATCH3
        )
        self.assertEqual(strip_face_markers(record), "晚上吃啥")


# ==========================================
# 5. 测量工具自检：duo_sim（B 段裁判自己先验一遍）
# ==========================================


class TestSimMeasurement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import duo_sim as D

        cls.D = D

    def test_build_sim_batch按行拆批次(self):
        """仿真里"他"一轮多行 = 生产里他连发多条被聚合（FIXES21）。"""
        batch = self.D.build_sim_batch("第一条\n第二条\n第三条", 7)
        self.assertEqual([b["index"] for b in batch], [1, 2, 3])
        self.assertEqual([b["text"] for b in batch], ["第一条", "第二条", "第三条"])
        # message_id 必须是负数：一眼可辨是仿真伪造的
        self.assertTrue(all(b["message_id"] < 0 for b in batch))
        self.assertEqual(len({b["message_id"] for b in batch}), 3)

    def test_build_sim_batch单行与空串(self):
        self.assertEqual(len(self.D.build_sim_batch("就一句", 1)), 1)
        self.assertEqual(self.D.build_sim_batch("", 1), [])
        self.assertEqual(self.D.build_sim_batch("   \n  ", 1), [])

    def test_端到端投影不丢quotes(self):
        """**踩过坑才写的**：_rec_to_dict 少带一个字段，指标就把"用了 1 次"报成 0。

        与 FIXES20 faces 那次同型：测量工具的投影丢字段 = 有数据被报成无样本。
        只测 metric_quote_usage 的叶子函数发现不了，必须整条投影走一遍。
        """
        batch = self.D.build_sim_batch("甲\n乙", 3)
        rec = self.D.TurnRecord(
            idx=2, speaker="her", text="乙", time="t",
            bubbles=["乙"], quotes=[{"index": 2, "message_id": -32}],
            his_batch=batch,
        )
        projected = self.D._rec_to_dict(rec)
        self.assertEqual(projected.get("quotes"), [{"index": 2, "message_id": -32}])
        m = self.D.compute_metrics([projected], stage=1, cost=0.0)["multi_turn"]["引用回复"]
        self.assertEqual(m["count"], 1, "投影把 quotes 弄丢了，指标会把用过报成没用过")
        self.assertNotEqual(m["verdict"], "N/A")

    def test_零引用报N_A不报PASS(self):
        m = self.D.metric_quote_usage([
            {"idx": 1, "speaker": "her", "bubbles": ["在呢"], "quotes": [], "his_batch": []},
        ])
        self.assertEqual(m["verdict"], "N/A")
        self.assertTrue(m["not_run_reason"])

    def test_有连发场景却没引用也给N_A(self):
        m = self.D.metric_quote_usage([
            {"idx": 1, "speaker": "her", "bubbles": ["在呢"], "quotes": [],
             "his_batch": self.D.build_sim_batch("甲\n乙", 1)},
        ])
        self.assertEqual(m["verdict"], "N/A")
        self.assertIn("连发", m["not_run_reason"])

    def test_编号越界判FAIL(self):
        """引用了不存在的东西 = 错，不是命中。"""
        m = self.D.metric_quote_usage([{
            "idx": 1, "speaker": "her", "bubbles": ["在呢"],
            "quotes": [{"index": 9, "message_id": -19}],
            "his_batch": self.D.build_sim_batch("甲\n乙", 1),
        }])
        self.assertEqual(m["verdict"], "FAIL")
        self.assertEqual(len(m["invalid"]), 1)

    def test_超一条引用判FAIL(self):
        m = self.D.metric_quote_usage([{
            "idx": 1, "speaker": "her", "bubbles": ["乙"],
            "quotes": [{"index": 1, "message_id": -11}, {"index": 2, "message_id": -12}],
            "his_batch": self.D.build_sim_batch("甲\n乙", 1),
        }])
        self.assertEqual(m["verdict"], "FAIL")
        self.assertEqual(m["over_cap_turns"], [1])

    def test_他说的话不算进她的统计(self):
        m = self.D.metric_quote_usage([{
            "idx": 1, "speaker": "user", "bubbles": ["甲"],
            "quotes": [{"index": 1, "message_id": -11}],
            "his_batch": self.D.build_sim_batch("甲\n乙", 1),
        }])
        self.assertEqual(m["verdict"], "N/A")

    def test_标注了id是伪造的(self):
        """防止以后有人把仿真数字当真 id 引用。"""
        m = self.D.metric_quote_usage([{
            "idx": 1, "speaker": "her", "bubbles": ["乙"],
            "quotes": [{"index": 2, "message_id": -12}],
            "his_batch": self.D.build_sim_batch("甲\n乙", 1),
        }])
        self.assertTrue(m["synthetic_ids"])
        self.assertIn("A 段", m["synthetic_ids_note"])


class TestTurnHandlerSilence(unittest.IsolatedAsyncioTestCase):
    """turn_handler 级的沉默判定（终审打回的那处 bug 就在这里）

    之前所有沉默测试都停在 `parse_reply` 层，而 turn_handler 在 parse 之后
    **又自己判了一次**、判的是未剥引用的原文——所以 bug 一直没被照到。
    这一节钉住"从 turn_handler 进去的完整行为"：沉默必须**不发送**、
    **不落 assistant 记录**、**不结算 observer**，也**绝不能**掉进
    "刚刚走神了……你再说一次？"那个兜底。
    """

    async def _run_turn(self, raw_reply: str, batch=None):
        from helpers import card_path, close_db, make_db, make_engine_stack, make_mock_gateway

        db = await make_db()
        try:
            gw = make_mock_gateway()
            stack = make_engine_stack(
                db, persona_path=card_path(), gateway=gw
            )
            sent: List[Dict[str, Any]] = []
            turned: List[Tuple[bool, Any]] = []

            async def collect(chunk):
                sent.append(chunk)

            async def fake_typing(typing: bool) -> bool:
                return True

            async def spy_stream(*a, **k):
                pieces = raw_reply.split("||")
                for p in pieces:
                    yield p

            class _Proactive:
                async def reset_unanswered_count(self):
                    return None

            handler = _make_handler(stack, collect, fake_typing, _Proactive(), spy_stream)
            await handler.handle_turn("在吗", None, batch)
            rows = await stack.memory.db.fetchall("SELECT role, content FROM turns")
            return sent, [dict(r) for r in rows]
        finally:
            await close_db(db)

    async def test_纯沉默不发不发记录(self):
        sent, rows = await self._run_turn("[沉默]")
        self.assertEqual(sent, [], "沉默轮不该发任何消息")
        self.assertFalse(
            any(r["role"] == "assistant" and r["content"] for r in rows),
            f"沉默轮不该落 assistant 记录：{rows}",
        )

    async def test_带引用的沉默不发(self):
        """**终审打回的那条**：原文是 "[quote:2]\\n[沉默]"，判沉默必须剥掉引用前缀。"""
        batch = [
            {"index": 1, "text": "甲", "message_id": 9001},
            {"index": 2, "text": "乙", "message_id": 9002},
        ]
        sent, rows = await self._run_turn("[quote:2]\n[沉默]", batch)
        self.assertEqual(sent, [], "带引用的沉默必须被当成沉默，不能发兜底文案")
        for r in rows:
            self.assertNotIn("走神了", str(r["content"]))
        self.assertFalse(
            any(r["role"] == "assistant" and r["content"] for r in rows)
        )

    async def test_只有引用没正文按沉默处理(self):
        """只输出 [quote:N]：引用被丢弃后等于什么都没说，不能说"走神了"。"""
        batch = [
            {"index": 1, "text": "甲", "message_id": 9001},
            {"index": 2, "text": "乙", "message_id": 9002},
        ]
        sent, rows = await self._run_turn("[quote:2]", batch)
        self.assertEqual(sent, [])
        for r in rows:
            self.assertNotIn("走神了", str(r["content"]))

    async def test_普通回复照发(self):
        """回归：正常话不能被这套判定误伤。"""
        sent, rows = await self._run_turn("在呢||你说啥")
        self.assertTrue(sent, "正常回复必须发出去")
        self.assertTrue(any(r["role"] == "assistant" for r in rows))


class TestSyncImageDesc(unittest.TestCase):
    """`_sync_image_desc_to_batch` 写回哪一条（终审抓出的错位）"""

    def test_写回image_index指的那一条(self):
        from companion.turn_handler import _sync_image_desc_to_batch

        batch = [
            {"index": 1, "text": "", "message_id": 1, "has_image": True},
            {"index": 2, "text": "", "message_id": 2, "has_image": True, "image_index": 2},
        ]
        _sync_image_desc_to_batch(batch, "前一句话 [发来一张照片：一只猫]")
        self.assertEqual(batch[0]["text"], "", "第一张图不是被采用的那张，不该写它")
        self.assertIn("一只猫", batch[1]["text"])

    def test_没有image_index时退回第一个带图条目(self):
        from companion.turn_handler import _sync_image_desc_to_batch

        batch = [
            {"index": 1, "text": "你先说", "message_id": 1, "has_image": True},
            {"index": 2, "text": "", "message_id": 2},
        ]
        _sync_image_desc_to_batch(batch, "[发来一张照片：晚霞]")
        self.assertIn("晚霞", batch[0]["text"])

    def test_批次为空不炸(self):
        from companion.turn_handler import _sync_image_desc_to_batch

        _sync_image_desc_to_batch([], "[发来一张照片：x]")


class _DummyObserver:
    def __init__(self):
        self.calls: List[Any] = []

    async def settle_turn(self, user_message, assistant_reply, user_image_path=None):
        self.calls.append((user_message, assistant_reply))
        return {}


def _make_handler(stack, collect, set_typing, proactive, stream_chat):
    """用真实 assembler/memory 组一个 TurnHandler（只换网关与发送端）

    config 用真实的 `Config(...)`：TurnHandler 只读 llm.text_model /
    llm.vision_model / get_holidays 三个口子，造一个假 Config 反而容易漏字段。
    """
    from companion.config import AccountConfig, Config
    from companion.turn_handler import TurnHandler

    cfg = Config(account=AccountConfig(allowed_user_id=1, bot_qq=0))

    class _Gw:
        config = cfg.llm

        def __init__(self):
            self._stream = stream_chat

        def stream_chat(self, *a, **k):
            return self._stream(*a, **k)

        async def chat(self, *a, **k):
            return "{}"

    return TurnHandler(
        config=cfg,
        gateway=_Gw(),
        assembler=stack.assembler,
        replier=stack.replier,
        memory=stack.memory,
        observer=_DummyObserver(),
        proactive=proactive,
        send_chunk_fn=collect,
        set_typing_fn=set_typing,
        timing_config=None,
    )


if __name__ == "__main__":
    unittest.main()
