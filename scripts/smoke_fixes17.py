"""FIXES17 真实 API 冒烟 (scripts/smoke_fixes17.py)

单测用的是假网关，**证明不了真模型会不会照新规则吐三栏 JSON**。如果真模型
返回的还是一句话，parse_sticker_desc_json 一律 None，collect_sticker 就静默降级
回旧的"15 字画面描述"——任务 4 等于没做，而且测试照样全绿。所以必须真跑一次。

验三件事：
A. 真模型 + 新 STICKER_DESC_PROMPT → 能不能解析出画面/含义/适用场景三栏
B. 解析出来的含义，是不是真的换成了语用功能（不是把画面复读一遍）
C. 换完后提示词列表长什么样（任务 3 的产物在真实数据上的样子）

零服务器副作用、零生产库读写（本地沙箱库 + 本地 config.toml 的真实 key）。
报告落 data/smoke_fixes17_report.json。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.config import Config
from companion.db import TIME_FORMAT, Database
from companion.gateway import LLMGateway
from companion.prompts import STICKER_DESC_PROMPT
from companion.stickers import StickerManager, image_to_base64_data_url, parse_sticker_desc_json

SANDBOX_DB = "data/smoke_fixes17_sandbox.db"
REPORT_FILE = "data/smoke_fixes17_report.json"
WORKDIR = "data/sticker_audit_workdir/stickers"

# 挑三张有代表性的：模板角色（已知含义容易塌）、静物、需要脑补情绪的
PICKS = ["菲比欢呼", "收到", "冰雕小猫"]


async def main() -> int:
    if os.path.exists(SANDBOX_DB):
        os.remove(SANDBOX_DB)
    config = Config.load()
    db = Database(SANDBOX_DB)
    await db.init_tables()
    gateway = LLMGateway(config.llm, db)
    model = config.llm.vision_model

    with open(os.path.join(WORKDIR, "index.json"), "r", encoding="utf-8") as f:
        index = json.load(f)

    report = {
        "generated_at": datetime.now().strftime(TIME_FORMAT),
        "model": model,
        "A_three_column_parse": {},
        "B_meaning_is_pragmatic": {},
        "C_prompt_list_sample": [],
    }
    failures = 0

    for name in PICKS:
        rel = index.get(name, {}).get("file", "")
        path = os.path.join(WORKDIR, rel)
        if not os.path.exists(path):
            print(f"[X] 找不到 {name} 的图 {rel}")
            failures += 1
            continue
        data_url, err = image_to_base64_data_url(path)
        if not data_url:
            print(f"[X] 读图失败 {name}: {err}")
            failures += 1
            continue

        reply = await gateway.chat(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": STICKER_DESC_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            model=model,
            temperature=0.5,
            purpose="sticker_desc",
        )
        parsed = parse_sticker_desc_json(reply)

        print("=" * 62)
        print(f"■ {name}（模型 {model}）")
        print(f"  原始回复: {(reply or '').strip()[:200]}")
        if not parsed:
            print("  ✗ 解析失败 → collect_sticker 会降级成旧格式（任务4 等于没生效）")
            report["A_three_column_parse"][name] = False
            report["B_meaning_is_pragmatic"][name] = False
            failures += 1
            continue

        report["A_three_column_parse"][name] = True
        print(f"  ✓ 画面: {parsed['画面']}")
        print(f"  ✓ 含义: {parsed['含义']}")
        print(f"  ✓ 适用场景: {parsed['适用场景']}")

        # B：含义里若原样出现画面的头 4 个字，且整句很短 → 是复读，判失败
        head = parsed["画面"][:4]
        is_echo = bool(head) and head in parsed["含义"] and len(parsed["含义"]) <= 12
        report["B_meaning_is_pragmatic"][name] = not is_echo
        print(f"  {'✗' if is_echo else '✓'} 含义是否只是复读画面: {'是（不合格）' if is_echo else '否'}")

    await gateway.close()

    # C：用真实入库后的 index 看提示词列表长什么样
    mgr = StickerManager(WORKDIR, db)
    lst = mgr.get_prompt_sticker_list()
    report["C_prompt_list_sample"] = lst[:6]
    report["C_prompt_list_total"] = len(lst)
    print("=" * 62)
    print(f"■ 提示词列表（共 {len(lst)} 条，抽样 6 条）：")
    for x in lst[:6]:
        print(f"    {x}")

    row = await db.fetchone(
        "SELECT COUNT(*) as c, COALESCE(SUM(cost_estimate),0) as cost FROM llm_calls"
    )
    report["cost"] = {"calls": row["c"] if row else 0, "cost_estimate": round((row["cost"] or 0.0) if row else 0.0, 4)}

    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("=" * 62)
    ok_a = all(report["A_three_column_parse"].values()) and report["A_three_column_parse"]
    ok_b = all(report["B_meaning_is_pragmatic"].values()) and report["B_meaning_is_pragmatic"]
    print(f"A 真模型能解析出三栏: {'PASS' if ok_a else 'FAIL'}")
    print(f"B 含义不是画面复读   : {'PASS' if ok_b else 'FAIL'}")
    print(f"费用 ¥{report['cost']['cost_estimate']}（{report['cost']['calls']} 次）")
    print(f"报告 {REPORT_FILE}")
    await db.close()
    return 0 if (ok_a and ok_b) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
