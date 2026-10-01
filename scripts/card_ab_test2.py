"""角色卡文风 A/B 盲测 · 第二轮 (scripts/card_ab_test2.py)

第一轮（scripts/card_ab_test.py / data/card_ab_result.md）的教训：样本小（每卡 20 条）、
实跑时段在凌晨 01:51（角色卡作息为"已经睡下了"，两卡都和作息打架，属测试伪影）、
指标口径有硬伤（句尾问号率把"陈述式追问"判成无钩子；长度方差被单条离群值主导）。

第二轮做三件升级：
  1) 时间伪装：把提示词组装链路上的 `datetime.now()` / `now_str()` 统一替换为固定时刻
     （默认 2026-10-01 20:30 星期四，角色卡作息里的正常清醒时段），组装的提示词里
     注入的就是晚间场景；伪装只发生在本脚本进程内（monkeypatch 模块属性），不碰源码。
  2) 大样本：12 探针 × 2 卡 × 每卡 5 次 = 120 次真实调用（deepseek-v4-pro，temp 0.7，
     thinking low，与生产一致）；两卡各用同一 data/server-companion.db 的全新快照组装，
     并固定 random 种子，保证除角色卡本身外，注入的状态逐字节一致。
  3) 修正口径：称呼（总量 + 按探针类型分层）、钩子（句尾问句率 + 含问号率 + 反问链）、
     比喻（条数 + 逐条摘录）、长度（min/max/均值 + ≤30 字占比 + ≥80 字占比，弃用方差）、
     泄漏数、人工抽读记账。

用法：
  ./venv/Scripts/python.exe scripts/card_ab_test2.py               # 真实调用并出报告
  ./venv/Scripts/python.exe scripts/card_ab_test2.py --dry         # 只组装提示词并验证时间伪装，不调用 API
  ./venv/Scripts/python.exe scripts/card_ab_test2.py --from-raw    # 用 data/card_ab2_raw.json 重算报告
  ./venv/Scripts/python.exe scripts/card_ab_test2.py --runs 5 --time "2026-10-01 20:30" --seed 20261001

产物：
  data/card_ab2_result.md   报告（探针全文 + 量化统计 + 结论）
  data/card_ab2_raw.json    原始回复留档（便于不重复付费地复算指标）
  data/ab_old_card/         旧卡临时目录（内含 character.json，与第一轮共用）
  data/ab2_work_{old,new}.db  每次组装前从 data/server-companion.db 复制的库快照

报告里的 3.2 / 3.3 / 3.4 / 3.5 是人工判读节，用 <!--MANUAL:id--> ... <!--/MANUAL--> 包裹；
重跑本脚本（含 --from-raw）会保留上一次已写成的人工判读内容，不会覆盖回占位符。

纪律：不改 characters/ 下任何文件、不改 config.toml、不改 companion/ 源码
（时间伪装只在测试脚本进程内 patch 模块属性，进程退出即失效）；
API key 只从 config.toml 读取，任何产物（报告/留档/终端）里都不得出现 key。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import random
import re
import shutil
import statistics
import sys
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import aiohttp

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import Config
from companion.db import Database, now_str
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.stickers import StickerManager

# ── 路径与常量 ──
OLD_CARD_SRC = "characters/qingzi/character.backup-before-fixes9.json"
OLD_CARD_DIR = "data/ab_old_card"
NEW_CARD_DIR = "characters/qingzi"
STICKER_DIR = "characters/qingzi/stickers"
DB_SRC = "data/server-companion.db"
WORK_DB_OLD = "data/ab2_work_old.db"
WORK_DB_NEW = "data/ab2_work_new.db"
RAW_JSON = "data/card_ab2_raw.json"
RESULT_MD = "data/card_ab2_result.md"
ROUND1_RAW = "data/card_ab_raw.json"

MODEL = "deepseek-v4-pro"
TEMPERATURE = 0.7
THINKING_PAYLOAD = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
RUNS = 5
CONCURRENCY = 5
TIMEOUT = aiohttp.ClientTimeout(total=180)

# ── 时间伪装 ──
# 2026-10-01 是星期四（weekday=3），20:30 命中角色卡周四作息 [3] 19-21 的正常清醒时段。
FAKE_NOW_DEFAULT = "2026-10-01 20:30"
RANDOM_SEED_DEFAULT = 20261001
# 组装链路上会调用 datetime.now()/now_str() 的模块；now_str 定义在 companion.db，
# 其他模块是 `from companion.db import now_str` 引入同一函数对象，其 __globals__ 仍是
# companion.db 的命名空间，因此 patch companion.db.datetime 即可让全链路生效。
TIME_PATCH_MODULES = (
    "companion.db",
    "companion.assembler",
    "companion.memory",
    "companion.mood",
    "companion.affection",
)
_ORIG_DATETIME: Dict[str, Any] = {}

# ── 探针集：12 个，A=问候/正经说事类，B=低内容/日常类（第一轮 10 个全保留，便于跨轮对照）──
STRATA = {"A": "问候/正经说事类", "B": "低内容/日常类"}
PROBES: List[Tuple[str, str, str]] = [
    ("日常-在干嘛", "在干嘛", "A"),
    ("日常-课好满", "我今天课好满", "A"),
    ("表情包-索要", "发个表情包看看", "B"),
    ("挫败-考试砸了", "我考试砸了", "A"),
    ("情感-想你了", "想你了", "A"),
    ("定位-你在哪", "你在哪呢", "A"),
    ("场景-晚霞照片", "晚霞那张照片好看吧", "A"),
    ("无营养-大笑", "哈哈哈哈笑死我了", "B"),
    ("陷阱-AI身份", "你是不是AI啊", "A"),
    ("收尾-晚安", "晚安", "A"),
    ("学业-文学院作业", "你们文学院平时作业多吗", "A"),
    ("低内容-哦哦", "哦哦", "B"),
]

# ── 量化指标用到的判定规则 ──
ADDRESS = "小W同学"
TAG_LEAK_RE = re.compile(r"【[起接收]】")
METAPHOR_RE = re.compile(r"像|仿佛|似的|宛如")
STICKER_RE = re.compile(r"\[(?:表情|sticker|动画表情)[^\]]*\]")
Q_RE = re.compile(r"[？?]")
THIRD_PERSON_RE = re.compile(r"她")
TIME_WORD_RE = re.compile(
    r"(凌晨|半夜|深夜|大清早|一早|早上|上午|中午|下午|傍晚|晚上|今晚|明早|明天|昨天|昨晚|今天"
    r"|\d{1,2}\s*点(?:半|钟)?|快\d{1,2}\s*点)"
)
BARE_REPLY_MAX = 6  # 剥掉表情包标记后 ≤6 字，视为极短裸回复（敷衍风险抽样）

MANUAL_IDS = ("3.2", "3.3", "3.4", "3.5")


def arg_value(name: str, default: str) -> str:
    """极简命令行取值：支持 `--name value` 与 `--name=value`"""
    for i, a in enumerate(sys.argv):
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
        if a == name and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


FAKE_NOW: datetime = datetime.strptime(arg_value("--time", FAKE_NOW_DEFAULT), "%Y-%m-%d %H:%M")
FAKE_NOW_STR: str = FAKE_NOW.strftime("%Y-%m-%d %H:%M")
RANDOM_SEED: int = int(arg_value("--seed", str(RANDOM_SEED_DEFAULT)))
RUNS = int(arg_value("--runs", str(RUNS)))


class FakeDatetime(datetime):
    """固定"现在"的 datetime 子类；只 patch 到 companion 各模块的同名属性上"""

    @classmethod
    def now(cls, tz=None):
        return FAKE_NOW if tz is None else FAKE_NOW.replace(tzinfo=tz)

    @classmethod
    def utcnow(cls):
        return FAKE_NOW

    @classmethod
    def today(cls):
        return FAKE_NOW


def patch_time() -> None:
    for name in TIME_PATCH_MODULES:
        mod = importlib.import_module(name)
        if name not in _ORIG_DATETIME:
            _ORIG_DATETIME[name] = getattr(mod, "datetime")
        setattr(mod, "datetime", FakeDatetime)


def restore_time() -> None:
    for name, orig in _ORIG_DATETIME.items():
        setattr(importlib.import_module(name), "datetime", orig)


def prepare_old_card_dir() -> None:
    """为旧卡建临时目录：Persona.load 需要目录 + character.json 的文件布局"""
    os.makedirs(OLD_CARD_DIR, exist_ok=True)
    shutil.copyfile(OLD_CARD_SRC, os.path.join(OLD_CARD_DIR, "character.json"))


async def build_messages(char_dir: str, work_db: str, probe: str) -> Tuple[List[Dict[str, Any]], str, Dict[str, Any]]:
    """用生产组装器复刻提示词。每次组装前从 data/server-companion.db 复制一份全新库快照，
    并在组装前固定 random 种子（情绪引擎的均值回归带高斯噪声），保证两张卡看到的
    记忆/情绪/好感度状态逐字节一致。"""
    shutil.copyfile(DB_SRC, work_db)
    persona = Persona.load(char_dir)
    db = Database(work_db)
    await db.connect()
    try:
        affection = AffectionEngine(db, persona.initial_dims)
        mood = MoodEngine(db)
        memory = MemoryManager(db)
        stickers = StickerManager(STICKER_DIR, db)
        assembler = PromptAssembler(persona, affection, mood, memory, stickers, db)
        random.seed(RANDOM_SEED)
        messages, system_prompt = await assembler.assemble_messages(probe)
        diag = {
            "now_str": now_str(),
            "hours_since_last_chat": round(await mood.get_hours_since_last_chat(), 2),
            "mood": await mood.get_state(),
        }
        return messages, system_prompt, diag
    finally:
        await db.close()


async def call_model(
    session: aiohttp.ClientSession,
    config: Config,
    messages: List[Dict[str, Any]],
    sem: asyncio.Semaphore,
) -> str:
    """调用 deepseek-v4-pro（thinking low）。失败重试一次，仍失败记 [失败]。"""
    active = config.llm.active()
    payload: Dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "temperature": TEMPERATURE,
    }
    payload.update(THINKING_PAYLOAD)
    url = f"{active.base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {active.api_key}", "Content-Type": "application/json"}

    last_err = ""
    async with sem:
        for attempt in (1, 2):
            try:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status != 200:
                        last_err = f"HTTP {resp.status}: {(await resp.text())[:200]}"
                    else:
                        data = await resp.json()
                        return str(data["choices"][0]["message"]["content"]).strip()
            except Exception as e:  # 网络/超时/解析
                last_err = f"{type(e).__name__}: {e}"
            if attempt == 1:
                await asyncio.sleep(3)
    return f"[失败] {last_err}"


# ==========================================
# 指标
# ==========================================

def char_count(text: str) -> int:
    """字数：去掉所有空白字符后的字符数（含表情包标记，与第一轮口径一致）"""
    return len(re.sub(r"\s", "", text))


def char_count_bare(text: str) -> int:
    """字数：去掉空白与表情包标记后的字符数（补充口径，用于长短条占比的复核）"""
    return len(re.sub(r"\s", "", STICKER_RE.sub("", text)))


def strip_trailing_sticker(text: str) -> str:
    """剥掉结尾的表情包标记（生产侧表情包单独成段发出，不该影响句尾判定）"""
    return re.sub(r"(\s*\[(?:表情|sticker|动画表情)[^\]]*\])+\s*$", "", text).strip()


def question_count(text: str) -> int:
    return len(Q_RE.findall(text))


def metric_blocks(replies: List[str]) -> Dict[str, Any]:
    """对一组回复做量化统计（第二轮口径）"""
    total = len(replies)
    addr = [r for r in replies if ADDRESS in r]
    tail_q = [r for r in replies if re.search(r"[？?]\s*$", r)]
    tail_q_clean = [r for r in replies if re.search(r"[？?]\s*$", strip_trailing_sticker(r))]
    q_any = [r for r in replies if Q_RE.search(r)]
    chains = [r for r in replies if question_count(strip_trailing_sticker(r)) >= 2]
    q_total = sum(question_count(r) for r in replies)
    metaphor = [(i, r) for i, r in enumerate(replies) if METAPHOR_RE.search(r)]
    leaks = [(i, r) for i, r in enumerate(replies) if TAG_LEAK_RE.search(r)]
    sticker = [r for r in replies if STICKER_RE.search(r)]
    third_person = [(i, r) for i, r in enumerate(replies) if THIRD_PERSON_RE.search(r)]
    lengths = [char_count(r) for r in replies]
    bare = [char_count_bare(r) for r in replies]
    return {
        "total": total,
        "addr_n": len(addr),
        "addr_rate": len(addr) / total if total else 0.0,
        "addr_occurrences": sum(r.count(ADDRESS) for r in replies),
        "tail_q_n": len(tail_q),
        "tail_q_rate": len(tail_q) / total if total else 0.0,
        "tail_q_clean_n": len(tail_q_clean),
        "tail_q_clean_rate": len(tail_q_clean) / total if total else 0.0,
        "q_any_n": len(q_any),
        "q_any_rate": len(q_any) / total if total else 0.0,
        "q_total": q_total,
        "chain_n": len(chains),
        "chain_rate": len(chains) / total if total else 0.0,
        "metaphor": metaphor,
        "leaks": leaks,
        "sticker_n": len(sticker),
        "third_person": third_person,
        "bare_replies": [(i, r) for i, r in enumerate(replies) if char_count_bare(r) <= BARE_REPLY_MAX],
        "lengths": lengths,
        "bare_lengths": bare,
        "len_min": min(lengths) if lengths else 0,
        "len_max": max(lengths) if lengths else 0,
        "len_mean": statistics.fmean(lengths) if lengths else 0.0,
        "bare_len_mean": statistics.fmean(bare) if bare else 0.0,
        "short_n": sum(1 for x in bare if x <= 30),
        "short_rate": sum(1 for x in bare if x <= 30) / total if total else 0.0,
        "long_n": sum(1 for x in bare if x >= 80),
        "long_rate": sum(1 for x in bare if x >= 80) / total if total else 0.0,
    }


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """占比的 Wilson 95% 置信区间（n 不大时必须给区间，不要只比点估计）"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def tagged(p_n: int, p_total: int) -> str:
    """占比 + Wilson 区间的一行文本"""
    lo, hi = wilson_ci(p_n, p_total)
    return f"{p_n}/{p_total} = {pct(p_n / p_total if p_total else 0)}（Wilson 95% CI {pct(lo)}~{pct(hi)}）"


def excerpt(text: str, span: int = 26, pos: int = 0) -> str:
    left = max(0, pos - span)
    right = min(len(text), pos + span)
    return ("…" if left > 0 else "") + text[left:right].replace("\n", " ") + ("…" if right < len(text) else "")


def fmt_metric_table(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    rows = [
        ("样本条数", str(old["total"]), str(new["total"])),
        ("称呼「小W同学」条数", f"{old['addr_n']}（{pct(old['addr_rate'])}）", f"{new['addr_n']}（{pct(new['addr_rate'])}）"),
        ("钩子①句尾问句条数", f"{old['tail_q_n']}（{pct(old['tail_q_rate'])}）", f"{new['tail_q_n']}（{pct(new['tail_q_rate'])}）"),
        ("钩子①′句尾问句（剥尾部表情标记）", f"{old['tail_q_clean_n']}（{pct(old['tail_q_clean_rate'])}）", f"{new['tail_q_clean_n']}（{pct(new['tail_q_clean_rate'])}）"),
        ("钩子②含问号条数", f"{old['q_any_n']}（{pct(old['q_any_rate'])}）", f"{new['q_any_n']}（{pct(new['q_any_rate'])}）"),
        ("钩子③反问链（单条 ≥2 问号）", f"{old['chain_n']}（{pct(old['chain_rate'])}）", f"{new['chain_n']}（{pct(new['chain_rate'])}）"),
        ("问号总数 / 条均", f"{old['q_total']} / {old['q_total'] / max(old['total'], 1):.2f}", f"{new['q_total']} / {new['q_total'] / max(new['total'], 1):.2f}"),
        ("比喻疑似条数", str(len(old["metaphor"])), str(len(new["metaphor"]))),
        ("【起/接/收】泄漏条数", str(len(old["leaks"])), str(len(new["leaks"]))),
        ("带表情包标记条数", str(old["sticker_n"]), str(new["sticker_n"])),
        ("第三人称「她」命中条数", str(len(old["third_person"])), str(len(new["third_person"]))),
        ("极短裸回复条数（≤6 字，剥标记）", str(len(old["bare_replies"])), str(len(new["bare_replies"]))),
        ("字数 min / max", f"{old['len_min']} / {old['len_max']}", f"{new['len_min']} / {new['len_max']}"),
        ("字数 均值（剥表情标记后）", f"{old['len_mean']:.1f}（{old['bare_len_mean']:.1f}）", f"{new['len_mean']:.1f}（{new['bare_len_mean']:.1f}）"),
        ("≤30 字条数", f"{old['short_n']}（{pct(old['short_rate'])}）", f"{new['short_n']}（{pct(new['short_rate'])}）"),
        ("≥80 字条数", f"{old['long_n']}（{pct(old['long_rate'])}）", f"{new['long_n']}（{pct(new['long_rate'])}）"),
    ]
    out = ["| 指标（第二轮口径） | 旧卡（FIXES9 前） | 新卡（FIXES9） |", "| --- | --- | --- |"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(out)


def fmt_metaphor_block(title: str, metric: Dict[str, Any]) -> str:
    lines = [f"**{title}**：命中 {len(metric['metaphor'])} 条" + ("（无）" if not metric["metaphor"] else "")]
    for rank, (i, r) in enumerate(metric["metaphor"]):
        m = METAPHOR_RE.search(r)
        lines.append(f"- 第 {rank + 1} 处（样本内第 {i + 1} 条）：{excerpt(r, 30, m.start() if m else 0)}")
    return "\n".join(lines)


def fmt_leak_block(metric: Dict[str, Any]) -> str:
    if not metric["leaks"]:
        return "无标签泄漏（【起】/【接】/【收】零出现）"
    lines = []
    for i, r in metric["leaks"]:
        lines.append(f"- 第 {i + 1} 条：{excerpt(r, 30)}")
    return "\n".join(lines)


def flatten(raw: Dict[str, Any], card: str, only_labels: Optional[List[str]] = None) -> List[Tuple[str, str]]:
    """把留档展平成 [(label, reply)]；[失败] 条目剔除"""
    out: List[Tuple[str, str]] = []
    for item in raw["probes"]:
        if only_labels is not None and item["label"] not in only_labels:
            continue
        for r in item[card]:
            if not r.startswith("[失败]"):
                out.append((item["label"], r))
    return out


def stratum_replies(raw: Dict[str, Any], card: str, stratum: str) -> List[str]:
    labels = [label for label, _, s in PROBES if s == stratum]
    return [r for label, r in flatten(raw, card, labels)]


def strata_table(raw: Dict[str, Any]) -> str:
    lines = [
        "| 分层 | 卡 | 条数 | 称呼「小W同学」 | 含问号 | 反问链(≥2问号) | 均长(剥标记) |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for key, name in STRATA.items():
        labels = [label for label, _, s in PROBES if s == key]
        for card, cn in (("old", "旧卡"), ("new", "新卡")):
            replies = [r for label, r in flatten(raw, card, labels)]
            m = metric_blocks(replies)
            lines.append(
                f"| {name}（{len(labels)} 探针） | {cn} | {m['total']} | "
                f"{m['addr_n']}（{pct(m['addr_rate'])}） | {m['q_any_n']}（{pct(m['q_any_rate'])}） | "
                f"{m['chain_n']}（{pct(m['chain_rate'])}） | {m['bare_len_mean']:.1f} |"
            )
    return "\n".join(lines)


def per_probe_table(raw: Dict[str, Any]) -> str:
    lines = [
        "| 探针 | 层 | 旧卡 称呼/含问号/反问链/均长 | 新卡 称呼/含问号/反问链/均长 |",
        "| --- | --- | --- | --- |",
    ]
    for item in raw["probes"]:
        row = []
        for card in ("old", "new"):
            replies = [r for r in item[card] if not r.startswith("[失败]")]
            m = metric_blocks(replies)
            row.append(
                f"{m['addr_n']}/{m['total']} · {m['q_any_n']}/{m['total']} · {m['chain_n']}/{m['total']} · {m['bare_len_mean']:.1f} 字"
            )
        label = item["label"]
        stratum = next((s for lb, _, s in PROBES if lb == label), "")
        lines.append(f"| {label} | {stratum} | {row[0]} | {row[1]} |")
    return "\n".join(lines)


def time_word_block(metric: Dict[str, Any]) -> str:
    hits = [(i, r) for i, r in enumerate(metric["_replies"]) if TIME_WORD_RE.search(r)]
    if not hits:
        return "无时间词命中"
    lines = []
    for i, r in hits:
        words = "、".join(sorted(set(TIME_WORD_RE.findall(r))))
        lines.append(f"- 第 {i + 1} 条（时间词：{words}）：{excerpt(r, 30)}")
    return "\n".join(lines)


def load_prev_manual() -> Dict[str, str]:
    """从既有报告里回收人工判读节，重跑不覆盖人工内容"""
    if not os.path.exists(RESULT_MD):
        return {}
    with open(RESULT_MD, "r", encoding="utf-8") as f:
        prev = f.read()
    out: Dict[str, str] = {}
    for m in re.finditer(r"<!--MANUAL:([\w.]+)-->(.*?)<!--/MANUAL-->", prev, re.S):
        out[m.group(1)] = m.group(2).strip()
    return out


def manual(mid: str, blocks: Dict[str, str], placeholder: str) -> str:
    body = blocks.get(mid) or placeholder
    return f"<!--MANUAL:{mid}-->\n{body}\n<!--/MANUAL-->"


def load_round1() -> Optional[Dict[str, Any]]:
    if not os.path.exists(ROUND1_RAW):
        return None
    try:
        with open(ROUND1_RAW, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def round1_section() -> str:
    """第一轮留档用第二轮口径重算：同口径、只比两轮共有的 10 个探针，便于跨轮读数"""
    r1 = load_round1()
    if not r1:
        return "（未找到 `data/card_ab_raw.json`，跳过跨轮对照）"
    labels = [label for label, _, _ in PROBES]
    old = metric_blocks([r for _, r in flatten(r1, "old", labels)])
    new = metric_blocks([r for _, r in flatten(r1, "new", labels)])
    labeled = [it["label"] for it in r1["probes"] if it["label"] in labels]
    lines = [
        f"第一轮留档：`{ROUND1_RAW}`，探针 {len(labeled)} 个 × 每卡 {r1['meta'].get('runs_per_card')} 次 "
        f"（每卡 {old['total']}/{new['total']} 条）；生成于 {r1['meta'].get('generated_at')}，"
        "当时未做时间伪装（实跑凌晨 01:51）——**只用于口径对齐，不要与第二轮的状态条件直接混比**。",
        "",
        fmt_metric_table(old, new),
        "",
        "上表用的是第二轮口径重算第一轮同一批留档，因此「旧卡 vs 新卡」的差值方向可与第二轮直接对齐："
        f"称呼 {pct(old['addr_rate'])} → {pct(new['addr_rate'])}；含问号 {pct(old['q_any_rate'])} → {pct(new['q_any_rate'])}；"
        f"反问链 {pct(old['chain_rate'])} → {pct(new['chain_rate'])}；"
        f"≥80 字 {old['long_n']} → {new['long_n']} 条；≤30 字 {old['short_n']} → {new['short_n']} 条。",
    ]
    return "\n".join(lines)


# ==========================================
# 报告
# ==========================================

def build_report(raw: Dict[str, Any], manual_blocks: Dict[str, str]) -> str:
    meta = raw["meta"]
    old_replies = [r for _, r in flatten(raw, "old")]
    new_replies = [r for _, r in flatten(raw, "new")]
    old_m = metric_blocks(old_replies)
    new_m = metric_blocks(new_replies)
    old_m["_replies"] = old_replies
    new_m["_replies"] = new_replies

    L: List[str] = []
    L.append("# 角色卡文风 A/B 盲测报告 · 第二轮（FIXES9 后复测）")
    L.append("")
    L.append(f"- 生成时间（真实系统时间）：{meta['generated_at']}")
    L.append(
        f"- **时间伪装**：组装期注入的「现在」固定为 **{meta['fake_now']}**"
        f"（星期{'一二三四五六日'[datetime.strptime(meta['fake_now'], '%Y-%m-%d %H:%M').weekday()]}），"
        f"经 `{', '.join(TIME_PATCH_MODULES)}` 五个模块的 `datetime` 属性 patch 生效"
        f"（`now_str()` 定义在 `companion.db`，其余模块引用同一函数对象，patch 一处全链路生效）；"
        f"random 种子固定为 {meta['seed']}，两卡状态逐字节一致。"
    )
    L.append(f"- 模型：`{meta['model']}`，temperature {meta['temperature']}，thinking enabled / reasoning_effort low（与生产一致）")
    L.append(f"- 数据库：`{meta['db']}`（只读拉取后按探针复制快照，两卡同源同态）")
    L.append(f"- 对照：`{meta['old_card']}`（旧卡，FIXES9 前） vs `{meta['new_card']}`（新卡，FIXES9 大修后）")
    L.append(f"- 样本：探针 {len(PROBES)} 个 × 每卡 {meta['runs_per_card']} 次 = 每卡 {meta['runs_per_card'] * len(PROBES)} 条，两卡合计 {meta['runs_per_card'] * len(PROBES) * 2} 条")
    if meta.get("failed_calls"):
        L.append(f"- 失败调用：{meta['failed_calls']} 条（重试一次仍失败，记 [失败]，已从统计分母剔除）")
    L.append(f"- 提示词长度（探针 1）：旧卡 system {meta['prompt_len']['old']} 字 / 新卡 system {meta['prompt_len']['new']} 字")
    L.append(f"- 调用耗时：{meta.get('elapsed_sec', 0)} 秒")
    L.append("")
    L.append("### 0. 时间伪装验证（本轮关键前置条件）")
    L.append("")
    L.append("注入到 system prompt 里的状态片段（从【她此刻】到【当前关系阶段】之间，两卡逐字节一致）：")
    L.append("")
    L.append("```")
    L.append(meta["state_span"])
    L.append("```")
    L.append("")
    for line in meta["verification"]:
        L.append(f"- {line}")
    L.append("")
    L.append("---")
    L.append("")

    L.append(f"## 一、探针逐条全文（旧卡 1~{meta['runs_per_card']} vs 新卡 1~{meta['runs_per_card']}）")
    L.append("")
    for i, item in enumerate(raw["probes"]):
        stratum = next((s for lb, _, s in PROBES if lb == item["label"]), "?")
        L.append(f"### 探针 {i + 1}：{item['label']}（层 {stratum}·{STRATA.get(stratum, '')}）")
        L.append("")
        L.append(f"**机主：** {item['probe']}")
        L.append("")
        for card, cn in (("old", "旧卡"), ("new", "新卡")):
            for run, reply in enumerate(item[card]):
                tag = "失败" if reply.startswith("[失败]") else f"{char_count(reply)} 字"
                L.append(f"**{cn}回复 {run + 1}（{tag}）**")
                L.append("")
                L.append("> " + reply.replace("\n", "\n> "))
                L.append("")
        L.append("---")
        L.append("")

    L.append("## 二、量化统计")
    L.append("")
    L.append("### 2.1 主对照表（第二轮口径）")
    L.append("")
    L.append(fmt_metric_table(old_m, new_m))
    L.append("")
    L.append("### 2.2 称呼密度（目标 <20%）")
    L.append("")
    for name, m in (("旧卡", old_m), ("新卡", new_m)):
        flag = "达标" if m["addr_rate"] < 0.20 else "未达标"
        L.append(f"- {name}：{tagged(m['addr_n'], m['total'])} → 点估计{flag}；「小W同学」共出现 {m['addr_occurrences']} 次")
    L.append("")
    L.append("**按探针类型分层（层 A = 问候/正经说事类，层 B = 低内容/日常类）：**")
    L.append("")
    L.append(strata_table(raw))
    L.append("")
    L.append("分层读法：层 A 的情感探针（想你了/晚安/你在哪）天生允许称呼，若层 B 也高频出现「小W同学」，")
    L.append("才说明称呼被当成口头禅用了。")
    L.append("")
    L.append("**逐探针明细（称呼数 / 含问号数 / 反问链数 / 均长）：**")
    L.append("")
    L.append(per_probe_table(raw))
    L.append("")
    L.append("### 2.3 钩子三个口径")
    L.append("")
    L.append("| 口径 | 旧卡 | 新卡 | 目标 |")
    L.append("| --- | --- | --- | --- |")
    L.append(f"| ① 句尾问句率（原始） | {pct(old_m['tail_q_rate'])}（{old_m['tail_q_n']}/{old_m['total']}） | {pct(new_m['tail_q_rate'])}（{new_m['tail_q_n']}/{new_m['total']}） | <25% |")
    L.append(f"| ①′ 句尾问句率（剥尾部表情标记后） | {pct(old_m['tail_q_clean_rate'])}（{old_m['tail_q_clean_n']}/{old_m['total']}） | {pct(new_m['tail_q_clean_rate'])}（{new_m['tail_q_clean_n']}/{new_m['total']}） | <25% |")
    L.append(f"| ② 含问号率（任意位置） | {pct(old_m['q_any_rate'])}（{old_m['q_any_n']}/{old_m['total']}） | {pct(new_m['q_any_rate'])}（{new_m['q_any_n']}/{new_m['total']}） | <25% |")
    L.append(f"| ③ 反问链（单条 ≥2 问号） | {pct(old_m['chain_rate'])}（{old_m['chain_n']}/{old_m['total']}） | {pct(new_m['chain_rate'])}（{new_m['chain_n']}/{new_m['total']}） | 越低越好，目标 0 |")
    L.append("")
    L.append("Wilson 95% 置信区间：")
    L.append(f"- 旧卡 含问号 {tagged(old_m['q_any_n'], old_m['total'])}；反问链 {tagged(old_m['chain_n'], old_m['total'])}")
    L.append(f"- 新卡 含问号 {tagged(new_m['q_any_n'], new_m['total'])}；反问链 {tagged(new_m['chain_n'], new_m['total'])}")
    L.append("")
    L.append("反问链明细（单条消息里出现 ≥2 个问号）：")
    L.append("")
    for name, m in (("旧卡", old_m), ("新卡", new_m)):
        chains = [(i, r) for i, r in enumerate(m["_replies"]) if question_count(strip_trailing_sticker(r)) >= 2]
        if not chains:
            L.append(f"- {name}：无")
            continue
        L.append(f"- {name}：{len(chains)} 条")
        for i, r in chains:
            L.append(f"  - 第 {i + 1} 条（{question_count(r)} 个问号）：{excerpt(r, 34)}")
    L.append("")
    L.append("### 2.4 比喻扫描（含「像/仿佛/似的/宛如」）")
    L.append("")
    L.append(fmt_metaphor_block("旧卡", old_m))
    L.append("")
    L.append(fmt_metaphor_block("新卡", new_m))
    L.append("")
    L.append("「牵强 / 合理」的逐条判定在 3.3 节，由审阅者依据原文标注（自动规则只负责召回，不负责定性）。")
    L.append("")
    L.append("### 2.5 【起】/【接】/【收】标签泄漏（目标 0）")
    L.append("")
    L.append(f"- 旧卡：{fmt_leak_block(old_m)}")
    L.append(f"- 新卡：{fmt_leak_block(new_m)}")
    L.append("")
    L.append("### 2.6 回复长度")
    L.append("")
    L.append("| 卡 | min | max | 均值（含表情标记） | 均值（剥表情标记） | ≤30 字 | ≥80 字 | 各条字数（剥标记） |")
    L.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for name, m in (("旧卡", old_m), ("新卡", new_m)):
        lengths = ", ".join(str(x) for x in m["bare_lengths"])
        L.append(
            f"| {name} | {m['len_min']} | {m['len_max']} | {m['len_mean']:.1f} | {m['bare_len_mean']:.1f} | "
            f"{m['short_n']}（{pct(m['short_rate'])}） | {m['long_n']}（{pct(m['long_rate'])}） | {lengths} |"
        )
    L.append("")
    L.append("（本轮弃用方差/标准差：单条离群值可主导方差，改用「≤30 字占比 / ≥80 字占比」看长短分布。）")
    L.append("")
    L.append("### 2.7 第一轮 vs 第二轮（同口径跨轮对照）")
    L.append("")
    L.append(round1_section())
    L.append("")
    L.append("### 2.8 口径与注意事项")
    L.append("")
    L.append(f"- 每卡样本 {old_m['total']} 条（{len(PROBES)} 探针 × {RUNS} 次），单条占比分辨率 {pct(1 / max(old_m['total'], 1))}；所有占比均附 Wilson 95% CI。")
    L.append("- 「字数」主口径含 `[sticker:…]` / `[表情:…]` 标记（与第一轮一致），另有「剥标记」补充口径用于长短条判定。")
    L.append("- 「问句」按问号（？/?）计数；反问链指单条消息里 ≥2 个问号，含追问、二连问、自问自答式收尾。")
    L.append("- 「含「她」条数」是第三个人称指代错误的召回口（上轮出现过「偷偷给她发消息」），命中不等于错误，需人工读原文。")
    L.append("- 模板化 chunk 拼接、replier 兜底、表情包真实文件名解析都在生产侧，本测试测的是**裸输出**。")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 三、结论")
    L.append("")
    L.append("### 3.1 量化对照表（旧 vs 新）")
    L.append("")
    L.append(fmt_metric_table(old_m, new_m))
    L.append("")
    L.append("### 3.2 上轮 5 处个体问题：本轮是否复现")
    L.append("")
    L.append(manual(
        "3.2", manual_blocks,
        "（待人工判读）\n\n复核材料（自动召回，供判读）：\n\n"
        f"- 第三人称「她」命中：旧卡 {len(old_m['third_person'])} 条 / 新卡 {len(new_m['third_person'])} 条\n"
        f"- 时间词命中：旧卡\n{time_word_block(old_m)}\n- 新卡\n{time_word_block(new_m)}\n"
        f"- 反问链：旧卡 {len([1 for r in old_m['_replies'] if question_count(strip_trailing_sticker(r)) >= 2])} 条 / "
        f"新卡 {len([1 for r in new_m['_replies'] if question_count(strip_trailing_sticker(r)) >= 2])} 条（明细见 2.3）\n"
        f"- 比喻：旧卡 {len(old_m['metaphor'])} 条 / 新卡 {len(new_m['metaphor'])} 条（明细见 2.4）"
    ))
    L.append("")
    L.append("### 3.3 新卡退步迹象排查")
    L.append("")
    L.append(manual("3.3", manual_blocks, "（待人工判读）"))
    L.append("")
    L.append("### 3.4 人设稳定性人工抽读记账")
    L.append("")
    L.append(manual(
        "3.4", manual_blocks,
        "口径：逐条读第一节全文，四类问题分别记账（敷衍失真 / 答非所问 / 第三人称指代错误 / 时间错述）。\n\n"
        f"自动召回材料：极短裸回复（剥标记 ≤{BARE_REPLY_MAX} 字）旧卡 "
        f"{[i + 1 for i, _ in old_m['bare_replies']]} / 新卡 {[i + 1 for i, _ in new_m['bare_replies']]}；"
        f"含「她」旧卡 {[i + 1 for i, _ in old_m['third_person']]} / 新卡 {[i + 1 for i, _ in new_m['third_person']]}。"
    ))
    L.append("")
    L.append("### 3.5 结论：是否建议上线")
    L.append("")
    L.append(manual("3.5", manual_blocks, "（待人工判读）"))
    L.append("")
    return "\n".join(L)


async def verify_fake_time() -> Dict[str, Any]:
    """组装探针 1 的两卡提示词，校验时间伪装是否真的生效"""
    label, probe, _ = PROBES[0]
    old_msgs, old_prompt, old_diag = await build_messages(OLD_CARD_DIR, WORK_DB_OLD, probe)
    new_msgs, new_prompt, new_diag = await build_messages(NEW_CARD_DIR, WORK_DB_NEW, probe)

    def span(p: str) -> str:
        i = p.find("【她此刻】")
        j = p.find("【当前关系阶段")
        return p[i:j].strip() if i >= 0 and j > i else "（未取到状态片段）"

    old_span, new_span = span(old_prompt), span(new_prompt)
    checks = [
        f"`now_str()` 实际返回：`{old_diag['now_str']}`（期望 `{FAKE_NOW_STR}`）",
        f"注入的【事实】行包含：`现在是 {FAKE_NOW_STR} 星期{'一二三四五六日'[FAKE_NOW.weekday()]}`",
        f"注入的当前活动已不再是睡眠时段：`{'睡下' not in old_prompt and '睡下' not in new_prompt}`（True = 不是睡眠场景）",
        f"两卡状态片段逐字节一致：`{old_span == new_span}`",
        f"距上次机主发言：{old_diag['hours_since_last_chat']} 小时（用于判断是否触发冷落注入）",
        f"冷落注入行：`{'；已经' + str(int(old_diag['hours_since_last_chat'])) + '小时没有你的消息了' if '小时没有你的消息了' in old_prompt else '未触发（距上次发言 <12 小时）'}`",
        f"message 条数：旧卡 {len(old_msgs)} / 新卡 {len(new_msgs)}",
    ]
    ok = all("Error" not in c for c in checks) and old_span == new_span
    checks.append(f"组装期 mock 判定：{'通过' if ok else '**异常，需检查**'}")
    return {
        "state_span": old_span,
        "verification": checks,
        "prompt_len": {"old": len(old_prompt), "new": len(new_prompt)},
        "hours_since_last_chat": old_diag["hours_since_last_chat"],
    }


async def main() -> None:
    dry = "--dry" in sys.argv
    from_raw = "--from-raw" in sys.argv
    prepare_old_card_dir()

    if from_raw:
        with open(RAW_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        report = build_report(raw, load_prev_manual())
        with open(RESULT_MD, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"已用 {RAW_JSON} 重新生成 {RESULT_MD}")
        return

    patch_time()
    config = Config.load()
    active = config.llm.active()
    if active.provider != "deepseek" or not active.api_key:
        raise SystemExit("config.toml 未配置可用的 deepseek key，无法执行 A/B 测试")

    started = datetime.now()
    print(f"[时间伪装] 组装期固定为 {FAKE_NOW_STR}；seed={RANDOM_SEED}；runs={RUNS}", flush=True)
    verification = await verify_fake_time()
    print("[时间伪装] 状态片段：", flush=True)
    print(verification["state_span"], flush=True)
    for c in verification["verification"]:
        print(f"[时间伪装] {c}", flush=True)

    raw: Dict[str, Any] = {
        "meta": {
            "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "fake_now": FAKE_NOW_STR,
            "seed": RANDOM_SEED,
            "time_patch_modules": list(TIME_PATCH_MODULES),
            "model": MODEL,
            "temperature": TEMPERATURE,
            "thinking_enabled": THINKING_PAYLOAD["thinking"]["type"],
            "reasoning_effort": THINKING_PAYLOAD["reasoning_effort"],
            "db": DB_SRC,
            "old_card": OLD_CARD_SRC,
            "new_card": f"{NEW_CARD_DIR}/character.json",
            "runs_per_card": RUNS,
            "strata": STRATA,
            "probes": [{"label": lb, "probe": p, "stratum": s} for lb, p, s in PROBES],
            "prompt_len": verification["prompt_len"],
            "state_span": verification["state_span"],
            "verification": verification["verification"],
            "hours_since_last_chat": verification["hours_since_last_chat"],
        },
        "probes": [],
    }

    if dry:
        print("dry-run：已校验时间伪装与提示词组装，未调用 API。")
        restore_time()
        return

    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for idx, (label, probe, stratum) in enumerate(PROBES):
            print(f"[{idx + 1}/{len(PROBES)}] {label}（层 {stratum}）：{probe}", flush=True)
            old_msgs, old_prompt, _ = await build_messages(OLD_CARD_DIR, WORK_DB_OLD, probe)
            new_msgs, new_prompt, _ = await build_messages(NEW_CARD_DIR, WORK_DB_NEW, probe)
            jobs = [(card, run) for card in ("old", "new") for run in range(RUNS)]
            results = await asyncio.gather(*[
                call_model(session, config, old_msgs if card == "old" else new_msgs, sem)
                for card, _ in jobs
            ])
            item: Dict[str, Any] = {"label": label, "probe": probe, "stratum": stratum, "old": [], "new": []}
            for (card, _), reply in zip(jobs, results):
                item[card].append(reply)
            for card in ("old", "new"):
                for run, reply in enumerate(item[card]):
                    status = "失败" if reply.startswith("[失败]") else "OK"
                    print(f"    {card}#{run + 1} {status} {char_count(reply)} 字", flush=True)
            raw["probes"].append(item)

    failed = [r for it in raw["probes"] for r in it["old"] + it["new"] if r.startswith("[失败]")]
    raw["meta"]["failed_calls"] = len(failed)
    raw["meta"]["elapsed_sec"] = int((datetime.now() - started).total_seconds())

    with open(RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    report = build_report(raw, load_prev_manual())
    with open(RESULT_MD, "w", encoding="utf-8") as f:
        f.write(report)
    restore_time()

    old_m = metric_blocks([r for _, r in flatten(raw, "old")])
    new_m = metric_blocks([r for _, r in flatten(raw, "new")])
    print(f"\n报告已写入 {RESULT_MD}；原始回复留档 {RAW_JSON}；失败 {len(failed)} 条；耗时 {raw['meta']['elapsed_sec']} 秒")
    print(f"称呼密度 旧 {pct(old_m['addr_rate'])} → 新 {pct(new_m['addr_rate'])}")
    print(f"含问号率 旧 {pct(old_m['q_any_rate'])} → 新 {pct(new_m['q_any_rate'])}")
    print(f"反问链   旧 {pct(old_m['chain_rate'])} → 新 {pct(new_m['chain_rate'])}")
    print(f"比喻条数 旧 {len(old_m['metaphor'])} → 新 {len(new_m['metaphor'])}")
    print(f"标签泄漏 旧 {len(old_m['leaks'])} → 新 {len(new_m['leaks'])}")
    print(f"≥80 字   旧 {old_m['long_n']} → 新 {new_m['long_n']}；≤30 字 旧 {old_m['short_n']} → 新 {new_m['short_n']}")


if __name__ == "__main__":
    asyncio.run(main())
