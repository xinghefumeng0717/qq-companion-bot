"""FIXES9 验收单元测试 (tests/test_fixes9.py)

任务 9（角色卡示例重构）配套的代码防御：
1. 行首【起】/【接】/【收】触发方向标签必须被剥离，且不误伤正文中普通的【】
2. 剥离发生在旁白剥离之前，且普通回复与主动消息共用同一条 parse_reply 路径
3. 剥离不改变其余文本内容（幂等、不吞字）
"""

import asyncio
import json
import os
import unittest
from contextlib import asynccontextmanager

from companion.config import ReplyConfig
from companion.replier import (
    Replier,
    strip_direction_tag,
    unescape_literal_newlines,
    chunk_text_sentences,
    DIRECTION_TAG_PATTERN,
)


class _StubStickers:
    """最小替身：只提供 match_sticker 接口"""

    def match_sticker(self, desc: str):
        return None


def _make_replier() -> Replier:
    return Replier(config=ReplyConfig(), stickers=_StubStickers())


class TestDirectionTagStrip(unittest.TestCase):
    def test_strips_leading_tag(self):
        """行首标签必须被剥离"""
        for tag in ("【起】", "【接】", "【收】"):
            with self.subTest(tag=tag):
                out = strip_direction_tag(f"{tag}刚出琴房 紫金港的路灯刚亮")
                self.assertEqual(out, "刚出琴房 紫金港的路灯刚亮")

    def test_strips_with_whitespace_and_newline(self):
        """标签与正文之间可能有空格或换行"""
        self.assertEqual(
            strip_direction_tag("【起】\n刚出琴房"), "刚出琴房"
        )
        self.assertEqual(
            strip_direction_tag("  【收】 行 那你忙"), "行 那你忙"
        )

    def test_keeps_body_bracket_content(self):
        """正文中偶然出现的【】不能被误删"""
        src = "这章有道题【例3】我看不懂"
        self.assertEqual(strip_direction_tag(src), src)
        src2 = "【汪】这首歌我今天循环了一路"
        self.assertEqual(strip_direction_tag(src2), src2)

    def test_does_not_strip_unknown_tag(self):
        """只认【起】【接】【收】三个字，其他标签不动"""
        src = "【警示】这条不属于触发方向"
        self.assertEqual(strip_direction_tag(src), src)

    def test_only_first_occurrence(self):
        """正文中间再出现标签时不剥离（不是全局替换）"""
        src = "【起】开头有标签 中间又出现【起】"
        self.assertEqual(
            strip_direction_tag(src), "开头有标签 中间又出现【起】"
        )

    def test_idempotent(self):
        """连剥两次结果相同，不吞字"""
        once = strip_direction_tag("【起】安中楼下好像看到你了，走好快")
        twice = strip_direction_tag(once)
        self.assertEqual(once, twice)
        self.assertEqual(once, "安中楼下好像看到你了，走好快")

    def test_pattern_is_anchored(self):
        """正则必须带行首锚点，否则会变成全局删除"""
        self.assertTrue(DIRECTION_TAG_PATTERN.pattern.startswith("^"))


class TestParseReplyIntegration(unittest.TestCase):
    """parse_reply 是普通回复与主动消息（proactive.py:305）的唯一共用出口"""

    def setUp(self):
        self.r = _make_replier()

    def test_parse_reply_removes_tag(self):
        chunks, record = self.r.parse_reply("【起】刚出琴房 紫金港的路灯刚亮")
        self.assertTrue(chunks, "应至少产生一个待发段")
        for c in chunks:
            self.assertNotIn("【起】", c["content"])
            self.assertNotIn("【", c["content"])
        self.assertNotIn("【起】", record)
        self.assertEqual(record, "刚出琴房 紫金港的路灯刚亮")

    def test_parse_reply_tag_then_narration(self):
        """标签剥离必须发生在旁白剥离之前，两者叠加都要生效"""
        raw = "【接】刚出琴房（叹了口气）紫金港的路灯刚亮"
        chunks, record = self.r.parse_reply(raw)
        self.assertNotIn("【", record)
        self.assertNotIn("（", record)
        self.assertIn("刚出琴房", record)
        self.assertIn("紫金港的路灯刚亮", record)

    def test_parse_reply_tag_on_every_line(self):
        """模型可能每行都加标签，所有行都要剥干净"""
        raw = "【起】第一行\n【接】第二行"
        chunks, record = self.r.parse_reply(raw)
        self.assertNotIn("【", record)
        self.assertIn("第一行", record)
        self.assertIn("第二行", record)

    def test_parse_reply_clean_text_untouched(self):
        """无标签时行为完全不变（回归保护）"""
        chunks, record = self.r.parse_reply("行 那你忙")
        self.assertEqual(record, "行 那你忙")


class TestNewlineHardBoundary(unittest.TestCase):
    """换行是硬边界：模型写的换行 = 两条独立气泡，绝不能被合并成黏句"""

    def test_newline_splits_into_two_messages(self):
        chunks = chunk_text_sentences("刚出琴房\n紫金港的灯亮了", max_chunks=5)
        self.assertEqual(chunks, ["刚出琴房", "紫金港的灯亮了"])

    def test_no_cross_line_merge_even_when_short(self):
        """两行加起来 <= 15 字也绝不能合并（这正是旧实现会黏句的场景）"""
        chunks = chunk_text_sentences("辛苦啦\n刚路过大活", max_chunks=5)
        self.assertEqual(len(chunks), 2, "换行两侧不得合并")
        self.assertNotIn("辛苦啦刚路过大活", chunks)

    def test_within_line_merge_still_works(self):
        """行内相邻短句仍按 <= 15 字合并（原有行为不得回归）"""
        chunks = chunk_text_sentences("在呢！好的。没问题。", max_chunks=3)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], "在呢！好的。没问题。")

    def test_single_line_untouched(self):
        chunks = chunk_text_sentences("刚出琴房，紫金港的路灯刚亮", max_chunks=5)
        self.assertEqual(chunks, ["刚出琴房，紫金港的路灯刚亮"])

    def test_blank_lines_skipped(self):
        chunks = chunk_text_sentences("第一行\n\n第三行", max_chunks=5)
        self.assertEqual(chunks, ["第一行", "第三行"])


class TestLiteralNewlineUnescape(unittest.TestCase):
    """模型常把换行写成字面量 \\n（反斜杠+n），必须还原，否则原样发到 QQ 上"""

    def test_unescape_backslash_n(self):
        self.assertEqual(
            unescape_literal_newlines("刚出琴房\\n紫金港"), "刚出琴房\n紫金港"
        )

    def test_unescape_backslash_r_backslash_n(self):
        self.assertEqual(
            unescape_literal_newlines("A\\r\\nB"), "A\nB"
        )

    def test_leaves_real_newline_untouched(self):
        self.assertEqual(unescape_literal_newlines("A\nB"), "A\nB")

    def test_parse_reply_never_emits_literal_newline(self):
        r = _make_replier()
        for raw in (
            "刚出琴房\\n紫金港的灯亮了",
            "【起】刚出琴房\\n【接】紫金港",
            '刚出琴房\\n紫金港\\n路灯刚亮',
        ):
            with self.subTest(raw=raw):
                chunks, _ = r.parse_reply(raw)
                for c in chunks:
                    self.assertNotIn("\\n", c["content"], "字面 \\n 不得发出去")

    def test_parse_reply_literal_newline_becomes_two_bubbles(self):
        r = _make_replier()
        chunks, _ = r.parse_reply("【起】刚出琴房\\n【接】紫金港")
        self.assertEqual([c["content"] for c in chunks], ["刚出琴房", "紫金港"])


# ==========================================================
# 任务 1：QQ 引用回复感知（onebot.py）
# ==========================================================


class _FakeWS:
    """最小 WS 替身：send_str 按 action 预置回包，回包经 inbound 队列送回读循环入口。

    关键：不再直连 future 兑现 echo。真实 NapCat 的回包也是从 `async for msg in ws`
    进来、由读循环分发到 _handle_raw_message 的 echo 分支；直连 future 会把
    "读循环自锁" 这类分发问题整个掩盖掉。
    """

    def __init__(self, client, responses=None, drop=False):
        self.client = client
        self.responses = responses or {}
        self.drop = drop          # True = 永不回包（测超时）
        self.closed = False
        self.sent = []
        self._inbound: asyncio.Queue = asyncio.Queue()

    async def send_str(self, raw):
        payload = json.loads(raw)
        self.sent.append(payload)
        if self.drop:
            return
        action = payload.get("action")
        data = self.responses.get(action, {"status": "ok", "retcode": 0, "data": {}})
        self.feed({**data, "echo": payload.get("echo")})

    def feed(self, frame):
        """模拟服务端推来一帧（消息事件或 action 回包）"""
        self._inbound.put_nowait(json.dumps(frame))

    async def receive(self):
        return await self._inbound.get()

    async def close(self):
        self.closed = True


async def _read_loop(client, ws):
    """复刻 OneBotClient.start() 的读循环：同一任务内逐帧 await _handle_raw_message"""
    while True:
        frame = await ws.receive()
        await client._handle_raw_message(frame)


async def _wait_for(predicate, timeout=1.5):
    """轮询等待条件成立，不用固定 sleep 拖慢/拖脆测试"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _msg_event(message, user_id=10001, message_type="private"):
    """构造一条 OneBot 私聊消息上报事件"""
    return {
        "post_type": "message",
        "message_type": message_type,
        "user_id": user_id,
        "message": message,
    }


def _mk_client(responses=None, drop=False):
    from companion.config import OneBotConfig
    from companion.onebot import OneBotClient

    cfg = OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token="")
    got = []

    # FIXES21：OneBotClient 回调多第三个参数 message_id（引用回复要靠它）
    async def cb(text, img, message_id=None):
        got.append((text, img))

    c = OneBotClient(
        config=cfg, allowed_user_id=10001, on_message_callback=cb,
        image_save_dir="data/test_fixes9_imgs",
    )
    c._ws = _FakeWS(c, responses, drop)
    # 生产上读循环只在 start() 之后跑；dispatcher 带停机守卫，这里模拟"已启动"
    c._running = True
    return c, got


def _getmsg(sender_id, segments, message_str=None):
    data = {
        "sender": {"user_id": sender_id},
        "message": segments,
    }
    if message_str is not None:
        data["message_str"] = message_str
    return {"status": "ok", "retcode": 0, "data": data}


class _ConnectedCase(unittest.IsolatedAsyncioTestCase):
    """共用夹具：客户端 + 真实读循环任务；消息事件与 action 回包都走读循环入口"""

    @asynccontextmanager
    async def _connected(self, responses=None, drop=False):
        c, got = _mk_client(responses, drop)
        reader = asyncio.create_task(_read_loop(c, c._ws))
        try:
            yield c, got, c._ws
        finally:
            reader.cancel()
            await asyncio.wait({reader}, timeout=2.0)
            await c.stop()
            if c._dispatcher_task is not None:
                await asyncio.wait({c._dispatcher_task}, timeout=2.0)


class TestReplyPerception(_ConnectedCase):
    async def test_reply_to_her_message(self):
        """引用她自己发的消息 -> 「他引用了你之前说的「XXX」」"""
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "text", "data": {"text": "我在琴房练琴"}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9001}},
                {"type": "text", "data": {"text": "现在走吗"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1), "引用消息未送达回调")
            text, img = got[0]
            self.assertEqual(text, "（他引用了你之前说的「我在琴房练琴」）\n现在走吗")
            self.assertIsNone(img)

    async def test_reply_success_path_through_read_loop(self):
        """P0 回归：echo 回包只能由读循环送达。

        本用例把消息事件与 get_msg 回包都从读循环入口喂进去（生产路径，不做旁路）。
        旧实现里读循环会阻塞在 _fetch_reply_context，get_msg 必然等满 2 秒超时降级；
        正确实现里消息处理被派发出去，读循环继续收帧、当场兑现 echo。
        """
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "text", "data": {"text": "我在琴房练琴"}}])
        }) as (c, got, ws):
            start = asyncio.get_running_loop().time()
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9010}},
                {"type": "text", "data": {"text": "现在走吗"}},
            ]))
            self.assertTrue(
                await _wait_for(lambda: len(got) == 1, timeout=5.0),
                "读循环被消息处理占住，get_msg 回包没能及时送达",
            )
            elapsed = asyncio.get_running_loop().time() - start
            self.assertEqual(got[0][0], "（他引用了你之前说的「我在琴房练琴」）\n现在走吗")
            self.assertLess(
                elapsed, 1.5,
                "读循环必须能在 get_msg 等待期间继续收帧，而不是等满 2 秒超时降级",
            )
            self.assertFalse(c._pending_echoes)

    async def test_read_loop_stays_live_and_keeps_order(self):
        """引用待回包期间，后一条消息照样即时处理，且顺序不乱"""
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "text", "data": {"text": "在图书馆"}}])
        }) as (c, got, ws):
            start = asyncio.get_running_loop().time()
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9011}},
                {"type": "text", "data": {"text": "你在哪"}},
            ]))
            ws.feed(_msg_event([{"type": "text", "data": {"text": "我来找你"}}]))
            self.assertTrue(await _wait_for(lambda: len(got) == 2, timeout=5.0))
            elapsed = asyncio.get_running_loop().time() - start
            self.assertEqual(got[0][0], "（他引用了你之前说的「在图书馆」）\n你在哪")
            self.assertEqual(got[1][0], "我来找你")
            self.assertLess(elapsed, 1.5)

    async def test_dispatcher_task_lifecycle_managed(self):
        """派发协程必须有强引用（self._dispatcher_task）且随 stop() 收尾"""
        client = None
        async with self._connected({}) as (c, got, ws):
            ws.feed(_msg_event([{"type": "text", "data": {"text": "在"}}]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertIsNotNone(c._dispatcher_task)
            self.assertFalse(c._dispatcher_task.done())
            client = c
        self.assertTrue(client._dispatcher_task.done(), "stop() 后派发协程必须已经收尾")

    async def test_reply_to_own_message(self):
        """引用机主自己发过的消息 -> 「他之前说的」"""
        async with self._connected({
            "get_msg": _getmsg(10001, [{"type": "text", "data": {"text": "我明天要去补考"}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9002}},
                {"type": "text", "data": {"text": "那你还来吗"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "（他之前说的「我明天要去补考」）\n那你还来吗")

    async def test_reply_to_image_or_sticker(self):
        """被引是图片/表情 -> 描述它是什么，不编造内容（任务 1 规格）

        FIXES20 行为变更（原先 face/mface 也归「一张图」，现在给出真标签）：
        收侧把 face/mface 翻成方括号标签进了文本流（FIXES20 任务2），
        纯表情消息从此**不再是"没有任何文本"**，于是 `_describe_quoted_message`
        走文本分支、写标签而不是「一张图」。
        `_quoted_media_desc` 的归类逻辑一个字没动（任务书任务2 第3条），
        变的是"哪一边非空"：真照片仍然没有文本，照旧写「一张图」。

        为什么这是变好而不是把守卫改松：他说"这是啥"、被引的其实是一张狗头，
        告诉他「一张图」是**报错内容**（小黄脸 15KB，不是照片）；
        现在告诉他「[doge]」才是真的。仍然不编造被引内容里的任何细节。
        """
        # 真照片：没有任何文本 -> 仍旧「一张图」（这一条行为一个字没变）
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "image", "data": {"file": "abc.jpg"}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9003}},
                {"type": "text", "data": {"text": "这是啥"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "（他引用了你之前说的「一张图」）\n这是啥")

        # face / mface：翻译成标签后有文本了 -> 写标签，且都取自 NapCat 映射表
        cases = [
            ({"type": "face", "data": {"id": 179}}, "[doge]"),          # 狗头
            ({"type": "face", "data": {"id": 34}}, "[晕]"),             # 无语
            ({"type": "face", "data": {"file": "abc.jpg"}}, "[表情]"),   # 畸形 face：无 id 降级
            ({"type": "mface", "data": {"summary": "比心"}}, "[比心]"),  # 商城表情
            ({"type": "mface", "data": {"file": "abc.jpg"}}, "[大表情]"),  # 取不到名降级
        ]
        for quoted, expected in cases:
            with self.subTest(quoted=quoted):
                async with self._connected({
                    "get_msg": _getmsg(20002, [quoted])
                }) as (c, got, ws):
                    ws.feed(_msg_event([
                        {"type": "reply", "data": {"id": 9003}},
                        {"type": "text", "data": {"text": "这是啥"}},
                    ]))
                    self.assertTrue(await _wait_for(lambda: len(got) == 1))
                    self.assertEqual(
                        got[0][0], f"（他引用了你之前说的「{expected}」）\n这是啥"
                    )

    async def test_reply_mixed_text_and_image_keeps_text(self):
        """被引是"文本+图片"混排 -> 保留文本部分，只有真的没有文本才写「一张图」"""
        async with self._connected({
            "get_msg": _getmsg(20002, [
                {"type": "image", "data": {"file": "sunset.jpg"}},
                {"type": "text", "data": {"text": "今天的晚霞"}},
            ])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9012}},
                {"type": "text", "data": {"text": "好看"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "（他引用了你之前说的「今天的晚霞」）\n好看")

    async def test_reply_truncated_to_80(self):
        """被引文本超长时截断到 80 字（她发的消息）"""
        long_txt = "话" * 200
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "text", "data": {"text": long_txt}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9004}},
                {"type": "text", "data": {"text": "?"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            text = got[0][0]
            self.assertIn("话" * 80, text)
            self.assertNotIn("话" * 81, text)

    async def test_reply_from_owner_truncated_to_80(self):
        """引用机主自己的长消息同样截断到 80 字（与"她发的消息"分支一致）"""
        long_txt = "嗯" * 200
        async with self._connected({
            "get_msg": _getmsg(10001, [{"type": "text", "data": {"text": long_txt}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9013}},
                {"type": "text", "data": {"text": "就这样"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            text = got[0][0]
            self.assertIn("嗯" * 80, text)
            self.assertNotIn("嗯" * 81, text)

    async def test_reply_with_text_segments_multiple(self):
        """被引消息含多段文本，应拼接"""
        async with self._connected({
            "get_msg": _getmsg(20002, [
                {"type": "text", "data": {"text": "第一段"}},
                {"type": "text", "data": {"text": "第二段"}},
            ])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9005}},
                {"type": "text", "data": {"text": "嗯"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "（他引用了你之前说的「第一段第二段」）\n嗯")

    async def test_getmsg_failure_degrades(self):
        """get_msg 返回错误 -> 静默降级为无引用，不报错不阻塞"""
        async with self._connected({
            "get_msg": {"status": "failed", "retcode": 100, "wording": "no perm"}
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9006}},
                {"type": "text", "data": {"text": "在吗"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "在吗")

    async def test_getmsg_timeout_degrades(self):
        """get_msg 超时（不回包）-> 降级，且总耗时受 2s 超时约束"""
        async with self._connected(drop=True) as (c, got, ws):
            start = asyncio.get_running_loop().time()
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9007}},
                {"type": "text", "data": {"text": "在吗"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1, timeout=6.0))
            elapsed = asyncio.get_running_loop().time() - start
            self.assertEqual(got[0][0], "在吗")
            self.assertLess(elapsed, 5.0, "不得无限阻塞主流程")
            self.assertFalse(c._pending_echoes, "超时后不得残留 pending echo")

    async def test_reply_without_id_degrades(self):
        """reply 段没有 id -> 直接降级"""
        async with self._connected({}) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {}},
                {"type": "text", "data": {"text": "测试"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "测试")

    async def test_reply_empty_content_degrades(self):
        """get_msg 取回的消息里什么都没有 -> 用兜底描述，不编造内容"""
        async with self._connected({"get_msg": _getmsg(20002, [])}) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9014}},
                {"type": "text", "data": {"text": "?"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "（他引用了你的一条消息）\n?")

    async def test_reply_only_no_text(self):
        """只有引用段没有文本段时，仍应产出引用上下文"""
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "text", "data": {"text": "晚安"}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([{"type": "reply", "data": {"id": 9008}}]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "（他引用了你之前说的「晚安」）")

    async def test_plain_text_unaffected(self):
        """没有 reply 段时行为完全不变（回归保护）"""
        async with self._connected({}) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "text", "data": {"text": "晚上"}},
                {"type": "text", "data": {"text": "吃什么"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "晚上吃什么")

    async def test_other_user_message_ignored(self):
        """非机主的私聊消息在读循环入口就被忽略（回归保护）"""
        async with self._connected({}) as (c, got, ws):
            ws.feed(_msg_event([{"type": "text", "data": {"text": "在吗"}}], user_id=20002))
            await asyncio.sleep(0.1)
            self.assertEqual(got, [])

    async def test_no_send_reply_action(self):
        """本期不做她主动发引用：不得调用 send_msg / send_private_msg"""
        async with self._connected({
            "get_msg": _getmsg(20002, [{"type": "text", "data": {"text": "x"}}])
        }) as (c, got, ws):
            ws.feed(_msg_event([
                {"type": "reply", "data": {"id": 9009}},
                {"type": "text", "data": {"text": "y"}},
            ]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            actions = [p.get("action") for p in c._ws.sent]
            self.assertNotIn("send_msg", actions)
            self.assertNotIn("send_private_msg", actions)
            self.assertEqual(actions, ["get_msg"])


# ==========================================================
# 任务 2：聚合窗口调参 + 图片进缓冲（aggregator.py）
# ==========================================================


class TestAggregationSpec(unittest.TestCase):
    """真实常量与 docstring 校验（不打桩）"""

    def test_aggregation_constants(self):
        """任务书规格：静默窗 3.0->6.0，硬上限 8.0->15.0"""
        import companion.aggregator as aggmod
        self.assertEqual(aggmod.SILENCE_WINDOW, 6.0)
        self.assertEqual(aggmod.HARD_LIMIT, 15.0)

    def test_docstring_synced(self):
        """模块 docstring 第 2 行必须写新参数，且不得残留旧值"""
        import companion.aggregator as aggmod
        line2 = aggmod.__doc__.strip().splitlines()[1]
        self.assertIn("6 秒", line2)
        self.assertIn("15 秒", line2)
        self.assertNotIn("3 秒", line2)
        self.assertNotIn("8 秒", line2)
        self.assertNotIn("即刻触发", line2)


class TestAggregatorBuffering(unittest.IsolatedAsyncioTestCase):
    """行为测试用打桩后的时间常量；真实常量由 test_aggregation_constants 单独钉住。"""

    async def asyncSetUp(self):
        import companion.aggregator as aggmod
        from companion.aggregator import MessageAggregator

        # 真实值留档，然后把窗口缩到毫秒级以便测试
        self.real_silence = aggmod.SILENCE_WINDOW
        self.real_hard = aggmod.HARD_LIMIT
        aggmod.SILENCE_WINDOW = 0.12
        aggmod.HARD_LIMIT = 0.50
        self.addCleanup(self._restore, aggmod)

        self.turns = []

        # FIXES21：聚合器多送第三个参数（本轮批次的编号+message_id）。
        # 这里收下来但只断言前两个，批次内容由 tests/test_fixes21.py 专门验。
        async def handler(text, img, batch=None):
            self.turns.append((text, img, batch))

        self.agg = MessageAggregator(turn_handler=handler)
        self.agg.start()

    @staticmethod
    def _restore(mod):
        mod.SILENCE_WINDOW = 6.0
        mod.HARD_LIMIT = 15.0

    async def asyncTearDown(self):
        self.agg.stop()
        self._restore(__import__("companion.aggregator", fromlist=["x"]))

    async def _settle(self):
        await asyncio.sleep(0.3)
        await self.agg._queue.join()

    def test_aggregation_constants_patched_marker(self):
        """打桩窗口在测试期间生效（本类其余用例依赖此前提）"""
        import companion.aggregator as aggmod
        self.assertEqual(aggmod.SILENCE_WINDOW, 0.12)
        self.assertEqual(aggmod.HARD_LIMIT, 0.50)

    async def test_two_texts_merge_into_one_turn(self):
        await self.agg.push_message("第一句")
        await asyncio.sleep(0.02)
        await self.agg.push_message("第二句")
        await self._settle()
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.turns[0][0], "第一句\n第二句")
        self.assertIsNone(self.turns[0][1])

    async def test_image_no_longer_triggers_immediately(self):
        """图片不再即刻提交：静默窗内不得产生任何轮次"""
        await self.agg.push_message("你看这个", "data/x.jpg")
        await asyncio.sleep(0.03)
        self.assertEqual(len(self.turns), 0, "图片不得立即触发独立轮次")
        self.assertEqual(len(self.agg._queue._queue), 0)

    async def test_text_and_image_merge_into_one_turn(self):
        """任务书核心用例：文本+图片在窗口内合并为同一轮"""
        await self.agg.push_message("你看这个")
        await asyncio.sleep(0.02)
        await self.agg.push_message("", "data/x.jpg")
        await self._settle()
        self.assertEqual(len(self.turns), 1)
        # FIXES21：多第三个参数（本轮批次），前两个语义一个字没变
        text, img, batch = self.turns[0]
        self.assertEqual(text, "你看这个")
        self.assertEqual(img, "data/x.jpg")
        self.assertEqual([b["index"] for b in batch], [1, 2])
        self.assertTrue(batch[1]["has_image"])

    async def test_image_only_still_submits(self):
        await self.agg.push_message("", "data/only.jpg")
        await self._settle()
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.turns[0][0], "")
        self.assertEqual(self.turns[0][1], "data/only.jpg")

    async def test_image_does_not_reset_first_msg_time(self):
        """图片不重置首条计时：连续来图不会无限拖延，硬上限仍然兜底"""
        loop = asyncio.get_running_loop()
        await self.agg.push_message("先说一句")
        first = self.agg._first_msg_time
        self.assertNotEqual(first, 0.0)

        await self.agg.push_message("", "a.jpg")
        await self.agg.push_message("", "b.jpg")
        await self.agg.push_message("", "c.jpg")
        self.assertEqual(self.agg._first_msg_time, first,
                         "图片不得重置首条计时")
        # 三张图连续灌入，仍应在硬上限内被冲掉
        await asyncio.sleep(0.6)
        await self.agg._queue.join()
        self.assertGreaterEqual(len(self.turns), 1)

    async def test_hard_limit_flushes(self):
        """超过硬上限立即提交"""
        self.agg._first_msg_time = asyncio.get_running_loop().time() - 99.0
        await self.agg.push_message("超时了")
        await self.agg._queue.join()
        self.assertEqual(len(self.turns), 1)
        self.assertEqual(self.turns[0][0], "超时了")

    async def test_buffer_cleared_after_flush(self):
        await self.agg.push_message("第一轮")
        await self._settle()
        self.assertEqual(self.agg._text_buffer, [])
        self.assertIsNone(self.agg._image_buffer)
        self.assertEqual(self.agg._first_msg_time, 0.0)

    async def test_turn_handler_receives_batch(self):
        """turn_handler 签名：**FIXES21 起有意从 (text, image_path) 变成三元组**。

        第三个参数是本轮聚合批次（编号 + message_id + 文本），发侧引用回复
        靠它把 `[quote:N]` 翻译成真实 message_id。前两个参数的语义一字未变。
        批次内容本身由 tests/test_fixes21.py 专门验，这里只钉住"接口形状"。
        """
        await self.agg.push_message("带图", "z.jpg")
        await self._settle()
        self.assertEqual(len(self.turns), 1)
        self.assertIsInstance(self.turns[0], tuple)
        self.assertEqual(len(self.turns[0]), 3)
        _text, _img, batch = self.turns[0]
        self.assertIsInstance(batch, list)
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0]["index"], 1)

    async def test_last_image_wins(self):
        await self.agg.push_message("看", "a.jpg")
        await self.agg.push_message("", "b.jpg")
        await self._settle()
        self.assertEqual(self.turns[0][1], "b.jpg")

    async def test_empty_push_is_noop(self):
        await self.agg.push_message("")
        await self.agg.push_message("", None)
        await asyncio.sleep(0.2)
        self.assertEqual(len(self.turns), 0)

# ==========================================================
# 任务 5 联动：chat_style.plain_examples（日常废话流基线）注入支持
# ==========================================================


class TestChatStylePlainExamples(unittest.TestCase):
    """角色卡可选字段解析：缺省为空列表，向后兼容 characters/example 模板"""

    def test_example_card_without_field_defaults_empty(self):
        from companion.persona import Persona
        persona = Persona.load("characters/example")
        self.assertEqual(persona.chat_style.plain_examples, [])

    def test_field_is_loaded_when_present(self):
        import json
        import tempfile

        from companion.persona import Persona

        card = {
            "name": "测试",
            "user_address": "你",
            "core_description": "",
            "chat_style": {
                "rules": [],
                "good_examples": [],
                "bad_examples": [],
                "plain_examples": ["在干嘛", "吃饭", "麻辣烫，银泉那家"],
            },
            "stages": [
                {"name": f"阶段{i}", "tone": "", "instructions": [], "examples": []}
                for i in range(10)
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "character.json"), "w", encoding="utf-8") as f:
                json.dump(card, f, ensure_ascii=False)
            persona = Persona.load(tmp)

        self.assertEqual(
            persona.chat_style.plain_examples, ["在干嘛", "吃饭", "麻辣烫，银泉那家"]
        )


class TestPlainExamplesInjection(unittest.IsolatedAsyncioTestCase):
    """assembler 注入：无字段时组装结果与现状一致，有字段时块正确出现"""

    async def asyncSetUp(self):
        from helpers import make_db, make_engine_stack

        self.db = await make_db(":memory:")
        self.stack = make_engine_stack(self.db, "characters/example")

    async def asyncTearDown(self):
        from helpers import close_db

        await close_db(self.db)

    async def test_example_card_without_field_renders_no_block(self):
        """无 plain_examples 字段：整块（含标题）不渲染，与旧版组装结果逐字节一致"""
        prompt = await self.stack.assembler.assemble_system_prompt("在吗")
        self.assertNotIn("日常废话流基线", prompt)

        cs = self.stack.persona.chat_style
        bad_examples = "\n  - " + "\n  - ".join(cs.bad_examples)
        chat_rules = "\n  - " + "\n  - ".join(cs.rules)
        # 旧模板里「错误示范」段之后紧接「她的具体说话习惯」，中间不得插入任何内容
        self.assertIn(f"错误示范（禁止）：{bad_examples}\n  她的具体说话习惯：{chat_rules}", prompt)

    async def test_plain_examples_block_rendered_when_present(self):
        examples = ["在干嘛", "吃饭", "吃的啥", "[sticker]", "嗯，睡吧"]
        self.stack.persona.chat_style.plain_examples = list(examples)

        prompt = await self.stack.assembler.assemble_system_prompt("在吗")

        self.assertIn("日常废话流基线", prompt)
        for ex in examples:
            self.assertIn(f"  - {ex}", prompt)
        # 注入位置：正确示范之后、错误示范之前——基线是要鼓励的语气，
        # 挂在"错误示范（禁止）"下面会被读成禁止内容
        self.assertLess(prompt.index("正确示范"), prompt.index("日常废话流基线"))
        self.assertLess(prompt.index("日常废话流基线"), prompt.index("错误示范（禁止）"))
        self.assertLess(prompt.index("错误示范（禁止）"), prompt.index("她的具体说话习惯"))


if __name__ == "__main__":
    unittest.main()
