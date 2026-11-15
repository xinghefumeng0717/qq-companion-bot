"""M4.5 验收测试：语音输入模块（转码、本地 SenseVoice 识别与完整降级路径）"""

import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from companion.config import VoiceConfig
from companion.voice import FALLBACK_VOICE_TEXT, VoiceProcessor


class TestM45Voice(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.test_voice_dir = "data/test_voice"
        os.makedirs(self.test_voice_dir, exist_ok=True)
        self.config = VoiceConfig(enabled=True, model_dir="data/models/sensevoice")
        self.processor = VoiceProcessor(self.config, voice_dir=self.test_voice_dir)

    async def asyncTearDown(self):
        if os.path.exists(self.test_voice_dir):
            for f in os.listdir(self.test_voice_dir):
                try:
                    os.remove(os.path.join(self.test_voice_dir, f))
                except Exception:
                    pass
            try:
                os.rmdir(self.test_voice_dir)
            except Exception:
                pass

    async def test_normal_voice_recognition_path(self):
        """测试正常语音识别链路：模拟下载、转码与 ASR 成功返回（语音消息）前缀"""
        dummy_silk = os.path.join(self.test_voice_dir, "input.silk")
        with open(dummy_silk, "wb") as f:
            f.write(b"dummy silk content")

        def mock_transcode(in_file, out_file):
            with open(out_file, "wb") as f:
                f.write(b"dummy wav content")
            return True

        with patch.object(self.processor, "_download_silk", new=AsyncMock(return_value=dummy_silk)), \
             patch.object(self.processor, "_transcode_sync", side_effect=mock_transcode), \
             patch.object(self.processor, "_recognize_sync", return_value="今天天气不错"):

            result = await self.processor.process_voice("http://example.com/test.silk")
            self.assertEqual(result, "（语音消息）今天天气不错")

            # 验证临时文件已被清理
            self.assertFalse(os.path.exists(dummy_silk))
            wav_files = [f for f in os.listdir(self.test_voice_dir) if f.endswith(".wav")]
            self.assertEqual(len(wav_files), 0)

    async def test_ffmpeg_missing_fallback(self):
        """测试 ffmpeg 缺失或转码失败时的优雅降级"""
        dummy_silk = os.path.join(self.test_voice_dir, "input.silk")
        with open(dummy_silk, "wb") as f:
            f.write(b"dummy silk content")

        with patch.object(self.processor, "_download_silk", new=AsyncMock(return_value=dummy_silk)), \
             patch.object(self.processor, "_transcode_sync", return_value=False):

            result = await self.processor.process_voice("http://example.com/test.silk")
            self.assertEqual(result, FALLBACK_VOICE_TEXT)
            self.assertFalse(os.path.exists(dummy_silk))

    async def test_empty_recognition_fallback(self):
        """测试识别结果为空/置信度不足时的降级"""
        dummy_silk = os.path.join(self.test_voice_dir, "input.silk")
        with open(dummy_silk, "wb") as f:
            f.write(b"dummy silk content")

        def mock_transcode(in_file, out_file):
            with open(out_file, "wb") as f:
                f.write(b"dummy wav content")
            return True

        with patch.object(self.processor, "_download_silk", new=AsyncMock(return_value=dummy_silk)), \
             patch.object(self.processor, "_transcode_sync", side_effect=mock_transcode), \
             patch.object(self.processor, "_recognize_sync", return_value=""):

            result = await self.processor.process_voice("http://example.com/test.silk")
            self.assertEqual(result, FALLBACK_VOICE_TEXT)

    async def test_voice_disabled_fallback(self):
        """测试配置开关 voice.enabled = False 时直接返回降级占位符"""
        self.processor.config.enabled = False
        result = await self.processor.process_voice("http://example.com/test.silk")
        self.assertEqual(result, FALLBACK_VOICE_TEXT)


if __name__ == "__main__":
    unittest.main()
