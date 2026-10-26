import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from companion.main import CompanionBot


class _OneBot:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def send_private_msg(self, _uid, segments):
        self.calls.append(segments)
        return self.result


class _TTS:
    def __init__(self, path, error=False, bump_error=False):
        self.path = path
        self.error = error
        self.bump_error = bump_error

    async def synthesize(self, _text):
        if self.error:
            raise RuntimeError("synthetic tts failure")
        return self.path

    async def bump_daily(self):
        if self.bump_error:
            raise RuntimeError("synthetic counter failure")
        return 1

    @staticmethod
    def cleanup(_path):
        return None


class RuntimeDeliveryTests(unittest.IsolatedAsyncioTestCase):
    def _bot(self, onebot, tts):
        bot = CompanionBot.__new__(CompanionBot)
        bot.onebot = onebot
        bot.tts = tts
        bot.config = SimpleNamespace(account=SimpleNamespace(allowed_user_id=1))
        handler = MagicMock()
        handler.recover_voice_failure = AsyncMock(return_value=None)
        bot.turn_handler = handler
        return bot

    async def test_合成异常会转文字并留下真实状态(self):
        with tempfile.TemporaryDirectory() as d:
            onebot = _OneBot(result=True)
            bot = self._bot(onebot, _TTS(os.path.join(d, "x.mp3"), error=True))
            chunk = {"type": "voice", "content": "晚安"}
            await bot._send_voice_chunk(chunk)
            self.assertEqual(chunk["type"], "text")
            self.assertEqual(chunk["_voice_delivery"], "text")
            self.assertEqual(len(onebot.calls), 1)

    async def test_语音发送不携带引用段(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.mp3")
            with open(path, "wb") as f:
                f.write(b"fake-audio")
            onebot = _OneBot(result=True)
            bot = self._bot(onebot, _TTS(path))
            chunk = {"type": "voice", "content": "晚安", "_quote": {"index": 2, "message_id": 123}}
            await bot._send_voice_chunk(chunk)
            # NapCat/QQ 不支持 reply + record；引用只放文字段。
            self.assertEqual([s["type"] for s in onebot.calls[0]], ["record"])
            self.assertEqual(chunk["_voice_delivery"], "sent")

    async def test_已发出后计数异常不重复补发(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.mp3")
            with open(path, "wb") as f:
                f.write(b"fake-audio")
            onebot = _OneBot(result=True)
            bot = self._bot(onebot, _TTS(path, bump_error=True))
            chunk = {"type": "voice", "content": "晚安"}
            await bot._send_voice_chunk(chunk)
            self.assertEqual(chunk["type"], "voice")
            self.assertEqual(chunk["_voice_delivery"], "sent")
            self.assertEqual(len(onebot.calls), 1)


if __name__ == "__main__":
    unittest.main()
