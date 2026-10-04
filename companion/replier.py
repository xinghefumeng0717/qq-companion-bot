"""回复管道 (replier.py)
对 LLM 回复进行旁白剥离、表情包解析、QQ系统表情解析、句式切段、打字延迟模拟并分段发送。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from companion.config import ReplyConfig
from companion.faces import face_id_by_name
from companion.prompts import PROMPT_FACE_TAGS
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

# QQ 系统表情标记正则（FIXES20 任务3）：[face:标签名]
# 刻意不用裸标签 [旺柴]——那是收侧翻出来的**读**侧形态（她读他消息用），
# 裸标签在发侧分不清"这句是她要写给他看的字"还是"她要发的脸"，会撞车。
FACE_PATTERN = re.compile(r"\[face[:：]([^\]]+)\]", re.IGNORECASE)

# 两类标记合并成一条正则，一次扫描切出全部段：
# group(1) 命中 = sticker，group(2) 命中 = face，group(3) 命中 = quote。
# 用一条而不是多条，是为了保证"文字/表情包/表情/引用"在原句里的相对顺序
# 一次扫清（分多次扫会打乱顺序）。
# quote 带 ^ 与 MULTILINE：**只在行首匹配**——引用是"我回哪句"的指代，
# 位置固定在回复头部；写在一句话中间的 [quote:2] 一律当普通文字（降级不炸）。
MIXED_SEGMENT_PATTERN = re.compile(
    r"\[(?:sticker|表情)[:：]([^\]]+)\]"
    r"|\[face[:：]([^\]]+)\]"
    r"|^[ \t]*\[quote[:：](\d+)\]",
    re.IGNORECASE | re.MULTILINE,
)

# 整轮 QQ 表情硬上限（机制层，按机主真实使用数据定的刻度）：
# 他的纯表情气泡 92% 是 1~2 个，同款二连合法（他的语料里有 107 条二连），
# 三连罕见（21 条）。发侧取"每轮 ≤2"，同时也天然满足"同一气泡 ≤2"。
FACE_MAX_PER_TURN = 2

# typing/段间延迟里，一个非文字段（表情包或表情）折算成几个字——
# 沿用表情包既有折算（原来 sticker 就是这个 5），一个脸不该触发长打字。
NON_TEXT_CHUNK_CHARS = 5

# 沉默标记（FIXES13）：模型完整输出恰好是这一行时，表示她本轮选择不回。
# 只有"整条输出就是它"才算数；行内含 [沉默] 但还夹着别的文字的，一律按正常文本走，防滥用。
SILENCE_TOKEN = "[沉默]"

# 引用标记（FIXES21）：行首的 [quote:N]
QUOTE_PATTERN = re.compile(r"^[ \t]*\[quote[:：](\d+)\]", re.IGNORECASE | re.MULTILINE)


def strip_leading_quote(text: str) -> Tuple[str, Optional[int]]:
    """剥掉回复开头的引用标记行，返回 (剩下的文本, 编号)。

    **沉默权优先（FIXES21 备注要求定义的交互）**：模型偶尔同时输出
    `[quote:2]` 和 `[沉默]`。这两件事是矛盾的（引用=我回你第2句，沉默=我不回），
    让沉默赢：剩下的文本若恰好是 [沉默]，整轮按沉默处理。
    所以这里把引用标记先剥掉，再交给既有的 `is_silence_output` 判定——
    `is_silence_output` 本身的契约（"整条输出就是它"）一个字没动。
    """
    if not text:
        return text, None
    m = QUOTE_PATTERN.match(text)
    if not m:
        return text, None
    rest = text[m.end():]
    # 只吃紧跟着的空白/换行："[quote:2]你真棒" 里的正文不许被吃掉
    rest = re.sub(r"^[ \t]*\n?", "", rest)
    return rest, int(m.group(1))


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
    """把段列表压到 max_chunks 以内：优先保留表情包段与QQ表情段，先丢普通文本段。

    表情包是模型明确要求的整条内容，静默丢掉会改变回复的语义与态度；
    QQ 表情同理（它是她语气的一部分，丢了就变成"干巴巴一句话"，正是本次要治的病）；
    文字段少发一句只损失信息，不影响表达。保留的段维持原有先后顺序。
    """
    if max_chunks <= 0 or len(chunks) <= max_chunks:
        return chunks

    # 表情类段（表情包 / QQ 表情 / 混排气泡）优先保
    rich = [c for c in chunks if c["type"] in ("sticker", "face", "combo")]
    if len(rich) >= max_chunks:
        # 表情类自身就超限：只能按顺序取前 max_chunks 个，文字段全部让位
        return rich[:max_chunks]

    text_quota = max_chunks - len(rich)
    kept: List[Dict[str, Any]] = []
    text_used = 0
    for c in chunks:
        if c["type"] in ("sticker", "face", "combo"):
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


# ── QQ 系统表情发侧（FIXES20 任务3）─────────────────────────────────


def face_chunk(tag: str) -> Optional[Dict[str, Any]]:
    """把 [face:标签] 变成 face 段；清单外 / 表里查不到 → None（调用方按文字降级）。

    白名单校验在这一层做，两道关：
      1. 标签名必须在 PROMPT_FACE_TAGS（提示词清单，发侧能发出的那批）；
      2. 必须能在 NapCat 表里反查到 id（清单与表不同步时的兜底）。
    任一不过就返回 None——**不静默丢**，调用方会把标记还原成普通文字发出去。

    纯函数、不记日志：判定只在这里做一次，日志由 normalize_face_markers 统一出，
    免得同一个降级标记打两遍 INFO。
    """
    name = (tag or "").strip()
    if not name:
        return None
    if name not in PROMPT_FACE_TAGS:
        return None
    face_id = face_id_by_name(name)
    if face_id is None:
        return None
    return {"type": "face", "tag": name, "id": face_id}


def normalize_face_markers(text: str) -> str:
    """把**发不出去**的 [face:标签] 原地还原成普通文字，可发的标记原样留下。

    为什么在标记扫描之前做：降级后的标记就是普通文字，让它自然落进"文字段"，
    于是它和它前后同一行的字留在**同一个气泡**里（"你真棒[face:微笑]" 一条发出去），
    而不是被拆成"你真棒" + "[face:微笑]"两个气泡——后者看起来像她突然开始说脏话。

    只处理非法标记，合法标记一个字节都不动（下游按标记正常解析）。
    """
    if "[face" not in text and "[FACE" not in text and "［face" not in text:
        return text  # 绝大多数回复没这个标记，省一次全串扫描

    out: List[str] = []
    last = 0
    downgraded: List[str] = []
    for m in FACE_PATTERN.finditer(text):
        tag = m.group(1) or ""
        if face_chunk(tag) is not None:
            continue
        out.append(text[last : m.start()])
        out.append(m.group(0))  # 标记原样留下，当普通文字
        downgraded.append(tag.strip())
        last = m.end()
    if not downgraded:
        return text
    out.append(text[last:])
    logger.info(
        f"[Replier] QQ表情不在可用清单/表里，按普通文字原样发出: {downgraded}"
    )
    return "".join(out)


def keep_face_cap(
    chunks: List[Dict[str, Any]], limit: int = FACE_MAX_PER_TURN
) -> List[Dict[str, Any]]:
    """整轮 QQ 表情硬上限：最多 limit 个，超出的**按普通文字降级**（不丢内容）。

    降级而不是丢弃，和白名单外标签同一口径：模型连甩一串 `[face:x][face:y]...` 时，
    机主屏幕上会出现字面量 `[face:某标签]`（难看但可解释、且不丢她说的话），
    而不是她想发的那个脸**无声无息地少一个**——后者会让她显得欲言又止。
    降级动作记 INFO，事后能查是谁甩的串。
    """
    used = 0
    downgraded: List[str] = []
    out: List[Dict[str, Any]] = []
    for c in chunks:
        if c.get("type") != "face":
            out.append(c)
            continue
        if used < limit:
            used += 1
            out.append(c)
            continue
        marker = f"[face:{c.get('tag', '')}]"
        downgraded.append(marker)
        out.append({"type": "text", "content": marker})
    if downgraded:
        logger.info(
            f"[Replier] QQ表情硬上限：整轮只保留 {limit} 个，"
            f"多余 {len(downgraded)} 个按普通文字降级: {downgraded}"
        )
    return out


def _line_no(text: str, idx: int) -> int:
    """idx 位置在 text 里是第几行（0 起）。段级行号只用来判"是不是同一行"。"""
    return text.count("\n", 0, idx)


def merge_face_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 face 段并进**同一行**的前一个段，合成一条 QQ 消息（任务3 第3条主形态）。

    为什么要合：OneBot 一条消息可以混合 text + face 多个段，机主看到的是
    "你真棒"后面紧跟一个狗头的气泡；拆成两条就变成两个气泡，语气全断。
    这也是机主真实发法（他的混排占 65%，其中 97% 挂在句尾）。

    合并后段类型是 `combo`，带有序 parts 列表；发送端（main._send_chunk_to_onebot）
    按 parts 顺序拼 OneBot 消息段数组。表情包**不参与合并**——QQ 表情包段的既有语义
    一个字都不改（负面清单第1条），它仍然是独立一条。

    两种合并：
      1. 紧跟同一行的文字 → text + face 的 combo（"你真棒[face:doge]" 一个气泡）
      2. 紧跟同一行的另一个 face → 纯脸 combo（他语料里 107 条同款二连是**一条**
         气泡里两个脸，不是两个气泡）

    跨行绝不合并（`chunk_text_sentences` 的硬边界：换行 = 两条独立气泡）：
    "你真棒[face:doge]\\n[face:吃瓜]" 仍是两条——第 2 条是独立气泡的纯脸消息，
    机主语料里"文字气泡后 60 秒内紧跟纯表情气泡"245 次就是这个形态。

    前面没有可合并的段（行首纯脸、或前面是引用段）→ 保持独立 face 段。
    引用段不参与 face 合并：它还没并进任何消息，等下一步 merge_quote_into_next
    把它贴到紧随的那条消息头部（这样 "[quote:3][face:吃瓜]" 是一条 reply+face 消息，
    而不是 face 被吸进一个还没有正文的壳子里、把引用弄丢）。
    """
    out: List[Dict[str, Any]] = []
    for c in chunks:
        ctype = c.get("type")
        if ctype != "face" or not out:
            out.append(c)
            continue
        prev = out[-1]
        prev_type = prev.get("type")
        # sticker 永不合并；跨行也不合并（行号不相等即视为不同气泡）
        if prev_type in ("sticker", "quote") or prev.get("_end_line") != c.get("_line"):
            out.append(c)
            continue
        if prev_type == "face":
            # 纯脸二连 → 一个 combo（可以没有文字 parts）
            out[-1] = {
                "type": "combo",
                "parts": [
                    {"type": "face", "tag": prev["tag"], "id": prev["id"]},
                    {"type": "face", "tag": c["tag"], "id": c["id"]},
                ],
                "_end_line": c.get("_line"),
            }
            continue
        if prev_type == "text":
            parts = [
                {"type": "text", "content": prev["content"]},
                {"type": "face", "tag": c["tag"], "id": c["id"]},
            ]
        else:  # 已经是 combo：接在后面继续加脸（同款二连/连着两个不同脸）
            parts = list(prev.get("parts", [])) + [
                {"type": "face", "tag": c["tag"], "id": c["id"]}
            ]
        out[-1] = {"type": "combo", "parts": parts, "_end_line": c.get("_line")}
    return out


def chunk_text_weight(chunk: Dict[str, Any]) -> int:
    """一个段折算成多少"字"，用于段间延迟与打字表演（不让一个脸触发长 typing）。

    沿用表情包的既有折算：非文字段一律算 5 字（原来 sticker 就是这么折的）。
    combo（文字+脸混排）按 文字真实字数 + 每个脸 5 字算——脸本身不"打字"。
    """
    ctype = chunk.get("type")
    if ctype == "text":
        return len(chunk.get("content", ""))
    if ctype == "combo":
        total = 0
        for part in chunk.get("parts", []):
            if part.get("type") == "text":
                total += len(part.get("content", ""))
            else:
                total += NON_TEXT_CHUNK_CHARS
        return total
    return NON_TEXT_CHUNK_CHARS


def strip_face_markers(text: str) -> str:
    """把记录文本里的 [face:标签] 抹掉，只留"她真正打出来的字"。

    给 typing 时长用（FIXES20 任务3 第6条）：一个 3 字短句后面挂个脸，
    不该因为标记字符把打字时间拉长。表情包标记**不动**（既有折算保持原样）。
    """
    return FACE_PATTERN.sub("", text or "")


# ── 引用回复发侧（FIXES21）─────────────────────────────────


def quote_chunk(index: int, quote_targets: Optional[List[Dict[str, Any]]]) -> Optional[Dict[str, Any]]:
    """把 `[quote:N]` 变成 quote 段；不可引用时返回 None（调用方按普通文字处理）。

    四道关，任何一道不过都降级（**不炸、不丢内容**）：
      1. 编号得是本轮批次里真实存在的（越界 → 降级，防她引用不存在的东西）；
      2. 那条得带 message_id（没有 id 就没法在 QQ 上真的引用 → 降级）；
      3. message_id 必须是整数型（入站原样透传，出现脏值宁可降级也别发一个坏 id）；
      4. 主动消息（source="proactive"）调用方直接不传 quote_targets，
         那里根本没有可引用的对方消息。
    """
    if not quote_targets:
        return None
    target = None
    for item in quote_targets:
        if item.get("index") == index:
            target = item
            break
    if target is None:
        logger.info(f"[Replier] 引用编号越界（本轮没有第 {index} 条），按普通文字降级")
        return None
    message_id = target.get("message_id")
    if message_id is None:
        logger.info(
            f"[Replier] 第 {index} 条没有 message_id（历史/异常消息），本次不可引用，按文字降级"
        )
        return None
    try:
        message_id = int(message_id)
    except (TypeError, ValueError):
        logger.warning(f"[Replier] 第 {index} 条的 message_id 不是整数（{message_id!r}），按文字降级")
        return None
    return {"type": "quote", "index": index, "message_id": message_id}


def keep_first_quote(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """整轮只保留第一个引用段，多余的**按普通文字降级**（FIXES21 任务2 第3条）。

    引用是低频高精度动作：一轮里连着引两条既没意义（她只回一条消息），
    也会把 5 段预算吃光。降级而不是丢弃，与 face 超限同一口径。
    """
    used = False
    downgraded: List[int] = []
    out: List[Dict[str, Any]] = []
    for c in chunks:
        if c.get("type") != "quote":
            out.append(c)
            continue
        if not used:
            used = True
            out.append(c)
            continue
        marker = f"[quote:{c.get('index', '')}]"
        downgraded.append(c.get("index"))
        out.append({"type": "text", "content": marker})
    if downgraded:
        logger.info(
            f"[Replier] 引用硬上限：整轮只保留 1 条，多余 {len(downgraded)} 条按文字降级: "
            f"{downgraded}"
        )
    return out


def merge_quote_into_next(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把 quote 段并进**紧随其后的第一条消息**，作为它的 `_quote` 头（任务3 第1条）。

    为什么不单独发：OneBot 一条消息可以混 `reply` + `text` + `face` 段，
    "引用他第2条 + 回复内容"是同一条消息出去；单独发一条空引用就是刷屏。

    合并后的段可以是 text / face / combo（combo 里已有文字与脸），
    这样 quote × face 同轮共存不需要新的段型——`_quote` 是一个可选头，
    发送端（main._send_chunk_to_onebot）负责把它拼到段数组最前面。

    quote 后面没有任何正文（模型只输出了 `[quote:N]`）→ **直接丢弃**
    （任务3 第2条：不空发）。丢弃记 INFO，事后能看出模型想干吗。
    """
    out: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None
    dropped: List[int] = []

    for c in chunks:
        ctype = c.get("type")
        if ctype == "quote":
            if pending is not None:
                # 上一条引用还没等到正文，又来一条：前一个直接丢（上限已保证只有 1 条，
                # 这里是防御：万一上游改了上限，也不会连着发两条空引用）
                dropped.append(pending.get("index"))
            pending = c
            continue
        if pending is not None:
            if ctype in ("text", "face", "combo"):
                merged = dict(c)
                merged["_quote"] = {
                    "index": pending.get("index"),
                    "message_id": pending.get("message_id"),
                }
                out.append(merged)
                pending = None
                continue
            # 表情包段：引用不与表情包合并（表情包语义一个字不改），
            # 此时引用只能丢掉——不能让一条表情包消息凭空顶着一个引用
            dropped.append(pending.get("index"))
            pending = None
        out.append(c)

    if pending is not None:
        dropped.append(pending.get("index"))
    if dropped:
        logger.info(f"[Replier] 引用后面没有正文，丢弃引用（不空发）: {dropped}")
    return out


def combo_to_display(chunk: Dict[str, Any]) -> str:
    """combo 段的可读形态：文字后面直接挂上 [标签]（人看的形态）。

    落库记录用 [face:标签]（能自愈：她复读时会被重新解析成真表情），
    但 transcript/仪表盘这类给人看的地方用这个短形态，读起来就是她发出去的样子。
    """
    parts = chunk.get("parts", [])
    text = "".join(p.get("content", "") for p in parts if p.get("type") == "text")
    tags = "".join(f"[{p.get('tag', '')}]" for p in parts if p.get("type") == "face")
    return f"{text}{tags}"


def chunk_record_text(chunk: Dict[str, Any]) -> str:
    """一个段在落库记录里的形态（每段一行，对齐 QQ 上的多条气泡）。

    - text：原文
    - sticker：`[表情:描述词]`（既有形态，一个字都不改）
    - face / combo：`[face:标签]`
      用显式段语法而不是裸 `[旺柴]`：裸标签在记录里分不清"她当时发的是一张脸"
      还是"她写了这几个字"，而显式语法**能自愈**——她以后复读这条历史时，
      `[face:旺柴]` 会被 parse_reply 重新解析成真表情，裸标签则会原样当文字发出去。
      （收侧读他消息用的是裸标签形态，那里有接收方与词表兜底，语境不同。）
    """
    ctype = chunk.get("type")
    if ctype == "text":
        return chunk.get("content", "")
    if ctype == "sticker":
        return f"[表情:{chunk.get('desc', '')}]"
    if ctype == "face":
        return f"[face:{chunk.get('tag', '')}]"
    if ctype == "combo":
        parts = chunk.get("parts", [])
        text = "".join(p.get("content", "") for p in parts if p.get("type") == "text")
        tags = "".join(f"[face:{p.get('tag', '')}]" for p in parts if p.get("type") == "face")
        return f"{text}{tags}"
    return ""


class Replier:
    def __init__(
        self,
        config: ReplyConfig,
        stickers: StickerManager,
    ):
        self.config = config
        self.stickers = stickers

    @staticmethod
    def _append_marker_as_text(
        segments: List[Dict[str, Any]], marker: str, line: int
    ) -> bool:
        """把"发不出去"的标记（清单外 face / 不可引用的 quote）留在原地当普通文字。

        同一行时**粘回前一段文字**而不是新起一段：否则"你真棒[face:微笑]"会被拆成
        "你真棒" + "[face:微笑]"两个气泡，看起来像她突然开始说标记语法。

        返回 True = 已粘进前一段；False = 前面没有可粘的文字（标记在行首），
        调用方负责把它吸进**后面**那句（行首标记只能往前走，不能凭空成一条气泡）。
        """
        if (
            segments
            and segments[-1]["type"] == "text"
            and segments[-1].get("_end_line") == line
        ):
            segments[-1]["content"] += marker
            return True
        return False

    def is_silence_decision(
        self,
        raw_text: str,
        quote_targets: Optional[List[Dict[str, Any]]] = None,
    ) -> bool:
        """这一轮算不算"她选择沉默"（**带 [quote:N] 前缀的也算**）

        为什么要有这个方法（FIXES21 终审打回的真 bug）：调用方在 `parse_reply`
        之后还要自己判一次沉默（决定要不要落库、要不要结算 observer），
        判的却是**原始**输出全文。模型输出 `[quote:2]\\n[沉默]` 时，
        `is_silence_output(full)` 是 False（原文不是"整条就是 [沉默]"），
        于是流程落到 `if not chunks:` 的兜底，真的把"刚刚走神了……你再说一次？"
        发到了机主眼前——她明明选的是沉默。任务书 FIXES21 备注要求的
        "quote × 沉默权（沉默优先）"就是被这么破坏的。

        本方法与 `parse_reply` 的 0/0.5 步**同源**（同一个 strip_leading_quote +
        同一个 quote_chunk 校验），改一处必须改两处，所以逻辑只写在这里、
        parse_reply 也调它。两处判不一致的代价就是"静默失效型"事故。

        额外覆盖一种情况：**只输出 `[quote:N]` 没有正文**。引用被丢弃后等于
        什么都没说，按任务书"quote 后没有正文不空发"的意图，同样按沉默处理
        （而不是掉进"走神了"兜底，那是在说假话）。
        """
        stripped, index = strip_leading_quote(raw_text)
        if is_silence_output(stripped):
            return True
        if index is not None and not stripped.strip():
            # 前缀是一个**真的能引用**的编号 + 后面没正文 = 她没打算说话
            if quote_chunk(index, quote_targets) is not None:
                return True
        return False

    def parse_reply(
        self,
        raw_text: str,
        source: str = "reply",
        quote_targets: Optional[List[Dict[str, Any]]] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """处理回复全文：
        0. 沉默权（FIXES13）：完整输出恰为 [沉默] -> 返回 ([], "")
        0.5 剥掉回复开头的 [quote:N] 标记（FIXES21）：沉默权优先于引用
        1. 字面量换行还原
        2. 行首触发方向标签剥离
        3. 旁白剥离
        4. sticker / face / quote 标记与文字混排拆分（一次扫描，保原句顺序）
        5. 整行兜底滤网：图片占位符（[图片] 这类，FIXES12 / E8）→ 内心旁白（FIXES19）
        6. 句子切段
        7. 硬上限：表情包整轮 1 个 → QQ 表情整轮 2 个 → 引用整轮 1 条
        8. QQ 表情并进紧邻文字段（混排合并成同一个气泡）
        9. 引用并进紧随其后的第一条消息（_quote 头）
        10. 压到 max_chunks 以内（优先保表情包/表情/混排段）
        返回: (发送消息段列表, 纯文本记录)

        source 只用于占位符兜底的 INFO 日志标注（"reply" 主聊 / "proactive" 主动消息），
        默认主聊，既有调用方无需改动。

        `quote_targets`（FIXES21）：本轮聚合批次的每条消息
        （`{"index": 1, "text": "...", "message_id": 123}`）。它是**编号 → 真实
        message_id 的唯一翻译表**：不在表里的编号一律降级成文字。
        主动消息不传（那里没有可引用的对方消息），所以 proactive 通路天然不支持引用。

        落库记录由最终发出的段反推，实发多少就记多少：
        被截断丢弃的文字段不会留在记录里，不丢表情包段、不丢 QQ 表情段。
        引用**不进落库记录**：编号只对当轮有效，写进历史就成了无效指代
        （她以后读到 `[quote:2]` 会以为 2 还指着今天第二条）。

        **与滤网的先后关系（FIXES20 任务3 第7条，DEEP_AUDIT 面对账用）**：
        face / quote 标记的识别都在第 4 步，和 sticker 同一位置——即**整行滤网之前**。
        滤网（第5步）只作用于**文字部分**，face/sticker/quote 段原样穿过。
        推论与已知代价：模型若把一句话用 [face:] 从中间劈开（"他走了[face:流泪]我难受"），
        整行滤网看到的是劈开后的两个片段而不是整行，判断依据变窄——这与既有的
        sticker 行为**完全同构**（表情包标记同样会劈行），本次不引入新差异，
        也不为它扩大改动范围（宁漏勿错是这两道滤网的设计取舍）。
        quote 只在行首匹配（第 4 步的 ^ + MULTILINE），所以它**劈不出**行内片段，
        不给滤网制造新的判断歧义。
        """
        # 0.5 FIXES21：先剥引用标记，让沉默权有优先权（"引用+沉默"矛盾时沉默赢）
        #     剥之前先验一遍能不能用：能用的剥掉并单独存成 quote 段（在第 4 步前插到
        #     最前面，后面的合并阶段自然把它并进第一条消息）；不能用的**不剥**，
        #     让它按普通文字原样留在原位——降级口径与行中/越界的引用完全一致。
        stripped_text, leading_index = strip_leading_quote(raw_text)
        leading_quote = (
            quote_chunk(leading_index, quote_targets)
            if leading_index is not None
            else None
        )
        if leading_index is not None and leading_quote is None:
            logger.info(
                f"[Replier] 回复开头的引用（编号 {leading_index}）不可用，标记按普通文字原样保留"
            )
        else:
            raw_text = stripped_text

        # 0. 沉默权（FIXES13）：完整输出恰为 [沉默] -> 不发送、记录为空（其余情况不触发）
        #    判定走 is_silence_decision（与调用方同源），别在这里另写一份：
        #    带 [quote:N] 前缀的沉默与"只有引用没正文"也归它管。
        if self.is_silence_decision(raw_text, quote_targets):
            logger.info(
                "[Replier] 命中沉默：模型本轮不发送（[沉默] 或只有引用没有正文），记录为空"
            )
            return [], ""

        # 1. 字面量 \n 还原为真换行（模型常把换行写成两个字符）
        clean_text = unescape_literal_newlines(raw_text)

        # 2. 行首触发方向标签剥离（【起】/【接】/【收】）
        clean_text = strip_direction_tag(clean_text)

        # 3. 旁白剥离
        clean_text = strip_narration(clean_text)

        # 3.5 FIXES20：发不出去的 [face:] 标记原地还原成普通文字。
        #     放在标记扫描之前——降级后它就是普通文字，自然落进同一个文字段、
        #     留在同一个气泡里，不会被拆成"你真棒" + "[face:微笑]"两条。
        clean_text = normalize_face_markers(clean_text)

        # 4. 表情包 / QQ 表情标记匹配与切分（一条正则一次扫，原句顺序原样保留）
        #    每段带一个 "_end_line"：它结束在第几行。合并阶段靠它判断
        #    "这个脸和前面那句是不是同一行"（换行是硬边界，跨行不合并）。
        segments: List[Dict[str, Any]] = []
        last_idx = 0

        # 0.5 段剥出来的合法引用插到最前面：它本来就该是整条回复的头
        if leading_quote is not None:
            leading_quote["_line"] = 0
            leading_quote["_end_line"] = 0
            segments.append(leading_quote)

        # 降级标记的"吸进后面文字"暂存位（FIXES21）：
        # 降级后的标记必须留在**同一行那句话**里，不能自己占一条气泡。
        # face 不用暂存位——它总在文字后面，粘回前一段即可；
        # quote 可以在行首（那时前面没有任何文字可粘），只能吸向后面。
        pending_leading_marker: Optional[str] = None

        for m in MIXED_SEGMENT_PATTERN.finditer(clean_text):
            start, end = m.span()
            # 前置文字（先把暂存的降级标记吸进它，同一行必须是一条气泡）
            if start > last_idx:
                txt = clean_text[last_idx:start]
                if pending_leading_marker:
                    txt = pending_leading_marker + txt
                    pending_leading_marker = None
                if txt.strip():
                    segments.append(
                        {
                            "type": "text",
                            "content": txt,
                            "_end_line": _line_no(clean_text, start),
                        }
                    )

            sticker_desc = m.group(1)
            face_tag_in_match = m.group(2)
            quote_index = m.group(3)
            line = _line_no(clean_text, start)

            if sticker_desc is not None:
                # 表情包
                desc = sticker_desc.strip()
                sticker_path = self.stickers.match_sticker(desc)
                if sticker_path:
                    # desc 只用于落库记录，发送方只认 file
                    segments.append(
                        {"type": "sticker", "file": sticker_path, "desc": desc}
                    )
                else:
                    logger.info(f"[Replier] 表情包未匹配，丢弃标记: [sticker:{desc}]")
            elif quote_index is not None:
                # FIXES21 引用段。行首才认（正则里带了 ^）；编号能不能翻译由 quote_chunk 判
                qc = quote_chunk(int(quote_index), quote_targets)
                if qc is not None:
                    qc["_line"] = line
                    qc["_end_line"] = line
                    segments.append(qc)
                else:
                    # 越界/无 id/主动消息 → 按普通文字原样留在原位。
                    # 同一行粘回前一段文字；行首（前面没文字可粘）就暂存起来吸进后面。
                    if not self._append_marker_as_text(segments, m.group(0), line):
                        pending_leading_marker = m.group(0)
            else:
                # QQ 系统表情。3.5 步已把不合法的降级成文字了，这里拿到 None
                # 只可能是防御性分支（正则与 normalize 不一致），照样按文字处理。
                fc = face_chunk(face_tag_in_match or "")
                if fc is not None:
                    # 脸在原文里只占一个点：它的 "_end_line" 就是它自己那行
                    # （不给的话，后一个脸与它"同段"的比较会落空，二连合并不上）
                    fc["_line"] = line
                    fc["_end_line"] = line
                    segments.append(fc)
                else:
                    if not self._append_marker_as_text(segments, m.group(0), line):
                        pending_leading_marker = m.group(0)

            last_idx = end

        # 尾部文字（同样先吸暂存的降级标记）
        if last_idx < len(clean_text):
            txt = clean_text[last_idx:]
            if pending_leading_marker:
                txt = pending_leading_marker + txt
                pending_leading_marker = None
            if txt.strip():
                segments.append(
                    {
                        "type": "text",
                        "content": txt,
                        "_end_line": _line_no(clean_text, len(clean_text)),
                    }
                )
        elif pending_leading_marker:
            # 整条输出只剩一个降级标记（"[quote:9]"）：它自己就是正文，不丢
            segments.append({"type": "text", "content": pending_leading_marker,
                             "_end_line": _line_no(clean_text, len(clean_text))})

        # 5. 整行兜底滤网（都在标记拆分之后、切句之前：此时文字段还是模型原样的
        #    多行文本，切句会把它拆散，滤网就再也认不出"整行"了。整段被滤空则整段丢弃）
        #    5a. 整行图片占位符（FIXES12 任务1 / E8）
        #    5b. 整行内心旁白（FIXES19）
        #    非文字段（sticker / face / quote）原样穿过，滤网只管文字
        filtered_segments: List[Dict[str, Any]] = []
        for seg in segments:
            if seg["type"] in ("sticker", "face", "quote"):
                filtered_segments.append(seg)
                continue
            kept_text = drop_image_placeholder_lines(seg["content"], source)
            kept_text = drop_inner_narration_lines(kept_text, source)
            if kept_text.strip():
                seg["content"] = kept_text
                filtered_segments.append(seg)
        segments = filtered_segments

        # 6. 展开文字段切句
        #    切出来的子段只有**最后一个**继承原段的 "_end_line"：它才是这段文字
        #    真正结束的那一行，前面几句结束在更早的行上（不能都继承，否则
        #    "第一句[face:x]\n第二句" 这种会被误判成同一行）。
        final_chunks: List[Dict[str, Any]] = []
        for seg in segments:
            if seg["type"] in ("sticker", "face", "quote"):
                final_chunks.append(seg)
                continue
            sub_chunks = chunk_text_sentences(seg["content"], max_chunks=self.config.max_chunks)
            for i, sc in enumerate(sub_chunks):
                is_last = i == len(sub_chunks) - 1
                chunk = {"type": "text", "content": sc}
                if is_last:
                    chunk["_end_line"] = seg.get("_end_line")
                final_chunks.append(chunk)

        # 7. 硬上限：表情包整轮 1 个、QQ 表情整轮 2 个、引用整轮 1 条
        #    （都必须在**合并之前**：合并后的 face 段长在 combo.parts 里，
        #    到那时再数就数不到、闸门会形同虚设）
        final_chunks = keep_first_sticker(final_chunks)
        final_chunks = keep_face_cap(final_chunks)
        final_chunks = keep_first_quote(final_chunks)

        # 8. 混排合并（FIXES20 任务3 第3条）：同一行的文字+脸并成同一条 QQ 消息。
        #    必须在 fit_chunks **之前**：max_chunks 限的是"几条气泡"，
        #    "你真棒[doge]"合并后是 1 条气泡、合并前却是 2 段——
        #    先 fit 会把并进去的那条文字当成超编挤掉（实战里被挤掉的是"在呢"这种收尾句）。
        final_chunks = merge_face_chunks(final_chunks)

        # 8.5 FIXES21：引用并进紧随其后的第一条消息（_quote 头）。
        #     也在 fit_chunks **之前**：引用不是一条独立气泡，不该占 max_chunks 的名额。
        final_chunks = merge_quote_into_next(final_chunks)

        # 9. 总量控制：超限先丢普通文本段，表情包/表情/混排气泡优先保留
        final_chunks = fit_chunks(final_chunks, self.config.max_chunks)

        # 10. 记录与实发一致：每段一条，段间用换行对齐 QQ 上的多条气泡
        record_parts = [
            chunk_record_text(c) for c in final_chunks
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
            # FIXES20：文字量按 chunk_text_weight 折算（face/combo 都走这一份），
            # 语义与改前一致：文字段按真实字数、非文字段（含表情包）按 5 字
            char_count = chunk_text_weight(chunk)
            typing_delay = char_count * 0.03
            total_delay = base_delay + typing_delay

            if i == 0:
                total_delay *= 0.5

            await asyncio.sleep(total_delay)

            # 调用发送函数
            await send_fn(chunk)
