"""小艾评测 V5：真人情境对照 (scripts/sim/benchmark_v5.py)

V4 测的是"别犯错"（拖尾/叮嘱/越界）；V5 测的是"像不像人"。

题目来自所有者与真人（大一同学，女生）的真实聊天记录：每题 = 当时的上文 + 他那一轮原话
+ 她当时的真实回复。让小艾在同样的上文里回同一句，然后：
  1. 自动指标：小艾的回复与**同一批题里她的真实回复**并排统计（气泡数、单气泡字数、
     问号率、句号收尾率、比喻词、写景词、沉默率）。真人统计只是参考范围，不是目标——
     人有多样性，不需要一模一样；只用来发现明显跑偏（问号多几倍、句句写景）。
  2. 盲测卷：每题把"真人回复 + 各卡回复"打乱，所有者猜哪个是真人、挑最喜欢的。

题库：data/benchmark_v5/probes.json（已去名字与她的身份细节；原始聊天记录不入库）。
每个 (卡×题×采样) 一个全新临时库（系统临时目录，跑完即删），上文按原时刻写进 turns，
时钟伪装到原消息的时刻（星期按原日期对齐到 2026-10-12 这一周，避开节假日）。
只测主聊这一步（不跑观察者/日记），零生产库读写。

用法：
  ./venv/Scripts/python.exe scripts/sim/benchmark_v5.py --cards qingzi,qingzi_lean --runs 3
  可选：--model deepseek-flash --effort low --concurrency 6 --out data/benchmark_v5/run-xxx
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
import tempfile
from datetime import datetime, timedelta
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.affection import (  # noqa: E402
    STAGE_THRESHOLDS,
    AffectionEngine,
    calc_composite_score,
    determine_stage,
)
from companion.assembler import PromptAssembler  # noqa: E402
from companion.config import Config, ReplyConfig  # noqa: E402
from companion.db import STATE_KEY_AFFECTION, TIME_FORMAT, Database  # noqa: E402
from companion.gateway import LLMGateway  # noqa: E402
from companion.memory import MemoryManager  # noqa: E402
from companion.mood import MoodEngine  # noqa: E402
from companion.persona import Persona  # noqa: E402
from companion.replier import Replier, chunk_record_text, is_silence_output  # noqa: E402
from companion.reply_check import check_reply, gather_candidates, pick_candidate, trim_echo_lead  # noqa: E402
from companion.stickers import StickerManager  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
PROBES_FILE = "data/benchmark_v5/probes.json"
WEEK_MONDAY = datetime(2026, 10, 12)   # 非节假日的一周；题目的星期对齐到这周

# ── 时间伪装（与 benchmark_v4 同一手法）──
_FAKE_NOW = WEEK_MONDAY
TIME_PATCH_MODULES = (
    "companion.db", "companion.assembler", "companion.memory",
    "companion.mood", "companion.affection", "companion.config",
)
_ORIG: Dict[str, Any] = {}


class FakeDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return _FAKE_NOW if tz is None else _FAKE_NOW.replace(tzinfo=tz)

    @classmethod
    def today(cls):
        return _FAKE_NOW


def patch_time() -> None:
    for name in TIME_PATCH_MODULES:
        mod = importlib.import_module(name)
        _ORIG.setdefault(name, getattr(mod, "datetime"))
        setattr(mod, "datetime", FakeDatetime)


def restore_time() -> None:
    for name, orig in _ORIG.items():
        setattr(importlib.import_module(name), "datetime", orig)


def probe_clock(probe: Dict[str, Any]) -> datetime:
    orig = datetime.strptime(f"{probe['date']} {probe['time']}", "%Y-%m-%d %H:%M")
    day = WEEK_MONDAY + timedelta(days=orig.weekday())
    return day.replace(hour=orig.hour, minute=orig.minute)


# ==========================================
# 指标（纯函数：真人回复与小艾回复走同一套）
# ==========================================
_CHAR_RE = re.compile(r"[一-龥]|[A-Za-z]+|\d+")
METAPHOR_RE = re.compile(
    r"像[^，。！？\n]{1,12}(一样|似的|那样)|跟[^，。！？\n]{1,12}(似的|一样)|似的|(?<!好)像在|仿佛|宛如|如同"
)
_HANZI_RUN = re.compile(r"[\u4e00-\u9fa5]{4,}")
# 点评他的话：拿他的用词/说法当对象评一句（"这词从你嘴里说出来""这说法挺正经""这转折还挺值"）
COMMENT_RE = re.compile(r"这(个)?(词|说法|话说得|形容|比喻|转折|走向|用词)|被你这么(用|一说)|从你嘴里说出来|说得(还|倒|真)?挺?像(那么)?回事")


def echoes(reply: str, his: str, n: int = 4) -> bool:
    """复述：回复里原样出现了他这句话中连续 n 个以上的汉字（他话里的表情标记不算）"""
    src = re.sub(r"\[[^\]]*\]", "", his or "")
    grams = {run[i:i + n] for run in _HANZI_RUN.findall(src) for i in range(len(run) - n + 1)}
    return any(g in (reply or "") for g in grams)
# 动态示例那位真人的招牌口头禅：看注入示例后小艾有没有被带成她的口吻
HER_TIC_RE = re.compile(r"哈哈哈|噢噢|我去|笑死|okok|可以可以|好叭|好耶|哇塞|蛙趣|谢谢哇|(?<![的])滴(?=$|[\s，。！？])")


# 温度相关（粗口径，只看趋势）：语气词 / 表情 / 关心的话 / 主动说自己的事
PARTICLE_RE = re.compile(r"[呀啦嘛哈呢嘿哎欸诶噢哦~～]")
EMOJI_RE = re.compile(r"\[(?:face|sticker|表情)[:：]")
CARE_RE = re.compile(r"早点|别太|别熬|记得|辛苦|累不累|还好吗|没事吧|注意|多喝|吃饭没|吃了吗|休息|歇会|躺会")
SELF_SHARE_RE = re.compile(r"我(今天|刚|也|这边|下午|上午|晚上|昨天|前两天|最近)")


def copies(reply: str, examples_her: str, n: int = 5) -> bool:
    """抄示例：回复里出现了注入示例中她的回复里连续 n 个以上的汉字"""
    grams = {run[i:i + n] for run in _HANZI_RUN.findall(examples_her or "") for i in range(len(run) - n + 1)}
    return any(g in (reply or "") for g in grams)


SCENERY_WORDS = ("夕阳", "晚霞", "晨光", "湖面", "月光", "微风", "风一吹", "阳光", "落日", "星星", "桂花")


def bubbles_of(text: str) -> List[str]:
    """一段回复拆成气泡（按换行；表情包/QQ表情标记行也算一个气泡）"""
    return [b.strip() for b in (text or "").split("\n") if b.strip()]


def text_chars(b: str) -> int:
    return len(_CHAR_RE.findall(re.sub(r"\[[^\]]*\]", "", b)))


def reply_metrics(text: str, his: str = "", examples_her: str = "") -> Dict[str, Any]:
    if not text or is_silence_output(text):
        return {"silence": True}
    bs = bubbles_of(text)
    word_bs = [b for b in bs if text_chars(b) > 0]
    return {
        "silence": False,
        "bubbles": len(bs),
        "chars": [text_chars(b) for b in word_bs],
        "q_end": sum(1 for b in word_bs if b.rstrip().endswith(("？", "?"))),
        "period_end": sum(1 for b in word_bs if b.rstrip().endswith("。")),
        "metaphor": len(METAPHOR_RE.findall(text)),
        "scenery": sum(text.count(w) for w in SCENERY_WORDS),
        "word_bubbles": len(word_bs),
        "echo": echoes(text, his),
        "comment": bool(COMMENT_RE.search(text)),
        "copy": copies(text, examples_her),
        "particles": len(PARTICLE_RE.findall(re.sub(r"\[[^\]]*\]", "", text))),
        "emoji": bool(EMOJI_RE.search(text)),
        "care": bool(CARE_RE.search(text)),
        "self_share": bool(SELF_SHARE_RE.search(text)),
        "tic": len(HER_TIC_RE.findall(text)),
    }


def aggregate(ms: List[Dict[str, Any]]) -> Dict[str, Any]:
    spoken = [m for m in ms if not m["silence"]]
    chars = [c for m in spoken for c in m["chars"]]
    wb = sum(m["word_bubbles"] for m in spoken) or 1
    n = len(spoken) or 1
    return {
        "回复数": len(ms),
        "沉默率%": round(100 * (len(ms) - len(spoken)) / (len(ms) or 1), 1),
        "每轮气泡数(均)": round(sum(m["bubbles"] for m in spoken) / n, 2),
        "单气泡字数(中位)": statistics.median(chars) if chars else 0,
        "单气泡≤5字%": round(100 * sum(1 for c in chars if c <= 5) / (len(chars) or 1), 1),
        "句尾问号%": round(100 * sum(m["q_end"] for m in spoken) / wb, 1),
        "句号收尾%": round(100 * sum(m["period_end"] for m in spoken) / wb, 1),
        "比喻(每百轮)": round(100 * sum(m["metaphor"] for m in spoken) / n, 1),
        "写景词(每百轮)": round(100 * sum(m["scenery"] for m in spoken) / n, 1),
        "复述他原话%": round(100 * sum(1 for m in spoken if m.get("echo")) / n, 1),
        "点评他的话%": round(100 * sum(1 for m in spoken if m.get("comment")) / n, 1),
        "抄示例%": round(100 * sum(1 for m in spoken if m.get("copy")) / n, 1),
        "语气词(每轮)": round(sum(m.get("particles", 0) for m in spoken) / n, 2),
        "带表情%": round(100 * sum(1 for m in spoken if m.get("emoji")) / n, 1),
        "关心的话%": round(100 * sum(1 for m in spoken if m.get("care")) / n, 1),
        "说自己的事%": round(100 * sum(1 for m in spoken if m.get("self_share")) / n, 1),
        "她的口头禅(每百轮)": round(100 * sum(m.get("tic", 0) for m in spoken) / n, 1),
    }


# ==========================================
# 单格：全新临时库 → 写上文 → 组装 → 主聊 → 切段
# ==========================================
_BANK = None  # --style-examples 时载入一次，各格共用
_SEED_STAGE = None  # --seed-stage：每格新库开局就落在这个关系阶段
_NO_CONTEXT = False  # --no-context：不写上文（排除真人上文对语气的带偏）


def seed_dims_for_stage(stage: int) -> Dict[str, float]:
    """六维取同一个值（阈值带中点），复合分即等于它（与 duo_sim.stage_seed_dims 同口径）。"""
    lower = STAGE_THRESHOLDS[stage]
    upper = STAGE_THRESHOLDS[stage + 1] if stage < 9 else 100.0
    v = round((lower + upper) / 2.0, 1)
    dims = {"warmth": v, "trust": v, "intimacy": v, "intrigue": v, "patience": v, "tension": 0.0}
    assert determine_stage(calc_composite_score(dims)) == stage
    return dims


def injected_examples_her(system_prompt: str) -> str:
    """从组装好的 system prompt 里取出这次注入的示例中"她："后面的文字"""
    from companion.style_examples import BLOCK_HEADER
    if BLOCK_HEADER not in system_prompt:
        return ""
    seg = system_prompt.split(BLOCK_HEADER, 1)[1].split("\n【当前关系阶段", 1)[0]
    return "\n".join(line.split("→ 她：", 1)[1] for line in seg.split("\n") if "→ 她：" in line)


async def run_cell(cfg: Config, card: str, probe: Dict[str, Any], tmp_root: str) -> Dict[str, Any]:
    db_path = os.path.join(tmp_root, f"{card}_{probe['id']}_{random.random():.8f}.db")
    db = Database(db_path)
    gateway = LLMGateway(cfg.llm, db)
    try:
        await db.init_tables()
        persona = Persona.load(os.path.join("characters", card))
        stickers = StickerManager(os.path.join(persona.base_dir, persona.stickers_dir), db)
        await stickers.sync_initial_stickers()  # 与生产一致：卡里开局表情包播种进库，否则提示词清单为空
        affection = AffectionEngine(db, persona.initial_dims)
        if _SEED_STAGE is not None:
            dims = seed_dims_for_stage(_SEED_STAGE)
            await db.set_state_json(STATE_KEY_AFFECTION, {
                "dims": dims, "composite": round(calc_composite_score(dims), 2),
                "stage": _SEED_STAGE, "last_updated": _FAKE_NOW.strftime(TIME_FORMAT),
            })
        mood = MoodEngine(db)
        memory = MemoryManager(db, None, affection, persona)
        assembler = PromptAssembler(persona, affection, mood, memory, stickers, db,
                                    holidays_provider=lambda: [])
        assembler.style_examples = _BANK
        replier = Replier(ReplyConfig(), stickers)

        # 上文按"每轮间隔 1 分钟、最后一轮在 1 分钟前"写进 turns
        now = _FAKE_NOW
        ctx = [] if _NO_CONTEXT else probe["context"]
        for k, t in enumerate(ctx):
            ts = (now - timedelta(minutes=len(ctx) - k)).strftime(TIME_FORMAT)
            role = "user" if t["who"] == "他" else "assistant"
            await db.execute(
                "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, 0, 0, ?)",
                (role, t["text"], ts),
            )

        lines = [x for x in probe["his"].split("\n") if x.strip()]
        batch = [{"index": i + 1, "text": x, "message_id": 1000 + i, "has_image": False}
                 for i, x in enumerate(lines)]
        messages, _ = await assembler.assemble_messages(probe["his"], numbered_batch=batch)
        examples_her = injected_examples_her(messages[0]["content"])
        async def _one() -> str:
            parts = []
            async for piece in gateway.stream_chat(messages=messages, model=cfg.llm.active().chat,
                                                   purpose="main_chat"):
                parts.append(piece)
            return "".join(parts)

        t0 = asyncio.get_running_loop().time()
        cand_info = None
        if cfg.reply.candidates > 1:
            texts = await gather_candidates(_one, cfg.reply.candidates)
            raw, cand_info = pick_candidate(
                probe["his"], texts,
                tiebreak=getattr(cfg.reply, "candidate_tiebreak", "warm"),
            )
            cand_info["n_got"] = len(texts)
            cand_info["texts"] = texts
        else:
            raw = (await _one()).strip()
        t1 = asyncio.get_running_loop().time()
        check_info = None
        draft = raw
        trimmed = ""
        if cfg.reply.trim_echo:
            raw, trimmed = trim_echo_lead(probe["his"], raw)
        if cfg.reply.self_check:
            raw, check_info = await check_reply(gateway, probe["his"], raw, cfg.reply.self_check_timeout)
        t2 = asyncio.get_running_loop().time()
        if replier.is_silence_decision(raw, batch):
            reply = "[沉默]"
        else:
            chunks, _ = replier.parse_reply(raw, quote_targets=batch)
            reply = "\n".join(chunk_record_text(c) for c in chunks)
        row = await db.fetchone("SELECT SUM(cost_estimate) c FROM llm_calls")
        return {"card": card, "probe": probe["id"], "raw": raw, "reply": reply, "draft": draft, "check": check_info, "trimmed": trimmed, "cand": cand_info,
                "examples_her": examples_her,
                "gen_s": round(t1 - t0, 2), "check_s": round(t2 - t1, 2),
                "cost": float(row["c"] or 0.0) if row else 0.0}
    except Exception as e:
        return {"card": card, "probe": probe["id"], "error": f"{type(e).__name__}: {e}"}
    finally:
        await gateway.close()
        await db.close()
        for suf in ("", "-wal", "-shm"):
            if os.path.exists(db_path + suf):
                os.remove(db_path + suf)


# ==========================================
# 报告与盲测卷
# ==========================================
def build_report(meta, probes, cells) -> str:
    L = ["# 小艾评测 V5 · 真人情境对照", ""]
    L.append(f"- 模型：`{meta['model']}` 思考 {meta['effort']}；每题每卡 {meta['runs']} 次；"
             f"题数 {len(probes)}；估算费用 ¥{meta['cost']:.4f}；失败 {meta['errors']} 格")
    L.append(f"- 开局阶段：{meta.get('seed_stage') if meta.get('seed_stage') is not None else '角色卡初始'}")
    L.append(f"- 动态示例：{'开' if meta.get('style_examples') else '关'}；首泡裁剪：{'开' if meta.get('trim_echo') else '关'}；"
             f"候选：{meta.get('candidates', 1)} 条")
    if ok_cells_all := [c for c in cells if "reply" in c]:
        tot = sorted(round(c.get("gen_s", 0) + c.get("check_s", 0), 2) for c in ok_cells_all)
        p90 = tot[min(len(tot) - 1, int(len(tot) * 0.9))]
        L.append(f"- 总耗时（生成+质检）：中位 {tot[len(tot)//2]}s / 90% {p90}s / 最长 {tot[-1]}s；"
                 f"裁掉首泡 {sum(1 for c in ok_cells_all if c.get('trimmed'))} 格")
    L.append("- 真人列 = 同一批题里她当时的真实回复（参考范围，不是目标）")
    ok_cells = [c for c in cells if "reply" in c]
    if ok_cells:
        gen = sorted(c.get("gen_s", 0) for c in ok_cells)
        chk = sorted(c.get("check_s", 0) for c in ok_cells)
        changed = sum(1 for c in ok_cells if (c.get("check") or {}).get("changed"))
        L.append(f"- 质检：{'开' if meta.get('self_check') else '关'}（思考{'开' if meta.get('check_thinking') else '关'}）；改写 {changed}/{len(ok_cells)} 格；"
                 f"主聊耗时中位 {gen[len(gen)//2]}s；质检耗时中位 {chk[len(chk)//2]}s / 最长 {chk[-1]}s")
    L.append("")
    cols = ["真人"] + meta["cards"]
    his_of = {p["id"]: p["his"] for p in probes}
    aggs = {"真人": aggregate([reply_metrics(p["her"], p["his"]) for p in probes])}
    for card in meta["cards"]:
        aggs[card] = aggregate([reply_metrics(c["reply"], his_of.get(c["probe"], ""), c.get("examples_her", "")) for c in cells
                                if c["card"] == card and "reply" in c])
    keys = list(aggs["真人"].keys())
    L.append("| 指标 | " + " | ".join(cols) + " |")
    L.append("| --- | " + " | ".join("---" for _ in cols) + " |")
    for k in keys:
        L.append(f"| {k} | " + " | ".join(str(aggs[c].get(k, "")) for c in cols) + " |")
    L.append("")
    L.append("## 按题型（单气泡字数中位 / 句尾问号%）")
    types = sorted({p["type"] for p in probes})
    for t in types:
        ids = {p["id"] for p in probes if p["type"] == t}
        row = [f"{aggregate([reply_metrics(p['her']) for p in probes if p['id'] in ids])['单气泡字数(中位)']}"]
        for card in meta["cards"]:
            a = aggregate([reply_metrics(c["reply"]) for c in cells
                           if c["card"] == card and c["probe"] in ids and "reply" in c])
            row.append(f"{a['单气泡字数(中位)']} / {a['句尾问号%']}")
        L.append(f"- {t}（{len(ids)} 题）：真人 {row[0]} 字｜" + "｜".join(
            f"{card} {r}" for card, r in zip(meta["cards"], row[1:])))
    L.append("")
    L.append("## 全部原文")
    for p in probes:
        L.append(f"\n### 题 {p['id']}（{p['type']}）")
        for t in p["context"][-4:]:
            L.append(f"> {t['who']}：{t['text'].replace(chr(10), ' / ')}")
        L.append(f"> **他：{p['his'].replace(chr(10), ' / ')}**")
        L.append(f"- 真人：{p['her'].replace(chr(10), ' / ')}")
        for c in cells:
            if c["probe"] == p["id"]:
                L.append(f"- {c['card']}：{(c.get('reply') or c.get('error', '')).replace(chr(10), ' / ')}")
    return "\n".join(L)


def build_blind(probes, cells, cards, seed=1019):
    rnd = random.Random(seed)
    sheet, key = [], {}
    for n, p in enumerate(probes, 1):
        opts = [("真人", p["her"])]
        for card in cards:
            first = next((c for c in cells if c["card"] == card and c["probe"] == p["id"]
                          and "reply" in c), None)
            if first:
                opts.append((card, first["reply"]))
        rnd.shuffle(opts)
        labels = "ABCDEFG"
        key[n] = {labels[i]: who for i, (who, _) in enumerate(opts)} | {"probe": p["id"]}
        sheet.append(f"\n第{n}题（{p['type']}）")
        for t in p["context"][-3:]:
            sheet.append(f"    {t['who']}：{t['text'].replace(chr(10), ' / ')}")
        sheet.append(f"    他：{p['his'].replace(chr(10), ' / ')}")
        for i, (_, txt) in enumerate(opts):
            sheet.append(f"  【{labels[i]}】{(txt or '').replace(chr(10), ' / ')}")
    return "\n".join(sheet), key


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cards", default="qingzi,qingzi_lean")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--model", default=None)
    ap.add_argument("--effort", default=None)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--probes", default=PROBES_FILE)
    ap.add_argument("--out", default=None)
    ap.add_argument("--self-check", action="store_true", help="开生成后质检（与生产 [reply].self_check 同一实现）")
    ap.add_argument("--seed-stage", type=int, default=None, help="每格开局的关系阶段（0~9，默认按角色卡初始）")
    ap.add_argument("--no-context", action="store_true", help="不写题目自带的上文，只留他这一句")
    ap.add_argument("--trim-echo", action="store_true", help="开首泡回声裁剪（与生产 [reply].trim_echo 同一实现）")
    ap.add_argument("--candidates", type=int, default=1, help="多候选择优条数（与生产 [reply].candidates 同一实现）")
    ap.add_argument("--style-examples", action="store_true", help="开动态示例（与生产 [reply].style_examples 同一实现）")
    ap.add_argument("--check-thinking", action="store_true", help="质检开思考（把 reply_check 加进思考白名单，强度同 thinking_effort_tasks）")
    args = ap.parse_args()
    os.chdir(ROOT)

    cfg = Config.load()
    if args.model:
        cfg.llm.presets[cfg.llm.current].chat = args.model
    if args.effort:
        cfg.llm.thinking_effort_chat = args.effort
    if args.self_check:
        cfg.reply.self_check = True
    cfg.reply.trim_echo = args.trim_echo
    cfg.reply.candidates = max(1, args.candidates)
    global _BANK, _SEED_STAGE, _NO_CONTEXT
    _SEED_STAGE = args.seed_stage
    _NO_CONTEXT = args.no_context
    if args.style_examples:
        from companion.style_examples import StyleExampleBank
        cfg.reply.style_examples = True
        _BANK = StyleExampleBank.load(cfg.reply.style_examples_path)
    if args.check_thinking:
        cfg.llm.thinking_tasks_purposes = list(cfg.llm.thinking_tasks_purposes) + ["reply_check"]
    cards = [c.strip() for c in args.cards.split(",") if c.strip()]
    probes = json.load(open(args.probes, encoding="utf-8"))
    out = args.out or f"data/benchmark_v5/run-{datetime.now():%Y%m%d-%H%M}"
    os.makedirs(out, exist_ok=True)
    tmp_root = tempfile.mkdtemp(prefix="bench_v5_")

    global _FAKE_NOW
    cells: List[Dict[str, Any]] = []
    sem = asyncio.Semaphore(args.concurrency)

    async def one(card, p):
        async with sem:
            r = await run_cell(cfg, card, p, tmp_root)
            cells.append(r)

    patch_time()
    try:
        for p in probes:  # 同一题共用一个伪装时钟，题与题之间串行切换
            _FAKE_NOW = probe_clock(p)
            await asyncio.gather(*[one(card, p) for card in cards for _ in range(args.runs)])
            print(f"  题 {p['id']} 完成（{len(cells)} 格）", flush=True)
    finally:
        restore_time()
        shutil.rmtree(tmp_root, ignore_errors=True)

    cells.sort(key=lambda c: (c["probe"], c["card"]))
    meta = {"model": cfg.llm.active().chat, "effort": cfg.llm.thinking_effort_chat,
            "self_check": cfg.reply.self_check,
            "style_examples": cfg.reply.style_examples,
            "trim_echo": cfg.reply.trim_echo, "candidates": cfg.reply.candidates,
            "seed_stage": args.seed_stage, "no_context": args.no_context,
            "check_thinking": cfg.llm.thinking_for_purpose("reply_check"),
            "runs": args.runs, "cards": cards,
            "cost": sum(c.get("cost", 0.0) for c in cells),
            "errors": sum(1 for c in cells if "error" in c)}
    json.dump({"meta": meta, "cells": cells}, open(f"{out}/raw.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    with open(f"{out}/report.md", "w", encoding="utf-8") as f:
        f.write(build_report(meta, probes, cells))
    sheet, key = build_blind(probes, cells, cards)
    with open(f"{out}/blind_sheet.txt", "w", encoding="utf-8") as f:
        f.write(sheet)
    json.dump(key, open(f"{out}/blind_key.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n完成：{len(cells)} 格，失败 {meta['errors']}，费用 ¥{meta['cost']:.4f}\n报告：{out}/report.md")


if __name__ == "__main__":
    asyncio.run(main())
