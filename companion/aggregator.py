"""消息聚合器 (aggregator.py)
对机主连发的短消息进行 6 秒静默聚合与 15 秒硬等待合并，图片消息与文本共用同一缓冲窗口。
使用异步任务队列保证生成回复时不丢弃、不插队。

FIXES21：批次里除了拼好的文本，还要保住**每条消息的编号与 message_id**
（发侧引用要靠它指回"他哪一条"）。批次元素形如
`{"index": 1, "text": "...", "message_id": 123, "has_image": False}`，
index 从 1 起，编号与 message_id 一一对应，模型看到的 [1][2][3] 就是这几个。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 静默窗：机主停手这么久就认为这一轮说完了（真人想一句发一句常见间隔 4~6 秒）
SILENCE_WINDOW = 6.0
# 硬上限：从本轮第一条消息起算，无论后续来多少条，最多等这么久
HARD_LIMIT = 15.0

# 批次里一条消息的形状（用 TypedDict 只是给读代码的人看，这里保持 dict 以便序列化）
BatchItem = Dict[str, Any]


class MessageAggregator:
    def __init__(
        self,
        turn_handler: Callable[..., Coroutine[Any, Any, None]],
    ):
        self.turn_handler = turn_handler
        self._text_buffer: List[str] = []
        self._batch: List[BatchItem] = []
        self._image_buffer: Optional[str] = None
        self._image_index: Optional[int] = None  # 批次里第几条带了图（编号从 1 起）
        self._first_msg_time: float = 0.0
        self._debounce_task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue[Tuple[str, Optional[str], List[BatchItem]]] = asyncio.Queue()
        self._consumer_task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._consumer_task = asyncio.create_task(self._consume_queue())
        logger.info("[Aggregator] 消息聚合器队列消费协程已启动")

    def stop(self) -> None:
        self._running = False
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        if self._consumer_task and not self._consumer_task.done():
            self._consumer_task.cancel()

    async def _consume_queue(self) -> None:
        """串行消费每轮对话输入，确保本轮生成回复时下一轮排队等待不丢弃"""
        while self._running:
            try:
                user_text, image_path, batch = await self._queue.get()
                try:
                    await self.turn_handler(user_text, image_path, batch)
                except Exception as e:
                    logger.error(f"[Aggregator] 处理一轮对话异常: {e}", exc_info=True)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Aggregator] 消费队列发生异常: {e}")

    async def push_message(
        self,
        text: str,
        image_path: Optional[str] = None,
        message_id: Any = None,
    ) -> None:
        """接收一条新消息（文本/图片/两者）进行缓冲与聚合。
        图片不再即时触发独立轮次，与文本走同一套缓冲逻辑，避免"几句话+一个表情包"被拆开。

        FIXES21：多带一个 message_id（入站事件里本来就有），与文本一起进批次。
        纯空消息（既没文字也没图、只有 message_id）**不进批次**——它没有可引用的内容。
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        has_content = bool(text) or bool(image_path)
        if has_content:
            self._text_buffer.append(text)
            if image_path:
                # 一轮内多图时保留最后一张（更可能是机主当下要问的那张）
                self._image_buffer = image_path
            self._batch.append({
                "index": len(self._batch) + 1,
                "text": text or "",
                "message_id": message_id,
                "has_image": bool(image_path),
            })
            if image_path:
                self._image_index = self._batch[-1]["index"]

        if not self._text_buffer and not self._image_buffer:
            return

        # 首条计时：图片与文本都只负责"起表"，不重置已开始的计时
        if self._first_msg_time == 0.0:
            self._first_msg_time = now

        elapsed = now - self._first_msg_time

        # 达到硬上限（从本轮第一条消息起算），立即触发
        if elapsed >= HARD_LIMIT:
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
            self._flush_buffer()
            return

        # 重置静默计时器，但永不越过硬上限
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        wait_time = min(SILENCE_WINDOW, HARD_LIMIT - elapsed)
        self._debounce_task = asyncio.create_task(self._wait_and_flush(wait_time))

    async def _wait_and_flush(self, wait_seconds: float) -> None:
        try:
            await asyncio.sleep(wait_seconds)
            self._flush_buffer()
        except asyncio.CancelledError:
            pass

    def _flush_buffer(self) -> None:
        """合并缓冲中的文本与图片并提交到队列（FIXES21：连同编号+message_id 的批次）"""
        if not self._text_buffer and not self._image_buffer:
            return

        combined_text = "\n".join(self._text_buffer).strip()
        image_path = self._image_buffer
        # **必须拷贝**：下面紧接着 self._batch.clear()，不清拷贝的话
        # 交出去的是同一个列表对象，clear 会把它一起清空 → 下游收到空批次
        # （症状：她看不到编号块，引用功能整体静默失效）
        batch = list(self._batch)
        if self._image_index is not None:
            # 告诉下游"哪一条带了图"：turn_handler 拿到视觉描述后要写回对应那条，
            # 否则编号块里那条会空着（她会以为他没说什么，只是发了张图）
            for item in batch:
                if item.get("index") == self._image_index:
                    item["image_index"] = self._image_index
                    break

        self._text_buffer.clear()
        self._batch.clear()
        self._image_buffer = None
        self._image_index = None
        self._first_msg_time = 0.0
        self._debounce_task = None

        quoteable = sum(1 for b in batch if b.get("message_id") is not None)
        logger.info(
            f"[Aggregator] 聚合静默到期，提交一轮: {len(batch)} 条"
            f"（其中 {quoteable} 条带 message_id、可被引用）, "
            f"text='{combined_text}'"
            f"{f', img={image_path}' if image_path else ''}"
        )
        self._queue.put_nowait((combined_text, image_path, batch))
