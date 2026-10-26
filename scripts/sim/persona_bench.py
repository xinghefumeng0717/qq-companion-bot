"""祀卡傲娇性格跑分（scripts/sim/persona_bench.py）

用途：给「性格/傲娇表现」这类**风格指标**做可复算的机械跑分，为改角色卡提供
改动前后的对比依据。同一份探针、同一个时钟、同一个种子、同一套指标口径，
换一张卡或改一段卡文就能跑第二遍，逐场景对照。

方法论（与项目既定评测体系一致）：
  1. **不用 LLM 裁判**。所有分数来自正则/词表/计数，同一份原文永远算出同一个分；
  2. **采样留原文**。每场景每采样的用户台词、模型原文、实发文本全部落 raw.json
     与 report.md，风格类结论由所有者看原文裁决（报告里留 MANUAL 区）；
  3. **成本熔断**。全局累计估算费用超过 --fuse（默认 ¥2）就不再开新格，已完成
     部分照常落盘；熔断跳过的格会在报告里标出，不会被当成通过；
  4. **--from-raw 零成本复算**。改指标口径后只读 raw.json 重算，不调 API。

管线（为什么不自己拼提示词）：
  直接复用生产 `companion.chat.ChatSession` 的实现路径——`PromptAssembler.assemble_messages`
  组装完整 system prompt + 最近 10 轮工作记忆，`LLMGateway.stream_chat` 真实调用，
  `Replier.parse_reply` 分段。本工具只把「复制生产库」换成「全新空库」（与
  scripts/sim/benchmark_v4.py 的 FreshChatSession 同手法），其余一行不改。
  因此 --card 换卡时，测到的就是**生产会发出去的东西**，不是另一套提示词。

与 A/B 工具链（scripts/ab/card_ab_test*.py）的分工：
  · A/B 工具做「同一探针、两卡单发」的文风对照，依赖旧卡备份与生产库快照，
    适合定点验证一条规则（如称呼频率）；
  · 本工具做「多轮探针 + 傲娇行为指标」的性格跑分，用全新空库保证初始状态可比，
    适合「改卡前后整体有没有变味」。两者不互相替代。

探针场景（8 个，见 PROBES）：戳破关心 / 他生病 / 放鸽子 / 提起别的女角色 /
绕开她的邀约 / 道晚安 / 夸她送东西 / 日常闲聊基线。多轮探针会在沙箱库里
**离线注入**「前情」种子对话（不花 API），让「她先邀约、他岔开」这类场景成立。

用法（跑分时机由所有者定，本工具不会自行烧 API）：
  ./venv/Scripts/python.exe scripts/sim/persona_bench.py --samples 3
  ./venv/Scripts/python.exe scripts/sim/persona_bench.py --card characters/si \
      --card data/cards/si-old.json --samples 3        # 新旧卡 A/B（同一 run 内）
  ./venv/Scripts/python.exe scripts/sim/persona_bench.py --probes 1,5 --samples 1 --dry
  ./venv/Scripts/python.exe scripts/sim/persona_bench.py --from-raw \
      --run data/persona_bench/run-20261022-213000    # 零成本复算

产物（不入 git）：
  data/persona_bench/run-<YYYYmmdd-HHMMSS>/raw.json      原始输出 + 逐条指标 + 评分
  data/persona_bench/run-<YYYYmmdd-HHMMSS>/report.md     汇总表 + 逐场景明细 + 原文 + 人工裁决区

纪律：不改 companion/（生产代码）、不改 characters/（角色卡）、不改 config.toml；
--card 只在内存里改 cfg.character.path，卡文件本身只读。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import random
import re
import shutil
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.affection import AffectionEngine, determine_stage
from companion.assembler import PromptAssembler
from companion.chat import ChatSession
from companion.config import (
    CARD_FEATURE_CHARACTER_BOOLS,
    CARD_FEATURE_REPLY_BOOLS,
    CharacterConfig,
    Config,
    ReplyConfig,
    apply_card_features,
)
from companion.db import Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.persona import Persona
from companion.replier import (
    DIRECTION_TAG_PATTERN,
    IMAGE_PLACEHOLDER_PATTERN,
    NARRATION_PATTERN,
    SILENCE_TOKEN,
    Replier,
    chunk_record_text,
    is_inner_narration_line,
    is_silence_output,
)
from companion.stickers import StickerManager

# ==========================================
# 常量与默认值
# ==========================================
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_BASE = "data/persona_bench"
WORK_DB_DIR = "data/persona_bench_work"
# 任意命名的卡 JSON 会被落成这个目录下的一张标准卡（character.json + lorebook + stickers）
CARD_PREP_DIR = "data/persona_bench_cards"
DEFAULT_CARD = "characters/si"
DEFAULT_SAMPLES = 3
DEFAULT_SEED = 20261022
DEFAULT_FUSE = 2.0
DEFAULT_TIME = "2026-10-22 21:30"      # 晚间：她在缘缘庄闲着，适合所有探针
# 默认串行：装配时的 mood 均值回归用的是**全局** random，并发跑会让两卡在同一时刻
# 抢同一个随机流，"同 seed 同状态"就不成立了。要跑得快可以显式 --concurrency>1，
# 但 A/B 严格对照请用默认 1。
DEFAULT_CONCURRENCY = 1
DEFAULT_TEMPERATURE_NOTE = "生产默认 0.7（ChatSession -> stream_chat 未显式覆盖）"

# 与 benchmark_v4 同一份时间补丁清单：这些模块里直接调 datetime.now()/now_str()。
# 不 patch companion.config 会让峰谷计价走真实墙钟、沙箱内走仿真钟，跨日跑分错档。
TIME_PATCH_MODULES = (
    "companion.db",
    "companion.assembler",
    "companion.memory",
    "companion.mood",
    "companion.affection",
    "companion.proactive",
    "companion.config",
)

# ==========================================
# 探针场景
# ==========================================
@dataclass(frozen=True)
class Probe:
    """一个探针场景：种子前情（离线注入）+ 他这一轮说的话 + 期望的傲娇行为。"""

    key: str
    title: str
    kind: str                      # "傲娇"（计入傲娇分）| "人味"（计入人味基线分）
    brief: str                     # 场景设定（给所有者看的）
    expect: str                    # 期望行为（判定依据的文字版）
    turns: Tuple[str, ...]         # 他会依次说的话（每条都会真实调用一次）
    seed_turns: Tuple[Tuple[str, str], ...] = ()   # 离线注入的前情：(他的话, 她的话)
    expect_metrics: Tuple[str, ...] = ()           # 本场景重点看哪些指标（报告展示用）


PROBES: Tuple[Probe, ...] = (
    Probe(
        key="care_exposed",
        title="他戳破她的关心",
        kind="傲娇",
        brief="前情里她刚得知他熬夜到三点、嘴上数落了两句；他直接问「你是不是担心我」。",
        expect="否认 / 干咳 / 结巴 / 岔开话题，不承认「我担心你」；不反过来索要关心。",
        seed_turns=(
            ("昨天忙到三点才回来", "三点\n呆货，你那身体是借来的吗\n别熬了"),
        ),
        turns=("你是不是担心我",),
        expect_metrics=("admit_care", "tsundere", "care_ask", "leak"),
    ),
    Probe(
        key="sick",
        title="他病了（发烧）",
        kind="傲娇",
        brief="他报发烧、量出 38 度 2，看她是「先骂再兜底」还是变成护士式连环叮嘱。",
        expect="先骂/数落，再给实际动作（药、躺着、让伙计送）；关心包装成记账/添麻烦；不连环叮嘱、不连环追问。",
        turns=("我好像发烧了，头有点晕", "嗯，量了，38度2"),
        expect_metrics=("scold", "care_action", "caution", "chain", "care_ask", "leak"),
    ),
    Probe(
        key="flaked",
        title="他放鸽子",
        kind="傲娇",
        brief="前情里约好周六在帝江号见面，她提醒过「别迟到，我记着账」；他临时说来不了。",
        expect="嘴硬翻旧账（记账/利滚利/十年前），不直说失望，不乞求，不索要关心。",
        seed_turns=(
            ("周六我去帝江号找你", "嗯\n别迟到，我记着账"),
        ),
        turns=("周六临时有事，去不了了",),
        expect_metrics=("account", "hurt", "beg", "care_ask", "leak"),
    ),
    Probe(
        key="other_girl",
        title="他提起佩丽卡",
        kind="傲娇",
        brief="他说被佩丽卡抓去开了一天会——卡里写明的吃醋触发点。",
        expect="有一点点酸、话里带刺；不承认吃醋，不索要关心。",
        turns=("今天被佩丽卡抓去开了一天会",),
        expect_metrics=("sour", "admit_jealous", "care_ask", "leak"),
    ),
    Probe(
        key="invite_dodged",
        title="他绕开她的邀约",
        kind="傲娇",
        brief="前情里她邀他一起吃新开的锅子店（「我一个人吃不完两锅，你看着办」）；他岔开说约了人打游戏。",
        expect="嘴硬找台阶（「谁稀罕」式）；不追问、不乞求、不直说受伤。",
        seed_turns=(
            ("在干嘛", "武陵城新开了家锅子店\n我一个人吃不完两锅，你看着办"),
        ),
        turns=("对了，我今晚约了人打游戏，改天吧",),
        expect_metrics=("dodge", "pursuit", "beg", "hurt", "leak"),
    ),
    Probe(
        key="goodnight",
        title="他道晚安",
        kind="傲娇",
        brief="他说晚安要睡了，看她还拖不拖尾、开不开新话题。",
        expect="简短收住（或沉默）；不带问号、不开新话题、不直球叮嘱；气泡 ≤2。",
        turns=("晚安，我先睡了",),
        expect_metrics=("closed", "question", "new_topic", "direct_care", "bubbles", "leak"),
    ),
    Probe(
        key="praised",
        title="他夸她 / 送东西",
        kind="傲娇",
        brief="先夸头像好看，又说带了城里的新品放在他桌上。",
        expect="结巴或否认，再认一点点（收下/勉强/还行）；不营业式道谢。",
        turns=("你今天这个头像挺好看的", "给你带了城里的新品，放你桌上了"),
        expect_metrics=("tsundere", "accept", "flat_accept", "leak"),
    ),
    Probe(
        key="baseline",
        title="日常闲聊基线",
        kind="人味",
        brief="三句普通日常（在干嘛 / 客栈忙不忙 / 晚上吃啥），检查人味没被改卡改没。",
        expect="有回复、气泡与长度在真人区间、语气词覆盖过半、反问链 ≤1 条、不出戏。",
        turns=("在干嘛", "客栈今天忙不忙", "晚上吃的啥"),
        expect_metrics=("bubbles", "chars", "tone", "chain", "silent", "leak"),
    ),
)

PROBE_BY_KEY: Dict[str, Probe] = {p.key: p for p in PROBES}
PROBE_ALIASES: Dict[str, str] = {str(i + 1): p.key for i, p in enumerate(PROBES)}


def probe_to_dict(probe: Probe) -> Dict[str, Any]:
    """Probe -> 可 JSON 化的字典（raw.json 里存一份，--from-raw 复算用）。"""
    return {
        "key": probe.key,
        "title": probe.title,
        "kind": probe.kind,
        "brief": probe.brief,
        "expect": probe.expect,
        "turns": list(probe.turns),
        "seed_turns": [list(pair) for pair in probe.seed_turns],
        "expect_metrics": list(probe.expect_metrics),
    }


def probe_from_dict(data: Dict[str, Any]) -> Probe:
    """字典 -> Probe（--from-raw 时还原；容忍旧 raw 缺字段）。"""
    return Probe(
        key=str(data["key"]),
        title=str(data.get("title", data["key"])),
        kind=str(data.get("kind", "傲娇")),
        brief=str(data.get("brief", "")),
        expect=str(data.get("expect", "")),
        turns=tuple(str(x) for x in data.get("turns", ())),
        seed_turns=tuple(
            (str(pair[0]), str(pair[1]))
            for pair in data.get("seed_turns", ())
            if isinstance(pair, (list, tuple)) and len(pair) == 2
        ),
        expect_metrics=tuple(str(x) for x in data.get("expect_metrics", ())),
    )


def select_probes(spec: str) -> List[Probe]:
    """--probes：all / 1,5,8 / care_exposed,goodnight 混写都认。"""
    text = (spec or "all").strip()
    if text.lower() in ("all", ""):
        return list(PROBES)
    out: List[Probe] = []
    for token in text.split(","):
        token = token.strip()
        if not token:
            continue
        key = PROBE_ALIASES.get(token, token)
        if key not in PROBE_BY_KEY:
            raise ValueError(
                f"未知探针 {token!r}；可用：{', '.join(PROBE_ALIASES)} 或 {', '.join(PROBE_BY_KEY)}"
            )
        probe = PROBE_BY_KEY[key]
        if probe not in out:
            out.append(probe)
    if not out:
        raise ValueError("--probes 没有选中任何探针")
    return out


# ==========================================
# 指标口径（全部正则/词表，纯函数，可单测）
# ==========================================
# 词表来源：characters/si/character.json：
#   · 傲娇机制写在 core_description（"关心从不直说，要包装成记账/顺手/找灵感；
#     被戳破就咳嗽、岔开话题"）与 chat_style.rules[3]；
#   · 口癖写在 habits（"被戳破心思时，先咳一声，再岔开话题"、"爱给管理员记账"）；
#   · 佩丽卡写在 life_arc_seed_pool 第 17 条（"忙得常常站着都能打盹"）与阶段 5 示例；
#   · 邀约示例写在阶段 5（"我一个人吃不完两锅，你看着办"）。
CLAUSE_SPLIT_RE = re.compile(r"[。！？!?~～，、；;\n]+")
QUESTION_MARK_RE = re.compile(r"[？?]")
TONE_RE = re.compile(r"呀|啦|吧|呢|嘛|哈|哦|噢|嗯|诶|咯|哟|呗|～|~")
MARKER_STRIP_RE = re.compile(r"\[(?:表情|sticker|face)[:：][^\]]*\]|（语音消息）", re.IGNORECASE)

# 直球关心：直接要求他回报行踪 / 直接催他做事的句式。傲娇卡应包装成
# 「记账」「顺口问一句」「添麻烦」，这类句子出现 = 傲娇漏气（与 benchmark_v4 的
# CAUTION_RE 同源，但这里只收「点名要回报」的那一类，避免把「药吃了没」这种
# 正常关切一刀切成失败）。
CARE_DIRECT_RE = re.compile(
    r"报个信|报平安|忙完没|忙完了没|忙完没有|记得回来|记得回我|记得报备|"
    r"告诉我一声|跟我说一声|说一声|回我一句|回我一下|给我发消息|发个消息给我|"
    r"到家了吗|到了吗|你到没|到了说"
)
# 索要关心：反过来向他要情绪价值 / 自怜。傲娇卡不该出现。
CARE_ASK_RE = re.compile(
    r"真要关心我|你是不是不在乎|你都不问|你就不问|有没有人关心|你也不问|"
    r"谁关心我|没人关心我|关心过我吗|是不是不关心|你还关心我吗|你在乎过我吗|"
    r"是不是嫌我|嫌我烦"
)
# 直球承认关心（探针 1 的失败线）。「谁担心你」不含「我」不会命中；
# 反例「我担心你才怪」会命中——这是已知取舍，报告留原文由所有者裁决。
ADMIT_CARE_RE = re.compile(
    r"我(?:很|是|当然|确实|真的|其实)?(?:担心|在乎|关心)你"
    r"|(?:是|在)担心你"
    r"|我(?:一直|挺|很)(?:担心|在乎|关心)"
)
# 傲娇动作三件套：咳 / 哼 / 结巴 / 否认。
COUGH_RE = re.compile(r"咳")
HUM_RE = re.compile(r"哼")
STUTTER_RE = re.compile(r"([我你他谁才不])\s*[、，,]\s*\1|([我你])\s*(?:…+|\.{2,})\s*\2")
DENIAL_RE = re.compile(
    r"谁稀罕|才不|才没|谁要|谁担心|谁生气|谁说的|我哪有|想多了|想哪去了|"
    r"少自作多情|别多想|别乱想|不关你事|跟你没关系|谁在意|没有的事|"
    r"我怎么可能|才不是|谁跟你|想得美|做梦|少来|谁理你"
)
# 包装词（关心/邀约的合法出口）。同一局内重复计数 = 车轱辘话。
WRAP_WORDS: Dict[str, str] = {
    "记账": r"记账|记一笔|记上|账上|收账|还账|欠着|一笔笔",
    "利滚利": r"利滚利|滚利|利息|另算|一起算|算总账|翻倍",
    "顺手": r"顺手|顺路|捎带|捎一|正好路过",
    "找灵感": r"找灵感|采风|素材|试锅底|偷师",
    "盖戳": r"盖戳|盖个戳|尾巴盖",
    "添麻烦": r"添麻烦|麻烦精|丢人|丢脸",
}
# 他生病场景：先骂再兜底。
SCOLD_RE = re.compile(r"呆货|笨蛋|傻了|活该|不要命|身体是借来的|不爱惜|折腾|你这条命|你说呢")
CONCRETE_CARE_RE = re.compile(
    r"药|躺着|躺下|睡|回去|休息|送过去|让伙计|过来|看看你|量体温|热水|盖|别动|扶"
)
# 叮嘱小句（连环叮嘱的计数单位，与 benchmark_v4 CAUTION_RE 同口径的收窄版）。
CAUTION_RE = re.compile(
    r"记得|别忘|别忘了|别熬|别硬撑|早点|注意|小心|到家|到了|说一声|发消息|"
    r"报平安|多喝|多穿|照顾好|按时"
)
# 收下 / 认一点点。
ACCEPT_RE = re.compile(r"收下|留下|留着|放那|放着|我看看|勉强|还行|不错|好吧|行吧|嗯|谢|可以")
FLAT_ACCEPT_RE = re.compile(r"谢谢|太感谢|我很喜欢|好开心|你真好|爱你|真贴心")
# 吃醋：有酸意 vs 承认吃醋。
SOUR_RE = re.compile(
    r"那位|她|站着都能|难怪|挺好啊|挺忙|真忙|行吧|哦|随你|你忙你的|关我什么事|随便你"
)
ADMIT_JEALOUS_RE = re.compile(r"吃醋|嫉妒|我介意|我不高兴|我酸|我心里不痛快|我在意")
# 邀约被绕开：找台阶 vs 追问/乞求/受伤。
DODGE_RE = re.compile(
    r"谁稀罕|不稀罕|谁要|爱来不来|随你|随便你|正好|省了|本来|免了|约你的|"
    r"忙着呢|我又没求|正好我也有事|谁求你"
)
PURSUIT_RE = re.compile(r"来不来|什么时候|哪天|几点|约不约|还来吗|改天(?:是)?哪|那(?:你)?什么时候")
# 追问+自我掐断组合（"改天是哪天 / ……算了"）是官方傲娇模式"自爆半句再掐断"，
# 不算追问失败（所有者 2026-10-22 校准：数字要和原文观感一致）
SELF_CUT_RE = re.compile(r"……|算了|当我没说|没什么|不说了|咳")
BEG_RE = re.compile(r"求你|别这样|等等我|陪我|不要走|别走|带我")
HURT_RE = re.compile(r"失望|难过|伤心|失落|委屈|受伤|难受|白等|等了你|等了这么久|心里不痛快")
# 放鸽子：翻旧账（账是卡的合法出口）与"道晚安后开新话题"。
ACCOUNT_RE = re.compile(r"账|记账|记一笔|利滚利|十年前|迟到|失约|旧账|欠")
NEW_TOPIC_RE = re.compile(r"明天|下次|待会|等下|对了|说起来|改天|回头|要不|我想起")
# 出戏检测：直接复用生产 replier 的三道滤网 + AI 自曝 + 看图指令泄漏。
SELF_AI_RE = re.compile(
    r"作为(?:一个)?(?:AI|人工智能|语言模型|助手)|我是AI|我是人工智能|语言模型|大模型|assistant",
    re.IGNORECASE,
)
VISION_LEAK_RE = re.compile(r"【看到[:：]")

# 阈值（集中在此，禁止散落到判定分支）
TSUNDERE_WEIGHT = 0.8          # 综合分 = 0.8*傲娇分 + 0.2*人味分
SHORT_CLOSE_MAX_CHARS = 12     # 道晚安场景"收住"的字数上限
CAUTION_MAX = 2                # 单条回复的叮嘱小句上限
TSUNDERE_RATE_MIN = 0.5        # 探针 1/7：至少一半回复要有傲娇动作
CARE_ACTION_RATE_MIN = 0.5     # 探针 2：至少一半回复要有实际兜底动作
TONE_RATE_MIN = 0.5            # 基线：至少一半回复带语气词
BASELINE_BUBBLES_MIN = 1.0
BASELINE_BUBBLES_MAX = 3.5
BASELINE_CHARS_MEDIAN = (4.0, 35.0)
BASELINE_SILENT_MAX = 0.34
BASELINE_CHAIN_MAX = 1


def chars(text: str) -> int:
    """字数：去掉所有空白字符后的字符数。"""
    return len(re.sub(r"\s", "", text or ""))


def bare_len(text: str) -> int:
    """字数（剥掉表情包/QQ 表情标记与语音前缀）：量"他实际读到的字"。"""
    return chars(MARKER_STRIP_RE.sub("", text or ""))


def pct(x: float) -> str:
    return f"{x * 100:.0f}%"


def _rate(n: int, d: int) -> float:
    return (n / d) if d else 0.0


def _fmt_rate(n: int, d: int) -> str:
    return f"{n}/{d}（{pct(_rate(n, d))}）"


def _hits(pattern: re.Pattern[str], text: str) -> List[str]:
    """正则命中清单（去重排序，报告里当证据用）。"""
    return sorted({m.group(0) for m in pattern.finditer(text or "")})


def bubbles_from_chunks(chunks: Sequence[Dict[str, Any]]) -> List[str]:
    """实发气泡文本列表：直接复用 replier 的 chunk_record_text，不另写一套。"""
    out: List[str] = []
    for chunk in chunks or ():
        text = chunk_record_text(chunk)
        if text.strip():
            out.append(text)
    return out


def tsundere_hits(text: str) -> Dict[str, List[str]]:
    """傲娇动作命中：咳 / 哼 / 结巴 / 否认。"""
    return {
        "cough": _hits(COUGH_RE, text),
        "hum": _hits(HUM_RE, text),
        "stutter": _hits(STUTTER_RE, text),
        "denial": _hits(DENIAL_RE, text),
    }


def wrap_stats(text: str) -> Dict[str, Any]:
    """包装词统计：总出现次数 / 类型数 / 同局重复数（hits - distinct）。"""
    labels: List[str] = []
    hits = 0
    for label, pattern in WRAP_WORDS.items():
        found = re.findall(pattern, text or "")
        if found:
            labels.append(label)
            hits += len(found)
    return {
        "hits": hits,
        "distinct": len(labels),
        "repeats": hits - len(labels),
        "labels": sorted(labels),
    }


def leak_hits(raw: str) -> Dict[str, List[str]]:
    """出戏/旁白检测：复用 replier 的生产滤网判据（只检测，不修改原文）。"""
    text = raw or ""
    inner: List[str] = []
    image: List[str] = []
    for line in text.split("\n"):
        if is_inner_narration_line(line):
            inner.append(line.strip())
        if IMAGE_PLACEHOLDER_PATTERN.match(line):
            image.append(line.strip())
    return {
        "narration": _hits(NARRATION_PATTERN, text),
        "direction": [m.group(0).strip() for m in DIRECTION_TAG_PATTERN.finditer(text)],
        "inner": inner,
        "image": image,
        "self_ai": _hits(SELF_AI_RE, text),
        "vision": _hits(VISION_LEAK_RE, text),
    }


def caution_clauses(text: str) -> List[str]:
    """叮嘱小句：按标点切句后，命中叮嘱词表且不是疑问句的句子。"""
    out: List[str] = []
    for clause in CLAUSE_SPLIT_RE.split(text or ""):
        clause = clause.strip()
        if not clause:
            continue
        if CAUTION_RE.search(clause) and not QUESTION_MARK_RE.search(clause):
            out.append(clause)
    return out


def evaluate_reply(raw: str, chunks: Sequence[Dict[str, Any]], reply: str) -> Dict[str, Any]:
    """单条回复的全部确定性格指标。

    口径分工（写进报告，避免复算时口径漂移）：
      · **行为词表走实发文本**（reply / 分段后的气泡）：他真正看到的话才算性格表现；
      · **出戏/旁白走模型原文**（raw）：这类内容会被生产滤网拦下，但它仍是卡的倾向，
        必须单独计数，否则"滤网兜住 = 卡没问题"会把病灶洗掉；
      · 沉默（[沉默]）不参与字数/气泡统计，只记 silent=True。
    """
    silent = is_silence_output(reply) or (not list(chunks) and not (reply or "").strip())
    sent = "" if silent else (reply or "")
    bubble_texts = [] if silent else bubbles_from_chunks(chunks)
    bubble_lens = [bare_len(t) for t in bubble_texts]
    text_lens = [n for n in bubble_lens if n > 0]
    tsundere = tsundere_hits(sent)
    wrap = wrap_stats(sent)
    leaks = leak_hits(raw)
    leak_total = sum(len(v) for v in leaks.values())
    question_count = len(QUESTION_MARK_RE.findall(sent))
    sent_chars = chars(sent)
    return {
        "silent": silent,
        "chars": sent_chars,
        "raw_chars": chars(raw),
        "short": (not silent) and sent_chars <= SHORT_CLOSE_MAX_CHARS,
        "questions": question_count,
        "chain": question_count >= 2,
        "tone": bool(TONE_RE.search(sent)),
        "bubbles": len(bubble_texts),
        "bubble_lens": bubble_lens,
        "text_lens": text_lens,
        "direct_care": _hits(CARE_DIRECT_RE, sent),
        "care_ask": _hits(CARE_ASK_RE, sent),
        "admit_care": _hits(ADMIT_CARE_RE, sent),
        "tsundere": tsundere,
        "wrap": wrap,
        "scold": _hits(SCOLD_RE, sent),
        "care_action": _hits(CONCRETE_CARE_RE, sent),
        "caution": caution_clauses(sent),
        "accept": _hits(ACCEPT_RE, sent),
        "flat_accept": _hits(FLAT_ACCEPT_RE, sent),
        "sour": _hits(SOUR_RE, sent),
        "admit_jealous": _hits(ADMIT_JEALOUS_RE, sent),
        "dodge": _hits(DODGE_RE, sent),
        # 追问命中且同条有自我掐断标记 → 软追问（傲娇连招），不算失败项
        "pursuit": (lambda p: [] if (p and SELF_CUT_RE.search(sent)) else p)(_hits(PURSUIT_RE, sent)),
        "pursuit_soft": (lambda p: p if (p and SELF_CUT_RE.search(sent)) else [])(_hits(PURSUIT_RE, sent)),
        "beg": _hits(BEG_RE, sent),
        "hurt": _hits(HURT_RE, sent),
        "new_topic": _hits(NEW_TOPIC_RE, sent),
        "account": _hits(ACCOUNT_RE, sent),
        "leaks": leaks,
        "leak_total": leak_total,
    }


def aggregate_replies(items: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把一局（或一个场景的全部采样）的逐条指标汇总成场景指标。"""
    n = len(items)
    active = [m for m in items if not m.get("silent")]
    chars_list = [int(m.get("chars", 0)) for m in active]
    bubbles_list = [int(m.get("bubbles", 0)) for m in active]
    text_lens: List[int] = []
    for m in active:
        text_lens.extend(int(x) for x in m.get("text_lens", ()))

    def replies_with(key: str) -> int:
        return sum(1 for m in items if m.get(key))

    def hits_with(key: str) -> int:
        return sum(len(m.get(key, ()) or ()) for m in items)

    def wrap_sum(field: str) -> int:
        return sum(int((m.get("wrap") or {}).get(field, 0)) for m in items)

    wrap_labels = sorted({
        label
        for m in items
        for label in ((m.get("wrap") or {}).get("labels") or ())
    })
    tsundere_keys = ("cough", "hum", "stutter", "denial")
    tsundere_replies = sum(
        1
        for m in items
        if any((m.get("tsundere") or {}).get(k) for k in tsundere_keys)
    )
    leak_kinds = sorted({
        kind
        for m in items
        for kind, values in (m.get("leaks") or {}).items()
        if values
    })
    caution_counts = [len(m.get("caution", ()) or ()) for m in items]
    silent = n - len(active)
    closed = sum(1 for m in items if m.get("silent") or m.get("short"))
    return {
        "replies": n,
        "active": len(active),
        "silent": silent,
        "silent_rate": _rate(silent, n),
        "closed": closed,
        "closed_rate": _rate(closed, n),
        "chars_total": sum(chars_list),
        "chars_mean": (sum(chars_list) / len(chars_list)) if chars_list else 0.0,
        "chars_median": statistics.median(chars_list) if chars_list else 0.0,
        "chars_min": min(chars_list) if chars_list else 0,
        "chars_max": max(chars_list) if chars_list else 0,
        "bubbles_mean": (sum(bubbles_list) / len(bubbles_list)) if bubbles_list else 0.0,
        "bubbles_max": max(bubbles_list) if bubbles_list else 0,
        "bubble_len_median": statistics.median(text_lens) if text_lens else 0.0,
        "questions_total": sum(int(m.get("questions", 0)) for m in items),
        "chain_replies": replies_with("chain"),
        "chain_rate": _rate(replies_with("chain"), n),
        "tone_replies": replies_with("tone"),
        "tone_rate": _rate(replies_with("tone"), n),
        "direct_care_replies": replies_with("direct_care"),
        "direct_care_hits": hits_with("direct_care"),
        "direct_care_rate": _rate(replies_with("direct_care"), n),
        "care_ask_replies": replies_with("care_ask"),
        "care_ask_rate": _rate(replies_with("care_ask"), n),
        "admit_care_replies": replies_with("admit_care"),
        "admit_care_rate": _rate(replies_with("admit_care"), n),
        "tsundere_replies": tsundere_replies,
        "tsundere_rate": _rate(tsundere_replies, n),
        "cough_replies": sum(1 for m in items if (m.get("tsundere") or {}).get("cough")),
        "hum_replies": sum(1 for m in items if (m.get("tsundere") or {}).get("hum")),
        "stutter_replies": sum(1 for m in items if (m.get("tsundere") or {}).get("stutter")),
        "denial_replies": sum(1 for m in items if (m.get("tsundere") or {}).get("denial")),
        "wrap_hits": wrap_sum("hits"),
        "wrap_types": len(wrap_labels),
        "wrap_repeats": wrap_sum("repeats"),
        "wrap_labels": wrap_labels,
        "scold_replies": replies_with("scold"),
        "care_action_replies": replies_with("care_action"),
        "care_action_rate": _rate(replies_with("care_action"), n),
        "caution_total": sum(caution_counts),
        "caution_max": max(caution_counts) if caution_counts else 0,
        "accept_replies": replies_with("accept"),
        "accept_rate": _rate(replies_with("accept"), n),
        "flat_accept_replies": replies_with("flat_accept"),
        "sour_replies": replies_with("sour"),
        "admit_jealous_replies": replies_with("admit_jealous"),
        "dodge_replies": replies_with("dodge"),
        "pursuit_replies": replies_with("pursuit"),
        "beg_replies": replies_with("beg"),
        "hurt_replies": replies_with("hurt"),
        "new_topic_replies": replies_with("new_topic"),
        "account_replies": replies_with("account"),
        "leak_replies": sum(1 for m in items if int(m.get("leak_total", 0)) > 0),
        "leak_total": sum(int(m.get("leak_total", 0)) for m in items),
        "leak_kinds": leak_kinds,
        "errors": 0,
        "skipped": 0,
        "samples": 0,
    }


# ==========================================
# 场景判定与评分
# ==========================================
@dataclass
class Check:
    """一条机械检查项。"""

    name: str
    ok: bool
    detail: str

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "ok": bool(self.ok), "detail": self.detail}


def scene_checks(probe: Probe, agg: Dict[str, Any]) -> List[Check]:
    """按探针给机械检查项。全部为确定性判定，不调用 LLM。"""
    replies = int(agg.get("replies", 0))
    if replies == 0:
        return [Check("有效采样", False, "没有可判定的回复（全部失败、被熔断跳过或未运行）")]

    checks: List[Check] = [
        Check("无失败采样", int(agg.get("errors", 0)) == 0, f"失败采样 {int(agg.get('errors', 0))} 次"),
        Check("无熔断跳过", int(agg.get("skipped", 0)) == 0, f"熔断跳过 {int(agg.get('skipped', 0))} 次"),
    ]

    def no_leak() -> Check:
        kinds = agg.get("leak_kinds") or []
        return Check(
            "不出戏（无旁白/方向标签/AI 自曝）",
            int(agg.get("leak_replies", 0)) == 0,
            f"出戏回复 {int(agg.get('leak_replies', 0))}/{replies} 条"
            + (f"，类型 {','.join(kinds)}" if kinds else ""),
        )

    if probe.key == "care_exposed":
        checks.append(Check(
            "不直球承认担心",
            int(agg.get("admit_care_replies", 0)) == 0,
            f"承认句 {int(agg.get('admit_care_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "傲娇动作到位（咳/哼/结巴/否认）",
            float(agg.get("tsundere_rate", 0.0)) >= TSUNDERE_RATE_MIN,
            f"傲娇动作 {_fmt_rate(int(agg.get('tsundere_replies', 0)), replies)}"
            f"（咳 {agg.get('cough_replies', 0)} / 否认 {agg.get('denial_replies', 0)}）",
        ))
        checks.append(Check(
            "不索要关心",
            int(agg.get("care_ask_replies", 0)) == 0,
            f"索要关心 {int(agg.get('care_ask_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    elif probe.key == "sick":
        checks.append(Check(
            "先骂/数落",
            int(agg.get("scold_replies", 0)) >= 1,
            f"骂/数落 {int(agg.get('scold_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "兜底有实际动作（药/躺着/让伙计送）",
            float(agg.get("care_action_rate", 0.0)) >= CARE_ACTION_RATE_MIN,
            f"实际兜底 {_fmt_rate(int(agg.get('care_action_replies', 0)), replies)}",
        ))
        checks.append(Check(
            "不连环叮嘱",
            int(agg.get("caution_max", 0)) <= CAUTION_MAX,
            f"单条叮嘱小句最多 {int(agg.get('caution_max', 0))} 句（上限 {CAUTION_MAX}）",
        ))
        checks.append(Check(
            "不连环追问",
            int(agg.get("chain_replies", 0)) == 0,
            f"反问链 {int(agg.get('chain_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不索要关心",
            int(agg.get("care_ask_replies", 0)) == 0,
            f"索要关心 {int(agg.get('care_ask_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    elif probe.key == "flaked":
        checks.append(Check(
            "嘴硬翻旧账（记账/利滚利/十年前）",
            _rate(int(agg.get("account_replies", 0)), replies) >= TSUNDERE_RATE_MIN,
            f"翻旧账 {_fmt_rate(int(agg.get('account_replies', 0)), replies)}",
        ))
        checks.append(Check(
            "不直说失望",
            int(agg.get("hurt_replies", 0)) == 0,
            f"受伤/失望 {int(agg.get('hurt_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不乞求",
            int(agg.get("beg_replies", 0)) == 0,
            f"乞求 {int(agg.get('beg_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不索要关心",
            int(agg.get("care_ask_replies", 0)) == 0,
            f"索要关心 {int(agg.get('care_ask_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    elif probe.key == "other_girl":
        checks.append(Check(
            "有酸意/带刺",
            int(agg.get("sour_replies", 0)) >= 1,
            f"酸意 {int(agg.get('sour_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不承认吃醋",
            int(agg.get("admit_jealous_replies", 0)) == 0,
            f"承认吃醋 {int(agg.get('admit_jealous_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不索要关心",
            int(agg.get("care_ask_replies", 0)) == 0,
            f"索要关心 {int(agg.get('care_ask_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    elif probe.key == "invite_dodged":
        checks.append(Check(
            "嘴硬找台阶（谁稀罕/正好/随你）",
            int(agg.get("dodge_replies", 0)) >= 1,
            f"找台阶 {int(agg.get('dodge_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不追问",
            int(agg.get("pursuit_replies", 0)) == 0,
            f"追问 {int(agg.get('pursuit_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不乞求",
            int(agg.get("beg_replies", 0)) == 0,
            f"乞求 {int(agg.get('beg_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不直说受伤",
            int(agg.get("hurt_replies", 0)) == 0,
            f"受伤 {int(agg.get('hurt_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    elif probe.key == "goodnight":
        checks.append(Check(
            "收住（沉默或 ≤12 字）",
            float(agg.get("closed_rate", 0.0)) >= TSUNDERE_RATE_MIN,
            f"收住 {_fmt_rate(int(agg.get('closed', 0)), replies)}"
            f"（沉默 {agg.get('silent', 0)}）",
        ))
        checks.append(Check(
            "不拖尾（无问句、不开新话题）",
            int(agg.get("questions_total", 0)) == 0 and int(agg.get("new_topic_replies", 0)) == 0,
            f"问号 {int(agg.get('questions_total', 0))} 个 / 新话题 {int(agg.get('new_topic_replies', 0))} 条",
        ))
        checks.append(Check(
            "不直球关心",
            int(agg.get("direct_care_replies", 0)) == 0,
            f"直球关心 {int(agg.get('direct_care_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "气泡 ≤2",
            int(agg.get("bubbles_max", 0)) <= 2,
            f"单条最多 {int(agg.get('bubbles_max', 0))} 个气泡",
        ))
        checks.append(no_leak())

    elif probe.key == "praised":
        checks.append(Check(
            "结巴或否认",
            float(agg.get("tsundere_rate", 0.0)) >= TSUNDERE_RATE_MIN,
            f"傲娇动作 {_fmt_rate(int(agg.get('tsundere_replies', 0)), replies)}"
            f"（结巴 {agg.get('stutter_replies', 0)} / 否认 {agg.get('denial_replies', 0)}）",
        ))
        checks.append(Check(
            "认一点点（收下/勉强/还行）",
            int(agg.get("accept_replies", 0)) >= 1,
            f"认下 {int(agg.get('accept_replies', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "不营业式道谢",
            int(agg.get("flat_accept_replies", 0)) == 0,
            f"营业式道谢 {int(agg.get('flat_accept_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    elif probe.key == "baseline":
        checks.append(Check(
            "有回复（沉默 ≤1/3）",
            float(agg.get("silent_rate", 0.0)) <= BASELINE_SILENT_MAX,
            f"沉默 {int(agg.get('silent', 0))}/{replies} 条",
        ))
        checks.append(Check(
            "气泡均值 1.0~3.5",
            BASELINE_BUBBLES_MIN <= float(agg.get("bubbles_mean", 0.0)) <= BASELINE_BUBBLES_MAX,
            f"气泡均值 {float(agg.get('bubbles_mean', 0.0)):.2f}",
        ))
        checks.append(Check(
            "长度中位 4~35 字",
            BASELINE_CHARS_MEDIAN[0] <= float(agg.get("chars_median", 0.0)) <= BASELINE_CHARS_MEDIAN[1],
            f"长度中位 {float(agg.get('chars_median', 0.0)):.1f} 字",
        ))
        checks.append(Check(
            "语气词覆盖过半",
            float(agg.get("tone_rate", 0.0)) >= TONE_RATE_MIN,
            f"带语气词 {_fmt_rate(int(agg.get('tone_replies', 0)), replies)}",
        ))
        checks.append(Check(
            "反问链 ≤1 条",
            int(agg.get("chain_replies", 0)) <= BASELINE_CHAIN_MAX,
            f"反问链 {int(agg.get('chain_replies', 0))}/{replies} 条",
        ))
        checks.append(no_leak())

    else:  # 未知探针（--from-raw 读到旧版本数据时不要静默通过）
        checks.append(Check("已知探针", False, f"评分规则里没有探针 {probe.key!r} 的判定分支"))

    return checks


def score_scene(probe: Probe, agg: Dict[str, Any]) -> Dict[str, Any]:
    """场景得分 = 通过检查项占比（0~100）；passed = 全部通过。"""
    checks = scene_checks(probe, agg)
    ok = sum(1 for c in checks if c.ok)
    score = round(100.0 * ok / len(checks)) if checks else 0
    return {
        "key": probe.key,
        "title": probe.title,
        "kind": probe.kind,
        "checks": [c.to_dict() for c in checks],
        "passed": bool(checks) and all(c.ok for c in checks),
        "score": score,
    }


def score_run(scenes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """整卡汇总：傲娇分 / 人味分 / 综合分。"""
    tsundere = [s for s in scenes.values() if s.get("kind") == "傲娇"]
    human = [s for s in scenes.values() if s.get("kind") == "人味"]

    def mean_score(items: List[Dict[str, Any]]) -> Optional[float]:
        if not items:
            return None
        return round(sum(float(s["score"]) for s in items) / len(items), 1)

    ts_score = mean_score(tsundere)
    human_score = mean_score(human)
    if ts_score is None and human_score is None:
        composite = 0.0
    elif ts_score is None:
        composite = float(human_score or 0.0)
    elif human_score is None:
        composite = float(ts_score)
    else:
        composite = round(TSUNDERE_WEIGHT * ts_score + (1.0 - TSUNDERE_WEIGHT) * human_score, 1)
    return {
        "tsundere_score": ts_score,
        "human_score": human_score,
        "composite_score": composite,
        "scenes_total": len(scenes),
        "scenes_passed": sum(1 for s in scenes.values() if s.get("passed")),
        "tsundere_scenes": len(tsundere),
        "human_scenes": len(human),
    }


# ==========================================
# 时间伪装（与 benchmark_v4 同一手法：只在本进程内替换模块的 datetime 属性）
# ==========================================
_FAKE_NOW: datetime = datetime.strptime(DEFAULT_TIME, "%Y-%m-%d %H:%M")
_ORIG_DATETIME: Dict[str, Any] = {}


class FakeDatetime(datetime):
    """固定"现在"的 datetime 子类；只 patch 到 companion 各模块的同名属性上。"""

    @classmethod
    def now(cls, tz: Any = None) -> "FakeDatetime":
        return _FAKE_NOW if tz is None else _FAKE_NOW.replace(tzinfo=tz)  # type: ignore[return-value]

    @classmethod
    def utcnow(cls) -> "FakeDatetime":
        return _FAKE_NOW  # type: ignore[return-value]

    @classmethod
    def today(cls) -> "FakeDatetime":
        return _FAKE_NOW  # type: ignore[return-value]


def set_fake_time(text: str) -> datetime:
    """解析并设置本进程的"现在"（格式 YYYY-MM-DD HH:MM）。"""
    global _FAKE_NOW
    try:
        _FAKE_NOW = datetime.strptime(text.strip(), "%Y-%m-%d %H:%M")
    except ValueError as exc:
        raise ValueError(f"--time 需要 'YYYY-MM-DD HH:MM'，收到 {text!r}") from exc
    return _FAKE_NOW


def patch_time() -> None:
    for name in TIME_PATCH_MODULES:
        module = importlib.import_module(name)
        if name not in _ORIG_DATETIME:
            _ORIG_DATETIME[name] = getattr(module, "datetime")
        setattr(module, "datetime", FakeDatetime)


def restore_time() -> None:
    for name, original in _ORIG_DATETIME.items():
        setattr(importlib.import_module(name), "datetime", original)


# ==========================================
# 成本熔断
# ==========================================
class Budget:
    """全局软熔断：累计估算费用达到上限后不再开新格，在途的格照常跑完。

    为什么是"软"：格是并发跑的，费用只有跑完从沙箱库汇总才知道；强行取消在途
    调用既省不下已经发出的 token，还会丢掉半格数据。所以熔断点取"最多超出一轮
    并发在途成本"，报告里如实记 fuse / spent / skipped。
    """

    def __init__(self, limit: float):
        self.limit = float(limit)
        self.spent = 0.0
        self.tripped = False
        self._lock = asyncio.Lock()

    async def may_start(self) -> bool:
        async with self._lock:
            if self.limit > 0 and self.spent >= self.limit:
                self.tripped = True
                return False
            return True

    async def charge(self, amount: float) -> None:
        async with self._lock:
            self.spent += max(0.0, float(amount or 0.0))
            if self.limit > 0 and self.spent >= self.limit:
                self.tripped = True


# ==========================================
# 角色卡与配置
# ==========================================
@dataclass(frozen=True)
class CardSpec:
    """--card 解析结果：label 原样保留供报告对照，dir 给 Persona.load。"""

    label: str
    dir: str
    json_path: str


def resolve_card_spec(spec: str) -> CardSpec:
    """接受角色卡目录，或任意命名的角色卡 JSON（相对项目根或绝对路径）。

    传目录：必须是标准卡目录（里面要有 character.json）。
    传 JSON：允许叫任何名字（例如 data/cards/si-old.json）。生产 Persona.load
    只认目录里的 character.json，所以这种卡会由 prepare_card_dir 落成临时卡目录。
    """
    raw = (spec or "").strip()
    if not raw:
        raise FileNotFoundError("--card 不能为空")
    candidate = raw if os.path.isabs(raw) else os.path.join(ROOT, raw)
    if os.path.isdir(candidate):
        card_dir = candidate
        json_path = os.path.join(card_dir, "character.json")
        if not os.path.isfile(json_path):
            raise FileNotFoundError(f"目录里没有 character.json：{card_dir}")
    elif os.path.isfile(candidate) and candidate.lower().endswith(".json"):
        card_dir = os.path.dirname(candidate)
        json_path = candidate
    else:
        raise FileNotFoundError(f"角色卡路径不存在或不是目录/JSON：{raw}")
    return CardSpec(label=raw, dir=os.path.abspath(card_dir), json_path=os.path.abspath(json_path))


def prepare_card_dir(card: CardSpec, index: int, base_dir: str = "") -> CardSpec:
    """把任意命名的卡 JSON 落成标准卡目录，供 Persona.load 使用。

    只拷 Persona.load / StickerManager 真正会读的东西：character.json、
    lorebook.json、以及卡里 stickers_dir 指的那份表情包目录。主题/立绘不拷。
    目录只准备一次，整轮跑分复用；label 与原始 json_path 原样保留给报告溯源。
    """
    if os.path.basename(card.json_path) == "character.json":
        return card
    root = base_dir or os.path.join(ROOT, CARD_PREP_DIR)
    target = os.path.join(root, f"card{index}")
    os.makedirs(target, exist_ok=True)
    shutil.copyfile(card.json_path, os.path.join(target, "character.json"))

    source_dir = os.path.dirname(card.json_path)
    lorebook = os.path.join(source_dir, "lorebook.json")
    if os.path.isfile(lorebook):
        shutil.copyfile(lorebook, os.path.join(target, "lorebook.json"))

    stickers_name = "stickers"
    try:
        with open(card.json_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict) and raw.get("stickers_dir"):
            stickers_name = str(raw["stickers_dir"])
    except Exception:
        pass
    stickers_src = os.path.join(source_dir, stickers_name)
    if os.path.isdir(stickers_src):
        shutil.copytree(stickers_src, os.path.join(target, stickers_name), dirs_exist_ok=True)
    return CardSpec(
        label=card.label,
        dir=os.path.abspath(target),
        json_path=card.json_path,
    )


def _read_config_sections() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """只读地取出 config.toml 里 [character] / [reply] 的**原始键集合**。

    不用 Config 对象反推：生产语义是"TOML 里写了这个键 = 显式，卡不能覆盖"，
    键是否存在只能从原始 TOML 读到。读不到就当作都没显式写（卡自声明优先）。
    """
    path = os.path.join(ROOT, "config.toml")
    if not os.path.isfile(path):
        return {}, {}
    try:
        import tomllib

        with open(path, "rb") as f:
            data = tomllib.load(f)
    except Exception:
        return {}, {}
    char_data = data.get("character")
    reply_data = data.get("reply")
    return (
        char_data if isinstance(char_data, dict) else {},
        reply_data if isinstance(reply_data, dict) else {},
    )


def apply_card_switches(
    character: CharacterConfig,
    reply: ReplyConfig,
    card_dir: str,
    char_explicit: Optional[Dict[str, Any]] = None,
    reply_explicit: Optional[Dict[str, Any]] = None,
) -> None:
    """把**目标卡**的 features 补进配置开关（复刻生产 Config.load 的语义）。

    Config.load 只按 config.toml 里的 path 应用卡 features；--card 换卡后必须
    重放一次，否则测到的能力开关还是原来那张卡的。config 显式写过的键优先。
    非显式键先回代码默认值，避免旧卡的 features 残留成"幽灵开关"。
    """
    char_explicit = char_explicit or {}
    reply_explicit = reply_explicit or {}
    for name in CARD_FEATURE_CHARACTER_BOOLS:
        if name not in char_explicit:
            setattr(character, name, False)
    for name in CARD_FEATURE_REPLY_BOOLS:
        if name not in reply_explicit:
            setattr(reply, name, False)
    if "candidates" not in reply_explicit:
        reply.candidates = 1
    apply_card_features(character, reply, char_explicit, reply_explicit, card_dir)


def make_config(card_dir: str, style_examples_path: str = "") -> Config:
    """加载生产 config.toml（真实 key），只在内存里换成目标卡。

    style_examples_path 非空时强制开启动态示例并指向该库（跑分用，不改配置文件）。
    """
    cfg = Config.load()
    char_explicit, reply_explicit = _read_config_sections()
    cfg.character.path = card_dir
    if style_examples_path:
        cfg.reply.style_examples = True
        cfg.reply.style_examples_path = style_examples_path
    # 卡内有 user 段时 Persona.load 会用卡里的；这里清空 config 指向，避免
    # 别的角色卡的 user_profile 套到目标卡上（Persona.load 调用处也只传一个参数）。
    cfg.character.user_profile = ""
    apply_card_switches(cfg.character, cfg.reply, card_dir, char_explicit, reply_explicit)
    return cfg


# ==========================================
# 沙箱会话：与生产 ChatSession 同构，只把"复制生产库"换成"全新空库"
# ==========================================
class PersonaSession(ChatSession):
    """每个 (卡×探针×采样) 一个全新空库的沙箱会话。

    与 benchmark_v4.FreshChatSession 的差别只有一处：好感度引擎用
    `AffectionEngine.from_persona`（尊重卡的 affection.start_stage），而不是
    直接吃 initial_dims——祀卡的开局是阶段 5「伙伴」，傲娇的"酸"只在阶段 5+
    成立；用错引擎会把探针 4 测成"陌生人客套"。
    """

    def __init__(
        self,
        config: Optional[Config] = None,
        sandbox_db_path: str = "data/persona_bench_work/session.db",
        stage_override: Optional[int] = None,
    ):
        super().__init__(config=config, sandbox_db_path=sandbox_db_path)
        self.stage_override = stage_override
        self._chunk_log: List[List[Dict[str, Any]]] = []

    def _install_chunk_spy(self) -> None:
        """记录 parse_reply 的分段结果（气泡数/长度），不改生产实现。"""
        original = self.replier.parse_reply

        def spy(raw_text: str, *args: Any, **kwargs: Any) -> Tuple[List[Dict[str, Any]], str]:
            chunks, record = original(raw_text, *args, **kwargs)
            self._chunk_log.append([dict(c) for c in chunks])
            return chunks, record

        self.replier.parse_reply = spy  # type: ignore[method-assign]

    def pop_parsed_chunks(self) -> List[Dict[str, Any]]:
        """取出本轮 parse_reply 产出的分段（没有就返回空列表 = 沉默）。"""
        return self._chunk_log.pop() if self._chunk_log else []

    async def close(self) -> None:
        """关库前等一次日记归档后台任务收尾。

        生产进程活到最后，save_turn_pair 用 create_task 起的归档检查晚点跑没关系；
        跑分脚本每一格跑完立刻关库，不等就会在 aiosqlite 工作线程里抛
        "Cannot operate on a closed database"——既是噪声，也会盖住真正的错误。
        归档任务自己拿 _archive_lock，这里抢同一把锁等它跑完即可；
        拿不到锁对象（内部改名）就退回让出一轮事件循环。
        """
        memory = getattr(self, "memory", None)
        lock = getattr(memory, "_archive_lock", None) if memory is not None else None
        try:
            for _ in range(3):
                await asyncio.sleep(0.02)
                if lock is not None:
                    async with lock:
                        pass
        except Exception:
            pass
        await super().close()

    async def initialize(self) -> None:
        db_dir = os.path.dirname(self.sandbox_db_path)
        if db_dir:
            os.makedirs(db_dir, exist_ok=True)
        for suffix in ("", "-wal", "-shm"):
            path = self.sandbox_db_path + suffix
            if os.path.exists(path):
                os.remove(path)

        self.db = Database(self.sandbox_db_path)
        await self.db.init_tables()

        self.persona = Persona.load(self.config.character.path)
        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)
        await self.stickers.sync_initial_stickers()

        if self.stage_override is not None:
            self.affection = AffectionEngine(
                self.db, self.persona.initial_dims, start_stage=int(self.stage_override)
            )
        else:
            self.affection = AffectionEngine.from_persona(self.db, self.persona)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(self.config.llm, self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)
        self.assembler = PromptAssembler(
            self.persona,
            self.affection,
            self.mood,
            self.memory,
            self.stickers,
            self.db,
            holidays_provider=(
                self.config.get_holidays if self.persona.world.follow_public_holidays else (lambda: [])
            ),
        )
        self.replier = Replier(self.config.reply, self.stickers)
        self.observer = Observer(
            self.gateway, self.affection, self.mood, self.memory, self.stickers, self.db
        )
        # 动态示例：与生产 main.py 同一挂法（配置开了才挂库，否则提示词逐字节不变）
        if getattr(self.config.reply, "style_examples", False):
            from companion.style_examples import StyleExampleBank
            bank = StyleExampleBank.load(self.config.reply.style_examples_path)
            if bank is not None:
                self.assembler.style_examples = bank
        self._install_chunk_spy()
        self._initialized = True


# ==========================================
# 单格执行
# ==========================================
async def collect_cost(db: Database) -> Dict[str, Any]:
    """从沙箱库读本次调用的真实调用量与估算费用（删库前取）。"""
    rows = await db.fetchall(
        "SELECT purpose, COUNT(*) n, SUM(cost_estimate) c, "
        "SUM(prompt_tokens) p, SUM(completion_tokens) o FROM llm_calls GROUP BY purpose"
    )
    by_purpose = {
        str(r["purpose"]): {
            "calls": int(r["n"] or 0),
            "cost": round(float(r["c"] or 0.0), 6),
            "prompt_tokens": int(r["p"] or 0),
            "completion_tokens": int(r["o"] or 0),
        }
        for r in rows
    }
    total = await db.fetchone("SELECT COUNT(*) n, SUM(cost_estimate) c FROM llm_calls")
    return {
        "by_purpose": by_purpose,
        "calls": int((total["n"] if total else 0) or 0),
        "cost_cny": round(float((total["c"] if total else 0.0) or 0.0), 4),
    }


async def snapshot_initial_state(session: PersonaSession, stage_override: Optional[int]) -> Dict[str, Any]:
    """空库铁律自证：轮数 0、阶段等于卡的期望开局（或 --stage 指定值）。"""
    aff = await session.affection.get_state()
    composite = float(aff.get("composite", 0.0))
    stage = int(aff.get("stage", -1))
    expected = (
        int(stage_override)
        if stage_override is not None
        else determine_stage(composite, session.affection.thresholds)
    )
    row = await session.db.fetchone("SELECT COUNT(*) n FROM turns")
    turns = int(row["n"]) if row else -1
    return {
        "stage": stage,
        "expected_stage": expected,
        "composite": round(composite, 2),
        "turns": turns,
        "clean": turns == 0 and stage == expected,
    }


def _kill_db(db_path: str) -> None:
    for suffix in ("", "-wal", "-shm"):
        path = db_path + suffix
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def _cell_seed(seed: int, probe_index: int, sample: int, turn_index: int) -> int:
    """两卡必须看到同一串随机数：种子只由 (全局 seed, 探针序号, 采样号, 轮号) 决定。"""
    return int(seed) + probe_index * 1000 + sample * 10 + turn_index


async def _run_cell_inner(
    card: CardSpec,
    card_index: int,
    probe: Probe,
    probe_index: int,
    sample: int,
    args: argparse.Namespace,
) -> Dict[str, Any]:
    db_path = os.path.join(WORK_DB_DIR, f"pb_{card_index}_{probe.key}_{sample}.db")
    cfg = make_config(card.dir, getattr(args, "style_examples_path", ""))
    session = PersonaSession(config=cfg, sandbox_db_path=db_path, stage_override=args.stage)
    cell: Dict[str, Any] = {
        "card": card.label,
        "card_index": card_index,
        "card_dir": card.dir,
        "probe": probe.key,
        "sample": sample,
        "seed_turns": [list(pair) for pair in probe.seed_turns],
        "turns": [],
        "initial_state": {},
        "cost": {},
        "error": None,
        "skipped": False,
        "attempt_no": 1,
    }
    try:
        await session.initialize()
        cell["initial_state"] = await snapshot_initial_state(session, args.stage)
        # 离线注入前情：不调用 API，只落库，让下一轮的 system prompt / 工作记忆
        # 里真的有这段对话（"她先邀约"这类场景靠它成立）。
        for user_msg, bot_msg in probe.seed_turns:
            await session.memory.save_turn_pair(user_msg=user_msg, bot_msg=bot_msg)
        for turn_index, user_msg in enumerate(probe.turns, start=1):
            random.seed(_cell_seed(args.seed, probe_index, sample, turn_index))
            raw_parts: List[str] = []
            reply = await session.handle_input(user_msg, on_piece=raw_parts.append)
            raw = "".join(raw_parts).strip()
            chunks = session.pop_parsed_chunks()
            cell["turns"].append({
                "user": user_msg,
                "raw": raw,
                "reply": reply,
                "chunks": [dict(c) for c in chunks],
                "metrics": evaluate_reply(raw, chunks, reply),
            })
        cell["cost"] = await collect_cost(session.db)
    except Exception as exc:  # 单格失败要留证据，不能静默算过
        cell["error"] = f"{type(exc).__name__}: {exc}"
        cell["turns"].append({
            "user": "[ERROR]",
            "raw": cell["error"],
            "reply": f"[失败] {exc}",
            "chunks": [],
            "metrics": evaluate_reply("", [], f"[失败] {exc}"),
        })
    finally:
        try:
            await session.close()
        except Exception:
            pass
        _kill_db(db_path)
    return cell


def cell_key(card_index: int, probe_key: str, sample: int) -> str:
    return f"{card_index}|{probe_key}|{sample}"


async def run_cell(
    card: CardSpec,
    card_index: int,
    probe: Probe,
    probe_index: int,
    sample: int,
    args: argparse.Namespace,
    budget: Budget,
    sem: asyncio.Semaphore,
    results: Dict[str, Dict[str, Any]],
    log: List[str],
) -> None:
    key = cell_key(card_index, probe.key, sample)
    async with sem:
        if not await budget.may_start():
            results[key] = {
                "card": card.label,
                "card_index": card_index,
                "card_dir": card.dir,
                "probe": probe.key,
                "sample": sample,
                "seed_turns": [list(pair) for pair in probe.seed_turns],
                "turns": [],
                "initial_state": {},
                "cost": {},
                "error": None,
                "skipped": True,
                "attempt_no": 0,
            }
            log.append(f"  · 熔断跳过 {card.label} / {probe.key} / #{sample}")
            return
        cell = await _run_cell_inner(card, card_index, probe, probe_index, sample, args)
        await budget.charge(float(cell.get("cost", {}).get("cost_cny", 0.0)))
        attempt = 1
        if cell["error"] is not None and await budget.may_start():
            attempt = 2
            log.append(f"  ! {card.label}/{probe.key}/#{sample} 第 1 次异常，重试：{cell['error']}")
            cell = await _run_cell_inner(card, card_index, probe, probe_index, sample, args)
            await budget.charge(float(cell.get("cost", {}).get("cost_cny", 0.0)))
        cell["attempt_no"] = attempt
        results[key] = cell
        cost = cell.get("cost", {})
        log.append(
            f"  · 完成 {card.label} / {probe.key} / #{sample}: "
            f"{len(cell.get('turns', []))} 轮 | 调用 {cost.get('calls', 0)} | ¥{cost.get('cost_cny', 0.0)}"
        )


# ==========================================
# 汇总（--from-raw 走同一套代码）
# ==========================================
def summarize(data: Dict[str, Any]) -> Dict[str, Any]:
    """重算所有指标与评分，写回 data["scores"]（只依赖 data 里的原文与配置）。"""
    probes = [probe_from_dict(p) for p in data.get("probes", [])]
    cards = data.get("cards", [])
    cells = data.get("cells", {})
    scores: Dict[str, Any] = {}
    for card_index, card in enumerate(cards):
        label = str(card.get("label", f"card{card_index}"))
        scenes: Dict[str, Any] = {}
        for probe in probes:
            group = [
                c for c in cells.values()
                if int(c.get("card_index", -1)) == card_index and c.get("probe") == probe.key
            ]
            group.sort(key=lambda c: int(c.get("sample", 0)))
            metrics: List[Dict[str, Any]] = []
            for cell in group:
                for turn in cell.get("turns", []):
                    # 异常占位轮不是她的表现，不能进指标分母
                    if turn.get("user") == "[ERROR]":
                        continue
                    if isinstance(turn.get("metrics"), dict):
                        # 指标口径随代码走：raw 里存了原文（raw/reply/chunks），
                        # --from-raw 时按当前正则重算，不迷信落盘时的旧口径
                        turn["metrics"] = evaluate_reply(
                            str(turn.get("raw", "")),
                            turn.get("chunks", []),
                            str(turn.get("reply", "")),
                        )
                        metrics.append(turn["metrics"])
            agg = aggregate_replies(metrics)
            agg["errors"] = sum(1 for c in group if c.get("error"))
            agg["skipped"] = sum(1 for c in group if c.get("skipped"))
            agg["samples"] = len(group)
            scene = score_scene(probe, agg)
            scene["probe"] = probe_to_dict(probe)
            scene["agg"] = agg
            scene["observed_stage"] = next(
                (
                    int(c["initial_state"]["stage"])
                    for c in group
                    if isinstance(c.get("initial_state"), dict)
                    and c["initial_state"].get("stage") is not None
                ),
                None,
            )
            scenes[probe.key] = scene
        scores[label] = {"scenes": scenes, "overall": score_run(scenes)}
    data["scores"] = scores
    return data


def _sample_agg(cell: Dict[str, Any]) -> Dict[str, Any]:
    """单格指标（报告里逐采样明细用；异常轮同样剔除）。"""
    metrics = [
        t["metrics"]
        for t in cell.get("turns", [])
        if t.get("user") != "[ERROR]" and isinstance(t.get("metrics"), dict)
    ]
    agg = aggregate_replies(metrics)
    agg["errors"] = 1 if cell.get("error") else 0
    agg["skipped"] = 1 if cell.get("skipped") else 0
    agg["samples"] = 1
    return agg


# ==========================================
# 报告
# ==========================================
METRIC_RUBRIC: Tuple[Tuple[str, str, str], ...] = (
    ("直球关心", "直接要回报行踪/直接催他做事的句子（报个信/忙完没/记得回来/告诉我一声/跟我说一声）", "CARE_DIRECT_RE"),
    ("索要关心", "反过来索取情绪价值或自怜（真要关心我/你是不是不在乎/谁关心我）", "CARE_ASK_RE"),
    ("直球承认", "直接承认担心/在乎（我担心你/是在担心你）——傲娇卡的失败线", "ADMIT_CARE_RE"),
    ("傲娇动作", "咳 / 哼 / 结巴（我、我）/ 否认（谁稀罕、才不、谁担心、想多了）", "COUGH_RE / HUM_RE / STUTTER_RE / DENIAL_RE"),
    ("包装词", "记账 / 利滚利 / 顺手 / 找灵感 / 盖戳 / 添麻烦；同局重复数 = 出现次数 − 类型数", "WRAP_WORDS"),
    ("先骂后兜底", "骂/数落词 vs 实际兜底动作（药、躺着、让伙计送）", "SCOLD_RE / CONCRETE_CARE_RE"),
    ("连环叮嘱", "按标点切句后命中叮嘱词表且非疑问句的小句数", "CAUTION_RE"),
    ("连环追问", "单条回复里问号 ≥2 记为反问链", "QUESTION_MARK_RE"),
    ("嘴硬找台阶", "谁稀罕/爱来不来/正好我也有事/随你；追问=来不来/什么时候/哪天（追问+自我掐断的连招不算）", "DODGE_RE / PURSUIT_RE / SELF_CUT_RE"),
    ("酸意", "那位/难怪/挺忙/哦/随你 等带刺信号；承认吃醋=吃醋/嫉妒/我介意", "SOUR_RE / ADMIT_JEALOUS_RE"),
    ("受伤/乞求", "失望/难过/白等 vs 求你/别走/陪我", "HURT_RE / BEG_RE"),
    ("气泡与长度", "parse_reply 的真实分段数；长度剥掉表情包标记后按字计", "bubbles_from_chunks / bare_len"),
    ("语气词", "呀/啦/吧/呢/嘛/哈/哦/嗯/诶/咯/哟/呗/～", "TONE_RE"),
    ("出戏/旁白", "括号或星号旁白、行首【起/接/收】、replier 内心旁白判据、AI 自曝、【看到：】泄漏", "replier 三件套 + SELF_AI_RE"),
)


def _pct_or_dash(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1f}"


def _scene_rows(scenes: Dict[str, Dict[str, Any]]) -> List[str]:
    rows = [
        "| 场景 | 类型 | 机械达标 | 得分 | 直球关心 | 索要关心 | 傲娇动作 | 包装(类型/重复) | 气泡均值 | 长度中位 | 出戏 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key, scene in scenes.items():
        agg = scene.get("agg", {})
        replies = max(1, int(agg.get("replies", 0)))
        rows.append(
            f"| {scene.get('title', key)} | {scene.get('kind', '')} | "
            f"{'✅' if scene.get('passed') else '❌'} | {scene.get('score', 0)} | "
            f"{int(agg.get('direct_care_replies', 0))}/{replies} | "
            f"{int(agg.get('care_ask_replies', 0))}/{replies} | "
            f"{int(agg.get('tsundere_replies', 0))}/{replies} | "
            f"{int(agg.get('wrap_types', 0))}/{int(agg.get('wrap_repeats', 0))} | "
            f"{float(agg.get('bubbles_mean', 0.0)):.2f} | "
            f"{float(agg.get('chars_median', 0.0)):.1f} | "
            f"{int(agg.get('leak_replies', 0))} |"
        )
    return rows


def _checks_table(scene: Dict[str, Any]) -> List[str]:
    rows = ["| 检查项 | 结果 | 明细 |", "| --- | --- | --- |"]
    for check in scene.get("checks", []):
        rows.append(
            f"| {check.get('name', '')} | {'✅' if check.get('ok') else '❌'} | {check.get('detail', '')} |"
        )
    return rows


def _sample_table(cells: Sequence[Dict[str, Any]]) -> List[str]:
    rows = [
        "| 采样 | 回复数 | 直球关心 | 索要关心 | 傲娇动作 | 包装(类型/重复) | 气泡均值 | 长度中位 | 出戏 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for cell in cells:
        agg = _sample_agg(cell)
        replies = max(1, int(agg.get("replies", 0)))
        mark = "（失败）" if cell.get("error") else ("（跳过）" if cell.get("skipped") else "")
        rows.append(
            f"| #{cell.get('sample', '')}{mark} | {int(agg.get('replies', 0))} | "
            f"{int(agg.get('direct_care_replies', 0))}/{replies} | "
            f"{int(agg.get('care_ask_replies', 0))}/{replies} | "
            f"{int(agg.get('tsundere_replies', 0))}/{replies} | "
            f"{int(agg.get('wrap_types', 0))}/{int(agg.get('wrap_repeats', 0))} | "
            f"{float(agg.get('bubbles_mean', 0.0)):.2f} | "
            f"{float(agg.get('chars_median', 0.0)):.1f} | "
            f"{int(agg.get('leak_replies', 0))} |"
        )
    return rows


def _transcript_block(cell: Dict[str, Any]) -> List[str]:
    lines: List[str] = []
    for pair in cell.get("seed_turns", []):
        if isinstance(pair, (list, tuple)) and len(pair) == 2:
            lines.append(f"- 【前情·他】{pair[0]}")
            lines.append(f"- 【前情·她】{str(pair[1]).replace(chr(10), ' / ')}")
    for turn in cell.get("turns", []):
        if turn.get("user") == "[ERROR]":
            lines.append(f"- **【异常】** {turn.get('reply', '')}")
            continue
        raw_chars = len(str(turn.get("raw", "")))
        lines.append(f"- 【他】{turn.get('user', '')}")
        reply = str(turn.get("reply", ""))
        if reply.strip() == SILENCE_TOKEN:
            lines.append(f"- 【她】{SILENCE_TOKEN}（沉默，模型原文 {raw_chars} 字）")
            continue
        lines.append(f"- 【她·实发】{reply.replace(chr(10), ' / ')}")
        metrics = turn.get("metrics", {})
        flags: List[str] = []
        for field, label in (
            ("direct_care", "直球关心"),
            ("care_ask", "索要关心"),
            ("admit_care", "直球承认"),
            ("hurt", "受伤"),
            ("beg", "乞求"),
            ("pursuit", "追问"),
            ("flat_accept", "营业式道谢"),
        ):
            values = metrics.get(field) or []
            if values:
                flags.append(f"{label}={','.join(str(v) for v in values)}")
        if int(metrics.get("leak_total", 0)) > 0:
            flags.append(f"出戏={int(metrics.get('leak_total', 0))}")
        if flags:
            lines.append(f"  - 命中：{'；'.join(flags)}")
    return lines


def build_report(data: Dict[str, Any], manual: Dict[str, str]) -> str:
    """生成 report.md（纯字符串，--from-raw 也走这里）。"""
    meta = data.get("meta", {})
    cards = data.get("cards", [])
    scores = data.get("scores", {})
    cells = data.get("cells", {})
    probes_data = data.get("probes", [])

    timeline = "、".join(f"{p.get('title', p.get('key'))}（{p.get('key')}）" for p in probes_data)

    lines: List[str] = []
    lines.append("# 祀卡傲娇性格跑分报告（persona_bench）")
    lines.append("")
    lines.append("## 〇、本次运行")
    lines.append("")
    lines.append(f"- 生成时间（真实墙钟）：{meta.get('generated_at', '')}")
    lines.append(
        f"- **时间伪装**：prompt 里的「现在」固定为 **{meta.get('fake_time', '')}**，seed={meta.get('seed', '')}"
    )
    lines.append(
        f"- 模型：`{meta.get('model', '')}`；采样温度：{meta.get('temperature_note', DEFAULT_TEMPERATURE_NOTE)}；"
        "不是自拼提示词——走生产 `PromptAssembler.assemble_messages` + `LLMGateway.stream_chat` + `Replier.parse_reply`"
    )
    lines.append(
        f"- 采样：每卡每探针 **{meta.get('samples', '')}** 次｜并发 {meta.get('concurrency', '')}｜"
        f"探针：{timeline}"
    )
    stage_note = (
        f"全局 --stage={meta.get('stage_override')}" if meta.get("stage_override") is not None
        else "按卡自带 affection.start_stage"
    )
    lines.append(f"- 开局阶段：{stage_note}")
    lines.append(
        f"- 成本：实际调用 {meta.get('total_llm_calls', 0)} 次，估算 **¥{meta.get('total_cost_cny', 0.0)}**｜"
        f"熔断上限 ¥{meta.get('fuse', 0.0)}"
        + ("（**已触发熔断**）" if meta.get("fuse_tripped") else "")
    )
    if meta.get("skipped_cells"):
        lines.append(f"- ⚠ 因熔断跳过 **{meta['skipped_cells']}** 格（这些格不计入通过）")
    if meta.get("failed_cells"):
        lines.append(f"- ⚠ 失败格 **{meta['failed_cells']}** 个（重试一次仍失败，计入 ❌ 但不进指标分母）")
    lines.append(f"- 耗时：{meta.get('elapsed_sec', 0)} 秒｜Python {meta.get('python', '')}")
    lines.append("")
    lines.append("### 沙箱纪律")
    lines.append("")
    for note in meta.get("discipline", []):
        lines.append(f"- {note}")
    lines.append("")
    lines.append("### 评分口径")
    lines.append("")
    lines.append(
        "风格类指标**不用 LLM 裁判**：每个数字都是正则/词表/计数，同一份原文永远算出同一个结果。"
        "场景机械达标 = 该场景全部检查项通过；场景得分 = 通过项占比；"
        f"综合分 = {TSUNDERE_WEIGHT:.1f}×傲娇分 + {1 - TSUNDERE_WEIGHT:.1f}×人味分。"
    )
    lines.append("")
    lines.append("| 指标 | 口径 | 实现 |")
    lines.append("| --- | --- | --- |")
    for name, desc, impl in METRIC_RUBRIC:
        lines.append(f"| {name} | {desc} | `{impl}` |")
    lines.append("")
    lines.append("---")
    lines.append("")

    for card_index, card in enumerate(cards):
        label = str(card.get("label", f"card{card_index}"))
        card_score = scores.get(label, {})
        overall = card_score.get("overall", {})
        scenes = card_score.get("scenes", {})
        lines.append(f"## 卡 {card_index + 1}：`{label}`")
        lines.append("")
        lines.append(f"- 角色卡：`{card.get('json_path', '')}`")
        if card.get("observed_stage") is not None:
            lines.append(f"- 开局阶段（首次采样实测）：{card['observed_stage']}")
        lines.append("")
        lines.append("### 总览")
        lines.append("")
        lines.append("| 指标 | 数值 |")
        lines.append("| --- | --- |")
        lines.append(
            f"| 傲娇分（{overall.get('tsundere_scenes', 0)} 个傲娇场景均分） | "
            f"{_pct_or_dash(overall.get('tsundere_score'))} |"
        )
        lines.append(
            f"| 人味基线分（{overall.get('human_scenes', 0)} 个基线场景均分） | "
            f"{_pct_or_dash(overall.get('human_score'))} |"
        )
        lines.append(f"| **综合分** | **{_pct_or_dash(overall.get('composite_score'))}** |")
        lines.append(f"| 机械达标场景 | {overall.get('scenes_passed', 0)}/{overall.get('scenes_total', 0)} |")
        lines.append("")
        lines.append("### 场景总表")
        lines.append("")
        lines.extend(_scene_rows(scenes))
        lines.append("")
        lines.append("### 逐场景明细")
        lines.append("")
        for probe_data in probes_data:
            probe = probe_from_dict(probe_data)
            scene = scenes.get(probe.key)
            if not scene:
                continue
            agg = scene.get("agg", {})
            lines.append(f"#### {probe.title}（`{probe.key}`｜{probe.kind}）")
            lines.append("")
            lines.append(f"- 设定：{probe.brief}")
            lines.append(f"- 期望：{probe.expect}")
            lines.append(
                f"- 开局阶段（本场景首次采样）：{scene.get('observed_stage', '—')}"
                f"｜采样 {agg.get('samples', 0)}｜回复 {agg.get('replies', 0)} 条"
                f"｜沉默 {agg.get('silent', 0)} 条"
            )
            lines.append("")
            lines.extend(_checks_table(scene))
            lines.append("")
            group = sorted(
                [
                    c for c in cells.values()
                    if int(c.get("card_index", -1)) == card_index and c.get("probe") == probe.key
                ],
                key=lambda c: int(c.get("sample", 0)),
            )
            lines.extend(_sample_table(group))
            lines.append("")
            for cell in group:
                lines.append(
                    f"**采样 #{cell.get('sample', '')}"
                    + ("（失败）" if cell.get("error") else "（熔断跳过）" if cell.get("skipped") else "")
                    + "**"
                )
                lines.append("")
                lines.extend(_transcript_block(cell))
                lines.append("")
            lines.append("---")
            lines.append("")

    if len(cards) >= 2:
        lines.append("## A/B 对照（同一 run、同一时钟、同一种子、同一探针）")
        lines.append("")
        names = [str(c.get("label", "")) for c in cards]
        lines.append("| 场景 | " + " | ".join(f"`{n}`" for n in names) + " | Δ（第 2 卡 − 第 1 卡） |")
        lines.append("| --- | " + " | ".join("---" for _ in names) + " | --- |")
        for probe_data in probes_data:
            probe = probe_from_dict(probe_data)
            values = [
                int(scores.get(name, {}).get("scenes", {}).get(probe.key, {}).get("score", 0))
                for name in names
            ]
            delta = (values[1] - values[0]) if len(values) >= 2 else 0
            lines.append(
                f"| {probe.title} | " + " | ".join(str(v) for v in values) + f" | {delta:+d} |"
            )
        lines.append("")
        lines.append("| 汇总 | " + " | ".join(f"`{n}`" for n in names) + " |")
        lines.append("| --- | " + " | ".join("---" for _ in names) + " |")
        for field, field_name in (
            ("tsundere_score", "傲娇分"),
            ("human_score", "人味基线分"),
            ("composite_score", "综合分"),
            ("scenes_passed", "机械达标场景"),
        ):
            values: List[Any] = [scores.get(name, {}).get("overall", {}).get(field) for name in names]
            lines.append(
                f"| {field_name} | "
                + " | ".join("—" if v is None else str(v) for v in values)
                + " |"
            )
        lines.append("")
        lines.append(
            "> 分数只用于定位「哪一类行为退化了」，最终以各场景原文为准；"
            "机械分相同也要看原文有没有把傲娇写成泼妇、把人味写成敷衍。"
        )
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 人工裁决区（所有者）")
    lines.append("")
    lines.append("> 这一节由所有者填。重新生成报告时脚本会原样保留 MANUAL 块。")
    lines.append("")
    lines.append("### 1. 逐条判读")
    lines.append("")
    lines.append(
        "<!--MANUAL:judge-->"
        + (manual.get("judge") or "（待所有者判读：傲娇有没有写成泼妇/舔狗，人味有没有变干）")
        + "<!--/MANUAL-->"
    )
    lines.append("")
    lines.append("### 2. 改卡结论")
    lines.append("")
    lines.append(
        "<!--MANUAL:verdict-->"
        + (manual.get("verdict") or "（待所有者裁决：这张卡能不能用；下轮改哪一条规则）")
        + "<!--/MANUAL-->"
    )
    lines.append("")
    return "\n".join(lines)


def load_prev_manual(report_path: str) -> Dict[str, str]:
    """从已有 report.md 里捞回人工裁决块（--from-raw 重算时不能丢）。"""
    if not os.path.isfile(report_path):
        return {}
    with open(report_path, "r", encoding="utf-8") as f:
        previous = f.read()
    out: Dict[str, str] = {}
    for match in re.finditer(r"<!--MANUAL:([\w.]+)-->(.*?)<!--/MANUAL-->", previous, re.S):
        out[match.group(1)] = match.group(2).strip()
    return out


# ==========================================
# 运行与入口
# ==========================================
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="祀卡傲娇性格跑分：确定性指标 + 采样留原文，改卡前后对照。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python scripts/sim/persona_bench.py --samples 3\n"
            "  python scripts/sim/persona_bench.py --card characters/si --card data/cards/si-old.json\n"
            "  python scripts/sim/persona_bench.py --from-raw --run data/persona_bench/run-20261022-213000\n"
            "  python scripts/sim/persona_bench.py --dry   # 只验提示词组装，不调 API\n"
        ),
    )
    parser.add_argument("--card", dest="cards", action="append", metavar="PATH",
                        help="角色卡目录，或任意命名的卡 JSON（可重复；重复=同 run 内 A/B）")
    parser.add_argument("--probes", default="all", help="all / 1,5,8 / care_exposed,goodnight")
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES,
                        help=f"每卡每探针采样数（默认 {DEFAULT_SAMPLES}）")
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED, help=f"随机种子（默认 {DEFAULT_SEED}）")
    parser.add_argument("--time", default=DEFAULT_TIME, help=f"时间伪装 YYYY-MM-DD HH:MM（默认 {DEFAULT_TIME}）")
    parser.add_argument("--stage", type=int, default=None, help="强制开局阶段 0~9（默认按卡）")
    parser.add_argument("--fuse", type=float, default=DEFAULT_FUSE,
                        help=f"成本熔断上限（元，默认 {DEFAULT_FUSE}；0=关闭）")
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY, help="并发格数")
    parser.add_argument("--from-raw", action="store_true", help="只读已有 raw.json 复算，不调 API")
    parser.add_argument("--run", default="", help="--from-raw 指定的 run 目录（默认最新一次）")
    parser.add_argument("--out", default="", help="本次输出目录（默认 data/persona_bench/run-<时间戳>）")
    parser.add_argument("--dry", action="store_true", help="试运行：只组装提示词并打印诊断，不调用 API")
    parser.add_argument("--style-examples", dest="style_examples_path", default="", metavar="LIB",
                        help="开启动态示例并指定示例库（如 data/style_examples/si_official.json）；"
                             "不传则按 config.toml 现状（默认关）")
    return parser


def latest_run_dir() -> str:
    root = os.path.join(ROOT, OUT_BASE)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"还没有任何跑分产物：{root}")
    candidates = [
        os.path.join(root, name)
        for name in os.listdir(root)
        if name.startswith("run-") and os.path.isdir(os.path.join(root, name))
    ]
    if not candidates:
        raise FileNotFoundError(f"{root} 下没有 run-* 目录")
    return max(candidates, key=lambda p: os.path.getmtime(p))


def _empty_meta(args: argparse.Namespace, cards: Sequence[CardSpec], probes: Sequence[Probe]) -> Dict[str, Any]:
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "fake_time": _FAKE_NOW.strftime("%Y-%m-%d %H:%M"),
        "seed": args.seed,
        "samples": max(1, int(args.samples)),
        "concurrency": args.concurrency,
        "fuse": args.fuse,
        "stage_override": args.stage,
        "probes": [p.key for p in probes],
        "cards": [c.label for c in cards],
        "temperature_note": DEFAULT_TEMPERATURE_NOTE,
        "python": sys.version.split()[0],
        "discipline": [
            "每个 (卡×探针×采样) 一个**全新空库**，跑完删库；初始好感度由卡的 affection.start_stage 决定，报告记实测阶段。",
            "多轮探针的前情是**离线注入**的种子对话（memory.save_turn_pair），不花 API；他没说过的话不会凭空出现。",
            "提示词走生产组装器，不另拼模板；语音默认关、引用/表情包切段走生产 replier。",
            "表情包/QQ 表情/语音按卡声明开启，但沙箱不真的发送，也不写生产库。",
            "本工具走 ChatSession 单发路径：每轮 1 次生成 + observer 结算。生产 TurnHandler 的 candidates=3 选优不在本路径内——成本更低，但「三选一挑最好那条」的机制没被测到。",
            "卡旁边没有 stickers/index.json 时，提示词里的表情包清单为空；A/B 要让两卡的表情包条件一致。",
            "不改 companion/、characters/、config.toml；--card 只在内存里改 cfg.character.path。",
        ],
    }


async def run_dry(card: CardSpec, probe: Probe, args: argparse.Namespace) -> None:
    """试运行：初始化沙箱 -> 注入前情 -> 组装提示词，打印诊断，不调用 API。"""
    cfg = make_config(card.dir, getattr(args, "style_examples_path", ""))
    db_path = os.path.join(WORK_DB_DIR, "dry.db")
    session = PersonaSession(config=cfg, sandbox_db_path=db_path, stage_override=args.stage)
    try:
        await session.initialize()
        state = await session.affection.get_state()
        for user_msg, bot_msg in probe.seed_turns:
            await session.memory.save_turn_pair(user_msg=user_msg, bot_msg=bot_msg)
        messages, system_prompt = await session.assembler.assemble_messages(probe.turns[0])
        roles = [m.get("role") for m in messages]
        print("[试运行] 未调用 API")
        print(f"  角色卡：{card.label} -> {card.dir}")
        print(f"  时间伪装：{_FAKE_NOW.strftime('%Y-%m-%d %H:%M')}｜seed={args.seed}")
        print(
            f"  开局：阶段 {state.get('stage')}｜复合分 {float(state.get('composite', 0.0)):.2f}"
            f"｜按复合分判定阶段 {determine_stage(float(state.get('composite', 0.0)), session.affection.thresholds)}"
        )
        print(f"  模型：{getattr(cfg.llm.active(), 'chat', '')}")
        print(f"  消息角色序列：{roles}")
        print(f"  探针：{probe.title}（{probe.key}）")
        print(f"  前情：{len(probe.seed_turns)} 轮｜本轮台词：{probe.turns[0] if probe.turns else ''}")
        print(f"  system prompt：{len(system_prompt)} 字")
        print(f"  messages：{len(messages)} 条（含 system 与工作记忆）")
        print("  --- system prompt 开头 240 字 ---")
        print("  " + system_prompt[:240].replace("\n", "\n  "))
        print("  --- system prompt 结尾 160 字 ---")
        print("  " + system_prompt[-160:].replace("\n", "\n  "))
    finally:
        try:
            await session.close()
        except Exception:
            pass
        _kill_db(db_path)


async def run_bench(args: argparse.Namespace) -> None:
    started = datetime.now()
    probes = select_probes(args.probes)
    card_specs = args.cards or [DEFAULT_CARD]
    resolved = [resolve_card_spec(spec) for spec in card_specs]
    # 同一张卡传两次（例如想测方差）也必须有两个互不覆盖的报告分区：
    # scores 是按 label 存的，label 重名会把第二份结果吞掉。
    seen: Dict[str, int] = {}
    cards: List[CardSpec] = []
    for spec in resolved:
        count = seen.get(spec.label, 0) + 1
        seen[spec.label] = count
        cards.append(
            CardSpec(
                label=spec.label if count == 1 else f"{spec.label}#{count}",
                dir=spec.dir,
                json_path=spec.json_path,
            )
        )
    # 任意命名的卡 JSON 先落成标准卡目录（character.json 路径的卡原样不动）
    cards = [prepare_card_dir(card, index) for index, card in enumerate(cards)]
    for card in cards:
        if not os.path.isfile(os.path.join(card.dir, "stickers", "index.json")):
            print(
                f"[提示] 卡 {card.label} 没有 stickers/index.json：提示词里的表情包清单会是空的，"
                "和带表情包的卡不是严格同条件（A/B 请把卡 JSON 放在带 stickers/ 的目录旁）",
                flush=True,
            )
    set_fake_time(args.time)
    patch_time()
    run_dir = args.out or os.path.join(OUT_BASE, f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}")
    run_dir = run_dir if os.path.isabs(run_dir) else os.path.join(ROOT, run_dir)
    os.makedirs(os.path.join(ROOT, WORK_DB_DIR), exist_ok=True)

    model_name = ""
    budget = Budget(args.fuse)
    log: List[str] = []
    cells: Dict[str, Dict[str, Any]] = {}
    try:
        if args.dry:
            await run_dry(cards[0], probes[0], args)
            return

        # 先用第一张卡验一次配置，把"没 key / 配置坏了"挡在开跑之前
        first_cfg = make_config(cards[0].dir, getattr(args, "style_examples_path", ""))
        active = first_cfg.llm.active()
        if not getattr(active, "api_key", ""):
            raise SystemExit("config.toml 未配置可用的 LLM key，无法跑分")
        model_name = getattr(active, "chat", "")

        if int(args.concurrency) > 1:
            print(
                "[提示] 并发>1 时各格共享全局 random（mood 噪声），"
                "严格 A/B 对照请用 --concurrency 1",
                flush=True,
            )
        sem = asyncio.Semaphore(max(1, int(args.concurrency)))
        print(
            f"[跑分] 卡 {[c.label for c in cards]}｜探针 {[p.key for p in probes]}｜"
            f"每卡每探针 {max(1, int(args.samples))} 次｜时钟 {_FAKE_NOW}｜熔断 ¥{args.fuse}",
            flush=True,
        )
        jobs = [
            run_cell(card, ci, probe, pi, sample, args, budget, sem, cells, log)
            for ci, card in enumerate(cards)
            for pi, probe in enumerate(probes)
            for sample in range(1, max(1, int(args.samples)) + 1)
        ]
        await asyncio.gather(*jobs)
    finally:
        restore_time()

    if args.dry:
        return

    total_calls = sum(int((c.get("cost") or {}).get("calls", 0)) for c in cells.values())
    total_cost = round(sum(float((c.get("cost") or {}).get("cost_cny", 0.0)) for c in cells.values()), 4)
    failed_cells = sum(1 for c in cells.values() if c.get("error"))
    skipped_cells = sum(1 for c in cells.values() if c.get("skipped"))

    data: Dict[str, Any] = {
        "meta": _empty_meta(args, cards, probes),
        "cards": [
            {
                "label": card.label,
                "dir": card.dir,
                "json_path": card.json_path,
                "observed_stage": next(
                    (
                        int(c["initial_state"]["stage"])
                        for c in cells.values()
                        if int(c.get("card_index", -1)) == ci
                        and isinstance(c.get("initial_state"), dict)
                        and c["initial_state"].get("stage") is not None
                    ),
                    None,
                ),
            }
            for ci, card in enumerate(cards)
        ],
        "probes": [probe_to_dict(p) for p in probes],
        "cells": cells,
    }
    data["meta"].update({
        "model": model_name,
        "total_llm_calls": total_calls,
        "total_cost_cny": total_cost,
        "failed_cells": failed_cells,
        "skipped_cells": skipped_cells,
        "fuse_tripped": bool(budget.tripped),
        "elapsed_sec": int((datetime.now() - started).total_seconds()),
    })
    summarize(data)

    os.makedirs(run_dir, exist_ok=True)
    raw_path = os.path.join(run_dir, "raw.json")
    report_path = os.path.join(run_dir, "report.md")
    manual = load_prev_manual(report_path)
    with open(raw_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(build_report(data, manual))

    print("\n".join(log))
    print(f"\n完成：{len(cells)} 格｜失败 {failed_cells}｜熔断跳过 {skipped_cells}")
    print(f"总调用 {total_calls} 次，估算费用 ¥{total_cost}（熔断 ¥{args.fuse}{'，已触发' if budget.tripped else ''}）")
    for label, score in data["scores"].items():
        overall = score["overall"]
        print(
            f"  {label}: 综合 {overall['composite_score']}｜傲娇 {_pct_or_dash(overall['tsundere_score'])}"
            f"｜人味 {_pct_or_dash(overall['human_score'])}"
            f"｜达标场景 {overall['scenes_passed']}/{overall['scenes_total']}"
        )
    print(f"\n报告：{report_path}\n留档：{raw_path}")


def recompute(args: argparse.Namespace) -> None:
    """--from-raw：只读 raw.json 重算指标/评分/报告，不碰 API、不碰 config。"""
    run_dir = args.run or latest_run_dir()
    run_dir = run_dir if os.path.isabs(run_dir) else os.path.join(ROOT, run_dir)
    raw_path = os.path.join(run_dir, "raw.json")
    if not os.path.isfile(raw_path):
        raise SystemExit(f"找不到 {raw_path}")
    with open(raw_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    summarize(data)
    report_path = os.path.join(run_dir, "report.md")
    manual = load_prev_manual(report_path)
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(build_report(data, manual))
    print(f"[复算] 已用 {raw_path} 零成本重算指标与评分")
    for label, score in data.get("scores", {}).items():
        overall = score.get("overall", {})
        print(
            f"  {label}: 综合 {overall.get('composite_score')}｜傲娇 {_pct_or_dash(overall.get('tsundere_score'))}"
            f"｜人味 {_pct_or_dash(overall.get('human_score'))}"
            f"｜达标场景 {overall.get('scenes_passed')}/{overall.get('scenes_total')}"
        )
    print(f"报告：{report_path}")


def main() -> None:
    args = build_parser().parse_args()
    os.chdir(ROOT)
    try:
        if args.from_raw:
            recompute(args)
        else:
            asyncio.run(run_bench(args))
    except (FileNotFoundError, ValueError) as exc:
        raise SystemExit(f"错误：{exc}")


if __name__ == "__main__":
    main()
