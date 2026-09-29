"""安全兜底规则 (safety.py)
纯关键词规则，不使用 LLM，不可配置关闭。
根据机主消息触发危机或关注提示，注入系统提示词末尾。
"""

from __future__ import annotations

import logging
from typing import Optional

from companion.prompts import CRISIS_PROMPT, WATCH_PROMPT

logger = logging.getLogger(__name__)

CRISIS_KEYWORDS = ["不想活", "想死", "自杀", "自残", "活不下去"]
WATCH_KEYWORDS = ["离不开你", "只有你了", "没有你我怎么办"]


class SafetyChecker:
    @staticmethod
    def check_message(user_message: str) -> Optional[str]:
        """检查用户消息是否触发安全关键词。
        返回注入 prompt 的提示词内容，未命中返回 None。
        """
        if not user_message:
            return None

        for kw in CRISIS_KEYWORDS:
            if kw in user_message:
                logger.warning(f"[Safety] 命中危机关键词 (crisis): {kw}")
                return CRISIS_PROMPT

        for kw in WATCH_KEYWORDS:
            if kw in user_message:
                logger.warning(f"[Safety] 命中关注关键词 (watch): {kw}")
                return WATCH_PROMPT

        return None
