"""评分系统数值验证仿真脚本 (scripts/sim/score_simulation.py)
用于 FIXES6 任务：验证好感度推进速度、情绪脉冲触发率、情绪冷落曲线与遗忘曲线。
严格按 FIXES6 规范：直接 import 生产代码公式，不改动任何业务代码。
"""

from __future__ import annotations

import asyncio
import datetime
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.affection import (
    ALPHA,
    DECAY_RATES,
    STAGE_THRESHOLDS,
    AffectionEngine,
    calc_composite_score,
    calc_resistance,
    determine_stage,
)
from companion.db import Database
from companion.memory import (
    ALL_SENTIMENTS,
    NEGATIVE_SENTIMENTS,
    POSITIVE_SENTIMENTS,
    calc_diary_strength,
)
from companion.mood import MoodEngine
from companion.persona import Persona


# ==============================================================================
# 任务 1：好感度推进速度仿真
# ==============================================================================

async def simulate_affection(
    initial_dims: Dict[str, float],
    scenarios: Dict[str, Dict[str, Any]],
    sim_days: int = 365,
) -> Dict[str, Any]:
    results = {}

    for key, sc in scenarios.items():
        db = Database(":memory:")
        await db.init_tables()
        engine = AffectionEngine(db, initial_dims)
        init_st = await engine.get_state()
        curr_stage = init_st["stage"]

        reached_stage: Dict[int, int] = {}
        daily_composite: List[float] = [init_st["composite"]]
        turn_deltas: List[float] = []
        pulses = 0
        total_turns = 0

        # 记录各阶段到达天数
        for day in range(1, sim_days + 1):
            is_chat = True
            if sc.get("days_rest", 0) > 0:
                cycle = sc["days_chat"] + sc["days_rest"]
                is_chat = (day % cycle) != 0

            if not is_chat:
                # 停聊日：让 last_updated 跨过 24h 以触发日衰减
                st = await engine.get_state()
                fake_dt = (
                    datetime.datetime.now() - datetime.timedelta(hours=25)
                ).strftime("%Y-%m-%d %H:%M")
                st["last_updated"] = fake_dt
                await engine.save_state(st)
                daily_composite.append(st["composite"])
                continue

            for t in range(sc["turns_per_day"]):
                old_comp = (await engine.get_state())["composite"]
                st, pulse = await engine.update(
                    self_disclosure=sc["sd"],
                    responsiveness=sc["rs"],
                    warmth_score=sc["wa"],
                    resonance=sc["re"],
                    moments=[],
                )
                total_turns += 1
                new_comp = st["composite"]
                turn_deltas.append(new_comp - old_comp)
                if pulse:
                    pulses += 1

                stg = st["stage"]
                if stg > curr_stage:
                    for s in range(curr_stage + 1, stg + 1):
                        if s not in reached_stage:
                            reached_stage[s] = day
                    curr_stage = stg

            daily_composite.append(st["composite"])

        final_st = await engine.get_state()
        results[key] = {
            "name": sc["name"],
            "initial_dims": initial_dims,
            "reached_stage": reached_stage,
            "final_composite": final_st["composite"],
            "final_stage": final_st["stage"],
            "final_dims": final_st["dims"],
            "daily_composite": daily_composite,
            "turn_deltas": turn_deltas,
            "pulses": pulses,
            "total_turns": total_turns,
        }
        await db.close()

    return results


async def run_scenario_d_long_term(
    initial_dims: Dict[str, float],
    days: int = 3650,
) -> Dict[str, Any]:
    """极端满分长期仿真（3650 天外推）"""
    db = Database(":memory:")
    await db.init_tables()
    engine = AffectionEngine(db, initial_dims)
    init_st = await engine.get_state()
    curr_stage = init_st["stage"]
    reached_stage: Dict[int, int] = {}

    checkpoints = {}
    for day in range(1, days + 1):
        for _ in range(40):
            st, _ = await engine.update(
                self_disclosure=10.0,
                responsiveness=10.0,
                warmth_score=10.0,
                resonance=10.0,
                moments=[],
            )
            stg = st["stage"]
            if stg > curr_stage:
                for s in range(curr_stage + 1, stg + 1):
                    if s not in reached_stage:
                        reached_stage[s] = day
                curr_stage = stg
        if day in (30, 90, 180, 365, 730, 1825, 3650):
            checkpoints[day] = {
                "composite": st["composite"],
                "stage": st["stage"],
                "warmth": st["dims"]["warmth"],
                "trust": st["dims"]["trust"],
                "intimacy": st["dims"]["intimacy"],
            }

    await db.close()
    return {
        "reached_stage": reached_stage,
        "checkpoints": checkpoints,
        "final_dims": st["dims"],
        "final_composite": st["composite"],
        "final_stage": st["stage"],
    }


# ==============================================================================
# 任务 2：情绪脉冲触发率
# ==============================================================================

def analyze_mood_pulse(scenario_results: Dict[str, Any]) -> Dict[str, Any]:
    pulse_analysis = {}
    for sc_key in ("A", "B"):
        res = scenario_results[sc_key]
        deltas = res["turn_deltas"]
        total = len(deltas)
        cur_trigger = sum(1 for d in deltas if d > 0.50)
        t_20 = sum(1 for d in deltas if d > 0.20)
        t_15 = sum(1 for d in deltas if d > 0.15)
        t_10 = sum(1 for d in deltas if d > 0.10)
        max_delta = max(deltas) if deltas else 0.0
        avg_delta = sum(deltas) / total if total > 0 else 0.0

        pulse_analysis[sc_key] = {
            "total_turns": total,
            "max_delta": max_delta,
            "avg_delta": avg_delta,
            "rate_cur_0_50": cur_trigger / total * 100 if total else 0.0,
            "rate_alt_0_20": t_20 / total * 100 if total else 0.0,
            "rate_alt_0_15": t_15 / total * 100 if total else 0.0,
            "rate_alt_0_10": t_10 / total * 100 if total else 0.0,
        }
    return pulse_analysis


# ==============================================================================
# 任务 3：情绪引擎冷落曲线
# ==============================================================================

async def simulate_neglect_curve(steps: int = 16, hours_per_step: float = 6.0) -> List[Dict[str, float]]:
    """从默认初态连续冷落 96 小时，每 6 小时采样一次"""
    # 运行 50 次取均值以消除 O-U 过程的高斯噪声
    runs = 50
    aggregated = [
        {"hours": i * hours_per_step, "v": 0.0, "a": 0.0, "t": 0.0, "frustration": 0.0}
        for i in range(steps + 1)
    ]

    for run_idx in range(runs):
        db = Database(":memory:")
        await db.init_tables()

        # 插入基线消息以设置距上次聊天时间
        base_time = datetime.datetime.now()
        await db.execute(
            "INSERT INTO turns (role, content, created_at) VALUES ('assistant', '你好呀', ?)",
            (base_time.strftime("%Y-%m-%d %H:%M"),),
        )

        mood = MoodEngine(db)
        init_st = await mood.get_state()
        aggregated[0]["v"] += init_st["v"]
        aggregated[0]["a"] += init_st["a"]
        aggregated[0]["t"] += init_st["t"]
        aggregated[0]["frustration"] += init_st["frustration"]

        sim_now = base_time
        for i in range(1, steps + 1):
            sim_now += datetime.timedelta(hours=hours_per_step)
            # 操纵 turns 表让 hours_since_chat 准确反映当前冷落小时数
            # 操纵 state['last_updated'] 让 elapsed 准确为 6.0 小时
            last_up = (sim_now - datetime.timedelta(hours=hours_per_step)).strftime("%Y-%m-%d %H:%M")
            st = await mood.get_state()
            st["last_updated"] = last_up
            await mood.save_state(st)

            # 把 turns.created_at 固定在 base_time，并让 datetime.now() 在计算时感知到差值
            # 由于 get_hours_since_last_chat 用 datetime.now() - turns.created_at
            # 我们通过将 turns.created_at 向过去推移来模拟时间流逝
            sim_turn_created = (
                datetime.datetime.now() - datetime.timedelta(hours=i * hours_per_step)
            ).strftime("%Y-%m-%d %H:%M")
            await db.execute("UPDATE turns SET created_at = ?", (sim_turn_created,))

            # 同时更新 state['last_updated'] 使得 real_elapsed 恰好为 hours_per_step
            sim_state_updated = (
                datetime.datetime.now() - datetime.timedelta(hours=hours_per_step)
            ).strftime("%Y-%m-%d %H:%M")
            st["last_updated"] = sim_state_updated
            await mood.save_state(st)

            # 调用 update_mood，无对话冲击
            new_st = await mood.update_mood(composite_affection=25.0, conv_v=0.0, conv_a=0.0, conv_trust=0.0)
            aggregated[i]["v"] += new_st["v"]
            aggregated[i]["a"] += new_st["a"]
            aggregated[i]["t"] += new_st["t"]
            aggregated[i]["frustration"] += new_st["frustration"]

        await db.close()

    for item in aggregated:
        item["v"] = round(item["v"] / runs, 2)
        item["a"] = round(item["a"] / runs, 2)
        item["t"] = round(item["t"] / runs, 2)
        item["frustration"] = round(item["frustration"] / runs, 2)

    return aggregated


# ==============================================================================
# 任务 4：遗忘曲线半衰期表
# ==============================================================================

def compute_forgetting_table() -> List[Dict[str, Any]]:
    """生成记忆日记强度从初始值衰减至 0.5 阈值的天数表"""
    importances = [1, 3, 5, 8, 10]
    sentiments = [("正面", 2.0), ("中性", 1.0), ("负面", 1.5)]
    recall_counts = [0, 3, 8]

    rows = []
    for imp in importances:
        for sent_name, _ in sentiments:
            for rec in recall_counts:
                s0, tau_eff = calc_diary_strength(imp, rec, sent_name, 0.0)
                if s0 > 0.5:
                    days_to_threshold = tau_eff * math.log(s0 / 0.5)
                else:
                    days_to_threshold = 0.0

                # 额外计算半衰期（衰减到初始强度一半的时间）
                half_life_days = tau_eff * math.log(2.0)

                rows.append({
                    "importance": imp,
                    "sentiment": sent_name,
                    "recall_count": rec,
                    "initial_strength": round(s0, 2),
                    "tau_effective": round(tau_eff, 1),
                    "half_life_days": round(half_life_days, 1),
                    "days_to_0_5": round(days_to_threshold, 1),
                })
    return rows


# ==============================================================================
# 打印报告与主程序
# ==============================================================================

async def main():
    print("=" * 80)
    print("   QQ 伴侣机器人 · FIXES6 评分系统数值仿真报告 (纯算不改)")
    print("=" * 80)

    persona = Persona.load("characters/qingzi")
    initial_dims = persona.initial_dims
    print(f"\n[基准环境] 角色: {persona.name}")
    print(f"初始好感向量: {initial_dims}")
    init_comp = calc_composite_score(initial_dims)
    init_stage = determine_stage(init_comp)
    print(f"初始复合分: {init_comp:.2f} (阶段 {init_stage}: {persona.get_stage(init_stage).name})")
    print(f"阶段阈值 STAGE_THRESHOLDS: {STAGE_THRESHOLDS}\n")

    # 1. 任务 1 仿真
    print("-" * 80)
    print("【任务 1：好感度推进速度仿真】")
    print("-" * 80)
    scenarios = {
        "A": {
            "name": "锚点后典型日常 (基准)",
            "sd": 5.0, "rs": 5.5, "wa": 6.0, "re": 5.5,
            "turns_per_day": 30, "days_chat": 6, "days_rest": 1,
        },
        "B": {
            "name": "热恋满分 (8.0)",
            "sd": 8.0, "rs": 8.0, "wa": 8.0, "re": 8.0,
            "turns_per_day": 30, "days_chat": 6, "days_rest": 1,
        },
        "C": {
            "name": "冷淡 (3.0)",
            "sd": 3.0, "rs": 3.0, "wa": 3.0, "re": 3.0,
            "turns_per_day": 15, "days_chat": 7, "days_rest": 0,
        },
        "D": {
            "name": "极端满分 (10.0 上界测试)",
            "sd": 10.0, "rs": 10.0, "wa": 10.0, "re": 10.0,
            "turns_per_day": 40, "days_chat": 7, "days_rest": 0,
        },
    }

    res_1 = await simulate_affection(initial_dims, scenarios, sim_days=365)

    print("\n[各场景到达各阶段所需天数 (365天内)]")
    print(f"{'场景':<24} | " + " | ".join([f"阶段{s}" for s in range(2, 10)]))
    print("-" * 85)
    for k, r in res_1.items():
        row_str = f"{k}. {r['name']:<20} | "
        for s in range(2, 10):
            d = r["reached_stage"].get(s)
            d_str = f"{d}天" if d is not None else "不可达"
            row_str += f"{d_str:<6} | "
        print(row_str)

    print("\n[365 天末状态一览]")
    print(f"{'场景':<24} | {'复合分':<8} | {'阶段':<6} | 温暖   信任   亲密   好奇   包容   紧张")
    print("-" * 85)
    for k, r in res_1.items():
        dims = r["final_dims"]
        dim_str = f"{dims['warmth']:<6.1f} {dims['trust']:<6.1f} {dims['intimacy']:<6.1f} {dims['intrigue']:<6.1f} {dims['patience']:<6.1f} {dims['tension']:<6.1f}"
        print(f"{k}. {r['name']:<20} | {r['final_composite']:<8.2f} | 阶段{r['final_stage']:<4} | {dim_str}")

    # 长期极端满分外推 3650 天
    print("\n[极端满分场景 D 长期外推 (3650 天 / 10年)]")
    res_d_long = await run_scenario_d_long_term(initial_dims, days=3650)
    for day, cp in res_d_long["checkpoints"].items():
        print(f"  Day {day:4d} ({day/365:4.1f}年): 复合分 = {cp['composite']:6.2f} (阶段 {cp['stage']}) | warmth={cp['warmth']:.1f}, trust={cp['trust']:.1f}, intimacy={cp['intimacy']:.1f}")

    # FIXES8 目标 vs 实测 对照表
    print("\n" + "=" * 80)
    print("【FIXES8 关系推进节奏：目标 vs 实测对照表 (基准场景 A: 日常 30 轮, 6聊1歇)】")
    print("=" * 80)
    print(f"{'阶段':<6} | {'阶段名':<6} | {'门槛分':<8} | {'目标时间':<12} | {'允许容差(±25%)':<16} | {'实测到达天数':<12} | 判定")
    print("-" * 80)
    target_pacing = [
        (2, "熟络", STAGE_THRESHOLDS[2], 7, 5.25, 8.75),
        (3, "同好", STAGE_THRESHOLDS[3], 21, 15.75, 26.25),
        (4, "知己", STAGE_THRESHOLDS[4], 45, 33.75, 56.25),
        (5, "微酸", STAGE_THRESHOLDS[5], 75, 56.25, 93.75),
        (6, "倾心", STAGE_THRESHOLDS[6], 120, 90.0, 150.0),
        (7, "依恋", STAGE_THRESHOLDS[7], 210, 157.5, 262.5),
        (8, "深情", STAGE_THRESHOLDS[8], 365, 273.75, 456.25),
        (9, "相守", STAGE_THRESHOLDS[9], 9999, 9999, 9999),
    ]

    all_pacing_pass = True
    for stg, name, th, target, tol_min, tol_max in target_pacing:
        actual = res_1["A"]["reached_stage"].get(stg)
        if stg == 9:
            target_str = "渐近线"
            tol_str = "3650天不可达"
            actual_str = f"未到达 (终态 {res_d_long['final_composite']:.2f})" if stg not in res_d_long["reached_stage"] else f"{actual}天"
            verdict = "【通过】" if (9 not in res_d_long["reached_stage"] and 9 not in res_1["A"]["reached_stage"]) else "【不通过】"
        else:
            target_str = f"~{target} 天"
            tol_str = f"[{tol_min:.1f}, {tol_max:.1f}] 天"
            actual_str = f"{actual} 天" if actual else "未到达"
            passed = actual is not None and (tol_min <= actual <= tol_max)
            if not passed:
                all_pacing_pass = False
            verdict = "【通过】" if passed else "【不通过】"

        print(f"阶段{stg:<4} | {name:<6} | {th:<8.2f} | {target_str:<12} | {tol_str:<16} | {actual_str:<12} | {verdict}")

    # 场景 C 复合分单调下行检查
    c_series = res_1["C"]["daily_composite"]
    c_mono_down = all(c_series[i] >= c_series[i+1] - 0.05 for i in range(len(c_series)-1))
    c_tension = res_1["C"]["final_dims"]["tension"]
    crit_c_pass = c_mono_down and c_tension <= initial_dims["tension"] and res_1["C"]["final_composite"] == 0.0

    print(f"\n冷淡场景 C 验证: 复合分从 {init_comp:.2f} 单调递减归零至 0.00，无负数无异常: {'【通过】' if crit_c_pass else '【不通过】'}")

    # 2. 任务 2 仿真
    print("\n" + "-" * 80)
    print("【任务 2：情绪脉冲触发率】")
    print("-" * 80)
    pulse_res = analyze_mood_pulse(res_1)
    for sc_k in ("A", "B"):
        p = pulse_res[sc_k]
        print(f"场景 {sc_k} ({scenarios[sc_k]['name']}):")
        print(f"  总轮次: {p['total_turns']}")
        print(f"  单轮复合分最大涨幅: {p['max_delta']:.4f}, 平均涨幅: {p['avg_delta']:.4f}")
        print(f"  新阈值 (>0.15) 触发率: {p['rate_alt_0_15']:.2f}%")
        print(f"  备选阈值 (>0.10) 触发率: {p['rate_alt_0_10']:.2f}%")

    # 3. 任务 3 仿真
    print("\n" + "-" * 80)
    print("【任务 3：情绪引擎冷落曲线 (连续 96 小时)】")
    print("-" * 80)
    neglect_data = await simulate_neglect_curve(steps=16, hours_per_step=6.0)
    print(f"{'冷落时长':<10} | {'愉悦度 v':<10} | {'唤醒度 a':<10} | {'安心度 t':<10} | {'冷落驱力 frustration':<15}")
    print("-" * 65)
    for pt in neglect_data:
        print(f"{pt['hours']:>4.0f} 小时   | {pt['v']:>8.2f}   | {pt['a']:>8.2f}   | {pt['t']:>8.2f}   | {pt['frustration']:>12.2f}")

    v_48h = next(pt["v"] for pt in neglect_data if pt["hours"] == 48.0)
    crit_3_1_pass = -5.0 <= v_48h < 0.0
    print(f"\n[任务 3 验收] 48 小时冷落后 v 处于 [-5, 0) 区间: {'【通过】' if crit_3_1_pass else '【不通过】'} (实际 v = {v_48h:.2f})")

    # 4. 任务 4 仿真
    print("\n" + "-" * 80)
    print("【任务 4：遗忘曲线半衰期与门槛表 (衰减至 0.5 阈值)】")
    print("-" * 80)
    forgetting_rows = compute_forgetting_table()

    print(f"{'重要性':<6} | {'情感倾向':<8} | {'回忆次数':<8} | {'初始强度':<8} | {'τ_eff(天)':<10} | {'衰减至0.5天数':<12} | 评价")
    print("-" * 75)
    for r in forgetting_rows:
        flag = ""
        if r["importance"] == 5 and r["sentiment"] == "中性" and r["recall_count"] == 0:
            flag = "⭐ 日常主力 (60~80天)"
        elif r["importance"] == 8 and r["sentiment"] == "正面" and r["recall_count"] == 0:
            flag = "⭐ 正面重要 (~1年)"
        elif r["importance"] == 10 and r["recall_count"] == 8:
            flag = "⭐ 永久深层记忆"
        print(f"{r['importance']:<6d} | {r['sentiment']:<8} | {r['recall_count']:<8d} | {r['initial_strength']:<8.2f} | {r['tau_effective']:<10.1f} | {r['days_to_0_5']:<12.1f} | {flag}")

    imp1_neutral_0 = next(r for r in forgetting_rows if r["importance"] == 1 and r["sentiment"] == "中性" and r["recall_count"] == 0)
    imp5_neutral_0 = next(r for r in forgetting_rows if r["importance"] == 5 and r["sentiment"] == "中性" and r["recall_count"] == 0)
    imp8_pos_0 = next(r for r in forgetting_rows if r["importance"] == 8 and r["sentiment"] == "正面" and r["recall_count"] == 0)
    imp10_pos_8 = next(r for r in forgetting_rows if r["importance"] == 10 and r["sentiment"] == "正面" and r["recall_count"] == 8)

    print("\n[FIXES8 任务 4 遗忘曲线验证结论]")
    print(f"  1. 重要性 1 (中性, 0次回忆): {imp1_neutral_0['days_to_0_5']:.1f} 天 (目标 ~7 天, 容差 [5.6, 8.4]) -> {'【通过】' if 5.6 <= imp1_neutral_0['days_to_0_5'] <= 8.4 else '【不通过】'}")
    print(f"  2. 重要性 5 (中性日常, 0次回忆): {imp5_neutral_0['days_to_0_5']:.1f} 天 (目标 60~80 天) -> {'【通过】' if 60 <= imp5_neutral_0['days_to_0_5'] <= 80 else '【不通过】'}")
    print(f"  3. 重要性 8 (正面感动, 0次回忆): {imp8_pos_0['days_to_0_5']:.1f} 天 (目标 ~1 年, 容差 [292, 438]) -> {'【通过】' if 292 <= imp8_pos_0['days_to_0_5'] <= 438 else '【不通过】'}")
    print(f"  4. 重要性 10 (正面+8次回忆加固): {imp10_pos_8['days_to_0_5']/365:.1f} 年 (目标 多年永久记忆) -> {'【通过】' if imp10_pos_8['days_to_0_5']/365 >= 2.5 else '【不通过】'}")

    print("\n" + "=" * 80)
    print("   仿真运行完毕。全部 4 个任务均已产出严格量化证据。")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(main())
