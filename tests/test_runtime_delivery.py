import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from companion.main import CompanionBot
from companion.kernel.contracts import SendReceipt
from companion.kernel.delivery import DeliveryRejectedError


class _OneBot:
    def __init__(self, result=True):
        self.result = result
        self.calls = []

    async def send_private_msg_receipt(self, _uid, segments):
        self.calls.append(segments)
        if not self.result:
            raise DeliveryRejectedError("synthetic rejection")
        return SendReceipt(message_id=1)


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
        return bot

    async def test_合成异常交给执行器且发送器不擅自补文字(self):
        with tempfile.TemporaryDirectory() as d:
            onebot = _OneBot(result=True)
            bot = self._bot(onebot, _TTS(os.path.join(d, "x.mp3"), error=True))
            with self.assertRaisesRegex(RuntimeError, "synthetic tts failure"):
                await bot.send_voice("晚安")
            self.assertEqual(onebot.calls, [])

    async def test_语音发送不携带引用段(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.mp3")
            with open(path, "wb") as f:
                f.write(b"fake-audio")
            onebot = _OneBot(result=True)
            bot = self._bot(onebot, _TTS(path))
            receipt = await bot.send_voice("晚安", quote_message_id=123)
            # NapCat/QQ 不支持 reply + record；实际回执也不得伪造显示引用。
            self.assertEqual([s["type"] for s in onebot.calls[0]], ["record"])
            self.assertIsNone(receipt.actual_quote_message_id)

    async def test_已发出后计数异常不重复补发(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "x.mp3")
            with open(path, "wb") as f:
                f.write(b"fake-audio")
            onebot = _OneBot(result=True)
            bot = self._bot(onebot, _TTS(path, bump_error=True))
            receipt = await bot.send_voice("晚安")
            self.assertEqual(receipt.message_id, 1)
            self.assertEqual(len(onebot.calls), 1)


if __name__ == "__main__":
    unittest.main()
