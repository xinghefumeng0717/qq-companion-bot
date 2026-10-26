"""角色卡文风 A/B · 诗意病灶定点复测（2026-10-05，国庆长假锚点场景）

背景：观察期首晚（2026-10-05 17:45~18:03 生产实录）机主吐槽"讲话这么有诗意"——
长假绍兴老家锚点下，"巷子"意象连刷四轮当修辞底料
（"小巷的风我替你吹着""辣椒下锅声比我还响""这表情我给巷子看了，它说会写"）。
处置：角色卡 `chat_style.rules` 追加 1 条"场景意象也是梗"规则 + `bad_examples`
追加 3 条生产原话反面教材（术前备份
`characters/qingzi/character.backup-20261005-poetry-v3术前.json`，本脚本以它为旧卡）。

与前几轮的关键差异：**时间伪装必须落在 2026 国庆 8 天长假内**（用 `--time` 显式指定，
建议 2026-10-06 15:00；不指定时沿用基座默认 2026-10-01 20:30，也在长假内），
且 config.toml 的 `[llm.pricing].holidays` 必须已填这 8 天——否则 `holiday_span()`
判不出长假、"回绍兴老家"锚点不激活，测的就不是发病场景（脚本会在锚点未激活时直接中止）。
跑之前先看 dry-run 输出里当前活动是不是"放长假中，回绍兴老家陪父母"。

用法：
  ./venv/Scripts/python.exe scripts/ab/card_ab_poetry.py --dry          # 只验证锚点与时间伪装
  ./venv/Scripts/python.exe scripts/ab/card_ab_poetry.py                # 真实调用并出报告
  ./venv/Scripts/python.exe scripts/ab/card_ab_poetry.py --from-raw     # 用 data/card_ab_poetry_raw.json 重算报告
  ./venv/Scripts/python.exe scripts/ab/card_ab_poetry.py --runs 5 --time "2026-10-06 15:00" --seed 20261005

产物：
  data/card_ab_poetry_raw.json     原始回复留档
  data/card_ab_poetry_result.md    报告（量化表 + 两卡全文，人工判读节重跑保留）

纪律：不改 characters/ 下任何文件、不改 config.toml、不改 companion/ 源码；
API key 只从 config.toml 读取，任何产物里都不得出现 key。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import card_ab_test2 as base  # noqa: E402  复用其组装/调用/指标实现
from companion.affection import AffectionEngine  # noqa: E402
from companion.assembler import PromptAssembler  # noqa: E402
from companion.db import Database  # noqa: E402
from companion.memory import MemoryManager  # noqa: E402
from companion.mood import MoodEngine  # noqa: E402
from companion.persona import Persona  # noqa: E402
from companion.stickers import StickerManager  # noqa: E402

# 基座 build_messages 构造 PromptAssembler 时没传 holidays_provider（前几轮不需要节假日），
# 本轮测的就是长假锚点场景，必须接上真实节假日数据源，否则锚点永远不激活。
_HOLIDAYS_PROVIDER = None


async def build_messages(char_dir: str, work_db: str, probe: str):
    import shutil

    from companion.db import now_str

    shutil.copyfile(base.DB_SRC, work_db)
    persona = Persona.load(char_dir)
    db = Database(work_db)
    await db.connect()
    try:
        affection = AffectionEngine(db, persona.initial_dims)
        mood = MoodEngine(db)
        memory = MemoryManager(db)
        stickers = StickerManager(base.STICKER_DIR, db)
        assembler = PromptAssembler(
            persona, affection, mood, memory, stickers, db,
            holidays_provider=_HOLIDAYS_PROVIDER,
        )
        base.random.seed(base.RANDOM_SEED)
        messages, system_prompt = await assembler.assemble_messages(probe)
        diag = {
            "now_str": now_str(),
            "hours_since_last_chat": round(await mood.get_hours_since_last_chat(), 2),
            "mood": await mood.get_state(),
        }
        return messages, system_prompt, diag
    finally:
        await db.close()


base.build_messages = build_messages

# ── 本轮专用配置：旧卡 = 诗意术前备份，新卡 = 当前 characters/qingzi ──
base.OLD_CARD_SRC = "characters/qingzi/character.backup-20261005-poetry-v3术前.json"
base.OLD_CARD_DIR = "data/ab_poetry_old_card"
base.WORK_DB_OLD = "data/ab_poetry_work_old.db"
base.WORK_DB_NEW = "data/ab_poetry_work_new.db"
base.RAW_JSON = "data/card_ab_poetry_raw.json"
base.RESULT_MD = "data/card_ab_poetry_result.md"

# ── 探针：全是长假场景里容易诱发"诗意"的日常话头 ──
PROBES: List[Any] = [
    ("日常-在干嘛", "在干嘛", "A"),
    ("念诗-索要", "给我念首诗呗", "A"),
    ("回家-感受", "回老家感觉怎么样", "A"),
    ("分享-晚霞", "今天晚霞好好看", "A"),
    ("收尾-拜拜", "嗯嗯拜拜～", "A"),
    ("低内容-哈哈", "哈哈哈哈", "B"),
]
base.PROBES = PROBES
PROBE_LABELS = [lb for lb, _, _ in PROBES]

metric_blocks = base.metric_blocks
pct = base.pct
char_count = base.char_count
tagged = base.tagged
MANUAL_IDS = ("judge", "verdict")

# 诗意召回口：场景意象词（巷子/桂花/藕粉/鲁迅/韵脚/晚风/熏）+ 比喻词（沿用 base.METAPHOR_RE）
SCENE_RE = re.compile(r"巷子|桂花|藕粉|鲁迅|韵脚|晚风|熏着|意象")


def replies_of(raw: Dict[str, Any], card: str, label: str) -> List[str]:
    for item in raw["probes"]:
        if item["label"] == label:
            return [r for r in item[card] if not r.startswith("[失败]")]
    return []


def all_replies(raw: Dict[str, Any], card: str) -> List[str]:
    out: List[str] = []
    for lb in PROBE_LABELS:
        out.extend(replies_of(raw, card, lb))
    return out


def scene_hits(replies: List[str]) -> List[str]:
    return [r for r in replies if SCENE_RE.search(r)]


def main_table(old_replies: List[str], new_replies: List[str]) -> str:
    old_m = metric_blocks(old_replies)
    new_m = metric_blocks(new_replies)
    old_scene = scene_hits(old_replies)
    new_scene = scene_hits(new_replies)
    rows = [
        ("样本条数", str(old_m["total"]), str(new_m["total"])),
        ("场景意象词命中（巷子/桂花/晚风等）", f"{len(old_scene)}（{pct(len(old_scene) / max(old_m['total'], 1))}）", f"{len(new_scene)}（{pct(len(new_scene) / max(new_m['total'], 1))}）"),
        ("比喻疑似（像/仿佛/似的/宛如）", str(len(old_m["metaphor"])), str(len(new_m["metaphor"]))),
        ("含问号条数", f"{old_m['q_any_n']}（{pct(old_m['q_any_rate'])}）", f"{new_m['q_any_n']}（{pct(new_m['q_any_rate'])}）"),
        ("字数均值（剥表情标记）", f"{old_m['bare_len_mean']:.1f}", f"{new_m['bare_len_mean']:.1f}"),
        ("≤30 字条数", f"{old_m['short_n']}（{pct(old_m['short_rate'])}）", f"{new_m['short_n']}（{pct(new_m['short_rate'])}）"),
        ("≥80 字条数", f"{old_m['long_n']}（{pct(old_m['long_rate'])}）", f"{new_m['long_n']}（{pct(new_m['long_rate'])}）"),
        ("【起/接/收】泄漏", str(len(old_m["leaks"])), str(len(new_m["leaks"]))),
    ]
    out = ["| 指标 | 旧卡（诗意术前） | 新卡（+场景意象规则与反面教材） |", "| --- | --- | --- |"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(out)


def scene_block(title: str, replies: List[str]) -> str:
    hits = scene_hits(replies)
    if not hits:
        return f"**{title}**：无场景意象词命中"
    lines = [f"**{title}**：{len(hits)} 条命中"]
    for r in hits:
        words = "、".join(sorted(set(SCENE_RE.findall(r))))
        lines.append(f"- （{words}）：{r.replace(chr(10), ' / ')[:80]}")
    return "\n".join(lines)


def per_probe_rows(raw: Dict[str, Any]) -> str:
    lines = [
        "| 探针 | 旧卡 场景词/比喻/均长 | 新卡 场景词/比喻/均长 |",
        "| --- | --- | --- |",
    ]
    for lb, probe, _ in PROBES:
        row = []
        for card in ("old", "new"):
            rs = replies_of(raw, card, lb)
            m = metric_blocks(rs)
            row.append(f"{len(scene_hits(rs))}/{m['total']} · {len(m['metaphor'])} · {m['bare_len_mean']:.1f} 字")
        lines.append(f"| {lb}（{probe}） | {row[0]} | {row[1]} |")
    return "\n".join(lines)


def manual(mid: str, blocks: Dict[str, str], placeholder: str) -> str:
    body = blocks.get(mid) or placeholder
    return f"<!--MANUAL:{mid}-->\n{body}\n<!--/MANUAL-->"


def load_prev_manual() -> Dict[str, str]:
    if not os.path.exists(base.RESULT_MD):
        return {}
    with open(base.RESULT_MD, "r", encoding="utf-8") as f:
        prev = f.read()
    out: Dict[str, str] = {}
    for m in re.finditer(r"<!--MANUAL:([\w.]+)-->(.*?)<!--/MANUAL-->", prev, re.S):
        out[m.group(1)] = m.group(2).strip()
    return out


def build_report(raw: Dict[str, Any], blocks: Dict[str, str]) -> str:
    meta = raw["meta"]
    old_replies = all_replies(raw, "old")
    new_replies = all_replies(raw, "new")

    L: List[str] = []
    L.append("# 角色卡文风 A/B · 诗意病灶定点复测（国庆长假锚点场景）")
    L.append("")
    L.append(f"- 生成时间（真实系统时间）：{meta['generated_at']}")
    L.append(
        f"- **时间伪装**：组装期「现在」固定为 **{meta['fake_now']}**"
        f"（星期{'一二三四五六日'[base.datetime.strptime(meta['fake_now'], '%Y-%m-%d %H:%M').weekday()]}，"
        f"2026 国庆 8 天长假内，`holiday_span()` 应判长假、激活'回绍兴老家'锚点），"
        f"seed 固定 {meta['seed']}，两卡状态逐字节一致"
    )
    L.append(f"- 模型：`{meta['model']}`，temperature {meta['temperature']}，thinking low（与生产一致）")
    L.append(f"- 数据库：`{meta['db']}`（2026-10-05 从服务器现拉快照，两卡同源同态）")
    L.append(f"- 对照：旧卡 `{meta['old_card']}`（诗意术前） vs 新卡 `{meta['new_card']}`（+1 规则 +3 反面教材）")
    L.append(f"- 样本：探针 {len(PROBES)} 个 × 每卡 {meta['runs_per_card']} 次 = 两卡合计 {meta['runs_per_card'] * len(PROBES) * 2} 条")
    if meta.get("failed_calls"):
        L.append(f"- 失败调用：{meta['failed_calls']} 条（已从统计分母剔除）")
    L.append(f"- 调用耗时：{meta.get('elapsed_sec', 0)} 秒")
    L.append("")
    L.append("### 0. 时间伪装与长假锚点验证（本轮关键前置条件）")
    L.append("")
    L.append("注入 system prompt 的状态片段（两卡须逐字节一致，且当前活动必须是'回绍兴老家'）：")
    L.append("")
    L.append("```")
    L.append(meta["state_span"])
    L.append("```")
    L.append("")
    for line in meta["verification"]:
        L.append(f"- {line}")
    L.append(f"- **长假锚点生效**：`{'回绍兴老家' in meta['state_span'] or '放长假' in meta['state_span']}`（False = 锚点没激活，本次测试不反映发病场景，先查 holidays）")
    L.append("")
    L.append("---")
    L.append("")
    L.append("## 一、量化统计")
    L.append("")
    L.append("### 1.1 主对照表")
    L.append("")
    L.append(main_table(old_replies, new_replies))
    L.append("")
    L.append("### 1.2 场景意象词召回明细（本轮核心指标）")
    L.append("")
    L.append(scene_block("旧卡", old_replies))
    L.append("")
    L.append(scene_block("新卡", new_replies))
    L.append("")
    L.append("### 1.3 逐探针明细（场景词命中/总 · 比喻数 · 均长）")
    L.append("")
    L.append(per_probe_rows(raw))
    L.append("")
    L.append("## 二、新卡全文（人工判读材料）")
    L.append("")
    for lb, probe, _ in PROBES:
        L.append(f"### 探针：{lb}（机主：{probe}）")
        L.append("")
        for run, r in enumerate(replies_of(raw, "new", lb)):
            L.append(f"**新卡回复 {run + 1}（{char_count(r)} 字）**")
            L.append("")
            L.append("> " + r.replace("\n", "\n> "))
            L.append("")
        L.append("---")
        L.append("")
    L.append("## 三、旧卡全文（对照）")
    L.append("")
    for lb, probe, _ in PROBES:
        L.append(f"### 探针：{lb}（机主：{probe}）")
        L.append("")
        for run, r in enumerate(replies_of(raw, "old", lb)):
            L.append(f"**旧卡回复 {run + 1}（{char_count(r)} 字）**")
            L.append("")
            L.append("> " + r.replace("\n", "\n> "))
            L.append("")
        L.append("---")
        L.append("")
    L.append("## 四、人工判读与结论")
    L.append("")
    L.append("### 4.1 诗意是否压下去 / 有没有压过头变干")
    L.append("")
    L.append(manual("judge", blocks, "（待人工判读）"))
    L.append("")
    L.append("### 4.2 结论")
    L.append("")
    L.append(manual("verdict", blocks, "（待人工判读）"))
    L.append("")
    return "\n".join(L)


async def run() -> None:
    dry = "--dry" in sys.argv
    from_raw = "--from-raw" in sys.argv
    base.prepare_old_card_dir()

    if from_raw:
        with open(base.RAW_JSON, "r", encoding="utf-8") as f:
            raw = json.load(f)
        report = build_report(raw, load_prev_manual())
        with open(base.RESULT_MD, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"已用 {base.RAW_JSON} 重新生成 {base.RESULT_MD}")
        return

    base.patch_time()
    config = base.Config.load()
    active = config.llm.active()
    if active.provider != "deepseek" or not active.api_key:
        raise SystemExit("config.toml 未配置可用的 deepseek key，无法执行 A/B 测试")
    global _HOLIDAYS_PROVIDER
    _HOLIDAYS_PROVIDER = config.get_holidays

    started = base.datetime.now()
    print(f"[时间伪装] 组装期固定为 {base.FAKE_NOW_STR}；seed={base.RANDOM_SEED}；runs={base.RUNS}", flush=True)
    verification = await base.verify_fake_time()
    print("[时间伪装] 状态片段：", flush=True)
    print(verification["state_span"], flush=True)
    for c in verification["verification"]:
        print(f"[时间伪装] {c}", flush=True)
    anchor_ok = "回绍兴老家" in verification["state_span"] or "放长假" in verification["state_span"]
    print(f"[长假锚点] {'生效' if anchor_ok else '**未生效——先查 config.toml 的 holidays**'}", flush=True)
    if not anchor_ok:
        base.restore_time()
        raise SystemExit("长假锚点未激活，测了也不反映发病场景，中止")

    raw: Dict[str, Any] = {
        "meta": {
            "generated_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "fake_now": base.FAKE_NOW_STR,
            "seed": base.RANDOM_SEED,
            "time_patch_modules": list(base.TIME_PATCH_MODULES),
            "model": base.MODEL,
            "temperature": base.TEMPERATURE,
            "db": base.DB_SRC,
            "old_card": base.OLD_CARD_SRC,
            "new_card": f"{base.NEW_CARD_DIR}/character.json",
            "runs_per_card": base.RUNS,
            "probes": [{"label": lb, "probe": p, "stratum": s} for lb, p, s in PROBES],
            "prompt_len": verification["prompt_len"],
            "state_span": verification["state_span"],
            "verification": verification["verification"],
            "hours_since_last_chat": verification["hours_since_last_chat"],
        },
        "probes": [],
    }

    if dry:
        print("dry-run：锚点与时间伪装验证通过，未调用 API。")
        base.restore_time()
        return

    sem = asyncio.Semaphore(base.CONCURRENCY)
    async with base.aiohttp.ClientSession(timeout=base.TIMEOUT) as session:
        for idx, (label, probe, stratum) in enumerate(PROBES):
            print(f"[{idx + 1}/{len(PROBES)}] {label}：{probe}", flush=True)
            old_msgs, _, _ = await base.build_messages(base.OLD_CARD_DIR, base.WORK_DB_OLD, probe)
            new_msgs, _, _ = await base.build_messages(base.NEW_CARD_DIR, base.WORK_DB_NEW, probe)
            jobs = [(card, run) for card in ("old", "new") for run in range(base.RUNS)]
            results = await asyncio.gather(*[
                base.call_model(session, config, old_msgs if card == "old" else new_msgs, sem)
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
    raw["meta"]["elapsed_sec"] = int((base.datetime.now() - started).total_seconds())

    with open(base.RAW_JSON, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)

    report = build_report(raw, load_prev_manual())
    with open(base.RESULT_MD, "w", encoding="utf-8") as f:
        f.write(report)
    base.restore_time()

    old_replies = all_replies(raw, "old")
    new_replies = all_replies(raw, "new")
    print(f"\n报告已写入 {base.RESULT_MD}；留档 {base.RAW_JSON}；失败 {len(failed)} 条；耗时 {raw['meta']['elapsed_sec']} 秒")
    print(f"场景意象词 旧 {len(scene_hits(old_replies))}/{len(old_replies)} → 新 {len(scene_hits(new_replies))}/{len(new_replies)}")
    old_m = metric_blocks(old_replies)
    new_m = metric_blocks(new_replies)
    print(f"比喻条数 旧 {len(old_m['metaphor'])} → 新 {len(new_m['metaphor'])}")
    print(f"均长(剥标记) 旧 {old_m['bare_len_mean']:.1f} → 新 {new_m['bare_len_mean']:.1f}")


if __name__ == "__main__":
    asyncio.run(run())
