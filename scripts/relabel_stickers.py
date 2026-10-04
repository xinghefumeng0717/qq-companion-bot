"""FIXES17 表情包三栏重标注 (scripts/relabel_stickers.py)

对**服务器生产表情包库**（任务 0 已只读拉回 `data/sticker_audit_workdir/`）逐张调用视觉模型，
按三栏制（画面 / 含义 / 适用场景）打标，生成所有者可编辑的审核表 `data/sticker_audit.md`。

病因（所有者实测确诊）：旧标注是视觉模型自动打的**画面描述**（"白色卡通小动物、紫底、带腮红"），
她选表情包时只能按画面贴话题，而表情包真正的货币是**语用功能**（这张图是"干饭/撒娇/无语"）。
典型案例：大鲸鱼吃白米饭那张，画面标注完全丢失了"干饭"语义。本脚本把三栏一次问出来。

**只读输入、只写本地审核表，零服务器写操作。**

断点续跑：已标注条目缓存在 `data/sticker_audit_cache.json`，重跑不重复烧钱。
（缓存是本脚本的施工脚手架，审核表 data/sticker_audit.md 才是交付物。）

用法（【本地电脑执行】PowerShell）：
    ./venv/Scripts/python.exe scripts/relabel_stickers.py --limit 3   # 抽样试跑，先看质量
    ./venv/Scripts/python.exe scripts/relabel_stickers.py             # 全量
    ./venv/Scripts/python.exe scripts/relabel_stickers.py --force     # 忽略缓存重跑

注意：`image_to_base64_data_url` 对长边 >1568px 的图会**就地缩放**。workdir 是服务器副本，
本地缩放不影响服务器，也不会回传，纯属省 token。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.stickers import image_to_base64_data_url

WORKDIR = "data/sticker_audit_workdir/stickers"
INDEX_FILE = os.path.join(WORKDIR, "index.json")
AUDIT_MD = "data/sticker_audit.md"
CACHE_FILE = "data/sticker_audit_cache.json"
SANDBOX_DB = "data/relabel_stickers_sandbox.db"
REPORT_FILE = "data/relabel_stickers_report.json"

CONCURRENCY = 4

# ==========================================
# 三栏提示词（写死，任务书任务 1 第 2 条）
# ==========================================

RELABEL_PROMPT = """你是一个表情包标注员。下面这张图是聊天里用的表情包/梗图。

请只输出一个 JSON 对象，不要输出任何解释、不要用 markdown 代码块，格式严格如下：
{"画面": "...", "含义": "...", "适用场景": "..."}

三栏各自的判据（这是重点，不要写成同一句话的三遍）：

- "画面"：一句话客观描述图里有什么，不超过 20 字。只说画面，禁止写情绪。
- "含义"：**发这张图的人当时在表达什么情绪或态度**。这是最重要的一栏——
  要的是语用功能（例如"委屈/破防""无语/懒得理你""撒娇/求安慰"），
  绝对禁止把画面元素重复一遍就算交差。如果画面完全看不出情绪，就根据角色/动作/夸张程度推断一个最合理的说话意图。
- "适用场景"：什么聊天语境下这张图发出来最合适，给 1~2 个具体语境，用顿号分隔。例如"遇到倒霉事吐槽、撒娇求安慰时用"。

示意示例（学习这个分栏的颗粒度）：
图：一个卡通角色张大嘴巴嚎啕大哭，眼泪哗哗往下淌。
{"画面": "卡通角色张大嘴嚎啕大哭", "含义": "委屈、破防，带一点夸张的卖惨", "适用场景": "遇到倒霉事吐槽、撒娇求安慰时用"}

注意：这三栏加起来是一个"表情包说明书"，模型以后要靠"含义"来挑图，所以"含义"必须具体、好用。
"""

# 软上限：防模型跑飞。提示词里"画面"仍写死"不超过 20 字"（任务书规格），
# 这里只留一道防溢出的大网——画面栏是纯审核用字段（不入 index.json、不进提示词），
# 砍太狠只会让所有者读到断在半句的"配文"没"这种垃圾，反而浪费审核时间。
CAP = {"画面": 30, "含义": 40, "适用场景": 40}

# 含义栏撞车检测：同一模板角色的一堆表情包，含义会塌成同一族，
# 这类行模型区分不了，必须由所有者凭记忆补——审核表里单独点名，别让她漏看。
MEANING_FAMILY_HEAD = 6
MEANING_FAMILY_MIN = 2


# ==========================================
# 工具函数
# ==========================================

def parse_json_reply(text: str) -> Optional[Dict[str, Any]]:
    """从模型回复里稳健地抠出 JSON 对象。失败返回 None，绝不猜。"""
    if not text:
        return None
    s = text.strip()
    # 去掉 ```json ... ``` 包裹
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    # 直接尝试
    for candidate in (s,):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    # 抠出第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i != -1 and j > i:
        try:
            obj = json.loads(s[i : j + 1])
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def pick_field(obj: Dict[str, Any], *keys: str) -> str:
    """按候选键名取值，容忍全角括号/空格差异。"""
    norm = {}
    for k, v in obj.items():
        if isinstance(v, str):
            norm[str(k).strip().strip("：:").strip()] = v.strip()
    for k in keys:
        if k in norm and norm[k]:
            return norm[k]
    return ""


def cell(text: str) -> str:
    """Markdown 表格单元格转义：竖线、换行、管道会破表。"""
    t = (text or "").replace("|", "／").replace("\r", " ").replace("\n", " ").strip()
    return t if t else "　"


async def label_one(gateway: LLMGateway, model: str, name: str, path: str) -> Tuple[Optional[Dict[str, str]], str]:
    """给一张图打三栏标。返回 (标注dict 或 None, 失败原因 或 "")。"""
    data_url, err = image_to_base64_data_url(path)
    if not data_url:
        return None, f"读图失败: {err}"

    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": RELABEL_PROMPT},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        }
    ]

    last_raw = ""
    for attempt in range(3):
        try:
            reply = await gateway.chat(
                messages=messages,
                model=model,
                temperature=0.4,
                purpose="sticker_desc",
            )
        except Exception as e:
            last_raw = f"API 异常: {e}"
            await asyncio.sleep(1.5 * (attempt + 1))
            continue

        last_raw = (reply or "")[:400]
        obj = parse_json_reply(reply)
        if obj:
            画面 = pick_field(obj, "画面", "描述", "image")
            含义 = pick_field(obj, "含义", "意思", "meaning")
            场景 = pick_field(obj, "适用场景", "场景", "usage", "使用场景")
            if 含义:  # 含义是本轮的核心，缺了就不算成功
                return (
                    {
                        "画面": 画面[: CAP["画面"]],
                        "含义": 含义[: CAP["含义"]],
                        "适用场景": 场景[: CAP["适用场景"]],
                    },
                    "",
                )
            last_raw = f"JSON 缺 meaning 栏: {last_raw}"
        await asyncio.sleep(1.0 * (attempt + 1))

    return None, last_raw or "未知失败"


# ==========================================
# 审核表生成
# ==========================================

def meaning_families(rows: List[Dict[str, Any]]) -> List[Tuple[str, List[str]]]:
    """找出含义栏塌成同一族的名字（模型区分不了的，交给所有者）。"""
    buckets: Dict[str, List[str]] = {}
    for r in rows:
        if not r.get("ok"):
            continue
        m = (r["value"].get("含义") or "").strip()
        if not m:
            continue
        key = re.split(r"[，,、/；;]", m)[0].strip()[:MEANING_FAMILY_HEAD]
        buckets.setdefault(key, []).append(r["name"])
    return [(k, v) for k, v in sorted(buckets.items(), key=lambda kv: -len(kv[1])) if len(v) >= MEANING_FAMILY_MIN]


def write_audit_md(rows: List[Dict[str, Any]], failures: List[Dict[str, str]]) -> None:
    """写所有者可编辑的 Markdown 审核表。"""
    ok = [r for r in rows if r.get("ok")]
    bad = [r for r in rows if not r.get("ok")]
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")

    lines: List[str] = []
    lines.append("# 表情包三栏标注 · 所有者审核表")
    lines.append("")
    lines.append(f"> 生成时间：{ts}　|　已标注 {len(ok)} 张　|　待人工 {len(bad)} 张")
    lines.append(">")
    lines.append("> **⚠ 这份 md 不要手改。它是机器生成的只读快照（供 grep/diff 用）。**")
    lines.append("> **请改 `data/sticker_audit.docx`**——那份带图片，")
    lines.append("> 第二段的 apply 脚本读的是 docx（`scripts/build_sticker_review_docx.py` 可回读自检）。")
    lines.append(">")
    lines.append("> 三栏判据：**画面**=图里有什么；**含义**=她发这张图在表达什么情绪/态度（最重要）；**适用场景**=什么语境下合适。")
    lines.append("> 最右列是旧的自动标注（画面描述），留着对照用，不用改。")
    lines.append("")
    lines.append("## 标注表")
    lines.append("")
    lines.append("| 名称 | 画面 | 含义 | 适用场景 | 旧标注（对照，不用改） |")
    lines.append("| --- | --- | --- | --- | --- |")
    for r in ok:
        v = r["value"]
        lines.append(
            f"| {cell(r['name'])} | {cell(v.get('画面', ''))} | {cell(v.get('含义', ''))} "
            f"| {cell(v.get('适用场景', ''))} | {cell(r.get('old_desc', ''))} |"
        )
    lines.append("")
    fams = meaning_families(rows)
    if fams:
        lines.append("## 需要你重点看的：含义栏撞车")
        lines.append("")
        lines.append("下面这些行的**含义开头是同一句**，等于在提示词里长得一模一样，")
        lines.append("模型挑表情包时区分不开，只能靠名字和画面瞎猜。")
        lines.append("请按你当初收藏它们时的印象改写含义——这种事模型没资格替你猜。")
        lines.append("（不一定是图重复，也可能是模型确实分不出来；以你为准。）")
        lines.append("")
        for key, names in fams:
            lines.append(f"- 含义开头「{key}」（{len(names)} 张）：{'、'.join(names)}")
        lines.append("")
    lines.append("## 待人工（模型标注失败的，应用脚本不会动这些）")
    lines.append("")
    if not bad:
        lines.append("（无）")
    else:
        lines.append("| 名称 | 旧标注 | 失败原因 |")
        lines.append("| --- | --- | --- |")
        for r in bad:
            lines.append(f"| {cell(r['name'])} | {cell(r.get('old_desc', ''))} | {cell(r.get('error', ''))} |")
    lines.append("")

    with open(AUDIT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


# ==========================================
# 主流程
# ==========================================

async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 张（抽样试跑用）")
    ap.add_argument("--force", action="store_true", help="忽略缓存重跑")
    args = ap.parse_args()

    if not os.path.exists(INDEX_FILE):
        print(f"[X] 找不到 {INDEX_FILE}，先做任务 0：从服务器只读拉回生产表情包库")
        return 2

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index: Dict[str, Dict[str, str]] = json.load(f)

    names = sorted(index.keys())
    if args.limit:
        names = names[: args.limit]

    cache: Dict[str, Any] = {}
    if os.path.exists(CACHE_FILE) and not args.force:
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            cache = json.load(f)
        print(f"[i] 载入缓存 {len(cache)} 条")

    todo = [n for n in names if n not in cache]
    print(f"[i] 本次待标注 {len(todo)} 张（共 {len(names)} 张，模型={Config.load().llm.vision_model}）")

    config = Config.load()
    model = config.llm.vision_model
    db = Database(SANDBOX_DB)
    # connect() 只开连接不建表，llm_calls 记不进去的话成本恒为 0（踩过）
    await db.init_tables()
    gateway = LLMGateway(config.llm, db)

    # 本轮成本基线（沙箱库是跨轮累计的）
    calls_before = tokens_before = ctokens_before = 0
    cost_before = 0.0
    try:
        _b = await db.fetchone(
            "SELECT COUNT(*) as c, COALESCE(SUM(prompt_tokens),0) as pt, "
            "COALESCE(SUM(completion_tokens),0) as ct, COALESCE(SUM(cost_estimate),0) as cost "
            "FROM llm_calls WHERE purpose='sticker_desc'"
        )
        if _b:
            calls_before, tokens_before, ctokens_before = _b["c"], _b["pt"], _b["ct"]
            cost_before = _b["cost"] or 0.0
    except Exception as e:
        print(f"[!] 成本基线读取失败: {e}")

    sem = asyncio.Semaphore(CONCURRENCY)
    done = {"n": 0}
    t0 = time.time()

    async def worker(name: str) -> None:
        async with sem:
            rel = index[name].get("file", "")
            path = os.path.join(WORKDIR, rel)
            if not os.path.exists(path):
                cache[name] = {"ok": False, "error": f"图文件不存在: {rel}"}
            else:
                value, err = await label_one(gateway, model, name, path)
                cache[name] = (
                    {"ok": True, "value": value}
                    if value
                    else {"ok": False, "error": err}
                )
            done["n"] += 1
            flag = "OK " if cache[name].get("ok") else "ERR"
            print(f"  [{done['n']:>2}/{len(todo)}] {flag} {name}", flush=True)

    if todo:
        await asyncio.gather(*(worker(n) for n in todo))
        with open(CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)

    await gateway.close()

    # 组装审核表：名称 + 新标注 + 旧标注对照
    rows: List[Dict[str, Any]] = []
    for name in names:
        c = cache.get(name, {})
        rows.append(
            {
                "name": name,
                "ok": bool(c.get("ok")),
                "value": c.get("value") or {},
                "old_desc": index[name].get("desc", ""),
                "error": c.get("error", ""),
            }
        )

    failures = [{"name": r["name"], "old_desc": r["old_desc"], "error": r["error"]} for r in rows if not r["ok"]]
    write_audit_md(rows, failures)

    # 成本
    cost = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "cost_estimate": 0.0}
    try:
        row = await db.fetchone(
            "SELECT COUNT(*) as calls, COALESCE(SUM(prompt_tokens),0) as pt, "
            "COALESCE(SUM(completion_tokens),0) as ct, COALESCE(SUM(cost_estimate),0) as cost "
            "FROM llm_calls WHERE purpose='sticker_desc'"
        )
        if row:
            cost = {
                "calls": row["calls"],
                "prompt_tokens": row["pt"],
                "completion_tokens": row["ct"],
                "cost_estimate": round(row["cost"] or 0.0, 4),
            }
    except Exception as e:
        print(f"[!] 成本统计失败（不影响交付物）: {e}")

    # 沙箱库跨轮累计，直接报 totals 会把前面 --force 重跑的次数也算进来。
    # 本轮真实开销 = 累计 - 本轮开始前的基线。
    run_cost = {
        "calls": cost["calls"] - calls_before,
        "prompt_tokens": cost["prompt_tokens"] - tokens_before,
        "completion_tokens": cost["completion_tokens"] - ctokens_before,
        "cost_estimate": round(cost["cost_estimate"] - cost_before, 4),
    }

    ok_n = len([r for r in rows if r["ok"]])
    # 防"判定自动成立"：本轮确实要干活、有成功标注却零 API 调用 → 计费链没接上，成本不可信。
    # （本轮无待标注项时零调用是正常的，不算可疑）
    billing_suspect = bool(todo) and ok_n > 0 and run_cost["calls"] == 0
    report = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "model": model,
        "total_in_scope": len(names),
        "labeled_ok": ok_n,
        "needs_human": len(failures),
        "elapsed_sec": round(time.time() - t0, 1),
        "cost_this_run": run_cost,
        "cost_cumulative": cost,
        "billing_suspect": billing_suspect,
        "meaning_collision_families": [{"head": k, "names": v} for k, v in meaning_families(rows)],
        "failures": failures,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print()
    print("=" * 56)
    print(f"审核表      : {AUDIT_MD}")
    print(f"施工报告    : {REPORT_FILE}")
    print(f"标注成功    : {ok_n} / {len(names)}")
    print(f"待人工      : {len(failures)}")
    for f in failures:
        print(f"   - {f['name']}: {f['error'][:100]}")
    print(f"本轮 API    : {run_cost['calls']} 次 | tokens {run_cost['prompt_tokens']}+{run_cost['completion_tokens']} | 费用 ¥{run_cost['cost_estimate']}")
    print(f"累计 API    : {cost['calls']} 次 | 费用 ¥{cost['cost_estimate']}（含 --force 重跑）")
    if billing_suspect:
        print("[!!] 警告：有标注成功但本轮 API 调用计数为 0，本次成本数字不可信（计费链没接上）")
    print(f"耗时        : {report['elapsed_sec']}s")
    print("=" * 56)
    print(">>> 停工：等所有者审核 data/sticker_audit.md，不要自动应用。")

    # aiosqlite 的连接线程不关的话进程会在退出时挂住（踩过）
    await db.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
