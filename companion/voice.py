"""语音输入处理模块 (voice.py)
处理 QQ 语音 (SILK 格式) 的下载、ffmpeg 转码与 sherpa-onnx 本地 SenseVoice 离线识别。
包含 ffmpeg 缺失、模型未下载、识别失败与配置关闭的完整降级逻辑。
"""

from __future__ import annotations

import array
import asyncio
import logging
import os
import re
import subprocess
import uuid
import wave
from typing import Optional
import aiohttp

from companion.config import VoiceConfig

logger = logging.getLogger(__name__)

FALLBACK_VOICE_TEXT = "[对方发来一条语音，但没能听清]"


class VoiceProcessor:
    def __init__(self, config: VoiceConfig, voice_dir: str = "data/voice"):
        self.config = config
        self.voice_dir = voice_dir
        self._recognizer = None
        os.makedirs(self.voice_dir, exist_ok=True)

    def _get_recognizer(self):
        """延迟加载 sherpa-onnx SenseVoice 识别器"""
        if self._recognizer is not None:
            return self._recognizer

        model_dir = self.config.model_dir
        model_path = os.path.join(model_dir, "model.int8.onnx")
        tokens_path = os.path.join(model_dir, "tokens.txt")

        if not os.path.exists(model_path) or not os.path.exists(tokens_path):
            logger.warning(
                f"[Voice] SenseVoice 模型文件未在 '{model_dir}' 找到 (缺少 model.int8.onnx 或 tokens.txt)。"
                f"请从 sherpa-onnx 官方仓库下载 SenseVoice 模型放入该目录。"
            )
            return None

        try:
            import sherpa_onnx
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
                model=model_path,
                tokens=tokens_path,
                num_threads=2,
                sample_rate=16000,
            )
            logger.info("[Voice] SenseVoice ASR 识别器加载成功")
            return self._recognizer
        except Exception as e:
            logger.error(f"[Voice] 初始化 sherpa-onnx 识别器失败: {e}")
            return None

    async def _download_silk(self, url: str, session: aiohttp.ClientSession) -> Optional[str]:
        """下载语音文件到本地临时路径"""
        if url.startswith("file://"):
            local_path = url[7:]
            if os.name == "nt" and local_path.startswith("/"):
                local_path = local_path[1:]
            return local_path if os.path.exists(local_path) else None

        filename = f"{uuid.uuid4().hex[:12]}.silk"
        save_path = os.path.join(self.voice_dir, filename)

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    with open(save_path, "wb") as f:
                        f.write(data)
                    return os.path.abspath(save_path)
                else:
                    logger.warning(f"[Voice] 下载语音文件失败 HTTP {resp.status}")
        except Exception as e:
            logger.error(f"[Voice] 下载语音网络异常: {e}")
        return None

    def _transcode_sync(self, in_file: str, out_file: str) -> bool:
        """调用系统 ffmpeg 将 silk/音频 转码为 16000Hz 单声道 PCM wav"""
        cmd = ["ffmpeg", "-y", "-i", in_file, "-ar", "16000", "-ac", "1", out_file]
        try:
            res = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if res.returncode != 0:
                logger.warning(f"[Voice] ffmpeg 转码失败 (code {res.returncode}): {res.stderr.decode('utf-8', errors='ignore')[:200]}")
                return False
            return True
        except FileNotFoundError:
            logger.warning("[Voice] 系统未安装 ffmpeg，无法转码语音文件，已降级为占位符")
            return False
        except Exception as e:
            logger.warning(f"[Voice] 执行 ffmpeg 异常: {e}")
            return False

    def _recognize_sync(self, wav_path: str) -> str:
        """读取 wav 文件并使用 sherpa-onnx 识别文本"""
        recognizer = self._get_recognizer()
        if not recognizer:
            return ""

        try:
            with wave.open(wav_path, "rb") as wf:
                sample_rate = wf.getframerate()
                num_frames = wf.getnframes()
                raw_bytes = wf.readframes(num_frames)
                raw_samples = array.array("h", raw_bytes)
                float_samples = [s / 32768.0 for s in raw_samples]

            stream = recognizer.create_stream()
            stream.accept_waveform(sample_rate, float_samples)
            recognizer.decode_stream(stream)
            raw_text = stream.result.text.strip()
            # 移除 SenseVoice 特殊标记如 <|zh|><|NEUTRAL|> 等
            clean_text = re.sub(r"<\|.*?\|>", "", raw_text).strip()
            return clean_text
        except Exception as e:
            logger.error(f"[Voice] 语音识别推理异常: {e}")
            return ""

    async def process_voice(
        self,
        voice_url_or_path: str,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> str:
        """处理一条语音：下载 -> ffmpeg 转码 -> ASR 识别 -> 格式化前缀与清理临时文件"""
        if not self.config.enabled:
            logger.info("[Voice] 语音输入功能已关闭，直接降级为占位符")
            return FALLBACK_VOICE_TEXT

        temp_silk = None
        temp_wav = None
        is_downloaded = False

        try:
            # 1. 获取本地文件路径
            if voice_url_or_path.startswith("http"):
                if not session:
                    async with aiohttp.ClientSession() as s:
                        temp_silk = await self._download_silk(voice_url_or_path, s)
                else:
                    temp_silk = await self._download_silk(voice_url_or_path, session)
                is_downloaded = True
            else:
                temp_silk = voice_url_or_path

            if not temp_silk or not os.path.exists(temp_silk):
                logger.warning("[Voice] 语音源文件获取失败")
                return FALLBACK_VOICE_TEXT

            # 2. ffmpeg 转码
            temp_wav = os.path.join(self.voice_dir, f"{uuid.uuid4().hex[:12]}.wav")
            trans_ok = await asyncio.to_thread(self._transcode_sync, temp_silk, temp_wav)
            if not trans_ok or not os.path.exists(temp_wav):
                return FALLBACK_VOICE_TEXT

            # 3. 本地 ASR 识别 (asyncio.to_thread 避免阻塞事件循环)
            text = await asyncio.to_thread(self._recognize_sync, temp_wav)
            if not text:
                logger.info("[Voice] 语音识别结果为空或置信度不足，降级处理")
                return FALLBACK_VOICE_TEXT

            logger.info(f"[Voice] 语音识别成功: '{text}'")
            return f"（语音消息）{text}"

        except Exception as e:
            logger.error(f"[Voice] 处理语音消息异常: {e}", exc_info=True)
            return FALLBACK_VOICE_TEXT
        finally:
            # 4. 清理临时文件
            if is_downloaded and temp_silk and os.path.exists(temp_silk):
                try:
                    os.remove(temp_silk)
                except Exception:
                    pass
            if temp_wav and os.path.exists(temp_wav):
                try:
                    os.remove(temp_wav)
                except Exception:
                    pass
