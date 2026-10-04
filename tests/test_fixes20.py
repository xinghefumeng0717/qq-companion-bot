"""FIXES20 QQ 系统表情双向测试集 (tests/test_fixes20.py)

两件一件事，各有各的病：

**收侧（bug 级感知盲区）**：`onebot._process_incoming_message` 原本只取 text 段，
face（QQ 小黄脸）段被**静默丢弃**——机主发"你真棒[旺柴]"，她只收到"你真棒"，
语气全断。他的语料里 22.5% 的消息带表情标签（[晕]×468/[捂脸]×367/[旺柴]×142），
这个盲区每天都在生效。本测试集盯住：保序、纯脸不空串、未知 id 降级、mface 优先，
以及"下游对空文本的假设"会不会被这条新链路踩到。

**发侧（新能力）**：她输出 `[face:标签]`，机制层硬上限每轮 ≤2、白名单校验、
同行"文字+脸"合并成**同一个**气泡、跨行不合并、清单外降级成文字不丢内容。

**版本闸**：提示词清单里每个标签都必须能在 NapCat 表里反查到 id，且不在
QQ 隐藏集里——发不出的 id 会被 NapCat 静默丢整段（issue #1987）。

**测量工具自检**：duo_sim 是 B 段裁判，它自己也会出错（combo 段被漏采、
零使用自动 PASS），所以这两块也在这里验，不留给冒烟现场翻车。

全部本地构造文本，零真实 API 调用。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

from companion.affection import AffectionEngine
from companion.aggregator import MessageAggregator
from companion.config import OneBotConfig, ReplyConfig
from companion.db import Database
from companion.faces import (
    QQ_FACE_HIDDEN,
    QQ_FACE_ID_BY_NAME,
    QQ_FACE_TAGS,
    face_id_by_name,
    face_tag_by_id,
    is_sendable_face_id,
    mface_tag,
    segment_face_tag,
)
from companion.main import CompanionBot
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.onebot import OneBotClient, build_face_segment
from companion.persona import Persona
from companion.prompts import (
    FACE_LEXICON_BLOCK,
    PROMPT_FACE_LIST,
    PROMPT_FACE_TAGS,
    PROACTIVE_GENERATE_PROMPT,
    SYSTEM_PROMPT_TEMPLATE,
)
from companion.replier import (
    FACE_MAX_PER_TURN,
    Replier,
    chunk_text_weight,
    face_chunk,
    keep_face_cap,
    merge_face_chunks,
    normalize_face_markers,
    strip_face_markers,
)

# 真实角色卡路径：下游回归（observer/记忆）要用真引擎堆，
# 不能拿 None 占位——那样只能验出"参数没传对"，验不到"纯标签会不会炸"。
_QINGZI = os.path.join("characters", "qingzi")


async def _async_return(value: str):
    return value

# ==========================================
# 夹具
# ==========================================


class _StubStickers:
    """假表情包管理器：按描述词回一个假路径（不落盘）"""

    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫"]


def _replier() -> Replier:
    return Replier(ReplyConfig(), _StubStickers())


def _face_seg(face_id: Any) -> Dict[str, Any]:
    return {"type": "face", "data": {"id": face_id}}


def _text_seg(text: str) -> Dict[str, Any]:
    return {"type": "text", "data": {"text": text}}


class _FakeWS:
    """极简服务端替身：能投帧、能收发（够 OneBot 收发两路用）"""

    def __init__(self, client: "OneBotClient", responses: Dict[str, Any] | None = None):
        import asyncio as _a

        self._inbound: _a.Queue = _a.Queue()
        self.closed = False
        self.client = client
        self.responses = responses or {}
        self.sent_payloads: List[Dict[str, Any]] = []

    def feed(self, frame):
        self._inbound.put_nowait(json.dumps(frame))

    async def receive(self):
        return await self._inbound.get()

    async def close(self):
        self.closed = True

    async def send_str(self, payload: str):
        """action 请求：按 action 查表回一包；send_msg 记录报文供发送结构断言"""
        data = json.loads(payload)
        action = data.get("action")
        if action == "send_msg":
            self.sent_payloads.append(data)
        reply = self.responses.get(action)
        if reply is not None:
            self._inbound.put_nowait(json.dumps({"echo": data.get("echo"), **reply}))


async def _read_loop(client, ws):
    while True:
        frame = await ws.receive()
        await client._handle_raw_message(frame)


async def _wait_for(predicate, timeout=1.5) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


def _msg_event(message, user_id=10001, message_type="private"):
    return {
        "post_type": "message",
        "message_type": message_type,
        "user_id": user_id,
        "message": message,
    }


class _ConnectedCase(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def _connected(self, responses=None):
        cfg = OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token="")
        got: List[Any] = []

        # FIXES21：回调多第三个参数 message_id
        async def cb(text, img, message_id=None):
            got.append((text, img))

        c = OneBotClient(
            config=cfg,
            allowed_user_id=10001,
            on_message_callback=cb,
            image_save_dir="data/test_fixes20_imgs",
        )
        c._ws = _FakeWS(c, responses)
        c._running = True
        reader = asyncio.create_task(_read_loop(c, c._ws))
        try:
            yield c, got, c._ws
        finally:
            reader.cancel()
            await asyncio.wait({reader}, timeout=2.0)
            await c.stop()
            if c._dispatcher_task is not None:
                await asyncio.wait({c._dispatcher_task}, timeout=2.0)


# ==========================================
# 1. 映射表本身（任务1）
# ==========================================


class TestFaceTable(unittest.TestCase):
    def test_全集规模与id范围(self):
        """NapCat sysface 全集 329 条，id 跨度 0~484（中间有空洞）。"""
        self.assertEqual(len(QQ_FACE_TAGS), 329)
        self.assertEqual(min(QQ_FACE_TAGS), 0)
        self.assertGreater(max(QQ_FACE_TAGS), 400)
        # 有空洞：315 不存在——照抄"连续数组"或线性公式的实现必错
        self.assertNotIn(315, QQ_FACE_TAGS)

    def test_任务书点名的id逐个核对(self):
        """任务书给的 12 个高频 id 与实表一致（防止手抄错位）。"""
        for face_id, name in (
            (34, "晕"), (264, "捂脸"), (28, "憨笑"), (5, "流泪"),
            (355, "耶"), (0, "惊讶"), (179, "doge"), (124, "OK"),
            (182, "笑哭"), (271, "吃瓜"), (76, "赞"), (319, "比心"),
            (107, "快哭了"), (111, "可怜"), (263, "沧桑"),
            (13, "呲牙"), (178, "斜眼笑"),
        ):
            with self.subTest(face_id=face_id):
                self.assertEqual(QQ_FACE_TAGS[face_id], name)

    def test_微信专属名不在QQ表里(self):
        """他的微信高频名在 QQ 侧查不到——所以发侧清单必须用 QQ 名。"""
        for name in ("旺柴", "好的", "苦涩", "破涕为笑", "嘿哈", "机智"):
            with self.subTest(name=name):
                self.assertIsNone(face_id_by_name(name))

    def test_重名取低位canonical(self):
        """450~457 是 1~8 的另一套渲染，反查取低位。"""
        self.assertEqual(face_id_by_name("撇嘴"), 1)
        self.assertEqual(face_id_by_name("微笑"), 14)
        self.assertEqual(face_id_by_name("睡"), 8)

    def test_未知id降级不炸(self):
        for bad in (None, "", "abc", 315, 99999, -1):
            with self.subTest(bad=bad):
                self.assertEqual(face_tag_by_id(bad), "[表情]")

    def test_标签名反查容错空白与大小写(self):
        self.assertEqual(face_id_by_name("  doge  "), 179)
        self.assertEqual(face_id_by_name("DOGE"), 179)
        self.assertIsNone(face_id_by_name("不存在"))
        self.assertIsNone(face_id_by_name(""))

    def test_mface取summary优先_否则降级(self):
        self.assertEqual(mface_tag({"summary": "比心"}), "[比心]")
        self.assertEqual(mface_tag({"emoji_id": "123"}), "[大表情]")
        self.assertIsNone(mface_tag({"url": "x.jpg"}), "普通图片不该被当表情")
        self.assertIsNone(mface_tag(None))
        self.assertIsNone(mface_tag("不是字典"))

    def test_隐藏集与可发送闸(self):
        self.assertEqual(len(QQ_FACE_HIDDEN), 110)
        self.assertTrue(is_sendable_face_id(179))
        self.assertFalse(is_sendable_face_id(222), "抱抱(222) 是隐藏项")
        self.assertFalse(is_sendable_face_id(315), "315 根本不存在")
        self.assertFalse(is_sendable_face_id(None))


# ==========================================
# 2. 收侧：face/mface 段进文本流（任务2）
# ==========================================


class TestReceiveSideSegments(unittest.TestCase):
    def test_face段翻译且保序(self):
        """文字+face+文字 混排必须按原始顺序拼。"""
        msg = {"message": [_text_seg("你真棒"), _face_seg(179), _text_seg("，不许骄傲")]}
        self.assertEqual(
            OneBotClient._extract_message_text(msg), "你真棒[doge]，不许骄傲"
        )

    def test_纯face消息不空串(self):
        """纯脸消息过去是空串（等于消息被吞），现在必须有内容。"""
        self.assertEqual(OneBotClient._extract_message_text({"message": [_face_seg(34)]}), "[晕]")

    def test_连续多个face保序(self):
        msg = {"message": [_face_seg(5), _face_seg(5), _text_seg("别哭了")]}
        self.assertEqual(OneBotClient._extract_message_text(msg), "[流泪][流泪]别哭了")

    def test_未知id降级(self):
        self.assertEqual(OneBotClient._extract_message_text({"message": [_face_seg(777)]}), "[表情]")

    def test_mface与商城大表情image段(self):
        """NapCat 收侧把 mface 转成 image 段（带 summary/emoji_id），两种形态都要认。"""
        as_mface = {"message": [{"type": "mface", "data": {"summary": "比心"}}]}
        as_image = {"message": [{"type": "image", "data": {"url": "x", "summary": "比心"}}]}
        self.assertEqual(OneBotClient._extract_message_text(as_mface), "[比心]")
        self.assertEqual(OneBotClient._extract_message_text(as_image), "[比心]")
        # 取不到名字的降级
        self.assertEqual(
            OneBotClient._extract_message_text({"message": [{"type": "mface", "data": {}}]}),
            "[大表情]",
        )

    def test_普通图片不受影响(self):
        """没有 summary/emoji_id 的 image 段照旧不产生任何文字。"""
        msg = {"message": [{"type": "image", "data": {"url": "x.jpg"}}, _text_seg("看这个")]}
        self.assertEqual(OneBotClient._extract_message_text(msg), "看这个")

    def test_字符串形态消息不受影响(self):
        self.assertEqual(OneBotClient._extract_message_text({"message": "在呢"}), "在呢")

    def test_引用消息描述带标签(self):
        msg = {"message": [_text_seg("你真棒"), _face_seg(179)]}
        self.assertEqual(
            OneBotClient._describe_quoted_message(msg), "你真棒[doge]"
        )

    def test_被引纯脸消息有标签(self):
        self.assertEqual(OneBotClient._describe_quoted_message({"message": [_face_seg(34)]}), "[晕]")

    def test_被引真照片仍是一张图(self):
        """`_quoted_media_desc` 的归类一个字没动：照片照旧「一张图」。"""
        msg = {"message": [{"type": "image", "data": {"file": "a.jpg"}}]}
        self.assertEqual(OneBotClient._describe_quoted_message(msg), "一张图")

    def test_segment_face_tag只管表情(self):
        self.assertIsNone(segment_face_tag(_text_seg("在呢")))
        self.assertIsNone(segment_face_tag("不是字典"))
        self.assertIsNone(segment_face_tag({"type": "record", "data": {}}))
        self.assertEqual(segment_face_tag(_face_seg(264)), "[捂脸]")


class TestReceiveSideDownstream(_ConnectedCase):
    """下游空文本假设排查：聚合器 / 记忆 / observer 喂纯标签都不能炸。"""

    async def test_实时来消息的回调拿到标签(self):
        async with self._connected() as (c, got, ws):
            ws.feed(_msg_event([_text_seg("你真棒"), _face_seg(179)]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "你真棒[doge]")

    async def test_纯face消息也进回调(self):
        async with self._connected() as (c, got, ws):
            ws.feed(_msg_event([_face_seg(34)]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            self.assertEqual(got[0][0], "[晕]")

    async def test_商城大表情不再被当成照片下载(self):
        """大表情长得像图片，但它是脸：不该走"下载 + 视觉识图"那条路。"""
        async with self._connected() as (c, got, ws):
            ws.feed(_msg_event([{"type": "image", "data": {"url": "x", "summary": "比心"}}]))
            self.assertTrue(await _wait_for(lambda: len(got) == 1))
            text, img = got[0]
            self.assertEqual(text, "[比心]")
            self.assertIsNone(img, "大表情不该进图片缓冲区")

    async def test_聚合器不跳过纯标签消息(self):
        """过去纯脸消息文本为空 → 聚合器 `if text:` 直接丢弃、整轮对话不发生。"""
        turn_texts: List[str] = []

        async def handle_turn(user_text, image_path, batch=None):
            turn_texts.append(user_text)

        agg = MessageAggregator(turn_handler=handle_turn)
        agg.start()
        try:
            await agg.push_message("[旺柴]", None)
            self.assertTrue(
                await _wait_for(lambda: len(turn_texts) == 1, timeout=8.0),
                "纯表情消息被聚合器跳过了——收侧修复等于白做",
            )
            self.assertEqual(turn_texts[0], "[旺柴]")
        finally:
            agg.stop()

    async def test_记忆加固遇纯标签不炸(self):
        from helpers import close_db, make_db, make_engine_stack

        db = await make_db()
        try:
            stack = make_engine_stack(db, persona_path=_QINGZI)
            await stack.memory.reinforce_memories("[流泪]")
            await stack.memory.reinforce_memories("")  # 空串仍是合法的降级输入
        finally:
            await close_db(db)

    async def test_观察者能吃纯标签用户消息(self):
        """observer 的用户消息只做截断，喂纯标签必须整条结算跑完、不抛异常。

        用真实引擎堆（affection/mood/memory 都是真的）而不是 None 占位：
        observer 第一步就调 affection.update，拿 None 当引擎只能验出"我没传对参数"。
        """
        from helpers import close_db, make_db, make_mock_gateway

        db = await make_db()
        try:
            gw = make_mock_gateway()
            obs = Observer(
                gw,
                AffectionEngine(db, Persona.load(_QINGZI).initial_dims),
                MoodEngine(db),
                MemoryManager(db, gateway=gw, affection=None),
                _StubStickers(),
                db,
            )
            # 网关返回坏 JSON：observer 走失败兜底返回中性默认，但**不许抛**
            gw.chat = AsyncMock(return_value='{"bad json')
            data = await obs.settle_turn("[旺柴]", "在呢", None)
            self.assertIsInstance(data, dict)
        finally:
            await close_db(db)


# ==========================================
# 3. 发侧：解析 / 白名单 / 合并 / 上限（任务3）
# ==========================================


class TestFaceChunkValidation(unittest.TestCase):
    def test_清单内标签产出face段(self):
        c = face_chunk("doge")
        self.assertEqual(c, {"type": "face", "tag": "doge", "id": 179})

    def test_清单外标签返回None(self):
        for bad in ("微笑", "旺柴", "不存在", "", None, "  "):
            with self.subTest(bad=bad):
                self.assertIsNone(face_chunk(bad))

    def test_空标签不炸(self):
        self.assertIsNone(face_chunk(""))


class TestParseReplyFace(unittest.TestCase):
    def test_混排合并成同一个气泡(self):
        """"你真棒[face:doge]" 是一个气泡，不是两条。"""
        chunks, record = _replier().parse_reply("你真棒[face:doge]")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "combo")
        self.assertEqual(
            chunks[0]["parts"],
            [
                {"type": "text", "content": "你真棒"},
                {"type": "face", "tag": "doge", "id": 179},
            ],
        )
        self.assertEqual(record, "你真棒[face:doge]")

    def test_纯表情气泡(self):
        chunks, record = _replier().parse_reply("[face:流泪]")
        self.assertEqual([c["type"] for c in chunks], ["face"])
        self.assertEqual(chunks[0]["id"], 5)
        self.assertEqual(record, "[face:流泪]")

    def test_同款二连是同一个气泡(self):
        """他语料里 107 条同款二连，是**一条消息两个脸**而不是两个气泡。"""
        chunks, _ = _replier().parse_reply("[face:流泪][face:流泪]")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "combo")
        self.assertEqual([p["id"] for p in chunks[0]["parts"]], [5, 5])
        self.assertTrue(all(p["type"] == "face" for p in chunks[0]["parts"]))

    def test_跨行不合并(self):
        """换行是硬边界：文字气泡后紧跟纯表情气泡是两种发法，不能黏成一条。"""
        chunks, record = _replier().parse_reply("你真棒[face:doge]\n[face:吃瓜]")
        self.assertEqual([c["type"] for c in chunks], ["combo", "face"])
        self.assertEqual(record, "你真棒[face:doge]\n[face:吃瓜]")

    def test_三种发法各自可复现(self):
        """三种发法在**不同回复**里各自成立。

        注意别把它们塞进同一条回复去试：每轮硬上限是 2 个脸，
        混排(1) + 纯脸(1) + 同款二连(2) = 4 个，越限的会被降级成文字。
        真实语料里这三种也是跨消息分布的（"文字气泡后 60 秒内紧跟纯表情气泡" 245 次）。
        """
        # 混排句尾 + 纯脸气泡：一条回复里两个脸，恰好用满上限
        chunks, record = _replier().parse_reply("你真棒[face:doge]\n[face:吃瓜]")
        self.assertEqual([c["type"] for c in chunks], ["combo", "face"])
        self.assertEqual(record.count("\n"), 1)

        # 同款二连：另一条回复
        chunks, _ = _replier().parse_reply("[face:流泪][face:流泪]")
        self.assertEqual([c["type"] for c in chunks], ["combo"])

    def test_白名单外降级为文字且留在同一气泡(self):
        """微笑(14) 是黑名单：不能发出去，但也**不能把一句话拆成两个气泡**。

        DEEP_AUDIT B-5 口径变更（所有者拍板）：降级 = **剥掉标记只发正文**，
        `[face:微笑]` 这种字面量不再上屏（真实仿真已出现过 `[face:月亮]` 漏屏）。
        """
        chunks, record = _replier().parse_reply("你真棒[face:微笑]")
        self.assertEqual([c["type"] for c in chunks], ["text"])
        self.assertEqual(chunks[0]["content"], "你真棒")
        self.assertEqual(record, "你真棒")

    def test_微信名降级(self):
        """模型照着历史复读 [face:旺柴]（我们自己的记录形态不会这样，但它来了也得降级）。

        B-5 口径变更：剥掉标记后这条没有任何正文可发 → 零段（调用方的空段兜底接管），
        绝不许把 `[face:旺柴]` 字面量发到机主眼前。
        """
        chunks, record = _replier().parse_reply("[face:旺柴]")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_降级标记与合法脸可以共处一个气泡(self):
        """非法的剥标记、合法的照发，两者仍在同一个气泡里（B-5 口径变更）。"""
        chunks, _ = _replier().parse_reply("你真棒[face:微笑][face:doge]")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "combo")
        self.assertEqual(chunks[0]["parts"][0]["content"], "你真棒")
        self.assertEqual(chunks[0]["parts"][1]["tag"], "doge")

    def test_每轮硬上限2_第三个丢弃(self):
        """超限额的脸只有标记、没有正文可留 → 整段丢弃（B-5 口径变更）。

        旧行为是把它降级成 `[face:晕]` 字面量发上屏，那是本次要治的病。
        """
        chunks, record = _replier().parse_reply("你[face:doge][face:流泪][face:晕]")
        total = sum(
            1
            for c in chunks
            for p in ([c] if c["type"] == "face" else c.get("parts", []))
            if p["type"] == "face"
        )
        self.assertEqual(total, FACE_MAX_PER_TURN)
        self.assertNotIn("[face:晕]", record, "超限的脸不许以字面量上屏")
        self.assertNotIn("晕", record)
        self.assertIn("你", record, "她的正文一个字都不能少")

    def test_同一气泡内face不超过2(self):
        chunks, _ = _replier().parse_reply("你[face:doge][face:流泪][face:晕]")
        for c in chunks:
            if c["type"] == "combo":
                n = sum(1 for p in c["parts"] if p["type"] == "face")
                self.assertLessEqual(n, 2, f"同气泡 {n} 个脸，超了")

    def test_上限函数单独验(self):
        """上限函数：保留最靠前的 2 个，超限的**直接丢**（不再造 [face:x] 文字段）。"""
        chunks = [{"type": "face", "tag": str(i), "id": i} for i in range(5)]
        out = keep_face_cap(chunks)
        self.assertEqual([c["type"] for c in out], ["face", "face"])
        self.assertEqual([c["tag"] for c in out], ["0", "1"], "保留最靠前的两个")
        self.assertFalse(
            [c for c in out if c["type"] == "text"],
            "超限的脸不许降级成文字段（会造出空气泡/字面量）",
        )

    def test_与表情包同轮共存(self):
        """脸与表情包不互斥（一个语气一个图），且表情包语义一个字没改。"""
        chunks, record = _replier().parse_reply("在呢[sticker:猫猫][face:贴贴]")
        self.assertEqual([c["type"] for c in chunks], ["text", "sticker", "face"])
        self.assertIn("[表情:猫猫]", record)
        self.assertIn("[face:贴贴]", record)

    def test_表情包永不与脸合并(self):
        chunks, _ = _replier().parse_reply("[sticker:猫猫][face:贴贴]")
        self.assertEqual([c["type"] for c in chunks], ["sticker", "face"])
        self.assertNotIn("combo", [c["type"] for c in chunks])

    def test_中文冒号与大小写容错(self):
        chunks, _ = _replier().parse_reply("你真棒[FACE：doge]")
        self.assertEqual(chunks[0]["type"], "combo")
        self.assertEqual(chunks[0]["parts"][1]["tag"], "doge")

    def test_空标记不炸(self):
        chunks, record = _replier().parse_reply("你真棒[face:]")
        self.assertEqual([c["type"] for c in chunks], ["text"])
        self.assertEqual(record, "你真棒[face:]")

    def test_裸标签不被误转成脸(self):
        """她把读侧裸标签当文字回写（"你真棒[旺柴]"）→ **原样发文字，不自动转脸**。

        这是有意的护栏：收侧的裸标签形态在她那边是"读"（机主发来的），
        机制层擅自把它转成脸 = 替她决定"我本来想发一张狗头"；
        提示词里已明写"照抄 [旺柴] 会变成没人看得懂的方括号"来纠正写法，
        机制层不越权。
        """
        chunks, record = _replier().parse_reply("你真棒[旺柴]")
        self.assertEqual([c["type"] for c in chunks], ["text"])
        self.assertEqual(chunks[0]["content"], "你真棒[旺柴]")
        self.assertEqual(record, "你真棒[旺柴]")

    def test_提示词明写不许把裸标签当文字(self):
        from companion.prompts import FACE_USAGE_RULES

        self.assertIn("不要把 [标签] 这种写法当普通文字发出去", FACE_USAGE_RULES)

    def test_纯文字无脸不受影响(self):
        chunks, record = _replier().parse_reply("在呢")
        self.assertEqual(chunks, [{"type": "text", "content": "在呢", "_end_line": 0}])
        self.assertEqual(record, "在呢")

    def test_沉默权不受影响(self):
        chunks, record = _replier().parse_reply("[沉默]")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_内心旁白滤网与脸共存(self):
        """脸段不参与整行滤网（与 sticker 同构），文字段照旧被滤。"""
        chunks, record = _replier().parse_reply("在呢[face:贴贴]\n这人嘴硬，我折回去看看。")
        self.assertEqual([c["type"] for c in chunks], ["combo"])
        self.assertNotIn("折回去", record)
        self.assertIn("[face:贴贴]", record)

    def test_图片占位符滤网与脸共存(self):
        chunks, record = _replier().parse_reply("在呢[face:贴贴]\n[图片]")
        self.assertEqual([c["type"] for c in chunks], ["combo"])
        self.assertNotIn("[图片]", record)

    def test_两脸混排也不挤掉收尾文字(self):
        """max_chunks 限的是气泡数：合并后再算预算，不能把"在呢"这种收尾挤掉。

        B-5 口径变更：超限的第 3 个脸整段丢弃（不再降级成一个文字段），段序随之少一项。
        """
        raw = "你真棒[face:doge]\n[face:流泪][face:流泪]\n在呢[sticker:猫猫]"
        chunks, record = _replier().parse_reply(raw)
        self.assertIn("在呢", record)
        self.assertEqual(
            [c["type"] for c in chunks], ["combo", "face", "text", "sticker"]
        )


class TestFaceHelpers(unittest.TestCase):
    def test_merge_face_chunks_同行合并(self):
        out = merge_face_chunks([
            {"type": "text", "content": "在呢", "_end_line": 0},
            {"type": "face", "tag": "晕", "id": 34, "_line": 0, "_end_line": 0},
        ])
        self.assertEqual(out[0]["type"], "combo")

    def test_merge_face_chunks_跨行不合并(self):
        out = merge_face_chunks([
            {"type": "text", "content": "在呢", "_end_line": 0},
            {"type": "face", "tag": "晕", "id": 34, "_line": 1, "_end_line": 1},
        ])
        self.assertEqual([c["type"] for c in out], ["text", "face"])

    def test_normalize_只降级非法标记(self):
        """合法标记一个字节都不动；非法的**标记剥掉**、只留它前后的正文（B-5 口径）。"""
        src = "你[face:doge]好[face:微笑]呀"
        out = normalize_face_markers(src)
        self.assertIn("[face:doge]", out, "合法标记必须一个字节都不动")
        self.assertEqual(out, "你[face:doge]好呀")
        self.assertNotIn("[face:微笑]", out, "非法标记不许以字面量留在正文里")

    def test_normalize_无标记短路(self):
        self.assertEqual(normalize_face_markers("在呢"), "在呢")

    def test_strip_face_markers只抹face(self):
        self.assertEqual(strip_face_markers("在呢[face:doge]"), "在呢")
        self.assertEqual(strip_face_markers("[表情:猫猫]"), "[表情:猫猫]")

    def test_文字量折算(self):
        self.assertEqual(chunk_text_weight({"type": "text", "content": "在呢"}), 2)
        self.assertEqual(chunk_text_weight({"type": "face", "tag": "晕", "id": 34}), 5)
        self.assertEqual(
            chunk_text_weight({
                "type": "combo",
                "parts": [
                    {"type": "text", "content": "在呢"},
                    {"type": "face", "tag": "晕", "id": 34},
                ],
            }),
            7,
        )
        self.assertEqual(
            chunk_text_weight({"type": "sticker", "file": "/x.png"}), 5,
            "表情包折算口径不能被这次改动带偏",
        )

    def test_一个脸不触发长typing(self):
        """3 字短句挂一个脸：去掉标记后还是 3 字，打字时长不该翻倍。"""
        record = "在呢[face:doge]"
        self.assertEqual(strip_face_markers(record), "在呢")
        self.assertLess(len(strip_face_markers(record)), len(record))


# ==========================================
# 4. 发送结构（任务3 第5条 / 冒烟 C 段同一条路）
# ==========================================


class _RecordingOneBot:
    def __init__(self):
        self.sent: List[Dict[str, Any]] = []

    async def send_private_msg(self, user_id, message_segments, max_retries=2):
        self.sent.append({"user_id": user_id, "message": message_segments})
        return True


class _StubBot(CompanionBot):
    """绕开 __init__ 的整套装配，只借 _send_chunk_to_onebot 这一段逻辑"""

    def __init__(self):  # noqa: D107 - 故意不调 super()
        self.config = type("C", (), {"account": type("A", (), {"allowed_user_id": 42})()})()
        self.onebot = _RecordingOneBot()


class TestSendStructure(unittest.TestCase):
    def test_build_face_segment(self):
        self.assertEqual(build_face_segment(179), {"type": "face", "data": {"id": 179}})

    def test_combo拼成一条含文字与表情的消息(self):
        bot = _StubBot()
        chunk = {
            "type": "combo",
            "parts": [
                {"type": "text", "content": "你真棒"},
                {"type": "face", "tag": "doge", "id": 179},
            ],
        }
        asyncio.run(bot._send_chunk_to_onebot(chunk))
        self.assertEqual(len(bot.onebot.sent), 1, "混排必须是一条消息而不是两条")
        self.assertEqual(
            bot.onebot.sent[0]["message"],
            [
                {"type": "text", "data": {"text": "你真棒"}},
                {"type": "face", "data": {"id": 179}},
            ],
        )
        self.assertEqual(bot.onebot.sent[0]["user_id"], 42)

    def test_纯脸气泡只有face段(self):
        bot = _StubBot()
        asyncio.run(bot._send_chunk_to_onebot({"type": "face", "tag": "晕", "id": 34}))
        self.assertEqual(bot.onebot.sent[0]["message"], [{"type": "face", "data": {"id": 34}}])

    def test_纯脸二连是一条消息两个face段(self):
        bot = _StubBot()
        asyncio.run(bot._send_chunk_to_onebot({
            "type": "combo",
            "parts": [
                {"type": "face", "tag": "流泪", "id": 5},
                {"type": "face", "tag": "流泪", "id": 5},
            ],
        }))
        self.assertEqual(
            bot.onebot.sent[0]["message"],
            [{"type": "face", "data": {"id": 5}}, {"type": "face", "data": {"id": 5}}],
        )

    def test_文本与表情包旧行为不变(self):
        bot = _StubBot()
        asyncio.run(bot._send_chunk_to_onebot({"type": "text", "content": "在呢"}))
        self.assertEqual(bot.onebot.sent[0]["message"], [{"type": "text", "data": {"text": "在呢"}}])

    def test_未知类型照旧静默丢弃(self):
        bot = _StubBot()
        asyncio.run(bot._send_chunk_to_onebot({"type": "whatever"}))
        self.assertEqual(bot.onebot.sent, [])

    def test_空combo不白发空消息(self):
        bot = _StubBot()
        asyncio.run(bot._send_chunk_to_onebot({"type": "combo", "parts": []}))
        self.assertEqual(bot.onebot.sent, [])


# ==========================================
# 5. 提示词：清单 / 纪律 / 读侧词表 / 版本闸（任务4）
# ==========================================


class TestFacePrompt(unittest.TestCase):
    def test_清单规模在拍板区间(self):
        self.assertGreaterEqual(len(PROMPT_FACE_LIST), 25)
        self.assertLessEqual(len(PROMPT_FACE_LIST), 29)
        self.assertEqual(len(PROMPT_FACE_TAGS), len(PROMPT_FACE_LIST), "不允许重名")

    def test_黑名单微笑永不进清单(self):
        """年轻人语境里 [微笑] 等于"呵呵/嘲讽"，会让语气变冷。"""
        self.assertNotIn("微笑", PROMPT_FACE_TAGS)
        self.assertNotIn("微笑", FACE_LEXICON_BLOCK)

    def test_他的高频12个都有替身或同名(self):
        for name in ("晕", "捂脸", "憨笑", "流泪", "耶", "惊讶",
                     "doge", "OK", "沧桑", "笑哭", "呲牙", "斜眼笑"):
            with self.subTest(name=name):
                self.assertIn(name, PROMPT_FACE_TAGS)

    def test_每项都有含义标注(self):
        for name, meaning in PROMPT_FACE_LIST:
            with self.subTest(name=name):
                self.assertTrue(meaning.strip(), f"{name} 缺含义标注")
                self.assertLessEqual(len(meaning), 40, "太长会把提示词撑爆")

    def test_版本闸_每项都发得出去(self):
        """清单里每个标签都能反查到 id，且不在 QQ 隐藏集里。

        这条是 NapCat issue #1987 的防线：发不出的 id 会被**静默丢整段**，
        症状是"她明明要发表情，屏幕上什么都没有"，最难查。
        """
        for name, _ in PROMPT_FACE_LIST:
            with self.subTest(name=name):
                face_id = face_id_by_name(name)
                self.assertIsNotNone(face_id, f"{name} 在 NapCat 表里反查不到")
                self.assertNotIn(
                    face_id, QQ_FACE_HIDDEN, f"{name}({face_id}) 是 QQ 隐藏项，发出去有被丢段风险"
                )
                self.assertTrue(is_sendable_face_id(face_id))

    def test_版本闸_被剔除项有据可查(self):
        """任务书点名的加油(315) 在 NapCat 表里不存在，替身已换上。"""
        self.assertNotIn(315, QQ_FACE_TAGS)
        self.assertIn("你真棒棒", PROMPT_FACE_TAGS, "加油语义的替身必须在清单里")

    def test_读侧词表覆盖全清单(self):
        for name, _ in PROMPT_FACE_LIST:
            with self.subTest(name=name):
                self.assertIn(f"[{name}]＝", FACE_LEXICON_BLOCK)

    def test_系统模板含纪律与清单(self):
        from companion.prompts import FACE_PROMPT_BLOCK

        self.assertIn("{face_block}", SYSTEM_PROMPT_TEMPLATE, "模板必须留注入点")
        for kw in ("偶尔", "[face:标签名]", "只写清单内的标签名", "他消息里"):
            with self.subTest(kw=kw):
                self.assertIn(kw, FACE_PROMPT_BLOCK)
        for name, _ in PROMPT_FACE_LIST[:3]:
            self.assertIn(f"[{name}]＝", FACE_PROMPT_BLOCK, "清单必须真的进了提示词段")

    def test_主动消息模板同样含纪律(self):
        self.assertIn("{face_block}", PROACTIVE_GENERATE_PROMPT)

    def test_纪律三条要点都在(self):
        from companion.prompts import FACE_USAGE_RULES

        for kw in ("偶尔", "最多发两个", "只写"):
            with self.subTest(kw=kw):
                self.assertIn(kw, FACE_USAGE_RULES)

    def test_读侧说明提到他消息里的标签(self):
        from companion.prompts import FACE_USAGE_RULES

        self.assertIn("他消息里", FACE_USAGE_RULES)


class TestFaceTagReadSideForObserver(unittest.TestCase):
    """DEEP_AUDIT 面 A-9：读侧词表以前只加在主聊提示词上，observer / 日记拿不到。

    收侧把 face 段翻成 [晕] 后原样喂给 observer（turn_handler 不做预处理），
    没有词表时纯表情轮次会被"全程无实质内容 0~1"的锚点系统性倒扣。
    """

    def test_observer系统提示词含词表说明(self):
        from companion.prompts import FACE_TAG_READ_NOTICE, OBSERVER_SYSTEM_PROMPT

        for kw in ("[xx]", "QQ 系统表情标签", "不是他说出的文字", "纯表情"):
            with self.subTest(kw=kw):
                self.assertIn(kw, OBSERVER_SYSTEM_PROMPT)
        # 同源：整份 FACE_LEXICON_BLOCK 直接被引用进来，不是另抄一份词表
        self.assertIn(FACE_LEXICON_BLOCK, OBSERVER_SYSTEM_PROMPT)
        self.assertIn(FACE_TAG_READ_NOTICE, OBSERVER_SYSTEM_PROMPT)

    def test_日记系统提示词含同款说明(self):
        from companion.prompts import FACE_TAG_READ_NOTICE, DIARY_SYSTEM_PROMPT

        self.assertIn(FACE_TAG_READ_NOTICE, DIARY_SYSTEM_PROMPT)
        self.assertIn(FACE_LEXICON_BLOCK, DIARY_SYSTEM_PROMPT)


class TestFaceTagReadSideInObserverMessages(unittest.IsolatedAsyncioTestCase):
    async def test_纯标签轮次的observer报文带词表(self):
        """输入 [晕][晕]：observer 实际发给模型的 system 里必须有词表说明（防回归）。"""
        from helpers import close_db, make_db, make_mock_gateway

        from companion.prompts import FACE_LEXICON_BLOCK

        db = await make_db()
        try:
            gw = make_mock_gateway()
            seen: Dict[str, Any] = {}

            async def _capture(**kwargs: Any) -> str:
                seen.update(kwargs)
                return json.dumps(
                    {
                        "self_disclosure": 4.0,
                        "responsiveness": 4.0,
                        "warmth_score": 4.0,
                        "resonance": 4.0,
                        "moments": [],
                        "mood_impact": {"v": 0.0, "a": 0.0, "trust": 0.0},
                        "facts": [],
                        "updated_facts": [],
                        "followups": [],
                        "done_followups": [],
                        "collect_sticker": False,
                        "sticker_name": "",
                        "user_state": "平静",
                    }
                )

            gw.chat = AsyncMock(side_effect=_capture)
            obs = Observer(
                gw,
                AffectionEngine(db, Persona.load(_QINGZI).initial_dims),
                MoodEngine(db),
                MemoryManager(db, gateway=gw, affection=None),
                _StubStickers(),
                db,
            )
            await obs.settle_turn("[晕][晕]", "现在知道晕了", None)
        finally:
            await close_db(db)

        messages = seen["messages"]
        self.assertIn("[晕][晕]", messages[1]["content"], "机主消息仍原样进报文（不改输入）")
        self.assertIn(FACE_LEXICON_BLOCK, messages[0]["content"], "system 必须带同源词表说明")
        for kw in ("[xx]", "不是他说出的文字", "纯表情", "不要按 0~1 分处理"):
            with self.subTest(kw=kw):
                self.assertIn(kw, messages[0]["content"])


# ==========================================
# 6. 测量工具自检：duo_sim（它是 B 段裁判，自己先验一遍）
# ==========================================


class TestSimMeasurement(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import duo_sim as D

        cls.D = D

    def test_combo段被摊平不被漏采(self):
        """老代码只认 type=="text"：combo 里的文字会被整段漏掉，指标全偏空。"""
        chunks, _ = _replier().parse_reply("你真棒[face:doge]\n在呢")
        c = self.D.collect_sent_chunks(chunks)
        self.assertIn("你真棒[doge]", c["bubbles"])
        self.assertIn("在呢", c["bubbles"])
        self.assertEqual(c["faces"], ["doge"])

    def test_纯脸与表情包也进账(self):
        chunks, _ = _replier().parse_reply("[face:流泪][face:流泪]\n[sticker:猫猫]")
        c = self.D.collect_sent_chunks(chunks)
        self.assertEqual(c["bubbles"], ["[流泪][流泪]"])
        self.assertEqual(c["faces"], ["流泪", "流泪"])
        self.assertEqual(len(c["stickers"]), 1)

    def test_零使用报N_A不报PASS(self):
        """零数据 == 零违规是自动成立的空断言，报 PASS 会让报告读起来像验过了。"""
        m = self.D.metric_face_usage([
            {"idx": 1, "speaker": "her", "bubbles": ["在呢"], "faces": []},
        ])
        self.assertEqual(m["verdict"], "N/A")
        self.assertTrue(m["not_run_reason"])

    def test_三种发法分别计数(self):
        m = self.D.metric_face_usage([{
            "idx": 2, "speaker": "her",
            "bubbles": ["你真棒[doge]", "[流泪][流泪]", "在呢"],
            "faces": ["doge", "流泪", "流泪"],
        }])
        self.assertEqual(m["forms"]["混排句尾"], 1)
        self.assertEqual(m["forms"]["纯表情气泡"], 1)
        self.assertEqual(m["forms"]["同款二连"], 1)
        self.assertEqual(m["count"], 3)

    def test_越上限判FAIL(self):
        m = self.D.metric_face_usage([{
            "idx": 3, "speaker": "her",
            "bubbles": ["[a][b][c]"],
            "faces": ["a", "b", "c"],
        }])
        self.assertEqual(m["verdict"], "FAIL")
        self.assertEqual(m["over_cap_turns"], [3])

    def test_降级字面量不算发了脸(self):
        m = self.D.metric_face_usage([{
            "idx": 4, "speaker": "her",
            "bubbles": ["你真棒[face:微笑]"],
            "faces": [],
        }])
        self.assertEqual(m["verdict"], "N/A")
        self.assertEqual(m["count"], 0)

    def test_他说的话不算进她的统计(self):
        m = self.D.metric_face_usage([{
            "idx": 5, "speaker": "user", "bubbles": ["你真棒[doge]"], "faces": ["doge"],
        }])
        self.assertEqual(m["verdict"], "N/A")

    def test_端到端投影不丢faces(self):
        """**这条是踩过坑才写的**：指标报 0/N/A，而 raw.json 里明明有 3 个脸。

        病根在 `_rec_to_dict` 这个投影上：它只带 idx/speaker/text/time/bubbles，
        **没带 faces**，于是 compute_metrics 看到的每一轮 `faces` 都是空的，
        判据"气泡里的标签 ⊆ 本轮实发清单"永远不成立 → 有数据被报成"无观测样本"。
        只测 metric_face_usage 的叶子函数发现不了这层（喂进去的 dict 本来就有 faces），
        必须整条投影走一遍。
        """
        rec = self.D.TurnRecord(
            idx=2, speaker="her", text="不错啊[赞]", time="2026-10-04 21:11",
            bubbles=["不错啊[赞]"], faces=["赞"],
        )
        projected = self.D._rec_to_dict(rec)
        self.assertEqual(projected.get("faces"), ["赞"], "投影把 faces 弄丢了")
        m = self.D.metric_face_usage([projected])
        self.assertEqual(m["count"], 1, "有数据被判成无样本：报告读起来像验过了，其实是瞎了")
        self.assertNotEqual(m["verdict"], "N/A")
        self.assertEqual(m["forms"]["混排句尾"], 1)

    def test_compute_metrics整体能数到脸(self):
        """再往上一层：汇总口径（owner 真正看的那份报告）也得数得到。"""
        recs = [
            self.D.TurnRecord(
                idx=1, speaker="her", text="不错啊[赞]", time="t",
                bubbles=["不错啊[赞]"], faces=["赞"],
            ),
            self.D.TurnRecord(
                idx=2, speaker="her", text="[流泪][流泪]", time="t",
                bubbles=["[流泪][流泪]"], faces=["流泪", "流泪"],
            ),
        ]
        m = self.D.compute_metrics([self.D._rec_to_dict(r) for r in recs], stage=1, cost=0.01)
        self.assertEqual(m["multi_turn"]["QQ表情"]["count"], 3)
        self.assertEqual(m["multi_turn"]["QQ表情"]["forms"]["同款二连"], 1)
        self.assertNotIn("QQ表情", m["not_run"])


if __name__ == "__main__":
    unittest.main()
