"""把 FIXES20 冒烟 B 段跑过的**每一局** face 使用数字重算并并进报告（scripts/face_runs_report20.py）。

为什么单独一个脚本：冒烟日志里 B 段跑过多局（加示范前 2 局、加示范后 2 局），
报告文件里只留了最后一局。**只报最后一局、或者只报好看的局，都是自欺**——
本脚本把所有局的数字按修好后的指标口径统一重算，全部写进报告的
`B_send_all_runs`，并给一行不加修饰的结论。零 API 成本（重算已存的 raw.json）。
"""

from __future__ import annotations

import glob
import json
import os
import sys
from typing import Any, Dict, List

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))

import duo_sim as D

REPORT_FILE = "data/smoke_fixes20_report.json"


def main() -> int:
    runs: List[Dict[str, Any]] = []
    for d in sorted(glob.glob("data/duo_sim/*-fixes20-*"), key=os.path.getmtime):
        raw_path = os.path.join(d, "raw.json")
        if not os.path.exists(raw_path):
            continue
        with open(raw_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        recs = [
            D.TurnRecord(
                idx=t["idx"], speaker=t["speaker"], text=t.get("text", ""),
                time=t.get("time", ""), bubbles=t.get("bubbles") or [],
                faces=t.get("faces") or [],
            )
            for t in raw["turns"]
        ]
        proj = [D._rec_to_dict(r) for r in recs]
        m = D.compute_metrics(proj, stage=1, cost=0.0)["multi_turn"]["QQ表情"]
        meta = raw.get("meta") or {}
        her_turns = sum(1 for r in proj if r["speaker"] == "her")
        runs.append({
            "run_id": os.path.basename(d),
            "scene": meta.get("scene"),
            "her_turns": her_turns,
            "face_count": m["count"],
            "forms": m["forms"],
            "bubble_ratio": m["bubble_ratio"],
            "verdict": m["verdict"],
            "over_cap_turns": m["over_cap_turns"],
            "detail": [u["bubble"] for u in m["detail"]],
        })
        print(
            f"{os.path.basename(d):34s} 场景{meta.get('scene')} 她{her_turns}轮 "
            f"脸{m['count']}次 {m['forms']} 判定{m['verdict']}"
        )

    total_faces = sum(r["face_count"] for r in runs)
    total_turns = sum(r["her_turns"] for r in runs)
    print(f"\n合计：{len(runs)} 局 / {total_turns} 个她回合 / 脸共 {total_faces} 次")

    if os.path.exists(REPORT_FILE):
        with open(REPORT_FILE, "r", encoding="utf-8") as f:
            report = json.load(f)
        report["B_send_all_runs"] = runs
        report["B_send_note"] = (
            f"B 段共跑过 {len(runs)} 局（加示范前 2 局 + 加示范后 2 局），每局数字全列，**不挑好局**。"
            "加示范前 2 局均 0 次；加示范后 2 局分别 3 次与 0 次——采纳率上去了但仍不稳定，"
            "样本小，别当稳定结论。"
        )
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"已并入报告：{REPORT_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
