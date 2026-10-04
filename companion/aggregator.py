"""消息聚合器 (aggregator.py)
对机主连发的短消息进行 6 秒静默聚合与 15 秒硬等待合并，图片消息与文本共用同一缓冲窗口。
使用异步任务队列保证生成回复时不丢弃、不插队。

FIXES21：批次里除了拼好的文本，还要保住**每条消息的编号与 message_id**
（发侧引用要靠它指回"他哪一条"）。批次元素形如
`{"index": 1, "text": "...", "message_id": 123, "has_image": False}`，
index 从 1 起，编号与 message_id 一一对应，模型看到的 [1][2][3] 就是这几个。

FIXES23：**他打字她等**。静默窗只认"停手 6 秒"，他打字慢、句间停超过 6 秒，
她就抢话把一段话劈成两截。NapCat 会上报对方输入状态（见 onebot.py），
这个信号让聚合窗"看见他在打字"：他手指没停，她就再等一等。
**纯增强**——收不到输入状态事件时 `_peer_typing` 恒为 False，
本文件行为与改动前逐字节一致（既有 test_fixes9 锁定 6.0/15.0 两个常量）。
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
# FIXES23 输入状态绝对上限：同样从本轮第一条消息起算，**他还在打字也只延到这么久**。
# 等待可以因为"他在打字"被延长，但绝不能被续命——他真写小作文她也不能等到天黑。
#
# 实测提醒（2026-10-04 冒烟）：这根线在真机上**基本够不着**——协议层的 15 秒自愈
# （onebot.INPUT_STATUS_STALE_LIMIT）几乎总与下面的 15 秒硬上限同时到点，轮次总在
# ~15 秒就关了。它是**冗余兜底**（自愈失效、或结束事件永远不来时的最后一道闸），
# 不是生效路径。真机可见的效果是"她最多把整段话攒住约 15 秒"。
TYPING_ABSOLUTE_LIMIT = 30.0

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
        # FIXES23：他此刻手指是否还搭在键盘上（由 onebot 的输入状态事件驱动）
        self._peer_typing: bool = False
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

        # FIXES23 双上限：他在打字时天花板从 15s 抬到 30s，但抬不抬**只取决于他手指停没停**。
        # 收不到输入状态事件时 _peer_typing 恒为 False，走的还是原来那条 15s 线。
        active_limit = TYPING_ABSOLUTE_LIMIT if self._peer_typing else HARD_LIMIT

        # 达到硬上限（从本轮第一条消息起算），立即触发
        if elapsed >= active_limit:
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
            self._flush_buffer()
            return

        # 重置静默计时器，但永不越过硬上限
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        if self._peer_typing:
            # 手指还搭在键盘上：他刚发的这句可能只是个逗号，静默窗在这里不适用，
            # 改挂"等到绝对上限"（同一条 deadline，重发不续命）
            self._debounce_task = asyncio.create_task(
                self._wait_typing_until_deadline(self._first_msg_time + TYPING_ABSOLUTE_LIMIT)
            )
            return

        wait_time = min(SILENCE_WINDOW, active_limit - elapsed)
        self._debounce_task = asyncio.create_task(self._wait_and_flush(wait_time))

    def notify_peer_typing(self, is_typing: bool) -> None:
        """他打字她等（FIXES23）：对方输入状态联动聚合窗。

        **缓冲为空时也要把状态记下来，但不起任何表**——"他没说话光打字"不能
        凭空等一轮，可这条知识更不能扔：真机上提示几乎总是**先于**消息到达
        （消息走内部队列异步消费，输入状态在读循环里同步处理），
        照任务书字面"缓冲为空直接忽略"的话，这条信号会被系统性丢掉，功能等于死的。
        下一条消息进缓冲时 `push_message` 会看到 `_peer_typing`，改挂绝对上限。

        开始输入 → 撤掉静默计时，改挂"等他停手"；停手 → 重新起满 SILENCE_WINDOW。

        纯同步（不 await）：onebot 在读循环里就地调它抢时间，排队会把这个信号作废。
        """
        if not self._running:
            # stop() 之后仍可能收到在途帧：必须直接丢弃，
            # 否则会把已经收尾的计时器重新拉起来（新的一条泄漏路径）
            return

        now = asyncio.get_running_loop().time()
        elapsed = now - self._first_msg_time
        has_buffer = bool(self._text_buffer) or bool(self._image_buffer)

        if is_typing:
            if self._peer_typing:
                # 重复上报：不重置任何东西。允许它续命的话，
                # 连发心跳就能把"他在打字"永远挂着，静默窗形同虚设。
                return
            self._peer_typing = True
            if not has_buffer:
                # 只记状态，不起表（这一条不该让她凭空等任何东西）
                logger.debug("[Aggregator] 缓冲为空，记下他已在打字，等他先开口")
                return
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
            self._debounce_task = asyncio.create_task(
                self._wait_typing_until_deadline(self._first_msg_time + TYPING_ABSOLUTE_LIMIT)
            )
            logger.info(
                f"[Aggregator] 他还在打字，暂不插话（本轮已等 {elapsed:.1f}s，静默窗已撤）"
            )
            return

        # 停手
        was_typing = self._peer_typing
        self._peer_typing = False
        if not has_buffer or not was_typing:
            # 无处可收的结束事件（乱序/重复/还没开口），别去重启静默窗——
            # 那等于凭空给一个早就该结束的窗口又续了 6 秒
            return

        if elapsed >= HARD_LIMIT:
            logger.info(
                f"[Aggregator] 他已停手，但本轮已等 {elapsed:.1f}s 已过 {HARD_LIMIT:.0f}s 硬上限，立刻回"
            )
            if self._debounce_task and not self._debounce_task.done():
                self._debounce_task.cancel()
            self._flush_buffer()
            return

        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        wait_time = min(SILENCE_WINDOW, HARD_LIMIT - elapsed)
        self._debounce_task = asyncio.create_task(self._wait_and_flush(wait_time))
        logger.info(
            f"[Aggregator] 他已停手，重新起静默窗 {wait_time:.1f}s（本轮已等 {elapsed:.1f}s）"
        )

    async def _wait_typing_until_deadline(self, deadline: float) -> None:
        """他一直在打字：睡到绝对上限就 flush，谁还在打字都不好使。

        刻意不轮询、不等"停手事件"——停手由 notify_peer_typing(False) 取消本任务、
        另挂静默窗。这样 `_debounce_task` 这个槽位始终只有一个活计时器，
        stop() 沿用原来的清理即可，不添新的泄漏路径。
        """
        try:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._flush_buffer()
        except asyncio.CancelledError:
            pass

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
        # FIXES23：一轮结束，下一轮从"他没在打字"重新开始
        self._peer_typing = False

        quoteable = sum(1 for b in batch if b.get("message_id") is not None)
        logger.info(
            f"[Aggregator] 聚合静默到期，提交一轮: {len(batch)} 条"
            f"（其中 {quoteable} 条带 message_id、可被引用）, "
            f"text='{combined_text}'"
            f"{f', img={image_path}' if image_path else ''}"
        )
        self._queue.put_nowait((combined_text, image_path, batch))
