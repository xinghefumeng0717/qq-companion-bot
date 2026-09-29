"""OneBot v11 WebSocket 客户端 (onebot.py)
管理与 NapCat 的 WebSocket 连接、心跳维护、断线重连、消息上报解析、图片下载与重试发送。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, Callable, Coroutine, Dict, List, Optional
import aiohttp

from companion.config import OneBotConfig

logger = logging.getLogger(__name__)


def detect_image_ext_and_mime(data: bytes, filename_or_url: str = "") -> Tuple[str, str]:
    """根据 url/文件名 或 字节魔数推断扩展名与 MIME 类型
    PNG: 89 50 4E 47 -> .png, image/png
    GIF: 47 49 46 -> .gif, image/gif
    JPEG: FF D8 FF -> .jpg, image/jpeg
    WEBP: 52 49 46 46 ... 57 45 42 50 -> .webp, image/webp
    """
    lower = filename_or_url.lower().split("?")[0]
    if lower.endswith(".png"):
        return ".png", "image/png"
    if lower.endswith(".gif"):
        return ".gif", "image/gif"
    if lower.endswith((".jpg", ".jpeg")):
        return ".jpg", "image/jpeg"
    if lower.endswith(".webp"):
        return ".webp", "image/webp"

    # 按字节魔数判断
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\x89PNG"):
        return ".png", "image/png"
    if data.startswith(b"GIF87a") or data.startswith(b"GIF89a"):
        return ".gif", "image/gif"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return ".webp", "image/webp"

    return ".jpg", "image/jpeg"


def build_text_segment(text: str) -> Dict[str, Any]:
    """构造 OneBot v11 纯文本消息段"""
    return {"type": "text", "data": {"text": text}}


def build_image_segment(file_path: str) -> Dict[str, Any]:
    """构造 OneBot v11 本地图片消息段 (优先转为 base64:// 格式，天然穿透 Docker 隔离)"""
    if os.path.exists(file_path):
        import base64
        with open(file_path, "rb") as f:
            b64_data = base64.b64encode(f.read()).decode("utf-8")
        return {"type": "image", "data": {"file": f"base64://{b64_data}"}}
    abs_path = os.path.abspath(file_path).replace("\\", "/")
    if not abs_path.startswith("/"):
        abs_path = "/" + abs_path
    return {"type": "image", "data": {"file": f"file://{abs_path}"}}


class OneBotClient:
    def __init__(
        self,
        config: OneBotConfig,
        allowed_user_id: int,
        on_message_callback: Optional[Callable[[str, Optional[str]], Coroutine[Any, Any, None]]] = None,
        image_save_dir: str = "data/images",
        voice_processor: Optional[Any] = None,
    ):
        self.config = config
        self.allowed_user_id = allowed_user_id
        self.on_message_callback = on_message_callback
        self.image_save_dir = image_save_dir
        self.voice_processor = voice_processor
        os.makedirs(self.image_save_dir, exist_ok=True)

        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._running = False
        self._last_heartbeat: float = 0.0
        self._pending_echoes: Dict[str, asyncio.Future[Dict[str, Any]]] = {}

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def start(self) -> None:
        """启动连接与消息接收循环（含指数退避断线重连）"""
        self._running = True
        self._session = aiohttp.ClientSession()
        reconnect_attempts = 0

        while self._running:
            headers = {}
            if self.config.access_token:
                headers["Authorization"] = f"Bearer {self.config.access_token}"

            try:
                logger.info(f"[OneBot] 正在连接 OneBot 服务: {self.config.ws_url}")
                async with self._session.ws_connect(
                    self.config.ws_url, headers=headers, heartbeat=30
                ) as ws:
                    self._ws = ws
                    reconnect_attempts = 0
                    logger.info("[OneBot] WebSocket 连接成功！")

                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await self._handle_raw_message(msg.data)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning(f"[OneBot] WebSocket 断开: {msg.type}")
                            break

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[OneBot] 连接异常: {e}")

            self._ws = None
            if not self._running:
                break

            # 指数退避断线重连：1s -> 2s -> 4s -> ... 上限 60s
            delay = min(60.0, 1.0 * (2 ** reconnect_attempts))
            reconnect_attempts += 1
            logger.info(f"[OneBot] 将在 {delay:.1f} 秒后重试连接 (第 {reconnect_attempts} 次)...")
            await asyncio.sleep(delay)

    async def stop(self) -> None:
        """停止客户端并释放资源"""
        self._running = False
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
        except Exception as e:
            logger.debug(f"[OneBot] ws close 异常: {e}")
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception as e:
            logger.debug(f"[OneBot] session close 异常: {e}")
        logger.info("[OneBot] 客户端已停止")

    async def _handle_raw_message(self, raw_text: str) -> None:
        """处理收到的 JSON 报文"""
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return

        # 1. 响应帧 echo 匹配
        echo = data.get("echo")
        if echo and echo in self._pending_echoes:
            fut = self._pending_echoes.pop(echo)
            if not fut.done():
                fut.set_result(data)
            return

        # 2. 心跳与存活时间戳
        post_type = data.get("post_type")
        if post_type == "meta_event":
            self._last_heartbeat = asyncio.get_event_loop().time()
            return

        # 3. 私聊消息过滤
        if post_type == "message":
            msg_type = data.get("message_type")
            user_id = data.get("user_id")

            # 只响应机主大号 QQ 号，其余一律忽略
            if msg_type == "private" and user_id == self.allowed_user_id:
                raw_msg = data.get("message")
                await self._process_incoming_message(raw_msg)

    async def _process_incoming_message(self, raw_msg: Any) -> None:
        """解析机主发来的消息段，下载图片，交给聚合器回调"""
        text_parts = []
        image_local_path: Optional[str] = None

        if isinstance(raw_msg, str):
            text_parts.append(raw_msg)
        elif isinstance(raw_msg, list):
            for seg in raw_msg:
                if not isinstance(seg, dict):
                    continue
                stype = seg.get("type")
                sdata = seg.get("data", {})

                if stype == "text":
                    text_parts.append(sdata.get("text", ""))
                elif stype == "record":
                    rec_url = sdata.get("url") or sdata.get("file")
                    if rec_url:
                        if self.voice_processor:
                            voice_text = await self.voice_processor.process_voice(rec_url, self._session)
                            text_parts.append(voice_text)
                        else:
                            text_parts.append("[对方发来一条语音，但没能听清]")
                elif stype == "image":
                    img_url = sdata.get("url") or sdata.get("file")
                    if img_url:
                        local_path = await self._download_image(img_url)
                        if local_path:
                            image_local_path = local_path

        full_text = "".join(text_parts).strip()
        if self.on_message_callback:
            await self.on_message_callback(full_text, image_local_path)

    async def _download_image(self, url: str) -> Optional[str]:
        """下载 QQ 图片到本地 data/images/（智能判断图片扩展名）"""
        if url.startswith("file://"):
            local_path = url[7:]
            if os.name == "nt" and local_path.startswith("/"):
                local_path = local_path[1:]
            return local_path if os.path.exists(local_path) else None

        if not self._session or not url.startswith("http"):
            return None

        try:
            async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    ext, _ = detect_image_ext_and_mime(data, url)
                    filename = f"{uuid.uuid4().hex[:12]}{ext}"
                    save_path = os.path.join(self.image_save_dir, filename)
                    with open(save_path, "wb") as f:
                        f.write(data)
                    logger.info(f"[OneBot] 图片下载成功: {save_path}")
                    return os.path.abspath(save_path)
                else:
                    logger.warning(f"[OneBot] 下载图片失败 HTTP {resp.status}")
        except Exception as e:
            logger.error(f"[OneBot] 下载图片网络异常: {e}")
        return None

    async def send_private_msg(
        self,
        user_id: int,
        message_segments: List[Dict[str, Any]],
        max_retries: int = 2,
    ) -> bool:
        """发送私聊消息（支持文本与图片段混排），失败重试 2 次"""
        if not self.is_connected or not self._ws:
            logger.error("[OneBot] 发送失败: WebSocket 未连接")
            return False

        for attempt in range(max_retries + 1):
            echo_id = str(uuid.uuid4())
            payload = {
                "action": "send_msg",
                "params": {
                    "message_type": "private",
                    "user_id": user_id,
                    "message": message_segments,
                },
                "echo": echo_id,
            }

            fut = asyncio.get_event_loop().create_future()
            self._pending_echoes[echo_id] = fut

            try:
                await self._ws.send_str(json.dumps(payload))
                res = await asyncio.wait_for(fut, timeout=10.0)
                status = res.get("status")
                retcode = res.get("retcode", -1)
                if status == "ok" or retcode == 0:
                    return True
                else:
                    logger.warning(f"[OneBot] 发送消息返回错误: {res}")
            except Exception as e:
                self._pending_echoes.pop(echo_id, None)
                if attempt < max_retries:
                    logger.warning(f"[OneBot] 发送私聊消息失败 (attempt {attempt+1}/{max_retries+1}): {e}，正在重试")
                    await asyncio.sleep(0.5)
                else:
                    logger.error(f"[OneBot] 发送私聊消息最终失败: {e}")
                    return False

        return False
