"""内容驱动的语音规划：主聊只产自然正文，从原文选连续片段并形成结构化语音段。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

from companion.tts import truncate_for_voice

# 明确请求正则：必须是本轮明确索要/请求语音，而非对声音的评价或喜好（保留导出供主流程使用）
VOICE_REFUSAL_RE = re.compile(
    r"(?:不|别|不要|不想|不用|甭)\s*(?:再|又)?\s*(?:给[我你他她它]|跟[我你他她它])?\s*(?:发|录|唱|用|听).{0,4}(?:语音|声音)"
    r"|(?:不|别|不要|不想|不用|甭)\s*(?:再|又)?\s*(?:给[我你他她它]|跟[我你他她它])?\s*唱[一句段个首]"
)
VOICE_REQUEST_RE = re.compile(
    r"(?:"
    r"(?:发|发来|来|留|录|发个|发条|发段|来个|来条|来段|录个|录条|录段)\s*(?:一条|一段|一个)?\s*(?:语音|语音条|音频)"
    r"|(?:用|换|发|说)\s*(?:语音|声音)\s*(?:说|聊|回复?|答|回|跟?我?讲)"
    r"|(?:想|要|能不能|可以|能|快|求|让我|我想)\s*(?:听听?|听一下|听一声|听一听)\s*(?:你(?:的)?(?:声音)?|你的声|语音)?"
    r"|让我听听你"
    r"|想听你(?:说话|唱歌|唱|念|读)"
    r"|唱[一句段个首]"
    r"|给(?:我)?(?:唱|念|读)[一句段个首]"
    r"|开麦|连麦|通个话|打语音"
    r")"
)

# 追问正则：语音呢/怎么没语音/没收到等追问使用；快递/验证码不能命中
VOICE_FOLLOWUP_RE = re.compile(
    r"(?:"
    r"(?:语音|声音)\s*(?:呢|去哪了|怎么没|咋没|怎么还没|为什么没|没出|没见|没来)"
    r"|(?:怎么|咋|为什么)\s*(?:没|没有|不见|还没)\s*(?:(?:发|收到|听到|听见)?\s*(?:语音|声音)|出声)"
    r"|(?:没|没能|未|无法)\s*(?:收到|听到|听见|播放)\s*(?:语音|声音)"
    r"|(?:语音|声音)\s*(?:发了?没|发出来了?吗|发了吗|发了没|没收到|没听到|没听见|挂了|坏了)"
    r")"
)

# 旧语音标记清理正则（不误伤普通括号，如【划重点】、[笑哭]、array[0]）
_OPEN_BRACKET = r"(?:\[|【|［)"
_CLOSE_BRACKET = r"(?:\]|】|］)"

_VOICE_PAIR = re.compile(
    _OPEN_BRACKET + r"\s*(?:voice|语音)\s*[:：]?[ \t]*" + _CLOSE_BRACKET + r"?(.*?)" +
    _OPEN_BRACKET + r"\s*/\s*(?:voice|语音)\s*" + _CLOSE_BRACKET,
    re.IGNORECASE | re.DOTALL,
)
_VOICE_SHORTHAND = re.compile(
    _OPEN_BRACKET + r"\s*voice\s*[:：]\s*([^\[\]【】［］]*?)" +
    _CLOSE_BRACKET + r"(?![^\n]*\[/voice])",
    re.IGNORECASE | re.DOTALL,
)
_VOICE_TAGS = re.compile(
    _OPEN_BRACKET + r"\s*/?\s*voice\s*[:：]?[ \t]*" + _CLOSE_BRACKET + r"?",
    re.IGNORECASE,
)
_VOICE_RECORD_PREFIX = re.compile(
    r"^[ \t]*(?:[（(]\s*语音消息\s*[）)]|" + _OPEN_BRACKET + r"\s*语音(?:消息)?\s*" + _CLOSE_BRACKET + r")\s*",
    re.MULTILINE,
)
_VOICE_STANDALONE = re.compile(
    _OPEN_BRACKET + r"\s*语音(?:消息)?\s*" + _CLOSE_BRACKET + r"|[（(]\s*语音(?:消息)?\s*[）)]",
    re.IGNORECASE,
)

# 仅用于朗读文本中去掉非文字标记（face/sticker等），正文中的非文字标记需保留
_TAG_RE = re.compile(
    r"\[quote[:：]\d+\]|[\[［](?:face|sticker|表情)[:：][^\]］]+[\]］]",
    re.IGNORECASE,
)
_SILENCE_REPLY_RE = re.compile(
    r"^\s*(?:\[quote[:：]\d+\]\s*)?[\[【［](?:沉默|SILENT|SILENCE)[\]】］]\s*$",
    re.IGNORECASE,
)

# 句子切分：按句末标点或换行切分
_PART_RE = re.compile(r"[^。！？!?~～\n]+[。！？!?~～]?|\n")

# 情绪与可读线索
_CUE_RE = re.compile(
    r"晚安|早安|想你|好想|喜欢|舍不得|在意|担心|心疼|难受|委屈|"
    r"开心|难过|生气|好烦|烦死|好累|累死|"
    r"有点(?:累|酸|疼|难受|困|委屈|烦)|"
    r"抱抱|哼|不理你|才不|讨厌|笨蛋|坏蛋|欠揍|想得美|不许|呆货|人家|"
    r"我其实|早点睡|别熬夜"
)
_PARTICLE_RE = re.compile(r"[呀啦嘛呢吧啊哈哟][。！？!?…～~]*$")
_EMO_PUNCT_RE = re.compile(r"[！？!?…～~]")
_GENERIC_ACK = {
    "嗯", "嗯嗯", "哦", "噢", "好", "好的", "行", "行吧", "在", "在呢",
    "我在呢", "在的", "收到",
}


def strip_legacy_voice_markup(text: str) -> str:
    """仅清理明确的旧语音标记，普通【划重点】、[笑哭]、array[0] 原样保留。"""
    if not text:
        return ""
    # 标记前后原本是不同发送段，清成文字时用换行保留边界，避免两句黏在一起。
    text = _VOICE_PAIR.sub(r"\1\n", text)
    text = _VOICE_SHORTHAND.sub(r"\1 ", text)
    text = _VOICE_TAGS.sub("", text)
    text = _VOICE_RECORD_PREFIX.sub("", text)
    text = _VOICE_STANDALONE.sub("", text)
    text = re.sub(r"^[ \t]*语音(?:消息)?\s*[:：]\s*", "", text, flags=re.MULTILINE)
    return text.strip()


def _score(text: str) -> int:
    normalized = re.sub(r"\s+", "", text).strip("。！？!?…～~")
    if normalized in _GENERIC_ACK:
        return 0
    score = 2 if _CUE_RE.search(text) else 0
    if _PARTICLE_RE.search(text) or _EMO_PUNCT_RE.search(text):
        score += 1
    return score


@dataclass(frozen=True)
class VoicePlan:
    mode: str
    body_text: str
    voice_text: str = ""
    reason: str = ""
    quote_index: Optional[int] = None
    requested: bool = False
    # 有语音时记录原位置：before → voice → after；无语音时为 None。
    before_text: Optional[str] = None
    after_text: Optional[str] = None

    @property
    def has_voice(self) -> bool:
        return bool(self.voice_text)


class VoicePlanner:
    def __init__(self, frequency: str = "frequent", max_chars: int = 90):
        self.frequency = frequency if frequency in {"frequent", "explicit", "off"} else "frequent"
        self.max_chars = max(1, int(max_chars))

    @staticmethod
    def explicit_request(user_text: str) -> bool:
        text = user_text or ""
        if VOICE_REFUSAL_RE.search(text):
            return False
        return bool(VOICE_REQUEST_RE.search(text))

    def allows(self, user_text: str, requested: Optional[bool] = None) -> bool:
        if self.frequency == "off":
            return False
        is_req = requested if requested is not None else self.explicit_request(user_text)
        if self.frequency == "explicit":
            return is_req
        return True

    def _clip(self, text: str) -> str:
        clipped = truncate_for_voice(text, self.max_chars).strip()
        # 省略号不是原文；截断只用原句前缀，余字全部回填文字段。
        clipped = clipped[:-1].rstrip() if clipped.endswith("…") else clipped
        if len(clipped) <= self.max_chars:
            return clipped
        if clipped.endswith("…"):
            return (clipped[: self.max_chars - 1].rstrip() + "…").strip()
        return clipped[: self.max_chars].strip()

    def plan(
        self,
        user_text: str,
        reply: str,
        *,
        voice_allowed: bool,
        requested: Optional[bool] = None,
    ) -> VoicePlan:
        # 1. 防御旧语音标记清理（不调模型，纯正则）
        cleaned_reply = strip_legacy_voice_markup(reply or "")
        if _SILENCE_REPLY_RE.match(cleaned_reply):
            return VoicePlan("text", "[沉默]", reason="她选择沉默", requested=bool(requested))

        # 2. 判定 requested
        is_requested = requested if requested is not None else self.explicit_request(user_text)

        # 3. 基础闸门与模式检查
        if not cleaned_reply or self.frequency == "off":
            return VoicePlan("text", cleaned_reply, reason="语音规划关闭或无内容", requested=is_requested)
        if not voice_allowed:
            return VoicePlan("text", cleaned_reply, reason="能力闸门未放行", requested=is_requested)
        if self.frequency == "explicit" and not is_requested:
            return VoicePlan("text", cleaned_reply, reason="仅响应明确请求", requested=is_requested)

        # 4. 解析引用
        quote_index = None
        working = cleaned_reply
        quote_match = re.match(r"^\s*\[quote[:：](\d+)\]", working, re.IGNORECASE)
        if quote_match:
            quote_index = int(quote_match.group(1))
            working = working[quote_match.end():].lstrip("\r\n \t")

        # 5. 切分候选正文片段
        parts: List[Tuple[int, int, str, str]] = []
        for match in _PART_RE.finditer(working):
            raw_seg = match.group(0)
            if not raw_seg.strip():
                continue
            cleaned_seg = _TAG_RE.sub("", raw_seg).strip()
            if cleaned_seg and re.search(r"[A-Za-z0-9\u4e00-\u9fa5]", cleaned_seg):
                parts.append((match.start(), match.end(), raw_seg, cleaned_seg))

        if not parts:
            return VoicePlan("text", cleaned_reply, reason="没有可朗读正文", quote_index=quote_index, requested=is_requested)

        # 6. 选择片段
        selected_parts: Optional[List[Tuple[int, int, str, str]]] = None
        reason = ""

        if is_requested:
            scored_parts = [(_score(p[3]), i, p) for i, p in enumerate(parts)]
            best_score, best_idx, best_p = max(scored_parts, key=lambda item: (item[0], -item[1]))

            joined_all_clean = " ".join(p[3] for p in parts)
            if len(parts) == 1:
                selected_parts = [parts[0]]
            elif best_score >= 2:
                selected_parts = [best_p]
            elif len(joined_all_clean) <= self.max_chars:
                selected_parts = parts
            else:
                selected_parts = [parts[0]]
            reason = "明确请求优先语音"
        else:
            scored_candidates = [(_score(p[3]), -i, p) for i, p in enumerate(parts) if _score(p[3]) >= 2]
            if not scored_candidates:
                return VoicePlan("text", cleaned_reply, reason="普通承接或信息内容优先文字", quote_index=quote_index, requested=is_requested)
            # 优先选择不超过 max_chars 的合适短句
            short_eligible = [item for item in scored_candidates if len(item[2][3]) <= self.max_chars]
            if short_eligible:
                best_item = max(short_eligible, key=lambda item: (item[0], item[1]))
            else:
                best_item = max(scored_candidates, key=lambda item: (item[0], item[1]))
            selected_parts = [best_item[2]]
            reason = "内容适合语音表达"

        # 7. 形成语音段与剩余文字段（原话连续片段，选中的正文从 body_text 精确剔除）
        start_idx = selected_parts[0][0]
        end_idx = selected_parts[-1][1]

        raw_selected_span = working[start_idx:end_idx]
        voice_text = _TAG_RE.sub("", raw_selected_span).strip()

        remainder = ""
        if len(voice_text) > self.max_chars:
            clipped_voice = self._clip(voice_text)
            common_len = 0
            while common_len < len(clipped_voice) and common_len < len(voice_text) and clipped_voice[common_len] == voice_text[common_len]:
                common_len += 1
            remainder = voice_text[common_len:].lstrip("，,、 ").strip()
            voice_text = clipped_voice

        if not voice_text:
            return VoicePlan("text", cleaned_reply, reason="语音正文为空", quote_index=quote_index, requested=is_requested)

        preserve_tags = re.findall(r"[\[［](?:face|sticker|表情)[:：][^\]］]+[\]］]", raw_selected_span, re.IGNORECASE)

        before_raw = working[:start_idx]
        after_raw = working[end_idx:]

        tags_str = ("\n".join(preserve_tags) + "\n") if preserve_tags else ""
        before_text = before_raw.strip()
        after_text = (tags_str + remainder + after_raw).strip()
        body_text = (before_text + ("\n" if before_text and after_text else "") + after_text).strip()

        mode = "mixed" if body_text else "voice"
        return VoicePlan(
            mode=mode,
            body_text=body_text,
            voice_text=voice_text,
            reason=reason,
            quote_index=quote_index,
            requested=is_requested,
            before_text=before_text,
            after_text=after_text,
        )
