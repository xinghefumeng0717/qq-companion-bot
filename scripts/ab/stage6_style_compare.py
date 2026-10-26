"""第六阶段：同一新内核动态示例关闭/开启的 5 题真实 A/B。

old 仅作为盲测格式的内部键，实际含义是“同内核关闭示例”；new 是开启。
两侧都走 SideSandbox(side=new)，不混入旧 assembler 链路差异。
默认只组装估价；真实执行必须同时提供 --execute --confirm-real-api。
"""
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

from companion.config import Config
from companion.kernel.ab import SideOutcome, outcomes_from_json
from scripts.ab import stage3_kernel_ab as stage3

EVALUATED_CASE_IDS = ("R01", "R07", "R08", "C14", "C15")
DEFAULT_BUDGET = 2.0
DEFAULT_SEED = 20261029
COMPARISON = "same-new-kernel: style examples off (old) vs on (new)"
ARM_STYLE_ENABLED = {"old": False, "new": True}


def _asset_paths() -> dict[str, Path]:
    config_path = ROOT / "config.toml"
    config = Config.load(str(config_path))
    bank_path = Path(config.reply.style_examples_path)
    if not bank_path.is_absolute():
        bank_path = ROOT / bank_path
    return {"config.toml": config_path, "style_examples.json": bank_path}


def _hashes(paths: dict[str, Path]) -> dict[str, str]:
    return {name: stage3.file_sha256(path) for name, path in paths.items()}


def _assert_hashes(paths: dict[str, Path], expected: dict[str, str]) -> None:
    for name, path in paths.items():
        if stage3.file_sha256(path) != expected[name]:
            raise RuntimeError(f"实验资产在运行中发生变化：{path}")


def _code_version(side: str) -> str:
    return "kernel-stage6-style-on" if side == "new" else "kernel-stage6-style-off"


def _new_sandbox(case, source_db: Path, run_dir: Path, side: str):
    # side 只表示盲测臂；实际两侧都使用新内核。
    return stage3.SideSandbox(
        case,
        "new",
        source_db,
        run_dir,
        style_examples_enabled=ARM_STYLE_ENABLED[side],
    )


def _public_estimate(prepared: dict[str, Any]) -> dict[str, Any]:
    return {
        "prompt_tokens": prepared["prompt_tokens"],
        "estimated_cost": prepared["estimated_cost"],
        "style_hits": prepared["style_hits"],
        "style_seed": prepared["style_seed"],
    }


async def main_async(args: argparse.Namespace) -> None:
    wanted = (
        {item.strip() for item in args.cases.split(",") if item.strip()}
        if args.cases else set(EVALUATED_CASE_IDS)
    )
    if args.repeat < 1:
        raise ValueError("--repeat 必须至少为 1")
    base_cases, sources = stage3.load_cases(
        ROOT / "scripts" / "ab" / "stage3_cases.json", wanted,
    )
    runs = []
    for case in base_cases:
        for repetition in range(1, args.repeat + 1):
            run_id = case.case_id if args.repeat == 1 else f"{case.case_id}__rep{repetition}"
            title = case.title if args.repeat == 1 else f"{case.title}（第 {repetition} 轮）"
            display_case = replace(case, case_id=run_id, title=title)
            runs.append((run_id, case, display_case))
    cases = [display_case for _run_id, _case, display_case in runs]
    run_dir = Path(args.output_dir).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)

    if args.from_results:
        data = json.loads(Path(args.from_results).read_text(encoding="utf-8"))
        outcomes = outcomes_from_json(data["outcomes"])
        complete = {
            case_id for case_id, pair in outcomes.items()
            if set(pair) == {"old", "new"}
        }
        render_cases = [case for case in cases if case.case_id in complete]
        if not render_cases:
            raise ValueError("既有结果没有可组成 A/B 的完整题目")
        stage3.write_blind(run_dir, render_cases, outcomes, args.seed)
        print(f"已仅从既有结果重排 {len(render_cases)} 题盲测稿，没有调用 API。")
        return

    asset_paths = _asset_paths()
    asset_hashes = _hashes(asset_paths)
    source_hashes = {
        case_id: stage3.file_sha256(path) for case_id, path in sources.items()
    }
    prepared: dict[str, dict[str, Any]] = {}
    for run_id, case, _display_case in runs:
        prepared[run_id] = {}
        for side in ("old", "new"):
            print(f"[组装 {run_id} {side}]", flush=True)
            sandbox = _new_sandbox(case, sources[case.case_id], run_dir, side)
            try:
                await sandbox.initialize()
                sandbox.kernel.code_version = _code_version(side)
                item = await sandbox.prepare()
                selection = item["prepared_turn"].style_examples
                item["style_hits"] = len(selection.items) if selection else 0
                item["style_seed"] = selection.seed if selection else None
                prepared[run_id][side] = item
            finally:
                await sandbox.close()
        if prepared[run_id]["new"]["style_hits"] == 0:
            raise RuntimeError(f"{run_id} 开臂没有示例命中，停止无效真实对照")
        if prepared[run_id]["old"]["messages"] == prepared[run_id]["new"]["messages"]:
            raise RuntimeError(f"{run_id} 两臂提示词相同，停止无效真实对照")

    _assert_hashes(asset_paths, asset_hashes)
    for case_id, path in sources.items():
        if stage3.file_sha256(path) != source_hashes[case_id]:
            raise RuntimeError(f"本地输入副本发生变化：{path}")

    estimated_total = round(sum(
        prepared[case_id][side]["estimated_cost"]
        for case_id in prepared for side in ("old", "new")
    ), 6)
    estimate = {
        "comparison": COMPARISON,
        "arm_meaning": {"old": "同一新内核关闭示例", "new": "同一新内核开启示例"},
        "case_count": len(base_cases),
        "repeat_count": args.repeat,
        "run_count": len(runs),
        "call_count": len(runs) * 2,
        "estimated_total_yuan": estimated_total,
        "budget_yuan": args.budget,
        "within_budget": estimated_total <= args.budget,
        "asset_sha256": asset_hashes,
        "source_sha256": source_hashes,
        "per_case": {
            case_id: {
                side: _public_estimate(prepared[case_id][side])
                for side in ("old", "new")
            }
            for case_id in prepared
        },
    }
    (run_dir / "estimate.json").write_text(
        json.dumps(estimate, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({
        key: value for key, value in estimate.items()
        if key not in {"per_case", "asset_sha256", "source_sha256"}
    }, ensure_ascii=False, indent=2))
    if not args.execute:
        print("仅完成组装与估价，未调用真实 API。")
        return
    if not args.confirm_real_api:
        raise RuntimeError("真实执行还必须显式提供 --confirm-real-api")
    if estimated_total > args.budget:
        raise RuntimeError("保守估价超过预算，未调用 API")

    outcomes: dict[str, dict[str, SideOutcome]] = {}
    metadata = {
        "comparison": COMPARISON,
        "arm_meaning": estimate["arm_meaning"],
        "case_count": len(base_cases),
        "repeat_count": args.repeat,
        "run_count": len(runs),
        "call_count": len(runs) * 2,
        "estimated_total_yuan": estimated_total,
        "actual_total_yuan": 0.0,
        "budget_yuan": args.budget,
        "seed": args.seed,
        "asset_sha256": asset_hashes,
        "source_sha256": source_hashes,
        "complete": False,
    }
    actual_total = 0.0
    results_path = run_dir / "results.json"
    try:
        for run_id, case, _display_case in runs:
            outcomes[run_id] = {}
            for side in ("old", "new"):
                estimated_call = prepared[run_id][side]["estimated_cost"]
                if actual_total + estimated_call > args.budget:
                    raise RuntimeError("实际费用加下一次保守估价将超过预算，停止后续调用")
                print(f"[真实生成 {run_id} {side}]", flush=True)
                sandbox = _new_sandbox(case, sources[case.case_id], run_dir, side)
                try:
                    await sandbox.initialize()
                    sandbox.kernel.code_version = _code_version(side)
                    with stage3.frozen_time(case.received_at):
                        generated = await sandbox.generate(prepared[run_id][side])
                    outcome = replace(generated, side=side)
                finally:
                    await sandbox.close()
                outcomes[run_id][side] = outcome
                actual_total = round(actual_total + outcome.cost, 6)
                metadata["actual_total_yuan"] = actual_total
                stage3.save_results(results_path, outcomes, metadata)
        metadata["complete"] = True
        stage3.save_results(results_path, outcomes, metadata)
        stage3.write_blind(run_dir, cases, outcomes, args.seed)
        print(f"完成：{len(base_cases)} 题 × {args.repeat} 轮，{len(runs) * 2} 次调用，实际费用 {actual_total} 元")
    finally:
        _assert_hashes(asset_paths, asset_hashes)
        for case_id, path in sources.items():
            if stage3.file_sha256(path) != source_hashes[case_id]:
                raise RuntimeError(f"本地输入副本发生变化：{path}")
        metadata["actual_total_yuan"] = actual_total
        stage3.save_results(results_path, outcomes, metadata)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--budget", type=float, default=DEFAULT_BUDGET)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--cases", help="逗号分隔题号；默认使用首轮五题")
    parser.add_argument("--repeat", type=int, default=1, help="每题重复轮数，提示词保持冻结")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-real-api", action="store_true")
    parser.add_argument("--from-results")
    return parser


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
