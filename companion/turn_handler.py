"""单轮对话流水线处理器 (companion/turn_handler.py)
从 main.py 圈养式解耦：承接消息聚合回调，负责多模态视觉感知、提示词组装、LLM流式生成、分段打字发送、落库、回忆加固与观察者结算触发。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable, Coroutine, Dict, Optional

from companion.assembler import PromptAssembler
from companion.config import Config
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.observer import Observer
from companion.proactive import ProactiveScheduler
from companion.prompts import VISION_PERCEPTION_PROMPT
from companion.replier import Replier
from companion.stickers import image_to_base64_data_url

logger = logging.getLogger("companion")


class TurnHandler:
    def __init__(
        self,
        config: Config,
        gateway: LLMGateway,
        assembler: PromptAssembler,
        replier: Replier,
        memory: MemoryManager,
        observer: Observer,
        proactive: ProactiveScheduler,
        send_chunk_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
    ):
        self.config = config
        self.gateway = gateway
        self.assembler = assembler
        self.replier = replier
        self.memory = memory
        self.observer = observer
        self.proactive = proactive
        self.send_chunk_fn = send_chunk_fn

    async def handle_turn(self, user_text: str, image_path: Optional[str]) -> None:
        """聚合完毕后，处理完整的一轮对话"""
        logger.info(f"[Bot] 处理新一轮输入: '{user_text}', image={image_path}")

        # 1. 重置主动消息未回计数
        await self.proactive.reset_unanswered_count()

        # 2. 图像多模态处理与降级
        image_data_url = None
        target_model = self.config.llm.text_model

        if image_path:
            data_url, err = image_to_base64_data_url(image_path)
            if err:
                # 超过 10MB 等错误回复
                await self.send_chunk_fn({"type": "text", "content": err})
                return

            if self.config.llm.vision_model:
                try:
                    logger.info("[Bot] 使用 Flash 视觉模型提取图片场景细节...")
                    desc_resp = await self.gateway.chat(
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": VISION_PERCEPTION_PROMPT},
                                    {"type": "image_url", "image_url": {"url": data_url}},
                                ],
                            }
                        ],
                        model=self.config.llm.vision_model,
                        purpose="vision_perception",
                    )
                    clean_desc = desc_resp.strip()[:120]
                    logger.info(f"[Bot] 视觉提取成功: {clean_desc}")
                    user_text = f"{user_text} [发来一张照片：{clean_desc}]".strip()
                except Exception as e:
                    logger.warning(f"[Bot] 视觉模型提取异常，降级为占位符: {e}")
                    user_text = f"{user_text} [发来一张图片，但没能看清]".strip()
                # 设计承重墙：两段式视觉设计（flash 看图 -> 描述注入主提示词），置空避免激活 assembler 的直接多模态分支
                image_data_url = None
            else:
                user_text = (user_text + " [对方发来一张图片，你看不到内容]").strip()

        # 3. 提示词组装
        messages, sys_prompt = await self.assembler.assemble_messages(user_text, image_data_url)

        # 4. LLM 流式调用
        reply_parts = []
        try:
            async for piece in self.gateway.stream_chat(
                messages=messages,
                model=target_model,
                purpose="main_chat",
            ):
                reply_parts.append(piece)
        except Exception as e:
            logger.error(f"[Bot] 调用 LLM 异常: {e}")
            # 兜底文案不能含括号/星号，否则会被旁白剥离正则整块剥除导致用户端静默
            reply_parts = ["刚刚走神了……你再说一次？"]

        full_reply = "".join(reply_parts).strip()
        logger.info(f"[Bot] LLM 回复全文: {full_reply}")

        # 5. 回复管道切段与打字延迟发送
        chunks, clean_record_text = self.replier.parse_reply(full_reply)
        if not chunks:
            chunks = [{"type": "text", "content": "刚刚走神了……你再说一次？"}]
            clean_record_text = "刚刚走神了……你再说一次？"
        await self.replier.send_reply_chunks(chunks, self.send_chunk_fn)

        # 6. 本轮对话落库
        await self.memory.save_turn_pair(
            user_msg=user_text,
            bot_msg=clean_record_text,
            has_image=bool(image_path),
        )

        # 7. 回忆加固
        await self.memory.reinforce_memories(user_text)

        # 8. 观察者异步结算（不阻塞）
        asyncio.create_task(
            self.observer.settle_turn(
                user_message=user_text,
                assistant_reply=clean_record_text,
                user_image_path=image_path,
            )
        )
