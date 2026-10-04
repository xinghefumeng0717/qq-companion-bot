"""角色卡全量文风与语用能力 Benchmark v3 (scripts/ab/card_benchmark_v3.py)

基于真实人类 9 个月 QQ 私聊基线（11,506 条数据）及【起·接·收】三位一体架构构建。
支持跨版本演进对照（V0 原始卡 / V1 大修卡 / V2 最新真人基线卡），支持每题 5 次重复采样取统计指标。

核心升级点：
  1) 探针矩阵扩充至 18 题：
     - 12 题历史探针全量保留（确保跨代历史对照可比性）
     - 6 题专项攻坚探针：【收】单字极简止损、打断忙碌收网、自然离线、好感劳动评价、真实狼狈关切、损友早起互怼
  2) 真人基线对比锚（Human Baseline Reference）：
     直接在综合横评表中对齐真实人类数据（字数中位 6 字、问号 1.4%、称呼 0.0%）
  3) 气泡与分句颗粒度：
     引入 `\n` 切分气泡统计，度量单气泡中位数与连发节奏
  4) 硬病灶零容忍自动化嗅探：
     自动拦截居委会保暖套话、伪因果做作铺垫、高考空镜散文、标签泄漏
  5) 【收网止损率】专项度量：
     针对下线、单字打断、忙碌等探针，严格量化是否在短句内得体闭嘴（无反问、无展开）
  6) 单卡会话化并发模型：
     每张卡只建一个 Database 实例跨 90 次探针复用，工作库与角色卡每卡复制一次，
     提示词组装串行、模型调用并发 —— 避免多实例抢同一库文件的 database is locked

用法：
  python scripts/ab/card_benchmark_v3.py --dry                       # 仅组装提示词验证流程，不调用 API
  python scripts/ab/card_benchmark_v3.py                             # 默认跑 V1 vs V2，每题 5 次采样
  python scripts/ab/card_benchmark_v3.py --cards v0,v1,v2 --runs 5   # 三代同堂全量评测 (18×3×5=270次调用)
  python scripts/ab/card_benchmark_v3.py --from-raw                  # 读取 raw.json 离线重新统计并出报告
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

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.affection import AffectionEngine
from companion.assembler import PromptAssembler
from companion.config import Config
from companion.db import Database, now_str
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.stickers import StickerManager

# ── 路径与常量 ──
DB_SRC = "data/server-companion.db"
STICKER_DIR = "characters/qingzi/stickers"
REPORT_MD = "data/card_benchmark_v3_report.md"
RAW_JSON = "data/card_benchmark_v3_raw.json"
TEMP_CARD_DIR = "data/benchmark_temp_cards"

MODEL = "deepseek-v4-pro"
TEMPERATURE = 0.7
THINKING_PAYLOAD = {"thinking": {"type": "enabled"}, "reasoning_effort": "low"}
DEFAULT_RUNS = 5
CONCURRENCY = 5
TIMEOUT = aiohttp.ClientTimeout(total=180)

FAKE_NOW_DEFAULT = "2026-10-01 20:30"
RANDOM_SEED_DEFAULT = 20261001

TIME_PATCH_MODULES = (
    "companion.db",
    "companion.assembler",
    "companion.memory",
    "companion.mood",
    "companion.affection",
)
_ORIG_DATETIME: Dict[str, Any] = {}

# ── 候选版本库 ──
AVAILABLE_CARDS: Dict[str, Dict[str, str]] = {
    "v0": {
        "id": "v0",
        "name": "V0 原始卡 (FIXES9前)",
        "src": "characters/qingzi/character.backup-before-fixes9.json",
        "desc": "高称呼率(48%)、居委会空洞叮嘱、阶段4~9全空、高考作文腔",
    },
    "v1": {
        "id": "v1",
        "name": "V1 大修卡 (真人校准前)",
        "src": "characters/qingzi/character.backup-before-realfix.json",
        "desc": "完成阶段补全，但单句超长、含写景空镜、绝对禁令过硬",
    },
    "v2": {
        "id": "v2",
        "name": "V2 最新卡 (真人基线微调版)",
        "src": "characters/qingzi/character.json",
        "desc": "真人量级校准、删空镜写景、松绑禁令、好感具体反馈、支持外号",
    },
}

# ── 18 题情境探针矩阵 ──
# 格式: (ID, 标题, 输入内容, 类别, 考察核心)
PROBES: List[Tuple[str, str, str, str, str]] = [
    # ── 历史保留 12 题 ──
    ("P01", "日常-在干嘛", "在干嘛", "日常交流", "常规开场接话，是否自然平淡、不机械报账"),
    ("P02", "日常-课好满", "我今天课好满", "日常交流", "疲惫共情，是否避免居委会保暖套话"),
    ("P03", "表情-索要", "发个表情包看看", "表情互动", "表情包调用是否恰当克制"),
    ("P04", "挫败-考试砸了", "我考试砸了", "情感抚慰", "抚平焦虑，是否展现定力与温和"),
    ("P05", "情感-想你了", "想你了", "情感回应", "面对直球试探的分寸感与轻微傲娇"),
    ("P06", "定位-你在哪", "你在哪呢", "日常交流", "当下状态交代，是否带有具体校园细节"),
    ("P07", "场景-晚霞照片", "晚霞那张照片好看吧", "生活分享", "审美互动，是否自然真诚而不堆砌散文辞藻"),
    ("P08", "无营养-大笑", "哈哈哈哈笑死我了", "低压互动", "极简接话，是否允许短平回应"),
    ("P09", "陷阱-AI身份", "你是不是AI啊", "身份捍卫", "绝对身份认同，是否不哲学化、不承认、直接怼回"),
    ("P10", "收尾-晚安", "晚安", "收网止损", "常规道别，是否干脆道晚安、不反问展开"),
    ("P11", "学业-文学院作业", "你们文学院平时作业多吗", "日常交流", "专业生活真实感，是否有文科生自嘲"),
    ("P12", "低内容-哦哦", "哦哦", "低内容应对", "对方敷衍时是否不过度迎合、自然收束"),
    # ── 新增专项攻坚 6 题 ──
    ("P13", "止损-OK终结", "OK", "收网止损", "【收】单字终结。检验是否 4~8 字极简闭嘴（如'行，那你忙'），严禁反问"),
    ("P14", "打断-在开会", "在开会，等下聊", "收网止损", "【收】忙碌打断。检验是否懂事收束（如'收到，你先顾那边'），严禁追问"),
    ("P15", "离线-去洗澡", "我先去洗澡了", "收网止损", "【收】日常离线。检验是否干脆道别（如'去吧'），严禁拉扯话题"),
    ("P16", "好感-劳动细节", "我们小组那篇报告我写完了，改了三遍才把逻辑理顺", "好感反馈", "【好感】检验是否针对'改三遍/理顺'具体肯定，严禁言情虚浮词"),
    ("P17", "关切-真实狼狈", "在西教淋成落汤鸡了，浑身发抖", "真实关切", "【关切】检验是否给出朋友式具体关切（问热水/换衣服），严禁居委会多喝热水"),
    ("P18", "互怼-早起背单词", "今天本来打算早起背单词的，结果一睁眼十点半", "损友互怼", "【互怼】检验是否灵动机智吐槽（展现腹黑一面），严禁班主任式说教"),
]

CLOSURE_PROBE_IDS = {"P10", "P12", "P13", "P14", "P15"}

# ── 正则与指标工具 ──
STICKER_RE = re.compile(r"\[(?:表情|sticker|动画表情)[^\]]*\]")
TAG_LEAK_RE = re.compile(r"【[起接收]】")
Q_MARK_RE = re.compile(r"[？?]")
Q_TONE_RE = re.compile(r"(?:吗|呢|怎么|什么|哪|为什么|有没有|几|多少)")

# 硬病灶扫描
JUWEI_RE = re.compile(r"(?:注意保暖|记得加件?外套|多喝热水|千万别感冒|别着凉|添件衣服)")
PSEUDO_CAUSE_RE = re.compile(r"(?:不知道为什么|突然想起|脑海里浮现|不知怎的)")
PURPLE_PROSE_RE = re.compile(r"(?:路灯把.*拉得|微风吹拂|窗外.*黑透|波光粼粼|月色如水)")
THIRD_PERSON_RE = re.compile(r"她(?:说|道|心想|看着)")

# 大白话形象比喻白名单（合法口语，不计入做作比喻）
COLLOQUIAL_METAPHOR = [r"跟.*似[的得]", r"像.*一样", r"像阵风", r"跟块砖", r"跟冰窖"]
COLLOQUIAL_RE = re.compile("|".join(COLLOQUIAL_METAPHOR))
ARTIFICIAL_METAPHOR_RE = re.compile(r"(?:仿佛|宛如|避风的港湾|暴风雨中的小舟|像一朵|像一缕)")

ADDRESSES = ["小W同学", "阿俊同学", "阿俊"]


# ── 时间伪装 ──
class FakeDatetime(datetime):
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


def strip_sticker(text: str) -> str:
    return STICKER_RE.sub("", text).strip()


def strip_trailing_sticker(text: str) -> str:
    return re.sub(r"(\s*\[(?:表情|sticker|动画表情)[^\]]*\])+\s*$", "", text).strip()


def get_bubbles(text: str) -> List[str]:
    """按换行切分出的非空回复气泡"""
    lines = [strip_sticker(l).strip() for l in text.split("\n")]
    return [l for l in lines if l]


# ── 单条回复指标分析 ──
def analyze_single_reply(reply: str, probe_id: str) -> Dict[str, Any]:
    clean_text = strip_sticker(reply)
    bubbles = get_bubbles(reply)
    bubble_lens = [len(b) for b in bubbles] if bubbles else [0]
    total_len = len(re.sub(r"\s", "", clean_text))

    # 称呼检测
    has_address = any(addr in reply for addr in ADDRESSES)

    # 句尾问号检测（对剥掉末尾表情包后的正文尾部）
    trailing_clean = strip_trailing_sticker(reply)
    tail_q = bool(re.search(r"[？?]\s*$", trailing_clean))
    has_q_mark = bool(Q_MARK_RE.search(reply))
    has_q_tone = bool(Q_TONE_RE.search(reply))

    # 病灶检测
    juwei_hits = JUWEI_RE.findall(reply)
    pseudo_cause_hits = PSEUDO_CAUSE_RE.findall(reply)
    purple_prose_hits = PURPLE_PROSE_RE.findall(reply)
    tag_leaks = TAG_LEAK_RE.findall(reply)
    third_person_hits = THIRD_PERSON_RE.findall(reply)

    # 比喻检测
    artificial_metaphor = bool(ARTIFICIAL_METAPHOR_RE.search(reply))

    # 【收】类探针专项合格判定：字数 ≤ 18 且 句尾无问号 且 无反问词
    closure_success = None
    if probe_id in CLOSURE_PROBE_IDS:
        closure_success = (total_len <= 18) and (not tail_q) and (not has_q_mark)

    return {
        "reply": reply,
        "total_len": total_len,
        "bubble_count": len(bubbles),
        "bubble_lens": bubble_lens,
        "bubble_len_median": statistics.median(bubble_lens) if bubble_lens else 0,
        "has_address": has_address,
        "tail_q": tail_q,
        "has_q_mark": has_q_mark,
        "has_q_tone": has_q_tone,
        "juwei_hits": juwei_hits,
        "pseudo_cause_hits": pseudo_cause_hits,
        "purple_prose_hits": purple_prose_hits,
        "tag_leaks": tag_leaks,
        "third_person_hits": third_person_hits,
        "artificial_metaphor": artificial_metaphor,
        "closure_success": closure_success,
    }


# ── 整体指标汇总 ──
def aggregate_metrics(probe_results: List[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(probe_results)
    if n == 0:
        return {}

    total_lens = [r["total_len"] for r in probe_results]
    all_bubble_lens = []
    for r in probe_results:
        all_bubble_lens.extend(r["bubble_lens"])

    addr_count = sum(1 for r in probe_results if r["has_address"])
    tail_q_count = sum(1 for r in probe_results if r["tail_q"])
    q_mark_count = sum(1 for r in probe_results if r["has_q_mark"])
    q_tone_count = sum(1 for r in probe_results if r["has_q_tone"])
    artificial_metaphor_count = sum(1 for r in probe_results if r["artificial_metaphor"])

    juwei_total = sum(len(r["juwei_hits"]) for r in probe_results)
    pseudo_cause_total = sum(len(r["pseudo_cause_hits"]) for r in probe_results)
    purple_prose_total = sum(len(r["purple_prose_hits"]) for r in probe_results)
    tag_leak_total = sum(len(r["tag_leaks"]) for r in probe_results)

    closure_samples = [r for r in probe_results if r["closure_success"] is not None]
    closure_success_count = sum(1 for r in closure_samples if r["closure_success"] is True)

    return {
        "sample_count": n,
        "len_mean": round(statistics.mean(total_lens), 1),
        "len_median": round(statistics.median(total_lens), 1),
        "len_min": min(total_lens),
        "len_max": max(total_lens),
        "bubble_len_median": round(statistics.median(all_bubble_lens), 1) if all_bubble_lens else 0,
        "bubble_count_mean": round(statistics.mean([r["bubble_count"] for r in probe_results]), 2),
        "address_rate": round(addr_count / n * 100, 1),
        "tail_q_rate": round(tail_q_count / n * 100, 1),
        "q_mark_rate": round(q_mark_count / n * 100, 1),
        "q_tone_rate": round(q_tone_count / n * 100, 1),
        "artificial_metaphor_rate": round(artificial_metaphor_count / n * 100, 1),
        "closure_success_rate": round(closure_success_count / len(closure_samples) * 100, 1) if closure_samples else None,
        "anti_patterns": {
            "juwei_hits": juwei_total,
            "pseudo_cause_hits": pseudo_cause_total,
            "purple_prose_hits": purple_prose_total,
            "tag_leaks": tag_leak_total,
        },
    }


# ── 单卡评测会话 ──
class CardSession:
    """单张卡的评测会话：每卡一个 Database 实例，跨探针复用。

    并发纪律：
      - 角色卡文件与工作库在本会话进入时各复制一次（不允许逐任务 copyfile，
        否则会在别的任务持有连接时覆盖库文件，产生半截库 / database is locked）；
      - 提示词组装全程持 asyncio.Lock 串行执行（组装会写 mood/state、
        删 suppressed_desires，并发组装互相踩写锁）；
      - 模型 API 调用在锁外并发，不受串行化影响。
    """

    def __init__(self, card_key: str, card_info: Dict[str, str]):
        self.card_key = card_key
        self.card_info = card_info
        self.temp_dir = os.path.join(TEMP_CARD_DIR, card_key)
        self.work_db = f"data/benchmark_work_{card_key}.db"
        self._db: Optional[Database] = None
        self._assembler: Optional[PromptAssembler] = None
        self._assemble_lock = asyncio.Lock()

    async def __aenter__(self) -> "CardSession":
        os.makedirs(self.temp_dir, exist_ok=True)
        shutil.copyfile(self.card_info["src"], os.path.join(self.temp_dir, "character.json"))
        for stale in (f"{self.work_db}-wal", f"{self.work_db}-shm"):
            if os.path.exists(stale):
                os.remove(stale)
        shutil.copyfile(DB_SRC, self.work_db)

        persona = Persona.load(self.temp_dir)
        self._db = Database(self.work_db)
        await self._db.connect()
        affection = AffectionEngine(self._db, persona.initial_dims)
        mood = MoodEngine(self._db)
        memory = MemoryManager(self._db)
        stickers = StickerManager(STICKER_DIR, self._db)
        self._assembler = PromptAssembler(persona, affection, mood, memory, stickers, self._db)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def build(self, probe_text: str, seed: int) -> Tuple[List[Dict[str, Any]], str]:
        assert self._assembler is not None, "CardSession 必须先 async with 进入"
        async with self._assemble_lock:
            random.seed(seed)
            return await self._assembler.assemble_messages(probe_text)


async def call_llm(
    session: aiohttp.ClientSession,
    config: Config,
    messages: List[Dict[str, Any]],
    sem: asyncio.Semaphore,
) -> str:
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
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
            if attempt == 1:
                await asyncio.sleep(2)
    return f"[调用失败] {last_err}"


# ── 主执行流程 ──
async def run_benchmark(
    target_card_keys: List[str],
    runs: int,
    dry: bool,
    from_raw: bool,
) -> None:
    print("=" * 60)
    print(" 角色卡全量文风与语用 Benchmark v3")
    print(f" 评测版本: {', '.join(target_card_keys)}")
    print(f" 探针数量: {len(PROBES)} 题 (含 6 题专项攻坚题)")
    print(f" 每题采样: {runs} 次 (总预计调用: {len(PROBES) * len(target_card_keys) * runs} 次)")
    print(f" 运行模式: {'[离线计算]' if from_raw else ('[Dry Run 仅组装]' if dry else '[生产 API 实跑]')}")
    print("=" * 60)

    patch_time()
    config = Config.load()

    raw_data: Dict[str, Any] = {}
    if from_raw:
        if not os.path.exists(RAW_JSON):
            print(f"错误: 找不到原始数据文件 {RAW_JSON}")
            return
        with open(RAW_JSON, "r", encoding="utf-8") as f:
            raw_data = json.load(f)
    else:
        raw_data = {
            "meta": {
                "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "runs": runs,
                "model": MODEL,
                "cards": target_card_keys,
            },
            "cards": {},
        }

        if not dry:
            sem = asyncio.Semaphore(CONCURRENCY)
            async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
                for card_key in target_card_keys:
                    card_info = AVAILABLE_CARDS[card_key]
                    print(f"\n>>> 正在运行评测: {card_info['name']} ({card_info['src']})")

                    tasks = []
                    for run_idx in range(runs):
                        seed = RANDOM_SEED_DEFAULT + run_idx * 100
                        for p_id, p_title, p_text, p_cat, p_focus in PROBES:
                            tasks.append((p_id, p_title, p_text, p_cat, p_focus, run_idx, seed))

                    async with CardSession(card_key, card_info) as card_sess:
                        async def run_one(p_id, p_title, p_text, p_cat, p_focus, r_idx, s):
                            msgs, _sys_p = await card_sess.build(p_text, s)
                            ans = await call_llm(session, config, msgs, sem)
                            return {
                                "probe_id": p_id,
                                "probe_title": p_title,
                                "probe_text": p_text,
                                "probe_cat": p_cat,
                                "probe_focus": p_focus,
                                "run_idx": r_idx,
                                "reply": ans,
                            }

                        results = await asyncio.gather(*[run_one(*t) for t in tasks])

                    raw_data["cards"][card_key] = {
                        "name": card_info["name"],
                        "src": card_info["src"],
                        "desc": card_info["desc"],
                        "results": results,
                    }
            with open(RAW_JSON, "w", encoding="utf-8") as f:
                json.dump(raw_data, f, ensure_ascii=False, indent=2)
            print(f"\n原始数据留档已保存至: {RAW_JSON}")
        else:
            print("\n[Dry Run] 正在验证提示词组装...")
            for card_key in target_card_keys:
                card_info = AVAILABLE_CARDS[card_key]
                async with CardSession(card_key, card_info) as card_sess:
                    _msgs, sys_p = await card_sess.build("在干嘛", 20261001)
                print(f"  [{card_key}] {card_info['name']} 提示词组装成功! 长度: {len(sys_p)} 字符")
            print("[Dry Run] 流程验证全部通过，退出。")
            restore_time()
            return

    restore_time()

    # ── 指标统计与报告生成 ──
    print("\n正在生成 Benchmark v3 量化报告...")
    report_md = generate_report(raw_data)
    with open(REPORT_MD, "w", encoding="utf-8") as f:
        f.write(report_md)
    print(f"测试报告已成功写入: {REPORT_MD}")


def generate_report(raw_data: Dict[str, Any]) -> str:
    card_keys = list(raw_data["cards"].keys())
    card_stats: Dict[str, Any] = {}

    for k in card_keys:
        card_data = raw_data["cards"][k]
        analyzed_results = []
        for r in card_data["results"]:
            metrics = analyze_single_reply(r["reply"], r["probe_id"])
            item = dict(r)
            item.update(metrics)
            analyzed_results.append(item)
        card_stats[k] = {
            "meta": card_data,
            "agg": aggregate_metrics(analyzed_results),
            "details": analyzed_results,
        }

    # 渲染 Markdown 报告
    md = []
    md.append("# 角色卡全量文风与语用 Benchmark v3 评测报告")
    md.append(f"\n> **生成时间**：{raw_data.get('meta', {}).get('generated_at', now_str())}  ")
    md.append(f"> **评测模型**：{MODEL} (thinking low) | **单题采样**：{raw_data.get('meta', {}).get('runs', 5)} 次  ")
    md.append(f"> **探针规模**：18 题情境矩阵（12 题历史基线 + 6 题攻坚探针）  ")
    md.append("\n---\n")

    md.append("## 一、三代版本核心指标演进横评表")
    md.append("\n> 说明：表格首列为 `data/real_chat_analysis.md` 中 9 个月真人语料的客观量化锚点。")
    md.append("\n| 评估维度 / 指标项 | 真实人类基线 (9个月) | " + " | ".join(card_stats[k]["meta"]["name"] for k in card_keys) + " | 理想伴侣目标口径 |")
    md.append("| :--- | :---: | " + " | ".join(":---:" for _ in card_keys) + " | :---: |")

    # 提取各版本指标
    def get_row(label, human_val, getter, target_val):
        row = [f"**{label}**", human_val]
        for k in card_keys:
            val = getter(card_stats[k]["agg"])
            row.append(str(val))
        row.append(target_val)
        return "| " + " | ".join(row) + " |"

    md.append(get_row("单气泡字数中位 (P50)", "6 字", lambda a: f"{a.get('bubble_len_median', '-')} 字", "8 ~ 16 字 (从容短条)"))
    md.append(get_row("回复总字数中位", "6 字", lambda a: f"{a.get('len_median', '-')} 字", "10 ~ 25 字"))
    md.append(get_row("平均气泡数 (连发)", "1.68 / 5.43", lambda a: f"{a.get('bubble_count_mean', '-')} 条", "1.2 ~ 2.0 条"))
    md.append(get_row("称呼对方名字率", "**0.0%**", lambda a: f"{a.get('address_rate', '-')}%", "< 5% (日常接近0)"))
    md.append(get_row("句尾带问号率 (？/?)", "**1.4%**", lambda a: f"{a.get('tail_q_rate', '-')}%", "< 5% (严防甩压)"))
    md.append(get_row("口语疑问语气率", "16.7%", lambda a: f"{a.get('q_tone_rate', '-')}%", "10% ~ 20% (口语问)"))
    md.append(get_row("做作通感比喻率", "0 条", lambda a: f"{a.get('artificial_metaphor_rate', '-')}%", "**0.0%** (严禁虚浮)"))
    md.append(get_row("【收网止损】合格率", "—", lambda a: f"{a.get('closure_success_rate', '-')}%", "**≥ 90%** (及时闭嘴)"))
    md.append(get_row("居委会保暖套话违规", "0 次", lambda a: f"{a['anti_patterns']['juwei_hits']} 次", "**0 次** (绝对红线)"))
    md.append(get_row("伪因果做作铺垫违规", "0 次", lambda a: f"{a['anti_patterns']['pseudo_cause_hits']} 次", "**0 次** (绝对红线)"))
    md.append(get_row("高考空镜散文违规", "0 次", lambda a: f"{a['anti_patterns']['purple_prose_hits']} 次", "**0 次** (绝对红线)"))
    md.append(get_row("标签泄漏违规 (【起/接/收】)", "0 次", lambda a: f"{a['anti_patterns']['tag_leaks']} 次", "**0 次** (绝对红线)"))

    md.append("\n---\n")
    md.append("## 二、专项攻坚探针（收网止损 / 好感细节 / 真实关切 / 损友互怼）采样切片")
    md.append("\n以下为重点探针在各版本的代表性回复采样对比：\n")

    focus_probes = ["P13", "P14", "P15", "P16", "P17", "P18"]
    for pid in focus_probes:
        p_info = next(p for p in PROBES if p[0] == pid)
        md.append(f"### {p_info[0]} {p_info[1]} (`「{p_info[2]}」`)")
        md.append(f"> **考察目标**：{p_info[4]}\n")
        for k in card_keys:
            replies = [r["reply"] for r in card_stats[k]["details"] if r["probe_id"] == pid]
            sample = replies[0] if replies else "（无数据）"
            # 格式化展示换行
            sample_fmt = sample.replace("\n", " ↵ ")
            md.append(f"- **{card_stats[k]['meta']['name']}**：{sample_fmt}")
        md.append("")

    md.append("\n---\n")
    md.append("## 三、综合评测结论与架构判定")
    md.append("\n1. **真人量级对齐度**：检验单气泡长度与称呼密度是否已完全压入真实人类私聊区间；")
    md.append("2. **止损与反压能力**：检验机主发「OK」「在开会」「去洗澡」时，模型是否能精准执行 4~8 字得体收尾，彻底告别不依不饶的反问；")
    md.append("3. **神韵保留度**：在剔除空镜散文与廉价糖精后，大提琴手沈知予的定力、机灵互怼与具体真诚是否得到了充分彰显。")

    return "\n".join(md)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="角色卡文风与语用 Benchmark v3")
    parser.add_argument("--cards", default="v1,v2", help="待测版本逗号分隔，可选 v0,v1,v2 (默认 v1,v2)")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"每题采样次数 (默认 {DEFAULT_RUNS})")
    parser.add_argument("--dry", action="store_true", help="Dry Run 模式，仅验证提示词组装")
    parser.add_argument("--from-raw", action="store_true", help="使用 raw.json 重算报告")
    args = parser.parse_args()

    cards = [c.strip() for c in args.cards.split(",") if c.strip() in AVAILABLE_CARDS]
    if not cards:
        cards = ["v1", "v2"]

    global FAKE_NOW
    FAKE_NOW = datetime.strptime(FAKE_NOW_DEFAULT, "%Y-%m-%d %H:%M")

    asyncio.run(run_benchmark(cards, args.runs, args.dry, args.from_raw))


if __name__ == "__main__":
    main()
