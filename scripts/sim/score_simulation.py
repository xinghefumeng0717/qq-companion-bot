"""评分系统数值验证仿真脚本 (scripts/sim/score_simulation.py)

验证好感度推进速度、阶段稳定性、情绪脉冲触发率、情绪冷落曲线与遗忘曲线。
直接 import 生产代码（AffectionEngine / MoodEngine 原样跑），**不复刻公式**：
时间用注入的假时钟（engine 的 now_fn）推进，所以日衰减走的是生产里真实的
decay_anchor 路径；冷落用 role='user' 的发言行（mood.py 只看 user 行）。

FIXES26 修正（旧脚本的两个口径事故）：
  1. 旧脚本停聊日只改 last_updated，而日衰减早已改按 decay_anchor 计 —— 仿真里衰减几乎从不发生，
     标出来的节奏在真实衰减下全部失真（S6 以上一年内到不了）；
  2. 旧冷落仿真插的是 role='assistant' 行，mood.py 只认 role='user'，冷落惩罚其实没被测到。

用法：
    python scripts/sim/score_simulation.py            # 全量报告（含 10 年渐近线，约 1~2 分钟）
    python scripts/sim/score_simulation.py --quick    # 跳过 10 年外推
不调用任何 LLM。
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import math
import os
import random
import sys
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.affection import (  # noqa: E402
    DECAY_RATES,
    RESISTANCE_EXPONENT,
    STAGE_HYSTERESIS,
    STAGE_THRESHOLDS,
    AffectionEngine,
    calc_composite_score,
    determine_stage,
)
from companion.db import TIME_FORMAT, Database  # noqa: E402
from companion.memory import calc_diary_strength  # noqa: E402
from companion.mood import MoodEngine  # noqa: E402

# qingzi 卡的开局六维（脚本自带一份，角色卡是私有内容，缺卡时也能跑）
DEFAULT_INITIAL_DIMS = {
    "warmth": 22.0, "trust": 24.0, "intimacy": 18.0,
    "intrigue": 26.0, "patience": 32.0, "tension": 1.5,
}

BASELINE_SCORES = (5.0, 5.5, 6.0, 5.5)  # sd, rs, wa, re（锚点后典型日常）

# 目标节奏（docs/FIXES8.md），容差 ±25%
TARGET_DAYS = {2: 7, 3: 21, 4: 45, 5: 75, 6: 120, 7: 210, 8: 365}
STAGE_NAMES = {2: "熟络", 3: "同好", 4: "知己", 5: "微酸", 6: "倾心", 7: "依恋", 8: "深情", 9: "相守"}
TOLERANCE = 0.25

SIM_START = datetime.datetime(2026, 1, 1, 0, 0)


class FakeClock:
    def __init__(self, start: datetime.datetime = SIM_START):
        self.now = start

    def __call__(self) -> datetime.datetime:
        return self.now


# ---------- 聊天节律 ----------
def pattern_6on1off(day: int) -> bool:
    return day % 7 != 0


def pattern_daily(day: int) -> bool:
    return True


def pattern_every_other(day: int) -> bool:
    return day % 2 == 1


def pattern_weekends(day: int) -> bool:
    return day % 7 in (6, 0)


# ==============================================================================
# 任务 1：好感度推进（真实引擎 + 假时钟）
# ==============================================================================

async def run_affection(
    scores: Tuple[float, float, float, float] = BASELINE_SCORES,
    turns_per_day: int = 30,
    pattern: Callable[[int], bool] = pattern_6on1off,
    days: int = 365,
    initial_dims: Optional[Dict[str, float]] = None,
    stage_max_drop: Optional[int] = None,
    silence: Optional[Tuple[int, int]] = None,
) -> Dict[str, Any]:
    """跑一条关系曲线。每个聊天日 20:00 起每 2 分钟一轮。

    silence=(start_day, n_days)：从 start_day（含）起整整 n_days 天不聊。
    返回：arrivals（各阶段首次到达日）、drops（到达过的阶段之后又掉下去的天）、每日曲线等。
    """
    sd, rs, wa, re_ = scores
    clock = FakeClock()
    db = Database(":memory:")
    await db.init_tables()
    engine = AffectionEngine(db, dict(initial_dims or DEFAULT_INITIAL_DIMS),
                             stage_max_drop=stage_max_drop, now_fn=clock)
    st = await engine.get_state()
    arrivals: Dict[int, int] = {}
    max_stage = st["stage"]
    drops: List[Tuple[int, int, int]] = []  # (day, 掉到的阶段, 当时历史最高)
    daily: List[Dict[str, Any]] = []
    deltas: List[float] = []
    pulses = 0

    for day in range(1, days + 1):
        silent = silence is not None and silence[0] <= day < silence[0] + silence[1]
        if silent or not pattern(day):
            daily.append({"day": day, "chat": False, "lo": st["composite"], "hi": st["composite"],
                          "stage": st["stage"]})
            continue
        lo = None
        for t in range(turns_per_day):
            clock.now = SIM_START + datetime.timedelta(days=day - 1, hours=20, minutes=2 * t)
            prev = st["composite"]
            st, pulse = await engine.update(sd, rs, wa, re_, [])
            deltas.append(st["composite"] - prev)
            pulses += int(pulse)
            if lo is None:
                lo = st["composite"]
            stg = st["stage"]
            if stg > max_stage:
                for s in range(max_stage + 1, stg + 1):
                    arrivals.setdefault(s, day)
                max_stage = stg
            elif stg < max_stage and (not drops or drops[-1][0] != day):
                drops.append((day, stg, max_stage))
        daily.append({"day": day, "chat": True, "lo": lo, "hi": st["composite"], "stage": st["stage"]})

    await db.close()
    return {
        "arrivals": arrivals,
        "drops": drops,
        "daily": daily,
        "final": st,
        "deltas": deltas,
        "pulses": pulses,
        "max_composite": max(d["hi"] for d in daily) if daily else st["composite"],
    }


def _arrival_str(arr: Dict[int, int], s: int) -> str:
    d = arr.get(s)
    return f"{d}" if d is not None else "-"


def print_target_table(base: Dict[str, Any], long_run: Optional[Dict[str, Any]]) -> bool:
    print(f"{'阶段':<4} | {'名称':<4} | {'门槛':>6} | {'目标':>5} | {'容差(±25%)':<14} | {'实测':>5} | 判定")
    print("-" * 72)
    ok_all = True
    for s in range(2, 9):
        tgt = TARGET_DAYS[s]
        lo, hi = tgt * (1 - TOLERANCE), tgt * (1 + TOLERANCE)
        act = base["arrivals"].get(s)
        ok = act is not None and lo <= act <= hi
        ok_all &= ok
        print(f"S{s:<3} | {STAGE_NAMES[s]:<4} | {STAGE_THRESHOLDS[s]:>6.1f} | {tgt:>4}d | "
              f"[{lo:6.1f},{hi:6.1f}] | {(str(act) + 'd') if act else '未到':>5} | {'通过' if ok else '不通过'}")
    if long_run is not None:
        ok9 = 9 not in long_run["arrivals"]
        ok_all &= ok9
        print(f"S9   | 相守 | {STAGE_THRESHOLDS[9]:>6.1f} | 渐近线 | 3650 天不可达   | "
              f"峰值 {long_run['max_composite']:.2f} | {'通过' if ok9 else '不通过'}")
    return ok_all


async def sensitivity(base_drops_ok: bool) -> None:
    print("\n[敏感性：各阶段首次到达天数（365 天内，'-' = 未到）；掉阶 = 到达后又跌回的次数]")
    header = f"{'场景':<30} | " + " ".join(f"S{s:<4}" for s in range(2, 10)) + " | 终分   | 掉阶"
    print(header)
    print("-" * len(header))
    cases = [
        ("分数 5（四项全 5）", dict(scores=(5, 5, 5, 5))),
        ("分数 6（四项全 6）", dict(scores=(6, 6, 6, 6))),
        ("分数 7（四项全 7）", dict(scores=(7, 7, 7, 7))),
        ("基准分 每天 10 轮", dict(turns_per_day=10)),
        ("基准分 每天 30 轮（基准）", dict()),
        ("基准分 每天 90 轮", dict(turns_per_day=90)),
        ("基准分 天天聊", dict(pattern=pattern_daily)),
        ("基准分 隔天聊", dict(pattern=pattern_every_other)),
        ("基准分 只周末聊", dict(pattern=pattern_weekends)),
    ]
    for name, kw in cases:
        r = await run_affection(**kw)
        row = f"{name:<30} | " + " ".join(f"{_arrival_str(r['arrivals'], s):<5}" for s in range(2, 10))
        print(f"{row} | {r['final']['composite']:6.2f} | {len(r['drops'])}")


async def silence_test() -> None:
    print("\n[停聊测试：基准节奏聊到第 N 天后完全不聊 7/30 天，回来聊第一轮时的阶段]")
    print(f"{'起点':<10} | {'停聊':<5} | {'停前阶段':<8} | {'无地板':<16} | {'stage_max_drop=1':<16}")
    print("-" * 70)
    for start in (150, 365):
        for gap in (7, 30):
            res = {}
            for floor in (None, 1):
                r = await run_affection(days=start + gap + 1, silence=(start + 1, gap), stage_max_drop=floor)
                before = r["daily"][start - 1]
                after = r["daily"][start + gap]
                res[floor] = (before, after)
            b, a0 = res[None]
            _, a1 = res[1]
            print(f"第 {start:<4} 天 | {gap:>2} 天 | S{b['stage']} ({b['hi']:.1f}) | "
                  f"S{a0['stage']} ({a0['lo']:.1f})      | S{a1['stage']} ({a1['lo']:.1f})")


# ==============================================================================
# 任务 2：情绪脉冲触发率
# ==============================================================================

def pulse_stats(deltas: List[float]) -> Dict[str, float]:
    n = len(deltas) or 1
    return {
        "turns": len(deltas),
        "max": max(deltas) if deltas else 0.0,
        "avg": sum(deltas) / n,
        "rate_015": sum(1 for d in deltas if d > 0.15) / n * 100,
        "rate_010": sum(1 for d in deltas if d > 0.10) / n * 100,
    }


# ==============================================================================
# 任务 3：情绪冷落曲线（真实引擎 + 假时钟，role='user'）
# ==============================================================================

async def simulate_neglect(
    call_every_hours: float,
    total_hours: float = 96.0,
    composite: float = 25.0,
    runs: int = 30,
    sample_hours: Tuple[float, ...] = (12, 24, 36, 48, 72, 96),
) -> Dict[float, Dict[str, float]]:
    """他最后一次发言后，每 call_every_hours 小时调一次 update_mood（无对话冲击）。
    返回各采样时刻（取 <= 该时刻的最近一次调用后的状态）的 runs 次均值。"""
    agg = {h: {"v": 0.0, "a": 0.0, "t": 0.0, "frustration": 0.0} for h in sample_hours}
    rng_state = random.getstate()
    random.seed(20261021)
    for _ in range(runs):
        clock = FakeClock()
        db = Database(":memory:")
        await db.init_tables()
        await db.execute(
            "INSERT INTO turns (role, content, created_at) VALUES ('user', '晚安', ?)",
            (SIM_START.strftime(TIME_FORMAT),),
        )
        mood = MoodEngine(db, now_fn=clock)
        st = await mood.get_state()  # 状态初始化于他最后发言那一刻
        n_calls = int(round(total_hours / call_every_hours))
        last = {0.0: st}
        for i in range(1, n_calls + 1):
            h = i * call_every_hours
            clock.now = SIM_START + datetime.timedelta(hours=h)
            st = await mood.update_mood(composite_affection=composite)
            last[h] = st
        for sh in sample_hours:
            key = max(k for k in last if k <= sh + 1e-9)
            for f in agg[sh]:
                agg[sh][f] += float(last[key][f])
        await db.close()
    random.setstate(rng_state)
    return {h: {f: round(v / runs, 2) for f, v in vals.items()} for h, vals in agg.items()}


# ==============================================================================
# 任务 4：遗忘曲线半衰期表
# ==============================================================================

def compute_forgetting_table() -> List[Dict[str, Any]]:
    rows = []
    for imp in (1, 3, 5, 8, 10):
        for sent_name in ("正面", "中性", "负面"):
            for rec in (0, 3, 8):
                s0, tau_eff = calc_diary_strength(imp, rec, sent_name, 0.0)
                days_to = tau_eff * math.log(s0 / 0.5) if s0 > 0.5 else 0.0
                rows.append({"importance": imp, "sentiment": sent_name, "recall_count": rec,
                             "initial_strength": round(s0, 2), "tau_effective": round(tau_eff, 1),
                             "days_to_0_5": round(days_to, 1)})
    return rows


# ==============================================================================
# 主程序
# ==============================================================================

async def main(quick: bool = False) -> None:
    print("=" * 80)
    print("   QQ 伴侣机器人 · 评分系统数值仿真（真实引擎 + 假时钟，FIXES26 口径）")
    print("=" * 80)
    init_comp = calc_composite_score(DEFAULT_INITIAL_DIMS)
    print(f"初始六维: {DEFAULT_INITIAL_DIMS}  复合分 {init_comp:.2f}（阶段 {determine_stage(init_comp)}）")
    print(f"STAGE_THRESHOLDS = {STAGE_THRESHOLDS}")
    print(f"DECAY_RATES = {DECAY_RATES}  阻力指数 = {RESISTANCE_EXPONENT}  降阶迟滞 = {STAGE_HYSTERESIS}")

    print("\n" + "-" * 80)
    print("【任务 1：好感度推进（基准：sd5.0 rs5.5 wa6.0 re5.5，30 轮/天，聊 6 歇 1）】")
    print("-" * 80)
    base = await run_affection(days=365)
    print("[门槛求解参考] 基准曲线在目标日的当日最高复合分（门槛即取这些值）：")
    print("  " + "  ".join(f"d{d}={base['daily'][d - 1]['hi']:.2f}" for d in TARGET_DAYS.values()))

    long_run = None
    if not quick:
        long_run = await run_affection(days=3650)
        extreme = await run_affection(scores=(10, 10, 10, 10), turns_per_day=40,
                                      pattern=pattern_daily, days=3650)
        print(f"[渐近线] 基准 10 年峰值复合分 {long_run['max_composite']:.2f}；"
              f"满分 10 x 40 轮/天 x 天天聊 10 年峰值 {extreme['max_composite']:.2f}"
              f"（相守门槛 {STAGE_THRESHOLDS[9]}，{'不可达' if 9 not in extreme['arrivals'] else '可达！'}）")
        print(f"[10 年稳定性] 基准节奏 10 年内到达后掉阶次数：{len(long_run['drops'])}")

    print("\n[目标 vs 实测]")
    ok = print_target_table(base, long_run)
    print(f"\n基准 365 天内到达后掉阶次数：{len(base['drops'])} "
          f"{'【通过】' if not base['drops'] else '【不通过】 ' + str(base['drops'][:5])}")
    print(f"节奏总判定：{'【通过】' if ok else '【不通过】'}")

    cold = await run_affection(scores=(3, 3, 3, 3), turns_per_day=15, pattern=pattern_daily)
    his = [d["hi"] for d in cold["daily"]]
    mono = all(his[i] >= his[i + 1] - 1e-9 for i in range(len(his) - 1))
    print(f"冷淡场景（全 3 分，15 轮/天）：复合分 {init_comp:.2f} -> {cold['final']['composite']:.2f}，"
          f"单调不增 {'【通过】' if mono else '【不通过】'}，无负数 {'【通过】' if min(his) >= 0 else '【不通过】'}")

    await sensitivity(not base["drops"])
    await silence_test()

    print("\n" + "-" * 80)
    print("【任务 2：情绪脉冲触发率（阈值 0.15）】")
    print("-" * 80)
    hot = await run_affection(scores=(8, 8, 8, 8))
    for name, r in (("基准", base), ("热恋 8 分", hot)):
        p = pulse_stats(r["deltas"])
        print(f"{name}: {p['turns']} 轮，单轮最大涨幅 {p['max']:.4f}，平均 {p['avg']:.4f}，"
              f">0.15 触发率 {p['rate_015']:.2f}%（>0.10: {p['rate_010']:.2f}%）")

    print("\n" + "-" * 80)
    print("【任务 3：情绪冷落曲线（composite=25，他最后发言后不再说话；30 次均值）】")
    print("-" * 80)
    print("同一段冷落、不同调用频率——曲线应基本重合（旧实现按调用次数累计，1h 调用 48h 时 v=-10）")
    print(f"{'调用间隔':<8} | " + " | ".join(f"{int(h):>2}h v/a/t/frus" for h in (24, 48, 72, 96)))
    for every in (1.0 / 6.0, 1.0, 6.0, 24.0):
        res = await simulate_neglect(every)
        cells = [f"{res[h]['v']:5.2f}/{res[h]['a']:5.2f}/{res[h]['t']:4.2f}/{res[h]['frustration']:4.2f}"
                 for h in (24, 48, 72, 96)]
        label = f"{every * 60:.0f}min" if every < 1 else f"{every:.0f}h"
        print(f"{label:>7}  | " + " | ".join(cells))

    print("\n" + "-" * 80)
    print("【任务 4：遗忘曲线（衰减至 0.5 阈值天数）】")
    print("-" * 80)
    rows = compute_forgetting_table()

    def pick(imp: int, sent: str, rec: int) -> float:
        return next(r["days_to_0_5"] for r in rows if r["importance"] == imp
                    and r["sentiment"] == sent and r["recall_count"] == rec)

    print(f"  重要性 1 中性 0 回忆: {pick(1, '中性', 0):.1f} 天（目标 ~7）")
    print(f"  重要性 5 中性 0 回忆: {pick(5, '中性', 0):.1f} 天（目标 60~80）")
    print(f"  重要性 8 正面 0 回忆: {pick(8, '正面', 0):.1f} 天（目标 ~365）")
    print(f"  重要性 10 正面 8 回忆: {pick(10, '正面', 8) / 365:.1f} 年（目标 多年）")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="跳过 10 年外推")
    args = ap.parse_args()
    asyncio.run(main(quick=args.quick))
