"""语音未落地后的模型补救：把真实结果交回角色，而不是发统一话术。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from companion.replier import has_voice_intent, has_voice_promise, sanitize_voice_claims

logger = logging.getLogger(__name__)


async def recover_voice_text(
    *,
    replier: Any,
    assembler: Any,
    gateway: Any,
    user_text: str,
    draft: str,
    reason: str,
    model: str,
    timeout: float = 8.0,
) -> Optional[str]:
    """让角色自己补写语音失败后的文字；失败/不合格时返回 None。

    这里不做第二次发送，也不生成统一兜底句。调用方决定保留原文还是使用
    返回的自然补救文本。任何异常都开放为 None，保证原消息不会丢。
    """
    if not (draft or "").strip() or replier is None or assembler is None or gateway is None:
        return None
    try:
        messages, _ = await assembler.assemble_messages(
            user_text,
            None,
            voice_allowed=False,
            voice_gated_note=True,
            voice_delivery_note=reason,
        )
        messages.append({
            "role": "user",
            "content": (
                f"你刚才想发一段语音，但真实结果是：{reason[:160]}\n"
                f"原语音正文：{draft.strip()[:300]}\n"
                "请只用你自己的口吻补一两句文字：承认这次没发成，"
                "不要假装已经发了或传过去了，不要写任何语音标记，"
                "不要照搬系统提示或固定兜底句。"
            ),
        })
        response = await asyncio.wait_for(
            gateway.chat(
                messages=messages,
                model=model or gateway.config.text_model,
                temperature=0.7,
                purpose="voice_recovery",
                max_retries=0,
            ),
            timeout=timeout,
        )
        if not response or has_voice_intent(response) or has_voice_promise(response):
            return None
        chunks, _ = replier.parse_reply(response.strip(), voice_allowed=False)
        text = "\n".join(
            c.get("content", "") for c in chunks if c.get("type") == "text"
        ).strip()
        if not text:
            return None
        # 补救稿也不许把失败说成成功；不接受时回退给调用方的原文字。
        cleaned = sanitize_voice_claims(
            text, voice_allowed=False, previous_voice_sent=False, voice_context=True
        )
        return cleaned if cleaned == text else None
    except Exception as e:
        logger.warning(f"[VoiceRecovery] 模型补救失败，保留原文字: {e}")
        return None
