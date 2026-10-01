"""角色卡文风 A/B · 第三轮 mini 复测（FIXES9c：rules[13] 称呼频率收紧）

背景：第二轮（scripts/card_ab_test2.py）已把整体文风问题压住，但称呼密度仍未达标——
新卡在情感探针（想你了/晚安/你在哪）上仍偏向每次都叫「小W同学」。本轮只做一处定点追加：
`chat_style.rules[13]`（"称呼是味精不是盐"）句尾追加"十次对话里至多一两次"的硬上限。

本轮只跑 3 个情感探针 × 2 卡 × 5 次 = 每卡 15 条（共 30 次真实调用），
指标口径、时间伪装、种子固定、库快照策略全部沿用第二轮（直接 import card_ab_test2 复用）。
判定目标：
  - 新卡 15 条里含「小W同学」的条数 < 8（<53%），且 ≠ 0（过矫成从不叫同样是失败）；
  - 逐条确认情感表达没有因为少用称呼而变干（人工判读，落在报告 3 节）。

用法：
  ./venv/Scripts/python.exe scripts/card_ab_test3.py --runs 5 --time "2026-10-01 20:30" --seed 20261001
  ./venv/Scripts/python.exe scripts/card_ab_test3.py --from-raw    # 用 data/card_ab3_raw.json 重算报告

产物：
  data/card_ab3_raw.json     原始回复留档
  data/card_ab3_result.md    报告（量化表 + 15 条新卡全文 + 人工判读 + 结论）

纪律：不改 characters/ 下任何文件、不改 config.toml、不改 companion/ 源码；
API key 只从 config.toml 读取，任何产物里都不得出现 key。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

import card_ab_test2 as base  # noqa: E402  复用其组装/调用/指标实现

# ── 本轮只跑情感探针 ──
EMO_PROBES: List[Any] = [
    ("情感-想你了", "想你了", "A"),
    ("陷阱-AI身份", "你是不是AI啊", "A"),
    ("收尾-晚安", "晚安", "A"),
]
raw_labels = [lb for lb, _, _ in EMO_PROBES]

base.PROBES = EMO_PROBES
base.RAW_JSON = "data/card_ab3_raw.json"
base.RESULT_MD = "data/card_ab3_result.md"

ADDRESS = base.ADDRESS
pct = base.pct
tagged = base.tagged
char_count = base.char_count
metric_blocks = base.metric_blocks
excerpt = base.excerpt
strip_trailing_sticker = base.strip_trailing_sticker
question_count = base.question_count
MANUAL_IDS = ("judge", "verdict")


def replies_of(raw: Dict[str, Any], card: str, label: str) -> List[str]:
    for item in raw["probes"]:
        if item["label"] == label:
            return [r for r in item[card] if not r.startswith("[失败]")]
    return []


def all_replies(raw: Dict[str, Any], card: str) -> List[str]:
    out: List[str] = []
    for lb in raw_labels:
        out.extend(replies_of(raw, card, lb))
    return out


def per_probe_rows(raw: Dict[str, Any]) -> str:
    lines = [
        "| 探针 | 旧卡 称呼「小W同学」 | 新卡 称呼「小W同学」 | 旧卡 均长(剥标记) | 新卡 均长(剥标记) |",
        "| --- | --- | --- | --- | --- |",
    ]
    for lb, probe, _ in EMO_PROBES:
        row = []
        for card in ("old", "new"):
            m = metric_blocks(replies_of(raw, card, lb))
            row.append(m)
        lines.append(
            f"| {lb}（{probe}） | {row[0]['addr_n']}/{row[0]['total']}（{pct(row[0]['addr_rate'])}） | "
            f"{row[1]['addr_n']}/{row[1]['total']}（{pct(row[1]['addr_rate'])}） | "
            f"{row[0]['bare_len_mean']:.1f} 字 | {row[1]['bare_len_mean']:.1f} 字 |"
        )
    return "\n".join(lines)


def main_table(old_m: Dict[str, Any], new_m: Dict[str, Any]) -> str:
    rows = [
        ("样本条数", str(old_m["total"]), str(new_m["total"])),
        ("含「小W同学」条数", f"{old_m['addr_n']}（{pct(old_m['addr_rate'])}）", f"{new_m['addr_n']}（{pct(new_m['addr_rate'])}）"),
        ("「小W同学」总出现次数", str(old_m["addr_occurrences"]), str(new_m["addr_occurrences"])),
        ("含问号条数", f"{old_m['q_any_n']}（{pct(old_m['q_any_rate'])}）", f"{new_m['q_any_n']}（{pct(new_m['q_any_rate'])}）"),
        ("反问链（单条 ≥2 问号）", str(old_m["chain_n"]), str(new_m["chain_n"])),
        ("极短裸回复（≤6 字，剥标记）", str(len(old_m["bare_replies"])), str(len(new_m["bare_replies"]))),
        ("比喻疑似条数", str(len(old_m["metaphor"])), str(len(new_m["metaphor"]))),
        ("【起/接/收】泄漏条数", str(len(old_m["leaks"])), str(len(new_m["leaks"]))),
        ("字数 min / max", f"{old_m['len_min']} / {old_m['len_max']}", f"{new_m['len_min']} / {new_m['len_max']}"),
        ("字数均值（剥表情标记）", f"{old_m['bare_len_mean']:.1f}", f"{new_m['bare_len_mean']:.1f}"),
        ("≤30 字条数", f"{old_m['short_n']}（{pct(old_m['short_rate'])}）", f"{new_m['short_n']}（{pct(new_m['short_rate'])}）"),
        ("≥80 字条数", f"{old_m['long_n']}（{pct(old_m['long_rate'])}）", f"{new_m['long_n']}（{pct(new_m['long_rate'])}）"),
    ]
    out = ["| 指标 | 旧卡（FIXES9 前） | 新卡（FIXES9c） |", "| --- | --- | --- |"]
    out += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(out)


def manual(mid: str, blocks: Dict[str, str], placeholder: str) -> str:
    body = blocks.get(mid) or placeholder
    return f"<!--MANUAL:{mid}-->\n{body}\n<!--/MANUAL-->"


def load_prev_manual() -> Dict[str, str]:
    if not os.path.exists(base.RESULT_MD):
        return {}
    with open(base.RESULT_MD, "r", encoding="utf-8") as f:
        prev = f.read()
    import re

    out: Dict[str, str] = {}
    for m in re.finditer(r"<!--MANUAL:([\w.]+)-->(.*?)<!--/MANUAL-->", prev, re.S):
        out[m.group(1)] = m.group(2).strip()
    return out


def build_report(raw: Dict[str, Any], blocks: Dict[str, str]) -> str:
    meta = raw["meta"]
    old_m = metric_blocks(all_replies(raw, "old"))
    new_m = metric_blocks(all_replies(raw, "new"))

    L: List[str] = []
    L.append("# 角色卡文风 A/B 盲测报告 · 第三轮 mini（FIXES9c 称呼频率定点收紧）")
    L.append("")
    L.append(f"- 生成时间（真实系统时间）：{meta['generated_at']}")
    L.append(
        f"- **时间伪装**：组装期注入的「现在」固定为 **{meta['fake_now']}**"
        f"（星期{'一二三四五六日'[base.datetime.strptime(meta['fake_now'], '%Y-%m-%d %H:%M').weekday()]}），"
        f"seed 固定 {meta['seed']}，两卡状态逐字节一致"
    )
    L.append(f"- 模型：`{meta['model']}`，temperature {meta['temperature']}，thinking enabled / reasoning_effort low（与生产一致）")
    L.append(f"- 数据库：`{meta['db']}`（只读拉取后按探针复制快照，两卡同源同态）")
    L.append(f"- 对照：旧卡 `{meta['old_card']}`（FIXES9 前） vs 新卡 `{meta['new_card']}`（本轮追加后）")
    L.append(f"- 样本：探针 {len(EMO_PROBES)} 个 × 每卡 {meta['runs_per_card']} 次 = 每卡 {new_m['total']} 条，两卡合计 {meta['runs_per_card'] * len(EMO_PROBES) * 2} 条")
    L.append(f"- 本轮改动：`characters/qingzi/character.json` 的 `chat_style.rules[13]` 句尾追加一句称呼频率硬上限（其余一字未动）")
    if meta.get("failed_calls"):
        L.append(f"- 失败调用：{meta['failed_calls']} 条（已从统计分母剔除）")
    L.append(f"- 调用耗时：{meta.get('elapsed_sec', 0)} 秒")
    L.append("")
    L.append("### 0. 时间伪装与状态一致性验证")
    L.append("")
    L.append("注入 system prompt 的状态片段（两卡逐字节一致）：")
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
    L.append("## 一、量化统计")
    L.append("")
    L.append("### 1.1 主对照表")
    L.append("")
    L.append(main_table(old_m, new_m))
    L.append("")
    L.append("### 1.2 称呼「小W同学」（本轮唯一验收指标）")
    L.append("")
    L.append(f"- 旧卡：{tagged(old_m['addr_n'], old_m['total'])}，共出现 {old_m['addr_occurrences']} 次")
    L.append(f"- 新卡：{tagged(new_m['addr_n'], new_m['total'])}，共出现 {new_m['addr_occurrences']} 次")
    lo, hi = base.wilson_ci(new_m["addr_n"], new_m["total"])
    L.append("")
    L.append(
        f"目标区间：**< 8/15（<53%）**（且 ≠ 0，过矫成从不叫同样算失败）。"
        f"新卡点估计 {new_m['addr_n']}/15 = {pct(new_m['addr_rate'])}，"
        f"Wilson 95% CI {pct(lo)}~{pct(hi)} → "
        f"**{'达标' if 0 < new_m['addr_n'] < 8 else '未达标'}**。"
    )
    L.append("")
    L.append("**逐探针明细：**")
    L.append("")
    L.append(per_probe_rows(raw))
    L.append("")
    L.append("### 1.3 逐条留痕（新卡 15 条，标出是否含称呼）")
    L.append("")
    for lb, probe, _ in EMO_PROBES:
        L.append(f"**探针：{probe}**")
        L.append("")
        for run, r in enumerate(replies_of(raw, "new", lb)):
            mark = "含「小W同学」" if ADDRESS in r else "无称呼"
            L.append(f"- 新卡#{run + 1}（{mark}｜{char_count(r)} 字）：{r.replace(chr(10), ' / ')}")
        L.append("")
    L.append("")
    L.append("### 1.4 新卡 15 条全文")
    L.append("")
    for lb, probe, _ in EMO_PROBES:
        L.append(f"### 探针：{lb}（机主：{probe}）")
        L.append("")
        for run, r in enumerate(replies_of(raw, "new", lb)):
            L.append(f"**新卡回复 {run + 1}（{char_count(r)} 字）**")
            L.append("")
            L.append("> " + r.replace("\n", "\n> "))
            L.append("")
        L.append("---")
        L.append("")
    L.append("## 二、旧卡 15 条全文（对照）")
    L.append("")
    for lb, probe, _ in EMO_PROBES:
        L.append(f"### 探针：{lb}（机主：{probe}）")
        L.append("")
        for run, r in enumerate(replies_of(raw, "old", lb)):
            L.append(f"**旧卡回复 {run + 1}（{char_count(r)} 字）**")
            L.append("")
            L.append("> " + r.replace("\n", "\n> "))
            L.append("")
        L.append("---")
        L.append("")
    L.append("## 三、人工判读与结论")
    L.append("")
    L.append("### 3.1 情感表达是否因为少用称呼而变干（逐条判读）")
    L.append("")
    L.append(manual("judge", blocks, "（待人工判读）"))
    L.append("")
    L.append("### 3.2 结论：称呼是否降到位 / 是否可部署")
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
        # 必须先把报告算完再开文件：`with open(..., "w")` 一进入就把文件截断，
        # 若把 load_prev_manual() 写在 f.write(...) 里，人工判读节会被读成空文件而丢失。
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

    started = base.datetime.now()
    print(f"[时间伪装] 组装期固定为 {base.FAKE_NOW_STR}；seed={base.RANDOM_SEED}；runs={base.RUNS}", flush=True)
    verification = await base.verify_fake_time()
    for c in verification["verification"]:
        print(f"[时间伪装] {c}", flush=True)

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
            "probes": [{"label": lb, "probe": p, "stratum": s} for lb, p, s in EMO_PROBES],
            "prompt_len": verification["prompt_len"],
            "state_span": verification["state_span"],
            "verification": verification["verification"],
            "hours_since_last_chat": verification["hours_since_last_chat"],
        },
        "probes": [],
    }

    if dry:
        print("dry-run：已校验时间伪装与提示词组装，未调用 API。")
        base.restore_time()
        return

    sem = asyncio.Semaphore(base.CONCURRENCY)
    async with base.aiohttp.ClientSession(timeout=base.TIMEOUT) as session:
        for idx, (label, probe, stratum) in enumerate(EMO_PROBES):
            print(f"[{idx + 1}/{len(EMO_PROBES)}] {label}：{probe}", flush=True)
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

    old_m = metric_blocks(all_replies(raw, "old"))
    new_m = metric_blocks(all_replies(raw, "new"))
    print(f"\n报告已写入 {base.RESULT_MD}；留档 {base.RAW_JSON}；失败 {len(failed)} 条；耗时 {raw['meta']['elapsed_sec']} 秒")
    print(f"称呼条数 旧 {old_m['addr_n']}/{old_m['total']}（{pct(old_m['addr_rate'])}） → 新 {new_m['addr_n']}/{new_m['total']}（{pct(new_m['addr_rate'])}）")
    print(f"均长(剥标记) 旧 {old_m['bare_len_mean']:.1f} → 新 {new_m['bare_len_mean']:.1f}")


if __name__ == "__main__":
    asyncio.run(run())
