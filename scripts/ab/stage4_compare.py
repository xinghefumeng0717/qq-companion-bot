"""第四阶段补全前后 20 题对照：复用第三阶段新侧，只生成当前新侧。"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from companion.kernel.ab import SideOutcome, outcomes_from_json
from scripts.ab.stage3_kernel_ab import (
    SideSandbox,
    file_sha256,
    load_cases,
    save_results,
    write_blind,
)

BASELINE_RESULTS = ROOT / "data/ab_stage3/run-20261026-blind-v2/results.json"
EVALUATED_CASE_IDS = (
    *(f"R{index:02d}" for index in range(1, 17)),
    "R19", "C10", "C12", "C15",
)


def load_baseline(path: Path = BASELINE_RESULTS) -> dict[str, SideOutcome]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("metadata", {}).get("complete") is not True:
        raise ValueError("第三阶段基线不是完整结果")
    outcomes = outcomes_from_json(data["outcomes"])
    baseline: dict[str, SideOutcome] = {}
    for case_id in EVALUATED_CASE_IDS:
        pair = outcomes.get(case_id, {})
        if "new" not in pair:
            raise ValueError(f"第三阶段基线缺少 {case_id} 新内核结果")
        baseline[case_id] = replace(pair["new"], side="old")
    return baseline


async def main_async(args: argparse.Namespace) -> None:
    wanted = set(EVALUATED_CASE_IDS)
    cases, sources = load_cases(ROOT / "scripts/ab/stage3_cases.json", wanted)
    baseline = load_baseline(Path(args.baseline).resolve())
    run_dir = Path(args.output_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.from_results:
        data = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
        outcomes = outcomes_from_json(data["outcomes"])
        write_blind(run_dir, cases, outcomes, args.seed)
        print("已仅从既有结果重排盲测稿，没有调用 API。")
        return

    source_hashes = {case_id: file_sha256(path) for case_id, path in sources.items()}
    prepared: dict[str, Any] = {}
    for index, case in enumerate(cases, 1):
        print(f"[组装 {index}/{len(cases)}] {case.case_id} {case.title}", flush=True)
        sandbox = SideSandbox(case, "new", sources[case.case_id], run_dir)
        try:
            await sandbox.initialize()
            prepared[case.case_id] = await sandbox.prepare()
        finally:
            await sandbox.close()

    estimated_total = round(sum(item["estimated_cost"] for item in prepared.values()), 6)
    estimate = {
        "comparison": "stage3-new-kernel vs stage4-new-kernel",
        "baseline_results": str(Path(args.baseline).resolve()),
        "case_count": len(cases),
        "new_call_count": len(cases),
        "estimated_new_cost_yuan": estimated_total,
        "budget_yuan": args.budget,
        "within_budget": estimated_total <= args.budget,
        "per_case": {
            case_id: {
                "prompt_tokens": item["prompt_tokens"],
                "estimated_cost": item["estimated_cost"],
            }
            for case_id, item in prepared.items()
        },
    }
    (run_dir / "estimate.json").write_text(
        json.dumps(estimate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({k: v for k, v in estimate.items() if k != "per_case"}, ensure_ascii=False, indent=2))
    if not args.execute:
        print("仅完成组装与估价，未调用真实 API。")
        return
    if not args.confirm_real_api:
        raise RuntimeError("真实执行还必须显式提供 --confirm-real-api")
    if estimated_total > args.budget:
        raise RuntimeError("保守估价超过预算，未调用 API")

    outcomes: dict[str, dict[str, SideOutcome]] = {}
    metadata = {
        "comparison": "stage3-new-kernel vs stage4-new-kernel",
        "baseline_results": str(Path(args.baseline).resolve()),
        "case_count": len(cases),
        "new_call_count": len(cases),
        "estimated_new_cost_yuan": estimated_total,
        "actual_new_cost_yuan": 0.0,
        "budget_yuan": args.budget,
        "seed": args.seed,
        "complete": False,
    }
    actual_total = 0.0
    results_path = run_dir / "results.json"
    try:
        for index, case in enumerate(cases, 1):
            estimate_cost = prepared[case.case_id]["estimated_cost"]
            if actual_total + estimate_cost > args.budget:
                raise RuntimeError("实际费用加下一次保守估价将超过预算，停止后续调用")
            print(f"[当前生成 {index}/{len(cases)}] {case.case_id}", flush=True)
            sandbox = SideSandbox(case, "new", sources[case.case_id], run_dir)
            try:
                await sandbox.initialize()
                outcome = await sandbox.generate(prepared[case.case_id])
            finally:
                await sandbox.close()
            outcomes[case.case_id] = {
                "old": baseline[case.case_id],
                "new": outcome,
            }
            actual_total = round(actual_total + outcome.cost, 6)
            metadata["actual_new_cost_yuan"] = actual_total
            save_results(results_path, outcomes, metadata)
        metadata["complete"] = True
        save_results(results_path, outcomes, metadata)
        write_blind(run_dir, cases, outcomes, args.seed)
        print(f"完成：{len(cases)} 题，新增 {len(cases)} 次调用，实际费用 {actual_total} 元")
    finally:
        for case_id, path in sources.items():
            if file_sha256(path) != source_hashes[case_id]:
                raise RuntimeError(f"本地输入副本发生变化：{path}")
        metadata["actual_new_cost_yuan"] = actual_total
        save_results(results_path, outcomes, metadata)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default=str(BASELINE_RESULTS))
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--budget", type=float, default=2.0)
    parser.add_argument("--seed", type=int, default=20261027)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-real-api", action="store_true")
    parser.add_argument("--from-results")
    return parser


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
