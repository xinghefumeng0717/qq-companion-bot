"""共享两段式视觉感知模块。

旧 turn_handler 与新 production 统一调用本模块提取图片描述。
purpose 契约统一为 "vision_perception"，截断统一为 120 字。
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from companion.prompts import VISION_PERCEPTION_PROMPT

logger = logging.getLogger(__name__)

STICKER_VISION_PROMPT = (
    "这是聊天中附带的一个表情。用一句短话说明它在对话里的语气或用途，"
    "例如偷笑、撒娇、无语、告别、故意逗对方；先看画面和神态，不要写成图片赏析，"
    "不要逐条罗列颜色与构图。拿不准就只说画面神态。"
)


async def extract_image_description(
    gateway: Any,
    model: str,
    data_url: str,
    prompt: str = VISION_PERCEPTION_PROMPT,
) -> Optional[str]:
    """使用视觉模型提取图片描述。

    成功返回截断至 120 字的描述文本；视觉模型调用异常返回 None。
    """
    try:
        logger.info("[Bot] 使用 Flash 视觉模型提取图片场景细节...")
        desc_resp = await gateway.chat(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            model=model,
            purpose="vision_perception",
        )
        if not desc_resp or not str(desc_resp).strip():
            logger.warning("[Bot] 视觉模型返回空内容，降级为占位符")
            return None
        clean_desc = str(desc_resp).strip()[:120]
        logger.info(f"[Bot] 视觉提取成功: {clean_desc}")
        return clean_desc
    except Exception as e:
        logger.warning(f"[Bot] 视觉模型提取异常，降级为占位符: {e}")
        return None
