"""语音失败模型补救的合同测试（不调用真实 API）。"""

import unittest
from types import SimpleNamespace

from companion.config import ReplyConfig
from companion.replier import Replier
from companion.voice_recovery import recover_voice_text


class _Stickers:
    def match_sticker(self, _desc):
        return None


class _Assembler:
    def __init__(self):
        self.kwargs = None

    async def assemble_messages(self, _user, _image, **kwargs):
        self.kwargs = kwargs
        return [{"role": "system", "content": "角色"}], ""


class _Gateway:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.kwargs = None
        self.config = SimpleNamespace(text_model="deepseek-flash")

    async def chat(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


class TestVoiceRecovery(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.replier = Replier(ReplyConfig(), _Stickers())
        self.assembler = _Assembler()

    async def test_模型看到失败原因并返回自然文字(self):
        gateway = _Gateway("刚那条没发成，先用文字跟你说。")
        result = await recover_voice_text(
            replier=self.replier,
            assembler=self.assembler,
            gateway=gateway,
            user_text="发条语音",
            draft="晚安",
            reason="TTS 合成失败",
            model="deepseek-flash",
        )
        self.assertEqual(result, "刚那条没发成，先用文字跟你说。")
        self.assertEqual(gateway.kwargs["purpose"], "voice_recovery")
        self.assertEqual(gateway.kwargs["max_retries"], 0)
        self.assertIn("TTS 合成失败", gateway.kwargs["messages"][-1]["content"])
        self.assertIn("晚安", gateway.kwargs["messages"][-1]["content"])
        self.assertFalse(self.assembler.kwargs["voice_allowed"])
        self.assertTrue(self.assembler.kwargs["voice_gated_note"])

    async def test_补救稿再次写语音标记则拒绝(self):
        gateway = _Gateway("[voice:]晚安[/voice]")
        result = await recover_voice_text(
            replier=self.replier,
            assembler=self.assembler,
            gateway=gateway,
            user_text="发条语音",
            draft="晚安",
            reason="OneBot 未确认",
            model="deepseek-flash",
        )
        self.assertIsNone(result)

    async def test_补救稿谎称已发则拒绝(self):
        gateway = _Gateway("发了啊")
        result = await recover_voice_text(
            replier=self.replier,
            assembler=self.assembler,
            gateway=gateway,
            user_text="发条语音",
            draft="晚安",
            reason="OneBot 未确认",
            model="deepseek-flash",
        )
        self.assertIsNone(result)

    async def test_调用异常返回空让调用方保留原文(self):
        gateway = _Gateway(error=RuntimeError("offline"))
        result = await recover_voice_text(
            replier=self.replier,
            assembler=self.assembler,
            gateway=gateway,
            user_text="发条语音",
            draft="晚安",
            reason="TTS 合成失败",
            model="deepseek-flash",
        )
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
