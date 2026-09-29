"""消息聚合器 (aggregator.py)
对机主连发的短消息进行 3 秒静默聚合与 8 秒硬等待合并，图片消息即刻触发独立轮次。
使用异步任务队列保证生成回复时不丢弃、不插队。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, List, Optional, Tuple

logger = logging.getLogger(__name__)


class MessageAggregator:
    def __init__(
        self,
        turn_handler: Callable[[str, Optional[str]], Coroutine[Any, Any, None]],
    ):
        self.turn_handler = turn_handler
        self._text_buffer: List[str] = []
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
        """接收一条新消息进行缓冲与聚合"""
        loop = asyncio.get_event_loop()
        now = loop.time()

        # 1. 包含图片：不等待聚合，单独触发一轮
        if image_path:
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
                self._debounce_task = None

            # 将之前缓冲的文字与当前文字合并
            combined_text = "\n".join(self._text_buffer + ([text] if text else [])).strip()
            self._text_buffer.clear()
            self._first_msg_time = 0.0

            logger.info(f"[Aggregator] 收到图片消息，即刻提交本轮: '{combined_text}', img={image_path}")
            await self._queue.put((combined_text, image_path))
            return

        # 2. 纯文本消息：启动/重置 3 秒静默计时器，上限 8 秒
        if text:
            self._text_buffer.append(text)

        if not self._text_buffer:
            return

        if self._first_msg_time == 0.0:
            self._first_msg_time = now

        elapsed = now - self._first_msg_time
        if elapsed >= 8.0:
            # 达到 8 秒硬上限，立即触发
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
            self._flush_text_buffer()
            return

        # 重置 3 秒静默计时器（但不超过 8 秒硬上限）
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        remaining_to_hard_limit = 8.0 - elapsed
        wait_time = min(3.0, remaining_to_hard_limit)
        self._debounce_task = asyncio.create_task(self._wait_and_flush(wait_time))

    async def _wait_and_flush(self, wait_seconds: float) -> None:
        try:
            await asyncio.sleep(wait_seconds)
            self._flush_text_buffer()
        except asyncio.CancelledError:
            pass

    def _flush_text_buffer(self) -> None:
        """合并缓冲消息并提交到队列"""
        if not self._text_buffer:
            return
        combined_text = "\n".join(self._text_buffer).strip()
        self._text_buffer.clear()
        self._first_msg_time = 0.0
        self._debounce_task = None

        logger.info(f"[Aggregator] 聚合静默到期，提交一轮文本输入: '{combined_text}'")
        self._queue.put_nowait((combined_text, None))
