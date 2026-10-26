"""2026-10-24 生产语音事故回归：解析、真实降级、历史与跨轮状态。"""

import os
import tempfile
import unittest
from types import SimpleNamespace

from companion.main import CompanionBot
from companion.memory import MemoryManager
from companion.replier import (
    Replier,
    has_voice_intent,
    has_voice_promise,
    record_text_from_chunks,
    sanitize_voice_claims,
    strip_voice_record_prefix_for_history,
)
from companion.config import ReplyConfig
from companion.turn_handler import TurnHandler
from helpers import close_db, make_db, make_engine_stack


class _Stickers:
    def match_sticker(self, _desc):
        return None


def _replier():
    return Replier(ReplyConfig(), _Stickers())


class TestProductionVoiceSyntax(unittest.TestCase):
    def test_12点36分的行尾简写仍然发语音(self):
        raw = "跟我讲条件？\n行吧，一条\n[voice:叫了怎么办 我可真不理你了啊]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertIn("voice", [c.get("type") for c in chunks])
        voice = next(c for c in chunks if c.get("type") == "voice")
        self.assertEqual(voice["content"], "叫了怎么办 我可真不理你了啊")
        self.assertNotIn("啊]", record)
        self.assertIn("（语音消息）叫了怎么办 我可真不理你了啊", record)

    def test_语音关闭时同一原句不留孤立括号(self):
        raw = "跟我讲条件？\n行吧，一条\n[voice:叫了怎么办 我可真不理你了啊]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=False)
        self.assertNotIn("voice", [c.get("type") for c in chunks])
        self.assertIn("叫了怎么办 我可真不理你了啊", record)
        self.assertNotIn("[voice", record)
        self.assertNotIn("啊]", record)

    def test_语音意图识别包括存档误写(self):
        self.assertTrue(has_voice_intent("（语音消息）晚安"))
        self.assertTrue(has_voice_intent("[voice:]晚安[/voice]"))
        self.assertFalse(has_voice_intent("晚安"))

    def test_全角和中文语音格式都兼容(self):
        cases = [
            "【voice:】晚安【/voice】",
            "［voice：晚安］",
            "[voice]晚安[/voice]",
            "[语音:晚安[/语音]",
            "【语音】晚安【/语音】",
            "【语音消息】晚安",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                chunks, record = _replier().parse_reply(raw, voice_allowed=True)
                self.assertIn("voice", [c.get("type") for c in chunks])
                self.assertIn("晚安", record)
                self.assertNotIn("【", record)
                self.assertNotIn("［", record)


class TestVoiceClaimTruthfulness(unittest.TestCase):
    def test_生产中的虚假断言不会原样发出(self):
        cases = [
            "行，听见了吧。\n满意了就别再顺嘴叫大小姐",
            "发了啊\n你没收到？那我再发一遍\n喏 听到了没有",
            "传过去了，这回听到了吧\n顺路的事就这么定了",
            "我传都传了\n你那边接不住，怪谁",
            "就发一句，听完赶紧放下去睡",
            "听好了啊，就念这一遍",
            "收到就好",
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                out = sanitize_voice_claims(
                    raw, voice_allowed=False, previous_voice_sent=False, voice_context=True
                )
                self.assertNotIn("这会儿语音没发出去，先打字说。", out)
                for forbidden in ("听见了吧", "听到了没有", "传过去了", "我传都传了", "再发一遍", "就发一句", "听好了", "收到就好"):
                    self.assertNotIn(forbidden, out)

    def test_口头承诺给一条但没有语音段会说明未落地(self):
        raw = "行行行，给你一条，收着吧"
        self.assertTrue(has_voice_promise(raw))
        out = sanitize_voice_claims(
            raw, voice_allowed=False, previous_voice_sent=False, voice_context=True
        )
        self.assertNotIn("给你一条", out)
        self.assertNotIn("这会儿语音没发出去，先打字说。", out)

    def test_真实发过的旧语音可以回顾(self):
        raw = "刚才那条听见了吧"
        self.assertEqual(
            sanitize_voice_claims(
                raw, voice_allowed=False, previous_voice_sent=True, voice_context=True
            ),
            raw,
        )

    def test_发图片不是语音断言(self):
        raw = "我发了张图片"
        self.assertEqual(
            sanitize_voice_claims(raw, voice_allowed=False, voice_context=True), raw
        )

    def test_非语音上下文不改普通聊天(self):
        raw = "我今天发了一个文件给你"
        self.assertEqual(
            sanitize_voice_claims(raw, voice_allowed=False, voice_context=False), raw
        )


class TestModelRecoveryDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_onebot失败后按原稿降级且不调用模型补写(self):
        class Handler:
            async def recover_voice_failure(self, text, reason):
                return "刚那条没发成，先用文字跟你说。"

        with tempfile.TemporaryDirectory() as d:
            tts = _FakeTTS(os.path.join(d, "voice.mp3"))
            ob = _OneBot(record_ok=False)
            bot = _bot(ob, tts)
            bot.turn_handler = Handler()
            chunk = {"type": "voice", "content": "睡了"}
            await bot._send_chunk_to_onebot(chunk)
            self.assertEqual(ob.sent[-1][0]["data"]["text"], "睡了")
            self.assertEqual(chunk["_voice_delivery"], "text")
            self.assertEqual(record_text_from_chunks([chunk]), "睡了")


class TestEnglishSilenceMarker(unittest.TestCase):
    def test_英文沉默标记不当正文发出(self):
        chunks, record = _replier().parse_reply("[SILENT]")
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")
        chunks, record = _replier().parse_reply("晚安\n[silent]")
        self.assertEqual(record, "晚安")


class _FakeTTS:
    def __init__(self, path, fail=False):
        self.path = path
        self.fail = fail
        self.bumped = False
        self.cleaned = []

    async def synthesize(self, _text):
        if self.fail:
            return None
        with open(self.path, "wb") as f:
            f.write(b"ID3 fake audio")
        return self.path

    async def bump_daily(self):
        self.bumped = True
        return 1

    def cleanup(self, path):
        self.cleaned.append(path)
        if path and os.path.exists(path):
            os.remove(path)


class _OneBot:
    def __init__(self, record_ok=False, text_ok=True):
        self.record_ok = record_ok
        self.text_ok = text_ok
        self.sent = []

    async def send_private_msg(self, _user_id, segments, max_retries=2):
        kind = segments[0]["type"]
        self.sent.append(segments)
        return self.text_ok if kind == "text" else self.record_ok


def _bot(onebot, tts):
    bot = CompanionBot.__new__(CompanionBot)
    bot.config = SimpleNamespace(account=SimpleNamespace(allowed_user_id=10001))
    bot.onebot = onebot
    bot.tts = tts
    return bot


class TestActualVoiceDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_语音成功时record不与reply混合(self):
        with tempfile.TemporaryDirectory() as d:
            tts = _FakeTTS(os.path.join(d, "voice.mp3"))
            ob = _OneBot(record_ok=True)
            chunk = {"type": "voice", "content": "完整一句话", "_quote": {"message_id": 5002}}
            await _bot(ob, tts)._send_chunk_to_onebot(chunk)
            self.assertEqual([s["type"] for s in ob.sent[0]], ["record"])
            self.assertNotIn("reply", [s["type"] for s in ob.sent[0]])
            self.assertEqual(chunk["_voice_delivery"], "sent")

    async def test_语音失败降级文字可保留reply(self):
        with tempfile.TemporaryDirectory() as d:
            tts = _FakeTTS(os.path.join(d, "voice.mp3"))
            ob = _OneBot(record_ok=False)
            chunk = {"type": "voice", "content": "完整一句话", "_quote": {"message_id": 5002}}
            await _bot(ob, tts)._send_chunk_to_onebot(chunk)
            self.assertEqual([s["type"] for s in ob.sent[1]], ["reply", "text"])
            self.assertEqual([s["type"] for s in ob.sent[-1]], ["text"])

    async def test_onebot拒绝语音后补文字且不落语音记录(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "voice.mp3")
            tts = _FakeTTS(path)
            ob = _OneBot(record_ok=False)
            chunk = {"type": "voice", "content": "睡了"}
            await _bot(ob, tts)._send_chunk_to_onebot(chunk)
            self.assertEqual([s[0]["type"] for s in ob.sent], ["record", "text"])
            self.assertEqual(chunk["type"], "text")
            self.assertEqual(chunk["_voice_delivery"], "text")
            self.assertFalse(tts.bumped, "发送失败不能消耗语音日额度")
            self.assertEqual(record_text_from_chunks([chunk]), "睡了")
            self.assertFalse(os.path.exists(path))

    async def test合成失败的段也按文字记录(self):
        with tempfile.TemporaryDirectory() as d:
            tts = _FakeTTS(os.path.join(d, "voice.mp3"), fail=True)
            ob = _OneBot()
            chunk = {"type": "voice", "content": "睡了"}
            await _bot(ob, tts)._send_chunk_to_onebot(chunk)
            self.assertEqual([s[0]["type"] for s in ob.sent], ["text"])
            self.assertEqual(record_text_from_chunks([chunk]), "睡了")
            self.assertFalse(tts.bumped)


class _Gate:
    def __init__(self):
        self.kwargs = None

    async def check_gate(self, _activity, **kwargs):
        self.kwargs = kwargs
        return kwargs["user_requested"], "test"


class TestCrossTurnVoiceState(unittest.IsolatedAsyncioTestCase):
    async def test_上一轮的语音请求下一轮可补偿一次(self):
        handler = TurnHandler.__new__(TurnHandler)
        gate = _Gate()
        handler.tts = gate
        handler._voice_retry_requested = True
        handler._voice_retry_used = False

        async def activity():
            return "出远门"

        handler._current_activity_text = activity
        ok, _ = await handler._check_voice_gate("成交")
        self.assertTrue(ok)
        self.assertTrue(gate.kwargs["user_requested"])
        self.assertTrue(handler._voice_retry_used)
        self.assertFalse(handler._voice_retry_requested)


class TestMemoryAndHistory(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_当天行踪不进长期自我记忆(self):
        memory = MemoryManager(self.db)
        await memory.add_self_fact("她今天在隔壁城闲逛，觉得那地方有点意思")
        await memory.add_self_fact("她喜欢喝冰美式")
        items = await memory.get_self_facts_for_prompt()
        self.assertEqual(len(items), 1)
        self.assertIn("冰美式", items[0])

    async def test_历史只剥她的语音存档前缀(self):
        stack = make_engine_stack(self.db)
        for role, content in (("user", "你好"), ("assistant", "（语音消息）吃了"), ("user", "（语音消息）你呢")):
            await self.db.execute(
                "INSERT INTO turns (role, content, created_at) VALUES (?, ?, datetime('now'))",
                (role, content),
            )
        messages, _ = await stack.assembler.assemble_messages("在吗")
        assistant_history = [m["content"] for m in messages if m["role"] == "assistant"]
        self.assertEqual(assistant_history, ["吃了"])
        self.assertTrue(any(m["role"] == "user" and m["content"] == "（语音消息）你呢" for m in messages))
        self.assertEqual(strip_voice_record_prefix_for_history("（语音消息）吃了"), "吃了")

    async def test_下一轮提示词带真实投递备注(self):
        stack = make_engine_stack(self.db)
        prompt = await stack.assembler.assemble_system_prompt(
            "在吗", voice_gated_note=True, voice_delivery_note="上一轮语音没有发出"
        )
        self.assertIn("上一轮语音没有发出", prompt)
        self.assertIn("不要说自己已经发了", prompt)


if __name__ == "__main__":
    unittest.main()
