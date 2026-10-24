"""回复质检 (reply_check.py)：主聊生成后、切段发送前，用 flash（low 思考）过一道"助手腔"质检。

只认两种毛病（复述或点评他的话 / 装饰性比喻），有就让同一次调用给出改写稿。
实测（v5 42 题）让它也管"讲道理/啰嗦"时会误伤关心和玩笑（"那就不写了呗"被删），故只管这两种。
失败纪律与 TTS 一致：超时、报错、JSON 坏、改写稿丢了标记或变长了——**一律照发原稿**，
质检只能让话变好，不能让她这句话丢掉。默认关（[reply].self_check = false）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from typing import Any, Awaitable, Callable, Dict, List, Tuple

from companion.gateway import parse_llm_json
from companion.prompts import REPLY_CHECK_PROMPT, REPLY_CHECK_USER
from companion.replier import is_silence_output

logger = logging.getLogger(__name__)

# 发送管道认的标记（改写稿必须一个不少地保留）
MARKER_RE = re.compile(
    r"\[(?:face|sticker|表情)[:：][^\]]*\]|\[quote[:：]\d+\]|\[voice[:：]\]?|\[/voice\]|\[沉默\]",
    re.IGNORECASE,
)
QUOTE_HEAD_RE = re.compile(r"(?m)^[ \t]*(\[quote[:：]\d+\])")
_HANZI = re.compile(r"[一-龥]")

# 短草稿不值得多花一次调用：几个字的回复几乎不可能复述/讲道理
MIN_HANZI_TO_CHECK = 8


def markers_of(text: str) -> Counter:
    return Counter(m.lower() for m in MARKER_RE.findall(text or ""))


def accept_fixed(draft: str, fixed: str) -> Tuple[bool, str]:
    """改写稿能不能用：非空、标记一个不少、不比原稿长。"""
    fixed = (fixed or "").strip()
    if not fixed:
        return False, "改写稿为空"
    if markers_of(fixed) != markers_of(draft):
        return False, "改写稿丢了或多了标记"
    if len(fixed) > len(draft):
        return False, "改写稿比原稿长"
    # 引用只认行首：原稿行首的 [quote:N] 改写后必须还在某一行的行首（否则发送端当文字降级）
    for q in QUOTE_HEAD_RE.findall(draft):
        if not re.search(r"(?m)^[ \t]*" + re.escape(q), fixed):
            return False, "引用标记不在行首了"
    return True, ""


async def check_reply(
    gateway: Any, his_text: str, draft: str, timeout: float = 10.0,
    her_identity: str = "她", his_identity: str = "他",
) -> Tuple[str, Dict[str, Any]]:
    """返回 (要发的文本, 质检记录)。任何异常都返回原稿。"""
    info: Dict[str, Any] = {"checked": False, "changed": False, "issues": [], "why": ""}
    if not draft or is_silence_output(draft) or len(_HANZI.findall(draft)) < MIN_HANZI_TO_CHECK:
        info["why"] = "草稿太短或为沉默，跳过"
        return draft, info
    messages = [
        {"role": "system", "content": REPLY_CHECK_PROMPT.replace("{her_identity}", her_identity)
                                      .replace("{his_identity}", his_identity)},
        {"role": "user", "content": REPLY_CHECK_USER.format(his=his_text or "（无）", draft=draft)},
    ]
    try:
        resp = await asyncio.wait_for(
            gateway.chat(
                messages=messages,
                model=gateway.config.observer_model,
                temperature=0.2,
                json_mode=True,
                purpose="reply_check",   # 在默认思考白名单里：开 low（改写稿是他看得见的字）
                max_retries=0,
            ),
            timeout=timeout,
        )
        data = parse_llm_json(resp)
    except Exception as e:
        info["why"] = f"质检调用失败，照发原稿: {type(e).__name__}"
        logger.info(f"[ReplyCheck] {info['why']}")
        return draft, info

    info["checked"] = True
    if not isinstance(data, dict) or data.get("ok") is not False:
        return draft, info
    issues = data.get("issues")
    info["issues"] = [str(x) for x in issues] if isinstance(issues, list) else []
    fixed = data.get("fixed") if isinstance(data.get("fixed"), str) else ""
    ok, why = accept_fixed(draft, fixed)
    if not ok:
        info["why"] = f"改写稿不合格，照发原稿: {why}"
        logger.info(f"[ReplyCheck] {info['why']}（问题: {info['issues']}）")
        return draft, info
    info["changed"] = True
    logger.info(f"[ReplyCheck] 已改写（{info['issues']}）: {draft!r} -> {fixed.strip()!r}")
    return fixed.strip(), info


# ==========================================
# 首泡回声裁剪（[reply].trim_echo，默认关）：零调用、零延迟
# ==========================================
# v5 三轮 38 条复述里 71% 只出现在第一个气泡，55% 是"首泡复述 + 后面还有话"——
# 模型的固定套路是先接住他的话（复述/点评）再说自己的，后半句往往是好的。
ECHO_MIN_HANZI = 4
_HANZI_RUN = re.compile(r"[一-龥]{%d,}" % ECHO_MIN_HANZI)
_TAG = re.compile(r"\[[^\]]*\]")
# 拿他的用词/说法当对象评一句（与 benchmark_v5 的 COMMENT_RE 同口径）
COMMENT_RE = re.compile(r"这(个)?(词|说法|话说得|形容|比喻|转折|走向|用词)|被你这么(用|一说)|从你嘴里说出来|说得(还|倒|真)?挺?像(那么)?回事")


def _his_grams(his_text: str) -> set:
    src = _TAG.sub("", his_text or "")
    n = ECHO_MIN_HANZI
    return {run[i:i + n] for run in _HANZI_RUN.findall(src) for i in range(len(run) - n + 1)}


def echoes_his(line: str, his_text: str) -> bool:
    """这一行原样用了他这句话里连续 ECHO_MIN_HANZI 个以上的汉字（他话里的表情标记不算）。"""
    return any(g in (line or "") for g in _his_grams(his_text))


# 去掉复述的字以后，剩下的这些字不算"她自己的东西"（应答词、虚字）
_FILLER = set("行好嗯啊呀吧哦噢呢了的是就这那你我他也挺还真都个嘛哈")
# 剩下的话里有这些，说明她借他的话在表态（调侃/反问/否定/安慰），是真人也会说的"接"
ATTITUDE_RE = re.compile(r"[？?！!]|不|没|别|还|才|怎么|谁|啥|什么|干嘛|哪|难道|居然|竟然")


def lead_kind(line: str, his_text: str) -> str:
    """第一个气泡属于哪类接法：
    "empty"   ——空接：点评他的用词，或去掉复述的字后几乎不剩她自己的东西（AI 腔，该删）；
    "attitude"——借他的话表态：调侃、反问、否定、安慰（真人朋友常这么接，留着）；
    "clean"   ——没复述也没点评。
    """
    if COMMENT_RE.search(line or ""):
        return "empty"
    text = _TAG.sub("", line or "")
    covered = [False] * len(text)
    for g in _his_grams(his_text):
        start = text.find(g)
        while start != -1:
            covered[start:start + len(g)] = [True] * len(g)
            start = text.find(g, start + 1)
    if not any(covered):
        return "clean"
    body = "".join(c for c, cov in zip(text, covered) if not cov)
    if ATTITUDE_RE.search(body):
        return "attitude"
    own = [c for c in _HANZI.findall(body) if c not in _FILLER]
    return "empty" if len(own) <= 2 else "attitude"


# 删掉首泡后，下一句开头这些词就接不上了，一起去掉
_DANGLING_HEAD = re.compile(r"^(不过|但是|但|而且|可是|然后|所以)[，,、\s]*")
# 删完如果只剩一句问话，宁可不删（只剩一个干巴巴的问题更冷）
_QUESTION_END = re.compile(r"([？?]|吗|呢|什么|啥|嘛|几|哪)\s*$")


def trim_echo_lead(his_text: str, reply: str) -> Tuple[str, str]:
    """第一个有字的气泡是"空接"、且删掉后剩下的还站得住，就删掉它。

    返回 (新回复, 删掉的那行；没删为空串)。只删一行、只看第一个有字的气泡；
    借他的话调侃/反问/安慰的（attitude）不删——那是带温度的那句；
    行首带 [quote:N] 的不动；删完只剩一句问话的不动；下一句开头的"不过/但是"一并去掉。
    """
    if not reply or is_silence_output(reply):
        return reply, ""
    lines = reply.split("\n")
    text_idx = [i for i, ln in enumerate(lines) if len(_HANZI.findall(_TAG.sub("", ln))) >= 2]
    if len(text_idx) < 2:
        return reply, ""
    first = text_idx[0]
    line = lines[first]
    if QUOTE_HEAD_RE.match(line) or lead_kind(line, his_text) != "empty":
        return reply, ""
    rest_text = [lines[i] for i in text_idx[1:]]
    if len(rest_text) == 1 and _QUESTION_END.search(_TAG.sub("", rest_text[0]).strip()):
        return reply, ""
    kept = lines[:first] + lines[first + 1:]
    nxt = text_idx[1] - 1  # 删掉一行后，原来的第二句落在这个下标
    kept[nxt] = _DANGLING_HEAD.sub("", kept[nxt].lstrip())
    return "\n".join(kept).strip(), line.strip()


# ==========================================
# 多候选择优（[reply].candidates，默认 1 = 不开）：不改一个字，选最干净的那条
# ==========================================
# 装饰性比喻（与 benchmark_v5 的 METAPHOR_RE 同口径）
METAPHOR_RE = re.compile(
    r"像[^，。！？\n]{1,12}(一样|似的|那样)|跟[^，。！？\n]{1,12}(似的|一样)|似的|(?<!好)像在|仿佛|宛如|如同"
)


def candidate_flaws(his_text: str, reply: str) -> int:
    """一条候选的毛病分：有空接（点评/空复述）2 分，打比方 1 分。

    借他的话表态（调侃/反问/安慰）不扣分——那是真人也会的接法，扣了会让挑出来的
    那条更干、更冷（成对盲评：扣表态分时四个阶段都是"关"的那组略暖，约 57:43）。
    """
    lines = [ln for ln in (reply or "").split("\n") if ln.strip()]
    score = 2 if any(lead_kind(ln, his_text) == "empty" for ln in lines) else 0
    return score + int(bool(METAPHOR_RE.search(reply or "")))


# 温度（毛病一样多时用来挑更暖的那条）：表情 / 说自己的事 / 语气词 / 问他怎么了。
# 叮嘱（早点睡、别熬夜）刻意不算——加分会把她推回"连环叮嘱"。
_EMOJI = re.compile(r"\[(?:face|sticker|表情)[:：]")
_SELF_SHARE = re.compile(r"我(今天|刚|也|这边|下午|上午|晚上|昨天|前两天|最近|现在)")
_PARTICLE = re.compile(r"[呀啦嘛哈呢嘿哎欸诶噢~～]")
_ASK_HIM = re.compile(r"怎么了|还好吗|没事吧|累不累|吃了吗|吃饭没|难受|舒服点")


def candidate_warmth(reply: str) -> int:
    text = _TAG.sub("", reply or "")
    return (
        int(bool(_EMOJI.search(reply or "")))
        + int(bool(_SELF_SHARE.search(text)))
        + int(bool(_PARTICLE.search(text)))
        + int(bool(_ASK_HIM.search(text)))
    )


def pick_candidate(
    his_text: str, candidates: List[str], tiebreak: str = "warm"
) -> Tuple[str, Dict[str, Any]]:
    """先挑毛病最少的；毛病打平后的决胜由 tiebreak 决定。空候选不选。

    tiebreak="warm"（默认）：打平挑更暖的，再一样取最先生成的——偏暖角色卡的
    既有行为，A/B 验过。
    tiebreak="first"：打平直接取最先生成的，不做任何温度偏好——给"暖=人设磨损"的
    卡（如傲娇）用：温度分里的软语气词/问身体加分会系统性地把带刺候选磨平。

    沉默只在全员沉默时合法：她明明有话说时，[沉默] 不许靠"毛病分/温度分都是 0、
    又总是最先生成完"在打平时赢下顺序决胜。
    """
    valid = [(i, c) for i, c in enumerate(candidates) if (c or "").strip()]
    if not valid:
        return (candidates[0] if candidates else ""), {"picked": 0, "flaws": [], "warmth": []}
    valid = [(i, c) for i, c in valid if not is_silence_output(c)] or valid
    flaws = [candidate_flaws(his_text, c) for _, c in valid]
    warmth = [candidate_warmth(c) for _, c in valid]
    if tiebreak == "first":
        best = min(range(len(valid)), key=lambda k: (flaws[k], k))
    else:
        best = min(range(len(valid)), key=lambda k: (flaws[k], -warmth[k], k))
    return valid[best][1], {"picked": valid[best][0], "flaws": flaws, "warmth": warmth}


# 第一条候选出来后，其余的最多再等这么久；没出来的取消（防一条卡住拖死整轮）
CANDIDATE_GRACE_SECONDS = 2.0


async def gather_candidates(
    generate: Callable[[], Awaitable[str]], n: int, grace: float = CANDIDATE_GRACE_SECONDS
) -> List[str]:
    """并行跑 n 次 generate：等到第一条成功的，再给其余 grace 秒，超时的取消。

    返回已成功的非空文本（按完成先后）；一条都没成功就抛出第一个异常（调用方走原兜底）。
    """
    tasks = [asyncio.ensure_future(generate()) for _ in range(n)]
    texts: List[str] = []
    errors: List[BaseException] = []

    def _collect(done) -> None:
        for t in done:
            if t.cancelled():
                continue
            exc = t.exception()
            if exc is not None:
                errors.append(exc)
            elif isinstance(t.result(), str) and t.result().strip():
                texts.append(t.result().strip())

    pending = set(tasks)
    try:
        # 阶段一：等到有一条成功（或全部结束）
        while pending and not texts:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            _collect(done)
        # 阶段二：其余的再等 grace 秒
        if pending and texts:
            done, pending = await asyncio.wait(pending, timeout=grace)
            _collect(done)
    finally:
        for t in pending:
            t.cancel()
    if not texts:
        raise errors[0] if errors else RuntimeError("候选全空")
    return texts

