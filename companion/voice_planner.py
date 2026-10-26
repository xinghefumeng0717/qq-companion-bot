"""内容驱动的高频语音规划：愿意多说，但不逢话必说。"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from companion.tts import truncate_for_voice
from companion.replier import normalize_voice_syntax_variants, repair_voice_line_shorthand

VOICE_REQUEST_RE = re.compile(r"语音|声音|听听你|唱[一句段个首]")
_TAG_RE = re.compile(
    r"\[(?:quote[:：]\d+|face[:：][^\]]+|sticker[:：][^\]]+|表情[:：][^\]]+)\]|"
    r"\[/?voice[:：]?\]?|\[语音(?:消息)?\]|（语音消息）",
    re.IGNORECASE,
)
_VOICE_MARKUP_RE = re.compile(
    r"\[/?voice[:：]?\]?|\[语音(?:消息)?\]|（语音消息）",
    re.IGNORECASE,
)
_PRESERVE_TAG_RE = re.compile(r"\[(?:face|sticker|表情)[:：][^\]]+\]", re.IGNORECASE)
_PART_RE = re.compile(r"[^。！？!?~～\n]+[。！？!?~～]?|\n")
# 只收“可读成语音”的短语线索，不收单字：单字“想/累/酸/怕/烦/笑”会把
# “酸奶、会议安排、积累经验”这类普通信息误判成情绪。新增线索必须配真实
# 失败样例，词表保持在二三十条以内。
_CUE_RE = re.compile(
    r"晚安|早安|想你|好想|喜欢|舍不得|在意|担心|心疼|难受|委屈|"
    r"开心|难过|生气|好烦|烦死|好累|累死|"
    r"有点(?:累|酸|疼|难受|困|委屈|烦)|"
    r"抱抱|哼|不理你|才不|讨厌|笨蛋|坏蛋|欠揍|想得美|不许|呆货|人家|"
    r"我其实|早点睡|别熬夜"
)
# 句末语气词；“我明天下午三点交报告吧”只因此得 1 分，仍不够发语音。
_PARTICLE_RE = re.compile(r"[呀啦嘛呢吧啊哈哟][。！？!?…～~]*$")
_EMO_PUNCT_RE = re.compile(r"[！？!?…～~]")
_GENERIC_ACK = {
    "嗯", "嗯嗯", "哦", "噢", "好", "好的", "行", "行吧", "在", "在呢",
    "我在呢", "在的", "收到",
}


@dataclass(frozen=True)
class VoicePlan:
    mode: str
    body_text: str
    voice_text: str = ""
    reason: str = ""
    quote_index: Optional[int] = None

    @property
    def has_voice(self) -> bool:
        return bool(self.voice_text)


def _score(text: str) -> int:
    normalized = re.sub(r"\s+", "", text).strip("。！？!?…～~")
    if normalized in _GENERIC_ACK:
        return 0
    score = 2 if _CUE_RE.search(text) else 0
    if _PARTICLE_RE.search(text) or _EMO_PUNCT_RE.search(text):
        score += 1
    return score


def _common_prefix_length(left: str, right: str) -> int:
    kept = 0
    while kept < len(left) and kept < len(right) and left[kept] == right[kept]:
        kept += 1
    return kept


class VoicePlanner:
    def __init__(self, frequency: str = "frequent", max_chars: int = 90):
        self.frequency = frequency if frequency in {"frequent", "explicit", "off"} else "frequent"
        self.max_chars = max(1, int(max_chars))

    @staticmethod
    def explicit_request(user_text: str) -> bool:
        return bool(VOICE_REQUEST_RE.search(user_text or ""))

    def allows(self, user_text: str) -> bool:
        return self.frequency != "off" and (self.frequency != "explicit" or self.explicit_request(user_text))

    def _clip(self, text: str) -> str:
        """截断到 max_chars 以内；省略号分支也不允许多出一字。"""
        clipped = truncate_for_voice(text, self.max_chars).strip()
        if len(clipped) <= self.max_chars:
            return clipped
        if clipped.endswith("…"):
            return (clipped[: self.max_chars - 1].rstrip() + "…").strip()
        return clipped[: self.max_chars].strip()

    def plan(self, user_text: str, reply: str, *, voice_allowed: bool) -> VoicePlan:
        reply = repair_voice_line_shorthand(normalize_voice_syntax_variants((reply or "").strip()))
        explicit = self.explicit_request(user_text)
        if not reply or self.frequency == "off":
            return VoicePlan("text", reply, reason="语音规划关闭或无内容")
        if not voice_allowed:
            return VoicePlan("text", reply, reason="能力闸门未放行")
        if self.frequency == "explicit" and not explicit:
            return VoicePlan("text", reply, reason="仅响应明确请求")

        quote_index = None
        working = reply
        quote_match = re.match(r"^\s*\[quote[:：](\d+)\]", reply, re.IGNORECASE)
        if quote_match:
            quote_index = int(quote_match.group(1))
            working = reply[quote_match.end():].lstrip()
        parts = []
        for match in _PART_RE.finditer(working):
            cleaned = _TAG_RE.sub("", match.group(0)).strip()
            if cleaned and re.search(r"[A-Za-z0-9一-龥]", cleaned):
                parts.append((match.start(), match.end(), cleaned))
        if not parts or not _TAG_RE.sub("", working).strip():
            return VoicePlan("text", reply, reason="没有可朗读正文")

        best_score, _, best_part = max(
            ((_score(part[2]), i, part) for i, part in enumerate(parts)),
            key=lambda item: (item[0], item[1]),
        )

        if explicit:
            # 明确要语音时不能只念最后两句：情绪句在前就念情绪句；单句短回复
            # 整句念；多句纯信息且总长放得下才整段念，否则至少念第一句。
            joined = " ".join(part[2] for part in parts)
            if len(parts) == 1:
                selected = parts
            elif best_score >= 2:
                selected = [best_part]
            elif len(joined) <= self.max_chars:
                selected = parts
            else:
                selected = [parts[0]]
            reason = "明确请求优先语音"
        elif best_score >= 2:
            # 扫描所有段而不是只看末尾，情绪/感受句在中间也能被选中。
            selected = [best_part]
            reason = "内容适合语音表达"
        else:
            return VoicePlan("text", reply, reason="普通承接或信息内容优先文字")

        start, end = selected[0][0], selected[-1][1]
        voice_text = " ".join(part[2] for part in selected)
        selected_raw = working[start:end]
        preserved = _PRESERVE_TAG_RE.findall(selected_raw)

        full_voice_text = voice_text
        voice_text = self._clip(voice_text)
        # 省略号不是原文前缀，不能用 startswith 判断余文；按公共前缀回填，
        # 并放回被选中片段原来的位置，避免余文重复或顺序错乱。
        remainder = full_voice_text[_common_prefix_length(voice_text, full_voice_text):]
        remainder = remainder.lstrip("，,、 ").strip()
        if not voice_text:
            return VoicePlan("text", reply, reason="语音正文为空")

        before = _VOICE_MARKUP_RE.sub("", working[:start])
        after = _VOICE_MARKUP_RE.sub("", working[end:])
        body = (before + "\n".join(preserved) + remainder + after).strip()
        body = re.sub(r"\n{3,}", "\n\n", body)
        return VoicePlan(
            "mixed" if body else "voice", body, voice_text, reason,
            quote_index=quote_index,
        )
