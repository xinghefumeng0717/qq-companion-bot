"""回复管道 (replier.py)
对 LLM 回复进行旁白剥离、表情包解析、句式切段、打字延迟模拟并分段发送。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from companion.config import ReplyConfig
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

# 旁白与动作描写正则兜底
NARRATION_PATTERN = re.compile(
    r"（[^（）\n]{1,40}）|\([^()\n]{1,40}\)|\*[^*\n]{1,40}\*"
)

# 表情包标记正则（同时兼容 [sticker:xxx]、[表情:xxx] 以及中英文冒号）
STICKER_PATTERN = re.compile(r"\[(?:sticker|表情)[:：]([^\]]+)\]", re.IGNORECASE)

# 切句分隔符正则（保留标点）
SENTENCE_SPLIT_PATTERN = re.compile(r"([^。！？!?\n~～]+[。！？!?\n~～]*)")


def strip_narration(text: str) -> str:
    """旁白剥离兜底：删除疑似动作描写段，删除时记 WARNING 日志"""
    matches = NARRATION_PATTERN.findall(text)
    if matches:
        for m in matches:
            logger.warning(f"[Replier] 旁白剥离兜底命中: {m}")
        text = NARRATION_PATTERN.sub("", text)
    return text.strip()


def chunk_text_sentences(text: str, max_chunks: int = 5) -> List[str]:
    """切段：按 。！？!?\n~～ 切句，相邻短句（合计 <= 15 字）合并，每段 1~2 句，总段数 <= max_chunks"""
    raw_sentences = [s.strip() for s in SENTENCE_SPLIT_PATTERN.findall(text) if s.strip()]
    if not raw_sentences:
        if text.strip():
            return [text.strip()]
        return []

    # 1. 相邻短句合并（合计 <= 15 字）
    merged_units: List[str] = []
    current_unit = ""

    for s in raw_sentences:
        if not current_unit:
            current_unit = s
        else:
            if len(current_unit) + len(s) <= 15:
                current_unit += s
            else:
                merged_units.append(current_unit)
                current_unit = s
    if current_unit:
        merged_units.append(current_unit)

    # 2. 每段 1~2 句打包
    chunks: List[str] = []
    i = 0
    while i < len(merged_units):
        # 如果剩下不止一句，随机或成对合并成 1~2 句
        if i + 1 < len(merged_units) and len(merged_units[i]) + len(merged_units[i + 1]) <= 25:
            chunks.append(merged_units[i] + merged_units[i + 1])
            i += 2
        else:
            chunks.append(merged_units[i])
            i += 1

    # 3. 总段数 <= max_chunks，超出部分并入最后一段
    if len(chunks) > max_chunks:
        kept = chunks[: max_chunks - 1]
        tail = "".join(chunks[max_chunks - 1 :])
        kept.append(tail)
        chunks = kept

    return [c.strip() for c in chunks if c.strip()]


class Replier:
    def __init__(
        self,
        config: ReplyConfig,
        stickers: StickerManager,
    ):
        self.config = config
        self.stickers = stickers

    def parse_reply(self, raw_text: str) -> Tuple[List[Dict[str, Any]], str]:
        """处理回复全文：
        1. 旁白剥离
        2. sticker 标记与文字混排拆分
        3. 句子切段
        返回: (发送消息段列表, 纯文本记录)
        """
        # 1. 旁白剥离
        clean_text = strip_narration(raw_text)

        # 2. 表情包标记匹配与切分
        segments: List[Dict[str, Any]] = []
        last_idx = 0
        clean_record_parts = []

        for m in STICKER_PATTERN.finditer(clean_text):
            start, end = m.span()
            # 前置文字
            if start > last_idx:
                txt = clean_text[last_idx:start]
                if txt.strip():
                    segments.append({"type": "text", "content": txt})
                    clean_record_parts.append(txt)

            # 表情包
            sticker_desc = m.group(1).strip()
            sticker_path = self.stickers.match_sticker(sticker_desc)
            if sticker_path:
                segments.append({"type": "sticker", "file": sticker_path})
                clean_record_parts.append(f"[表情:{sticker_desc}]")
            else:
                logger.info(f"[Replier] 表情包未匹配，丢弃标记: [sticker:{sticker_desc}]")

            last_idx = end

        # 尾部文字
        if last_idx < len(clean_text):
            txt = clean_text[last_idx:]
            if txt.strip():
                segments.append({"type": "text", "content": txt})
                clean_record_parts.append(txt)

        # 3. 展开文字段切句并控制总段数 <= max_chunks
        final_chunks: List[Dict[str, Any]] = []
        for seg in segments:
            if seg["type"] == "sticker":
                final_chunks.append(seg)
            else:
                sub_chunks = chunk_text_sentences(seg["content"], max_chunks=self.config.max_chunks)
                for sc in sub_chunks:
                    final_chunks.append({"type": "text", "content": sc})

        # 控制总段数 <= max_chunks
        if len(final_chunks) > self.config.max_chunks:
            kept = final_chunks[: self.config.max_chunks - 1]
            remaining = final_chunks[self.config.max_chunks - 1 :]
            # 将多余内容合并（如果是文字）
            merged_content = ""
            for r in remaining:
                if r["type"] == "text":
                    merged_content += r["content"]
                elif r["type"] == "sticker":
                    if merged_content:
                        kept.append({"type": "text", "content": merged_content})
                        merged_content = ""
                    kept.append(r)
            if merged_content:
                kept.append({"type": "text", "content": merged_content})
            final_chunks = kept[: self.config.max_chunks]

        clean_record_text = "".join(clean_record_parts).strip()
        return final_chunks, clean_record_text

    async def send_reply_chunks(
        self,
        chunks: List[Dict[str, Any]],
        send_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
    ) -> None:
        """分段发送：段间随机延迟 + 模拟打字延迟，首段延迟减半"""
        for i, chunk in enumerate(chunks):
            # 计算延迟
            base_delay = random.uniform(self.config.chunk_delay_min, self.config.chunk_delay_max)
            char_count = len(chunk["content"]) if chunk["type"] == "text" else 5
            typing_delay = char_count * 0.03
            total_delay = base_delay + typing_delay

            if i == 0:
                total_delay *= 0.5

            await asyncio.sleep(total_delay)

            # 调用发送函数
            await send_fn(chunk)
