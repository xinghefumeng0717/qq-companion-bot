"""安全兜底规则 (safety.py)
纯关键词规则，不使用 LLM，不可配置关闭。
根据机主消息触发危机或关注提示，注入系统提示词末尾。

关键词用裸 in 匹配会误伤高频撒娇话术（"我都想死你了"），
因此关键词表与排除规则表分开维护：命中关键词后再看后一个字是否落进排除表。
"""

from __future__ import annotations

import logging
from typing import Dict, Optional, Tuple

from companion.prompts import CRISIS_PROMPT, WATCH_PROMPT

logger = logging.getLogger(__name__)

CRISIS_KEYWORDS = ["不想活", "想死", "自杀", "自残", "活不下去"]
WATCH_KEYWORDS = ["离不开你", "只有你了", "没有你我怎么办"]

# 误伤排除规则：关键词后面紧跟这些字时属于日常语义，不算危机。
# "想死你了"/"想死我了" 是伴侣间的撒娇；"不想活动" 是懒得动弹。
CRISIS_EXCLUSIONS: Dict[str, Tuple[str, ...]] = {
    "想死": ("你", "您", "我", "他", "她", "它"),
    "不想活": ("动",),
}


def _is_excluded(text: str, keyword: str, index: int) -> bool:
    """keyword 在 index 处命中后，紧接着的那个字若在排除表里则视为日常语义"""
    followers = CRISIS_EXCLUSIONS.get(keyword)
    if not followers:
        return False
    next_char = text[index + len(keyword): index + len(keyword) + 1]
    return next_char in followers


def _match_crisis_keyword(user_message: str) -> Optional[str]:
    """按关键词表逐个扫描，返回第一个未被排除规则排除的危机关键词"""
    for keyword in CRISIS_KEYWORDS:
        start = 0
        while True:
            index = user_message.find(keyword, start)
            if index < 0:
                break
            if not _is_excluded(user_message, keyword, index):
                return keyword
            # 该处是日常用法，继续往后找同一个关键词的下一次出现
            start = index + 1
    return None


class SafetyChecker:
    @staticmethod
    def check_message(user_message: str) -> Optional[str]:
        """检查用户消息是否触发安全关键词。
        返回注入 prompt 的提示词内容，未命中返回 None。
        """
        if not user_message:
            return None

        crisis_kw = _match_crisis_keyword(user_message)
        if crisis_kw:
            logger.warning(f"[Safety] 命中危机关键词 (crisis): {crisis_kw}")
            return CRISIS_PROMPT

        for kw in WATCH_KEYWORDS:
            if kw in user_message:
                logger.warning(f"[Safety] 命中关注关键词 (watch): {kw}")
                return WATCH_PROMPT

        return None
