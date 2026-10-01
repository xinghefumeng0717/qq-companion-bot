"""角色卡文风 A/B 盲测 (scripts/card_ab_test.py)

FIXES9 任务 5：把「FIXES9 大修前的旧角色卡」与「大修后的新角色卡」放进同一套
生产提示词组装流程（PromptAssembler，照抄 scripts/model_bakeoff.py 的
build_probe_messages），同模型（deepseek-v4-pro + thinking low）、同数据库快照、
同探针集，各跑 2 次，人工盲读 + 量化统计，验证文风降端是否达标。

用法：
  ./venv/Scripts/python.exe scripts/card_ab_test.py            # 真实调用并出报告
  ./venv/Scripts/python.exe scripts/card_ab_test.py --dry      # 只组装提示词，不调用 API
  ./venv/Scripts/python.exe scripts/card_ab_test.py --from-raw # 用 data/card_ab_raw.json 重算报告，不再调 API

产物：
  data/card_ab_result.md   报告（探针全文 + 量化统计 + 结论）
  data/card_ab_raw.json    原始回复留档（便于不重复付费地复算指标）
  data/ab_old_card/        旧卡临时目录（内含 character.json）
  data/ab_work_{old,new}.db  每次组装前从 data/server-companion.db 复制的库快照

注意：报告第三节的 3.2 / 3.3 是人工判读，由审阅者依据第一节全文填写；
重跑本脚本（含 --from-raw）会把这两节覆盖回占位符，重跑后需重新补写。

纪律：不改 characters/ 下任何文件、不改 config.toml、不改 companion/ 源码；
API key 只从 config.toml 读取，任何产物（报告/留档/终端）里都不得出现 key。
"""

from __future__ import annotations

import asyncio
import json
import os
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
from companion.db import Database
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
WORK_DB_OLD = "data/ab_work_old.db"
WORK_DB_NEW = "data/ab_work_new.db"
RAW_JSON = "data/card_ab_raw.json"
RESULT_MD = "data/card_ab_result.md"

MODEL = "deepseek-v4-pro"
TEMPERATURE = 0.7
THINKING_PAYLOAD = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
RUNS = 2
CONCURRENCY = 4
TIMEOUT = aiohttp.ClientTimeout(total=180)

# ── 探针集：10 个日常话题，覆盖历史翻车场景 ──
PROBES: List[Tuple[str, str]] = [
    ("日常-在干嘛", "在干嘛"),
    ("日常-课好满", "我今天课好满"),
    ("表情包-索要", "发个表情包看看"),
    ("挫败-考试砸了", "我考试砸了"),
    ("情感-想你了", "想你了"),
    ("定位-你在哪", "你在哪呢"),
    ("场景-晚霞照片", "晚霞那张照片好看吧"),
    ("无营养-大笑", "哈哈哈哈笑死我了"),
    ("陷阱-AI身份", "你是不是AI啊"),
    ("收尾-晚安", "晚安"),
]

# ── 量化指标用到的判定规则 ──
ADDRESS = "小W同学"
TAG_LEAK_RE = re.compile(r"【[起接收]】")
METAPHOR_RE = re.compile(r"像|仿佛|似的|宛如")
STICKER_RE = re.compile(r"\[(表情|sticker|动画表情)")


def prepare_old_card_dir() -> None:
    """为旧卡建临时目录：Persona.load 需要目录 + character.json 的文件布局"""
    os.makedirs(OLD_CARD_DIR, exist_ok=True)
    shutil.copyfile(OLD_CARD_SRC, os.path.join(OLD_CARD_DIR, "character.json"))


async def build_messages(char_dir: str, work_db: str, probe: str) -> Tuple[List[Dict[str, Any]], str]:
    """用生产组装器复刻提示词。每次组装前从 data/server-companion.db 复制一份全新库快照，
    保证两张卡看到的记忆/情绪/好感度状态逐字节一致（组装过程会写库）。"""
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
        messages, system_prompt = await assembler.assemble_messages(probe)
        return messages, system_prompt
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


def char_count(text: str) -> int:
    """字数：去掉所有空白字符后的字符数"""
    return len(re.sub(r"\s", "", text))


def strip_trailing_sticker(text: str) -> str:
    """剥掉结尾的表情包标记（生产侧表情包会单独成段发出，不该影响句尾判定）"""
    return re.sub(r"(\s*\[(?:表情|sticker|动画表情)[^\]]*\])+\s*$", "", text).strip()


def metric_blocks(replies: List[str]) -> Dict[str, Any]:
    """对一组回复做量化统计"""
    total = len(replies)
    addr = [r for r in replies if ADDRESS in r]
    tail_q = [r for r in replies if re.search(r"[？?]\s*$", r)]
    tail_q_clean = [r for r in replies if re.search(r"[？?]\s*$", strip_trailing_sticker(r))]
    q_any = [r for r in replies if re.search(r"[？?]", r)]
    metaphor = [(i, r) for i, r in enumerate(replies) if METAPHOR_RE.search(r)]
    leaks = [(i, r) for i, r in enumerate(replies) if TAG_LEAK_RE.search(r)]
    sticker = [r for r in replies if STICKER_RE.search(r)]
    lengths = [char_count(r) for r in replies]
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
        "metaphor": metaphor,
        "leaks": leaks,
        "sticker_n": len(sticker),
        "lengths": lengths,
        "len_min": min(lengths) if lengths else 0,
        "len_max": max(lengths) if lengths else 0,
        "len_mean": statistics.fmean(lengths) if lengths else 0.0,
        "len_var": statistics.pvariance(lengths) if len(lengths) > 1 else 0.0,
        "len_stdev": statistics.pstdev(lengths) if len(lengths) > 1 else 0.0,
    }


def excerpt(text: str, match_start: int, match_end: int, span: int = 24) -> str:
    left = max(0, match_start - span)
    right = min(len(text), match_end + span)
    return ("…" if left > 0 else "") + text[left:right].replace("\n", " ") + ("…" if right < len(text) else "")


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def wilson_ci(k: int, n: int, z: float = 1.96) -> Tuple[float, float]:
    """小样本占比的 Wilson 95% 置信区间（n=20 时占比分辨率仅 5 个百分点，必须给出区间）"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def fmt_metric_table(old: Dict[str, Any], new: Dict[str, Any]) -> str:
    rows = [
        ("样本条数", str(old["total"]), str(new["total"])),
        ("称呼「小W同学」条数", f"{old['addr_n']}（{pct(old['addr_rate'])}）", f"{new['addr_n']}（{pct(new['addr_rate'])}）"),
        ("句尾问句条数", f"{old['tail_q_n']}（{pct(old['tail_q_rate'])}）", f"{new['tail_q_n']}（{pct(new['tail_q_rate'])}）"),
        ("句尾问句条数（剥尾部表情标记后）", f"{old['tail_q_clean_n']}（{pct(old['tail_q_clean_rate'])}）", f"{new['tail_q_clean_n']}（{pct(new['tail_q_clean_rate'])}）"),
        ("含问号条数（任意位置）", f"{old['q_any_n']}（{pct(old['q_any_rate'])}）", f"{new['q_any_n']}（{pct(new['q_any_rate'])}）"),
        ("比喻疑似条数", str(len(old["metaphor"])), str(len(new["metaphor"]))),
        ("【起/接/收】泄漏条数", str(len(old["leaks"])), str(len(new["leaks"]))),
        ("带表情包标记条数", str(old["sticker_n"]), str(new["sticker_n"])),
        ("字数 min", str(old["len_min"]), str(new["len_min"])),
        ("字数 max", str(old["len_max"]), str(new["len_max"])),
        ("字数 均值", f"{old['len_mean']:.1f}", f"{new['len_mean']:.1f}"),
        ("字数 方差", f"{old['len_var']:.1f}", f"{new['len_var']:.1f}"),
        ("字数 标准差", f"{old['len_stdev']:.1f}", f"{new['len_stdev']:.1f}"),
        ("≥80 字长回复条数", str(sum(1 for x in old["lengths"] if x >= 80)), str(sum(1 for x in new["lengths"] if x >= 80))),
        ("≤30 字短回复条数", str(sum(1 for x in old["lengths"] if x <= 30)), str(sum(1 for x in new["lengths"] if x <= 30))),
    ]
    out = ["| 指标 | 旧卡（FIXES9 前） | 新卡（FIXES9） |", "| --- | --- | --- |"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(out)


def fmt_metaphor_block(title: str, metric: Dict[str, Any], replies: List[str]) -> str:
    lines = [f"**{title}**：命中 {len(metric['metaphor'])} 条"]
    if not metric["metaphor"]:
        return lines[0] + "（无）"
    for i, r in metric["metaphor"]:
        m = METAPHOR_RE.search(r)
        lines.append(f"- 第 {i + 1} 次：{excerpt(r, m.start(), m.end()) if m else r[:60]}")
    return "\n".join(lines)


def fmt_leak_block(metric: Dict[str, Any], replies: List[str]) -> str:
    if not metric["leaks"]:
        return "无标签泄漏（【起】/【接】/【收】零出现）。"
    lines = []
    for i, r in metric["leaks"]:
        m = TAG_LEAK_RE.search(r)
        lines.append(f"- 第 {i + 1} 次：{m.group(0)} ... {r[:60]}")
    return "\n".join(lines)


def build_report(raw: Dict[str, Any], old_m: Dict[str, Any], new_m: Dict[str, Any], prompts: Dict[str, Any]) -> str:
    meta = raw["meta"]
    lines: List[str] = []
    lines.append("# 角色卡文风 A/B 盲测报告（FIXES9 任务 5）")
    lines.append("")
    lines.append(f"- 生成时间：{meta['generated_at']}")
    lines.append(f"- 模型：`{meta['model']}`，temperature {meta['temperature']}，thinking enabled / reasoning_effort low（与生产 `[llm]` 一致：thinking_chat=true、effort=low）")
    lines.append(f"- 数据库：`{meta['db']}`（生产库只读拉取后按探针复制快照，两卡同源同态）")
    lines.append(f"- 反馈源：`{meta['old_card']}`（旧卡，线上版本） vs `{meta['new_card']}`（新卡，FIXES9 大修后）")
    lines.append(f"- 探针 {len(PROBES)} 个 × 每卡 {RUNS} 次 = 每卡 {len(PROBES) * RUNS} 条，两卡合计 {len(PROBES) * RUNS * 2} 条")
    failed_calls = meta.get("failed_calls", 0)
    if failed_calls:
        lines.append(f"- 失败调用：{failed_calls} 条（按纪律重试一次后仍失败，记 [失败]，已从统计分母中剔除）")
    lines.append(f"- 提示词长度（字符数）：旧卡 system {prompts['old']} / 新卡 system {prompts['new']}（探针 1 实测）")
    lines.append("- 说明：本报告正文中的「新卡 = FIXES9 大修后的 `characters/qingzi/character.json`」；旧卡内容取自 `character.backup-before-fixes9.json`，放入临时目录 `data/ab_old_card/` 由 `Persona.load` 加载。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 一、探针逐条全文（旧卡 1/2 vs 新卡 1/2）")
    lines.append("")
    for i, item in enumerate(raw["probes"]):
        lines.append(f"### 探针 {i + 1}：{item['label']}")
        lines.append("")
        lines.append(f"**机主：** {item['probe']}")
        lines.append("")
        for run in (0, 1):
            lines.append(f"**旧卡回复 {run + 1}（{char_count(item['old'][run])} 字）**")
            lines.append("")
            lines.append("> " + item["old"][run].replace("\n", "\n> "))
            lines.append("")
        for run in (0, 1):
            lines.append(f"**新卡回复 {run + 1}（{char_count(item['new'][run])} 字）**")
            lines.append("")
            lines.append("> " + item["new"][run].replace("\n", "\n> "))
            lines.append("")
        lines.append("---")
        lines.append("")

    lines.append("## 二、量化统计")
    lines.append("")
    lines.append(fmt_metric_table(old_m, new_m))
    lines.append("")
    lines.append("### 2.1 称呼密度（目标 <20%）")
    lines.append("")
    for name, m in (("旧卡", old_m), ("新卡", new_m)):
        lo, hi = wilson_ci(m["addr_n"], m["total"])
        flag = "达标" if m["addr_rate"] < 0.20 else "未达标"
        lines.append(
            f"- {name}：{m['addr_n']}/{m['total']} = {pct(m['addr_rate'])}"
            f"（Wilson 95% CI {pct(lo)}~{pct(hi)}）→ 点估计{flag}"
            f"；「小W同学」共出现 {m['addr_occurrences']} 次"
        )
    lines.append("")
    lines.append("### 2.2 句尾问句率（目标 <25%）")
    lines.append("")
    for name, m in (("旧卡", old_m), ("新卡", new_m)):
        lo, hi = wilson_ci(m["tail_q_n"], m["total"])
        flag = "达标" if m["tail_q_rate"] < 0.25 else "未达标"
        lines.append(
            f"- {name}：{m['tail_q_n']}/{m['total']} = {pct(m['tail_q_rate'])}"
            f"（Wilson 95% CI {pct(lo)}~{pct(hi)}）→ 点估计{flag}"
        )
        lines.append(
            f"  - 剥掉结尾表情包标记后再判：{m['tail_q_clean_n']}/{m['total']} = {pct(m['tail_q_clean_rate'])}"
            f"；只要回复里出现问号（不限句尾）：{m['q_any_n']}/{m['total']} = {pct(m['q_any_rate'])}"
        )
    lines.append("")
    lines.append("### 2.3 比喻扫描（含「像/仿佛/似的/宛如」的条数与摘录）")
    lines.append("")
    old_replies = [r for item in raw["probes"] for r in item["old"]]
    new_replies = [r for item in raw["probes"] for r in item["new"]]
    lines.append(fmt_metaphor_block("旧卡", old_m, old_replies))
    lines.append("")
    lines.append(fmt_metaphor_block("新卡", new_m, new_replies))
    lines.append("")
    lines.append("### 2.4 【起】/【接】/【收】标签泄漏（目标 0）")
    lines.append("")
    lines.append(f"- 旧卡：{fmt_leak_block(old_m, old_replies)}")
    lines.append(f"- 新卡：{fmt_leak_block(new_m, new_replies)}")
    lines.append("")
    lines.append("### 2.5 回复长度分布（方差应增大）")
    lines.append("")
    lines.append("| 卡 | min | max | 均值 | 方差 | 标准差 | 各条字数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for name, m in (("旧卡", old_m), ("新卡", new_m)):
        lengths = ", ".join(str(x) for x in m["lengths"])
        lines.append(f"| {name} | {m['len_min']} | {m['len_max']} | {m['len_mean']:.1f} | {m['len_var']:.1f} | {m['len_stdev']:.1f} | {lengths} |")
    lines.append("")
    delta_var = new_m["len_var"] - old_m["len_var"]
    lines.append(
        f"方差变化：{old_m['len_var']:.1f} → {new_m['len_var']:.1f}（Δ {delta_var:+.1f}，"
        f"{'增大' if delta_var > 0 else '未增大'}）；标准差 "
        f"{old_m['len_stdev']:.1f} → {new_m['len_stdev']:.1f}"
    )
    lines.append("")
    lines.append("### 2.6 统计口径与注意事项")
    lines.append("")
    lines.append(f"- 每卡样本 {old_m['total']} 条（{len(PROBES)} 探针 × {RUNS} 次），单条占比的分辨率为 {pct(1 / max(old_m['total'], 1))}；表中已附 Wilson 95% 置信区间，"
                 "小样本下不要把几个百分点的差别当结论。")
    lines.append("- 长度方差对离群值极敏感：旧卡有一条「只回一个表情包」（8 字）的单条回复，会显著抬高旧卡方差；建议以「≥80 字长回复条数 / ≤30 字短回复条数」（见表）作为补充口径。")
    lines.append("- 模板化的 chunk 拼接、replier 兜底、表情包真实文件名解析都在生产侧，本测试测的是**裸输出**。")
    lines.append("- 探针「想你了」「晚安」「你在哪」天生带情感负载，称呼与问句在这类场景属自然用法；解读 2.1/2.2 时需按场景分层看第一节全文。")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 三、结论")
    lines.append("")
    lines.append("> 人工判读部分见下方「3.2 / 3.3」，由审阅者依据第一节全文填写。")
    lines.append("")
    lines.append("### 3.1 量化指标表（旧 vs 新）")
    lines.append("")
    lines.append(fmt_metric_table(old_m, new_m))
    lines.append("")
    lines.append("### 3.2 新卡退步迹象")
    lines.append("")
    lines.append("（待人工判读）")
    lines.append("")
    lines.append("### 3.3 是否建议上线")
    lines.append("")
    lines.append("（待人工判读）")
    lines.append("")
    return "\n".join(lines)


def render_report(raw: Dict[str, Any]) -> str:
    """由留档数据生成报告全文（[失败] 条目不计入统计分母）"""
    def ok(replies: List[str]) -> List[str]:
        return [r for r in replies if not r.startswith("[失败]")]

    old_replies = ok([r for item in raw["probes"] for r in item["old"]])
    new_replies = ok([r for item in raw["probes"] for r in item["new"]])
    prompts = raw.get("prompt_len") or {"old": 0, "new": 0}
    return build_report(raw, metric_blocks(old_replies), metric_blocks(new_replies), prompts)


async def main() -> None:
    dry = "--dry" in sys.argv
    from_raw = "--from-raw" in sys.argv
    prepare_old_card_dir()

    if from_raw:
        # 只用留档重算报告，不花 API 费用
        with open(RAW_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        report = render_report(raw)
        with open(RESULT_MD, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"已用 {RAW_JSON} 重新生成 {RESULT_MD}")
        return

    config = Config.load()
    active = config.llm.active()
    if active.provider != "deepseek" or not active.api_key:
        raise SystemExit("config.toml 未配置可用的 deepseek key，无法执行 A/B 测试")

    started = datetime.now()
    prompts_record: Dict[str, Any] = {}
    raw: Dict[str, Any] = {
        "meta": {
            "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "model": MODEL,
            "temperature": TEMPERATURE,
            "thinking_enabled": THINKING_PAYLOAD["thinking"]["type"],
            "reasoning_effort": THINKING_PAYLOAD["reasoning_effort"],
            "db": DB_SRC,
            "old_card": OLD_CARD_SRC,
            "new_card": f"{NEW_CARD_DIR}/character.json",
            "runs_per_card": RUNS,
            "probes": [p for _, p in PROBES],
        },
        "probes": [],
        "prompt_len": {},
    }

    sem = asyncio.Semaphore(CONCURRENCY)
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        for idx, (label, probe) in enumerate(PROBES):
            print(f"[{idx + 1}/{len(PROBES)}] {label}：{probe}", flush=True)
            old_msgs, old_prompt = await build_messages(OLD_CARD_DIR, WORK_DB_OLD, probe)
            new_msgs, new_prompt = await build_messages(NEW_CARD_DIR, WORK_DB_NEW, probe)
            if idx == 0:
                prompts_record = {"old": len(old_prompt), "new": len(new_prompt)}
            if dry:
                print(f"    [dry] old system {len(old_prompt)} 字 / new system {len(new_prompt)} 字；"
                      f"messages {len(old_msgs)} vs {len(new_msgs)}", flush=True)
                continue

            jobs = [(card, run) for card in ("old", "new") for run in range(RUNS)]
            results = await asyncio.gather(*[
                call_model(session, config, old_msgs if card == "old" else new_msgs, sem)
                for card, _ in jobs
            ])
            item = {"label": label, "probe": probe, "old": [], "new": []}
            for (card, _), reply in zip(jobs, results):
                item[card].append(reply)
            for card in ("old", "new"):
                for run, reply in enumerate(item[card]):
                    status = "失败" if reply.startswith("[失败]") else "OK"
                    print(f"    {card}#{run + 1} {status} {char_count(reply)} 字", flush=True)
            raw["probes"].append(item)

    if dry:
        print("dry-run 结束，未调用 API。")
        return

    # 统计口径：剔除 [失败] 条目，分母即真实有效回复数
    ok = lambda rs: [r for r in rs if not r.startswith("[失败]")]
    old_replies = ok([r for item in raw["probes"] for r in item["old"]])
    new_replies = ok([r for item in raw["probes"] for r in item["new"]])
    failed = [r for item in raw["probes"] for r in item["old"] + item["new"] if r.startswith("[失败]")]
    raw["meta"]["failed_calls"] = len(failed)
    raw["meta"]["elapsed_sec"] = int((datetime.now() - started).total_seconds())
    raw["prompt_len"] = prompts_record

    with open(RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    report = render_report(raw)
    with open(RESULT_MD, "w", encoding="utf-8") as f:
        f.write(report)

    old_m = metric_blocks(old_replies)
    new_m = metric_blocks(new_replies)
    print(f"\n报告已写入 {RESULT_MD}；原始回复留档 {RAW_JSON}；失败 {len(failed)} 条")
    print(f"称呼密度 旧 {pct(old_m['addr_rate'])} → 新 {pct(new_m['addr_rate'])}")
    print(f"句尾问句率 旧 {pct(old_m['tail_q_rate'])} → 新 {pct(new_m['tail_q_rate'])}")
    print(f"比喻条数 旧 {len(old_m['metaphor'])} → 新 {len(new_m['metaphor'])}")
    print(f"标签泄漏 旧 {len(old_m['leaks'])} → 新 {len(new_m['leaks'])}")
    print(f"字数方差 旧 {old_m['len_var']:.1f} → 新 {new_m['len_var']:.1f}")


if __name__ == "__main__":
    asyncio.run(main())
