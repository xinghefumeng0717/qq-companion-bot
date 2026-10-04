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

# 沉默标记（FIXES13）：模型完整输出恰好是这一行时，表示她本轮选择不回。
# 只有"整条输出就是它"才算数；行内含 [沉默] 但还夹着别的文字的，一律按正常文本走，防滥用。
SILENCE_TOKEN = "[沉默]"


def is_silence_output(text: str) -> bool:
    """模型完整输出（strip 后）是否恰好等于 [沉默]"""
    return text.strip() == SILENCE_TOKEN

# 切句分隔符正则（保留标点）
SENTENCE_SPLIT_PATTERN = re.compile(r"([^。！？!?\n~～]+[。！？!?\n~～]*)")

# 「图片」占位符兜底（FIXES12 任务1 / E8）：她没有摄像头，模型却会硬着头皮输出
# 字面量 [图片] 当普通文字发到 QQ 上（生产实锤：10-04 09:19 主动消息）。
# 提示词层已加了"不能拍照"的禁令，这里是第二道保险。
# 只丢「整行就是占位符」的行；行内夹杂其他文字的（今天[图片]里那只猫）一律保留。
IMAGE_PLACEHOLDER_PATTERN = re.compile(
    r"^[ \t]*(?:\[[ \t]*(?:图片|照片|image)[ \t]*\]|【[ \t]*(?:图片|照片)[ \t]*】)[ \t]*$",
    re.IGNORECASE,
)


def drop_image_placeholder_lines(text: str, source: str = "reply") -> str:
    """丢弃整行就是 [图片]/[照片]/[image] 这类占位符的行，行内夹杂文字的保留。

    必须在切句之前调用：切句会把行拆散，滤网就再也认不出"整行"了。
    丢弃时记 INFO 日志（内容 + 来源），便于回溯是哪条通路漏出来的。
    """
    kept: List[str] = []
    dropped: List[str] = []
    for line in text.split("\n"):
        if IMAGE_PLACEHOLDER_PATTERN.match(line):
            dropped.append(line.strip())
        else:
            kept.append(line)
    if dropped:
        logger.info(
            f"[Replier] 图片占位符兜底：丢弃 {len(dropped)} 行整行占位符"
            f"（来源: {source}）: {dropped}"
        )
    return "\n".join(kept)


# ── 内心旁白滤网（FIXES19）──────────────────────────────────────────
# 病灶证据：FIXES18 对聊仿真 S1 局（data/duo_sim/S1-smoke-20261004/transcript.md
# 第 9 轮）她把第三人称内心旁白当普通气泡发了出去——
#   「行吧，饿着躺。/ 我刚到楼门口，摸兜里还压着半块黑巧。/ 这人嘴硬，我折回去看看。」
# 病根：角色卡禁的是**括号旁白**（strip_narration 兜底），堵住了动作描写的传统形式，
# 却管不住"不加括号、整句内心戏直接上屏"这条漏网之路。剧情走到动作节拍
# （她决定折回去送巧克力）时动作没有合法出口，模型就从漏洞里挤出来。
# 本滤网是**机制层兜底**：模型再犯也到不了机主眼前。真正的治疗在卡内（红线流程）。
#
# 判定纪律：**三个条件同时命中才丢，缺一不可；宁漏勿错**。
#   a. 行内有第三人称指代机主：他（人称代词）/这人/这家伙/那家伙/那小子
#   b. 行内有第一人称动作或心理动词组：我 + 折回/回去/过去/…（见下方词表）
#   c. 整行**不含"你"**——她对机主说话用"你"，含"你"就是在对他说话，一律放行
#      （这是防误杀的保险丝，宁可放过一条旁白也不能删掉她正常说的话）
#
# 词表来源：S1 真实翻车原句 + 按同一病句式枚举（见 tests/test_fixes19.py）。
# 以后有新翻车句，照着往词表里加即可，不必改逻辑。

# a. 第三人称指代。
#    「他」必须排除长在词里的情况：其他（其）、吉他（吉）不是人称代词；
#    「他们/她们」是复数泛指、不是专指机主，按宁漏勿错也不当作命中信号。
INNER_NARRATION_THIRD_PERSON = re.compile(
    r"(?:(?<![其吉])他(?!们)|这人|这家伙|那家伙|那小子)"
)

# b. 第一人称动作/心理动词组。词表按动作与心理两类枚举。
INNER_NARRATION_VERBS: Tuple[str, ...] = (
    # 移动类
    "折回", "折返", "回去", "过去", "去找", "去看", "回来", "跟过去",
    # 观看/揣测类
    "回", "走", "跑", "看", "瞧", "想", "猜", "琢磨", "寻思",
    # 决断类
    "决定", "打算",
    # 情绪/身体类
    "忍", "憋", "笑", "叹", "摇头", "点头",
    # 操作类
    "翻", "摸", "掏", "关", "开", "拿", "放",
)
INNER_NARRATION_FIRST_PERSON = re.compile(
    r"我(?:" + "|".join(INNER_NARRATION_VERBS) + r")"
)


def is_inner_narration_line(line: str) -> bool:
    """整行判定：这一行是不是"她在心里演一遍、却直接发给了他"的内心旁白。

    三个条件缺一不可，见上方词表处的说明。c 条（含"你"就放行）是防误杀保险丝。
    """
    if not line.strip():
        return False
    if "你" in line:                     # c：含"你" = 在对他说话，放行
        return False
    if not INNER_NARRATION_THIRD_PERSON.search(line):   # a
        return False
    if not INNER_NARRATION_FIRST_PERSON.search(line):   # b
        return False
    return True


def drop_inner_narration_lines(text: str, source: str = "reply") -> str:
    """丢弃整行都是内心旁白的行，行内夹杂正常对话的一律保留（宁漏勿错）。

    必须在切句之前调用（与 drop_image_placeholder_lines 同理：切句会把行拆散，
    滤网就再也认不出"整行"了）。丢弃时记 INFO 日志（内容 + 来源）。

    已知盲区（设计取舍，不是 bug）：判定是**整行**级的，所以模型若把一句旁白拆成
    多行（"这人嘴硬。\\n我折回去看看。"），每行各缺一个条件，会整体漏过去。
    任务书第 2 条负面清单明确"不追求行内夹杂旁白的检测（超范围，宁漏勿错）"，
    本滤网只承诺挡住"整行都是旁白"这一种形态。真实翻车原句恰好是整行形态
    （"这人嘴硬，我折回去看看。"），实测可拦。
    """
    kept: List[str] = []
    dropped: List[str] = []
    for line in text.split("\n"):
        if is_inner_narration_line(line):
            dropped.append(line.strip())
        else:
            kept.append(line)
    if dropped:
        logger.info(
            f"[Replier] 内心旁白兜底：丢弃 {len(dropped)} 行整行旁白"
            f"（来源: {source}）: {dropped}"
        )
    return "\n".join(kept)


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
    #    用 \n 而不是空串黏合：换行是硬边界，"" 会把两条独立气泡拼成一句黏话
    if len(chunks) > max_chunks:
        kept = chunks[: max_chunks - 1]
        tail = "\n".join(chunks[max_chunks - 1 :])
        kept.append(tail)
        chunks = kept

    return [c.strip() for c in chunks if c.strip()]


def fit_chunks(chunks: List[Dict[str, Any]], max_chunks: int) -> List[Dict[str, Any]]:
    """把段列表压到 max_chunks 以内：优先保留表情包段，先丢普通文本段。

    表情包是模型明确要求的整条内容，静默丢掉会改变回复的语义与态度；
    文字段少发一句只损失信息，不影响表达。保留的段维持原有先后顺序。
    """
    if max_chunks <= 0 or len(chunks) <= max_chunks:
        return chunks

    stickers = [c for c in chunks if c["type"] == "sticker"]
    if len(stickers) >= max_chunks:
        # 表情包自身就超限：只能按顺序取前 max_chunks 个，文字段全部让位
        return stickers[:max_chunks]

    text_quota = max_chunks - len(stickers)
    kept: List[Dict[str, Any]] = []
    text_used = 0
    for c in chunks:
        if c["type"] == "sticker":
            kept.append(c)
        elif text_used < text_quota:
            kept.append(c)
            text_used += 1
    return kept


def keep_first_sticker(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整轮只保留第一个表情包段，多余的表情包段丢弃（FIXES11 任务5 硬上限）。

    提示词里"整轮最多一次"只是软约束，模型照样可能连发几个 [sticker:...]。
    这里是代码层的防线：无论模型输出几个表情包，实发最多一个，且保留最靠前的那个
    （位置最贴近她本该发的那句话）。位于表情包解析之后、fit_chunks 之前。
    """
    seen_sticker = False
    dropped: List[str] = []
    kept: List[Dict[str, Any]] = []
    for c in chunks:
        if c["type"] == "sticker":
            if seen_sticker:
                dropped.append(str(c.get("desc", "")))
                continue
            seen_sticker = True
        kept.append(c)
    if dropped:
        logger.info(
            f"[Replier] 表情包硬上限：整轮只保留 1 个，丢弃多余 {len(dropped)} 个: {dropped}"
        )
    return kept


class Replier:
    def __init__(
        self,
        config: ReplyConfig,
        stickers: StickerManager,
    ):
        self.config = config
        self.stickers = stickers

    def parse_reply(self, raw_text: str, source: str = "reply") -> Tuple[List[Dict[str, Any]], str]:
        """处理回复全文：
        0. 沉默权（FIXES13）：完整输出恰为 [沉默] -> 返回 ([], "")
        1. 字面量换行还原
        2. 行首触发方向标签剥离
        3. 旁白剥离
        4. sticker 标记与文字混排拆分
        5. 整行兜底滤网：图片占位符（[图片] 这类，FIXES12 / E8）→ 内心旁白（FIXES19）
        6. 句子切段
        7. 表情包硬上限（整轮只留第一个）
        8. 压到 max_chunks 以内（优先保表情包）
        返回: (发送消息段列表, 纯文本记录)

        source 只用于占位符兜底的 INFO 日志标注（"reply" 主聊 / "proactive" 主动消息），
        默认主聊，既有调用方无需改动。

        落库记录由最终发出的段反推，实发多少就记多少：
        被截断丢弃的文字段不会留在记录里，不丢表情包段。
        """
        # 0. 沉默权（FIXES13）：完整输出恰为 [沉默] -> 不发送、记录为空（其余情况不触发）
        if is_silence_output(raw_text):
            logger.info("[Replier] 命中沉默：模型完整输出为 [沉默]，本轮不发送、记录为空")
            return [], ""

        # 1. 字面量 \n 还原为真换行（模型常把换行写成两个字符）
        clean_text = unescape_literal_newlines(raw_text)

        # 2. 行首触发方向标签剥离（【起】/【接】/【收】）
        clean_text = strip_direction_tag(clean_text)

        # 3. 旁白剥离
        clean_text = strip_narration(clean_text)

        # 4. 表情包标记匹配与切分
        segments: List[Dict[str, Any]] = []
        last_idx = 0

        for m in STICKER_PATTERN.finditer(clean_text):
            start, end = m.span()
            # 前置文字
            if start > last_idx:
                txt = clean_text[last_idx:start]
                if txt.strip():
                    segments.append({"type": "text", "content": txt})

            # 表情包
            sticker_desc = m.group(1).strip()
            sticker_path = self.stickers.match_sticker(sticker_desc)
            if sticker_path:
                # desc 只用于落库记录，发送方只认 file
                segments.append(
                    {"type": "sticker", "file": sticker_path, "desc": sticker_desc}
                )
            else:
                logger.info(f"[Replier] 表情包未匹配，丢弃标记: [sticker:{sticker_desc}]")

            last_idx = end

        # 尾部文字
        if last_idx < len(clean_text):
            txt = clean_text[last_idx:]
            if txt.strip():
                segments.append({"type": "text", "content": txt})

        # 5. 整行兜底滤网（都在 sticker 拆分之后、切句之前：此时文字段还是模型原样的
        #    多行文本，切句会把它拆散，滤网就再也认不出"整行"了。整段被滤空则整段丢弃）
        #    5a. 整行图片占位符（FIXES12 任务1 / E8）
        #    5b. 整行内心旁白（FIXES19）
        filtered_segments: List[Dict[str, Any]] = []
        for seg in segments:
            if seg["type"] == "sticker":
                filtered_segments.append(seg)
                continue
            kept_text = drop_image_placeholder_lines(seg["content"], source)
            kept_text = drop_inner_narration_lines(kept_text, source)
            if kept_text.strip():
                seg["content"] = kept_text
                filtered_segments.append(seg)
        segments = filtered_segments

        # 6. 展开文字段切句
        final_chunks: List[Dict[str, Any]] = []
        for seg in segments:
            if seg["type"] == "sticker":
                final_chunks.append(seg)
            else:
                sub_chunks = chunk_text_sentences(seg["content"], max_chunks=self.config.max_chunks)
                for sc in sub_chunks:
                    final_chunks.append({"type": "text", "content": sc})

        # 7. 表情包硬上限：整轮只保留最靠前的第一个（提示词软约束之外的代码防线）
        final_chunks = keep_first_sticker(final_chunks)

        # 8. 总量控制：超限先丢普通文本段，表情包段优先保留
        final_chunks = fit_chunks(final_chunks, self.config.max_chunks)

        # 9. 记录与实发一致：每段一条，段间用换行对齐 QQ 上的多条气泡
        record_parts = [
            c["content"] if c["type"] == "text" else f"[表情:{c.get('desc', '')}]"
            for c in final_chunks
        ]
        clean_record_text = "\n".join(record_parts).strip()
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
