"""消息聚合器 (aggregator.py)
对机主连发的短消息进行 6 秒静默聚合与 15 秒硬等待合并，图片消息与文本共用同一缓冲窗口。
使用异步任务队列保证生成回复时不丢弃、不插队。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 静默窗：机主停手这么久就认为这一轮说完了（真人想一句发一句常见间隔 4~6 秒）
SILENCE_WINDOW = 6.0
# 硬上限：从本轮第一条消息起算，无论后续来多少条，最多等这么久
HARD_LIMIT = 15.0


class MessageAggregator:
    def __init__(
        self,
        turn_handler: Callable[[str, Optional[str]], Coroutine[Any, Any, None]],
    ):
        self.turn_handler = turn_handler
        self._text_buffer: List[str] = []
        self._image_buffer: Optional[str] = None
        self._first_msg_time: float = 0.0
        self._debounce_task: Optional[asyncio.Task] = None
        self._queue: asyncio.Queue[Tuple[str, Optional[str]]] = asyncio.Queue()
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
                user_text, image_path = await self._queue.get()
                try:
                    await self.turn_handler(user_text, image_path)
                except Exception as e:
                    logger.error(f"[Aggregator] 处理一轮对话异常: {e}", exc_info=True)
                finally:
                    self._queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Aggregator] 消费队列发生异常: {e}")

    async def push_message(self, text: str, image_path: Optional[str] = None) -> None:
        """接收一条新消息（文本/图片/两者）进行缓冲与聚合。
        图片不再即时触发独立轮次，与文本走同一套缓冲逻辑，避免"几句话+一个表情包"被拆开。
        """
        loop = asyncio.get_running_loop()
        now = loop.time()

        if text:
            self._text_buffer.append(text)
        if image_path:
            # 一轮内多图时保留最后一张（更可能是机主当下要问的那张）
            self._image_buffer = image_path

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
        """合并缓冲中的文本与图片并提交到队列"""
        if not self._text_buffer and not self._image_buffer:
            return

        combined_text = "\n".join(self._text_buffer).strip()
        image_path = self._image_buffer

        self._text_buffer.clear()
        self._image_buffer = None
        self._first_msg_time = 0.0
        self._debounce_task = None

        logger.info(
            f"[Aggregator] 聚合静默到期，提交一轮: text='{combined_text}'"
            f"{f', img={image_path}' if image_path else ''}"
        )
        self._queue.put_nowait((combined_text, image_path))
