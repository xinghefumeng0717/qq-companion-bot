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

# 角色卡示例的「触发方向」标签（【起】/【接】/【收】）
# assembler.py 把 examples 原样拼进提示词，模型有概率把标签复读进回复正文。
# 按行剥离：replier 会按 \n 切段后逐条发送，模型可能每行都加标签，
# 只剥首行会让第二行标签粘在第二条气泡上发出去。
# 只认行首且只认这三个字，正文中偶然出现的【】不受影响。
DIRECTION_TAG_PATTERN = re.compile(r"^[ \t]*【[起收接]】[ \t\n]*", re.MULTILINE)


def strip_direction_tag(text: str) -> str:
    """剥离行首的【起】/【接】/【收】标签，仅在确实命中时记 WARNING 日志"""
    stripped = DIRECTION_TAG_PATTERN.sub("", text)
    if stripped != text:
        logger.warning("[Replier] 触发方向标签剥离: %s", text.strip()[:30])
    return stripped

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


# 模型有时把换行写成字面量 "\n"（两个字符：反斜杠 + n）而不是真换行。
# 提示词里"多条用换行分隔"这类措辞会诱发这种输出，而 SENTENCE_SPLIT_PATTERN
# 只认真换行，字面量会原样发到 QQ 上。
LITERAL_NEWLINE_PATTERN = re.compile(r"\\r\\n|\\n|\\r")


def unescape_literal_newlines(text: str) -> str:
    """把字面量 \\n / \\r\\n 还原为真换行"""
    return LITERAL_NEWLINE_PATTERN.sub("\n", text)


def chunk_text_sentences(text: str, max_chunks: int = 5) -> List[str]:
    """切段：换行是硬边界（模型写的换行 = 两条独立气泡，绝不合并）；
    行内按 。！？!?~～ 切句后合并相邻短句（合计 <= 15 字），
    再按 1~2 句打包，总段数 <= max_chunks。
    """
    chunks: List[str] = []

    # 逐行独立成条：换行不再参与相邻合并，避免 "刚出琴房教学区的灯亮了" 这种黏句
    for line in text.split("\n"):
        if not line.strip():
            continue
        raw_sentences = [s.strip() for s in SENTENCE_SPLIT_PATTERN.findall(line) if s.strip()]
        if not raw_sentences:
            chunks.append(line.strip())
            continue

        # 1. 行内相邻短句合并（合计 <= 15 字）
        merged_units: List[str] = []
        current_unit = ""
        for s in raw_sentences:
            if not current_unit:
                current_unit = s
            elif len(current_unit) + len(s) <= 15:
                current_unit += s
            else:
                merged_units.append(current_unit)
                current_unit = s
        if current_unit:
            merged_units.append(current_unit)

        # 2. 每段 1~2 句打包
        i = 0
        while i < len(merged_units):
            if i + 1 < len(merged_units) and len(merged_units[i]) + len(merged_units[i + 1]) <= 25:
                chunks.append(merged_units[i] + merged_units[i + 1])
                i += 2
            else:
                chunks.append(merged_units[i])
                i += 1

    if not chunks:
        if text.strip():
            return [text.strip()]
        return []

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
        1. 字面量换行还原
        2. 行首触发方向标签剥离
        3. 旁白剥离
        4. sticker 标记与文字混排拆分
        5. 句子切段
        返回: (发送消息段列表, 纯文本记录)
        """
        # 1. 字面量 \n 还原为真换行（模型常把换行写成两个字符）
        clean_text = unescape_literal_newlines(raw_text)

        # 2. 行首触发方向标签剥离（【起】/【接】/【收】）
        clean_text = strip_direction_tag(clean_text)

        # 3. 旁白剥离
        clean_text = strip_narration(clean_text)

        # 4. 表情包标记匹配与切分
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

        # 5. 展开文字段切句并控制总段数 <= max_chunks
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
