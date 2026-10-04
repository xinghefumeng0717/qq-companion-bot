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
from companion.faces import segment_face_tag

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


def build_face_segment(face_id: int) -> Dict[str, Any]:
    """构造 OneBot v11 QQ 系统表情段（小黄脸）

    FIXES20 发侧：NapCat 发送不在 face_config 里的 id 会**静默丢弃整段**
    （issue #1987），所以 face_id 必须先过 companion.faces 的清单与版本闸，
    不合格的一律在 replier 层降级成文字，不走到这里。
    """
    return {"type": "face", "data": {"id": int(face_id)}}


def build_reply_segment(message_id: Any) -> Dict[str, Any]:
    """构造 OneBot v11 引用回复段

    FIXES21 发侧：她要引用他某条消息时，把 `reply` 段拼在同一条消息的**头部**。
    必须与正文同一条消息出去（OneBot 允许一条消息混 reply + text + face 段），
    单独发一条空引用是刷屏。
    """
    return {"type": "reply", "data": {"id": int(message_id)}}


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
        on_message_callback: Optional[Callable[..., Coroutine[Any, Any, None]]] = None,
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
        self._pending_echoes: Dict[str, asyncio.Future[Dict[str, Any]]] = {}
        # 消息事件与读循环解耦：读循环只负责收帧，消息处理由这个内部队列串行消费
        self._message_queue: asyncio.Queue[Any] = asyncio.Queue()
        self._dispatcher_task: Optional[asyncio.Task] = None

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
        if self._dispatcher_task and not self._dispatcher_task.done():
            self._dispatcher_task.cancel()
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
        """处理收到的 JSON 报文。

        echo 应答帧必须在本函数内同步兑现 future：get_msg / send_msg 的调用方
        就等在那些 future 上，晚一步兑现就是等满超时。
        消息事件只做派发，不在此等待处理完成——否则带引用的消息会在
        _fetch_reply_context 里等一个只有读循环才能送进来的回包，自己等自己。
        """
        try:
            data = json.loads(raw_text)
        except json.JSONDecodeError:
            return

        # 1. 响应帧 echo 匹配（同步，读循环内完成）
        echo = data.get("echo")
        if echo and echo in self._pending_echoes:
            fut = self._pending_echoes.pop(echo)
            if not fut.done():
                fut.set_result(data)
            return

        # 2. 心跳与存活
        post_type = data.get("post_type")
        if post_type == "meta_event":
            return

        # 3. 私聊消息过滤
        if post_type == "message":
            msg_type = data.get("message_type")
            user_id = data.get("user_id")

            # 只响应机主大号 QQ 号，其余一律忽略
            if msg_type == "private" and user_id == self.allowed_user_id:
                # FIXES21：message_id 随消息一起穿链（发侧引用要靠它指回具体哪一条）
                self._dispatch_message_event(data.get("message"), data.get("message_id"))

    def _dispatch_message_event(self, raw_msg: Any, message_id: Any = None) -> None:
        """把消息事件投进内部队列，立即返回，读循环不被消息处理拖住。

        FIXES21：队列元素从 `raw_msg` 变成 `(raw_msg, message_id)`。
        队列是本类内部实现（`_consume_message_queue` 同一个文件里消费），
        改形状不影响任何外部调用方。

        stop() 之后仍可能收到在途帧：此时必须直接丢弃，
        否则 _ensure_dispatcher 会把已经收尾的消费协程重新拉起来。
        """
        if not self._running:
            return
        self._ensure_dispatcher()
        self._message_queue.put_nowait((raw_msg, message_id))

    def _ensure_dispatcher(self) -> None:
        """惰性启动（并在异常退出后重启）消息消费协程；强引用常驻 client 实例"""
        if self._dispatcher_task is None or self._dispatcher_task.done():
            self._dispatcher_task = asyncio.create_task(self._consume_message_queue())

    async def _consume_message_queue(self) -> None:
        """串行消费消息事件：同一用户连发的消息仍按到达顺序处理，不插队、不丢弃"""
        while True:
            raw_msg, message_id = await self._message_queue.get()
            try:
                await self._process_incoming_message(raw_msg, message_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(f"[OneBot] 处理消息事件异常: {e}", exc_info=True)
            finally:
                self._message_queue.task_done()

    async def _process_incoming_message(
        self, raw_msg: Any, message_id: Any = None
    ) -> None:
        """解析机主发来的消息段，下载图片，取引用上下文，交给聚合器回调

        FIXES20 收侧：face 段（QQ 小黄脸）翻译成方括号文字标签按**原始顺序**并入文本流。
        病灶是这里原本只取 text 段、face 段被静默丢弃——机主发"你真棒[旺柴]"，
        她只收到"你真棒"，语气全断（他 22.5% 的消息带表情标签，盲区天天生效）。
        标签形态（`[旺柴]` 而非 `（狗头）`）与他聊天语料一致，模型读起来零障碍。

        FIXES21：入站事件里本来就带着 message_id，这里把它一起交出去（**原样透传，
        不做类型转换**——id 是 QQ 服务端给的 int，转成字符串再转回来只会多一个出错点；
        取不到就是 None，下游据此判"这条不可被引用"）。
        """
        text_parts = []
        image_local_path: Optional[str] = None
        reply_prefix: Optional[str] = None

        if isinstance(raw_msg, str):
            text_parts.append(raw_msg)
        elif isinstance(raw_msg, list):
            for seg in raw_msg:
                if not isinstance(seg, dict):
                    continue
                stype = seg.get("type")
                sdata = seg.get("data", {})

                # 表情段先判：face / mface / 被 NapCat 转成 image 段的大表情。
                # 命中即整段消费完：只并入文字标签，不再走下载/识图那条路
                # （商城大表情虽然长得像图片，但它是一张脸，不该被当成"他发来一张照片"）。
                face_tag = segment_face_tag(seg)
                if face_tag is not None:
                    text_parts.append(face_tag)
                    continue

                if stype == "text":
                    text_parts.append(sdata.get("text", ""))
                elif stype == "reply":
                    # OneBot 引用回复段：取被引消息文本并入本轮输入
                    if reply_prefix is None:
                        reply_prefix = await self._fetch_reply_context(sdata.get("id"))
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
        if reply_prefix:
            # 引用上下文进入文本流，与普通文本共用聚合窗口
            full_text = f"{reply_prefix}\n{full_text}" if full_text else reply_prefix

        if self.on_message_callback:
            await self.on_message_callback(full_text, image_local_path, message_id)

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

    async def _call_action(
        self,
        action: str,
        params: Dict[str, Any],
        timeout: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        """通过现有 WS 通道发起一次 OneBot action 并等待 echo 回包（通用）
        失败/超时/未连接一律返回 None，调用方负责降级，不抛异常。
        """
        if not self.is_connected or not self._ws:
            logger.warning(f"[OneBot] 调用 {action} 失败: WebSocket 未连接")
            return None

        echo_id = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo_id}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_echoes[echo_id] = fut

        try:
            await self._ws.send_str(json.dumps(payload))
            res = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending_echoes.pop(echo_id, None)
            logger.warning(f"[OneBot] 调用 {action} 超时 ({timeout}s)，降级处理")
            return None
        except Exception as e:
            self._pending_echoes.pop(echo_id, None)
            logger.warning(f"[OneBot] 调用 {action} 异常，降级处理: {e}")
            return None

        if res.get("status") == "ok" or res.get("retcode", -1) == 0:
            return res.get("data") or {}
        logger.warning(f"[OneBot] 调用 {action} 返回错误: {res.get('retcode')} {res.get('wording', '')}")
        return None

    @staticmethod
    def _extract_message_text(msg_data: Dict[str, Any]) -> str:
        """从 get_msg 返回的 data 中提取文本段。
        文本+图片混排时保留文本部分，只有真的没有任何文本才返回空串。

        FIXES20：face 段（以及被转成 image 段的商城大表情）同样翻成方括号标签并入，
        与实时来消息那条路（`_process_incoming_message`）走同一个 `segment_face_tag`，
        免得"他实时发狗头她看得见、引用一条狗头消息却看不见"这种半盲区。
        """
        segments = msg_data.get("message")
        texts: List[str] = []

        if isinstance(segments, str):
            texts.append(segments)
        elif isinstance(segments, list):
            for seg in segments:
                if not isinstance(seg, dict):
                    continue
                face_tag = segment_face_tag(seg)
                if face_tag is not None:
                    texts.append(face_tag)
                    continue
                if seg.get("type") == "text":
                    sdata = seg.get("data", {}) or {}
                    texts.append(sdata.get("text", ""))
        else:
            fallback = msg_data.get("message_str")
            if isinstance(fallback, str):
                texts.append(fallback)

        return "".join(texts).strip()

    @staticmethod
    def _quoted_media_desc(msg_data: Dict[str, Any]) -> Optional[str]:
        """被引消息里的非文本内容描述：图片/表情包写「一张图」，语音写「一条语音」"""
        segments = msg_data.get("message")
        if not isinstance(segments, list):
            return None
        kinds = {seg.get("type") for seg in segments if isinstance(seg, dict)}
        if kinds & {"image", "face", "mface"}:
            return "一张图"
        if "record" in kinds:
            return "一条语音"
        return None

    @classmethod
    def _describe_quoted_message(cls, msg_data: Dict[str, Any]) -> Optional[str]:
        """被引内容的一句话描述（文本截断 80 字）；取不到任何内容时返回 None"""
        text = cls._extract_message_text(msg_data)
        if text:
            return text[:80]
        return cls._quoted_media_desc(msg_data)

    async def _fetch_reply_context(self, reply_id: Any) -> Optional[str]:
        """取被引用消息，拼成「（他引用了你之前说的「XXX」）」前缀。
        任何失败都返回 None（调用方按无引用继续），不阻塞主流程。
        """
        if reply_id is None:
            return None

        data = await self._call_action("get_msg", {"message_id": reply_id}, timeout=2.0)
        if not data:
            return None

        try:
            sender_id = (data.get("sender") or {}).get("user_id")
            desc = self._describe_quoted_message(data)

            if sender_id is not None and sender_id == self.allowed_user_id:
                # 引用的是机主自己发过的消息（同样 80 字截断）
                body = f"「{desc}」" if desc else "一条消息"
                return f"（他之前说的{body}）"

            if not desc:
                return "（他引用了你的一条消息）"
            return f"（他引用了你之前说的「{desc}」）"
        except Exception as e:
            logger.warning(f"[OneBot] 解析引用消息异常，降级为无引用: {e}")
            return None

    async def set_input_status(self, user_id: int, typing: bool) -> bool:
        """设置/取消"正在输入"状态（FIXES15 打字视觉签名）。

        action: set_input_status，event_type 1=正在输入、0=停止。
        失败/超时（2s）/未连接 一律静默降级返回 False，绝不抛异常——
        打字状态只是"她正在打字"的表演，不支持/超时/网络抖动都不能影响主流程。
        """
        data = await self._call_action(
            "set_input_status",
            {"user_id": user_id, "event_type": 1 if typing else 0},
            timeout=2.0,
        )
        return data is not None

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

            fut = asyncio.get_running_loop().create_future()
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
