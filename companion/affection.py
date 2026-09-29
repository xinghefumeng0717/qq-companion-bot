"""好感度引擎 (affection.py)
纯 Python 数学实现，维护六维关系向量、高值阻力、EMA 平滑、日衰减与阶段判定。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from companion.db import Database, now_str

logger = logging.getLogger(__name__)

# 阶段门槛严格映射在 [0, 100] 范围内
# 仿真标定：S1->S2 ~7d, S2->S3 ~21d, S3->S4 ~45d, S4->S5 ~75d, S5->S6 ~120d, S6->S7 ~210d, S7->S8 ~365d
# 相守（阶段 9）说明：设计上不可达，是方向不是终点，作为极限渐近线（3650 天仿真不可达，且复合分在 95+ 走平）
STAGE_THRESHOLDS = [0, 15.0, 38.0, 65.0, 81.0, 93.0, 97.0, 99.0, 99.24, 99.8]

ALPHA = {
    "warmth": 0.80,
    "trust": 0.90,
    "intimacy": 0.85,
    "intrigue": 0.70,
    "patience": 0.95,
}

DECAY_RATES = {
    "warmth": 0.5,
    "trust": 0.2,
    "intimacy": 0.3,
    "intrigue": 2.0,
    "patience": 0.1,
    "tension": 0.8,
}


def calc_resistance(composite: float) -> float:
    """计算高值阻力 r。在 [0, 20] 为 1.0，在 100 时平滑归零，无保底杜绝破百。"""
    if composite <= 20.0:
        return 1.0
    if composite >= 100.0:
        return 0.0
    return ((100.0 - composite) / 80.0) ** 0.45


def calc_composite_score(dims: Dict[str, float]) -> float:
    """复合分 = warmth*0.25 + trust*0.25 + intimacy*0.25 + intrigue*0.10 + patience*0.15 - tension*0.3"""
    score = (
        dims.get("warmth", 0.0) * 0.25
        + dims.get("trust", 0.0) * 0.25
        + dims.get("intimacy", 0.0) * 0.25
        + dims.get("intrigue", 0.0) * 0.10
        + dims.get("patience", 0.0) * 0.15
        - dims.get("tension", 0.0) * 0.30
    )
    return min(100.0, max(0.0, score))


def determine_stage(composite: float) -> int:
    """根据 STAGE_THRESHOLDS 门槛判定阶段，composite >= 门槛[i] 即阶段 i"""
    stage = 0
    for i, threshold in enumerate(STAGE_THRESHOLDS):
        if composite >= threshold:
            stage = i
        else:
            break
    return min(9, stage)


class AffectionEngine:
    def __init__(self, db: Database, initial_dims: Optional[Dict[str, float]] = None):
        self.db = db
        self.initial_dims = initial_dims or {
            "warmth": 40.0,
            "trust": 50.0,
            "intimacy": 35.0,
            "intrigue": 30.0,
            "patience": 50.0,
            "tension": 3.0,
        }

    async def get_state(self) -> Dict[str, Any]:
        """获取当前好感度状态（含六维、复合分、阶段、上次更新时间）"""
        row = await self.db.fetchone("SELECT value FROM state WHERE key = 'affection'")
        if row and row["value"]:
            try:
                return json.loads(row["value"])
            except Exception as e:
                logger.error(f"[Affection] 解析好感度状态异常: {e}")

        # 未初始化则写入初始值
        init_state = {
            "dims": dict(self.initial_dims),
            "composite": calc_composite_score(self.initial_dims),
            "stage": determine_stage(calc_composite_score(self.initial_dims)),
            "last_updated": now_str(),
        }
        await self.save_state(init_state)
        return init_state

    async def save_state(self, state: Dict[str, Any]) -> None:
        val_str = json.dumps(state, ensure_ascii=False)
        await self.db.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES ('affection', ?)",
            (val_str,),
        )

    async def update(
        self,
        self_disclosure: float,
        responsiveness: float,
        warmth_score: float,
        resonance: float,
        moments: List[str],
    ) -> Tuple[Dict[str, Any], bool]:
        """每轮对话后根据观察者评分更新好感度。
        返回: (最新状态, 是否触发情绪大涨脉冲)
        """
        state = await self.get_state()
        dims = dict(state["dims"])
        last_updated_str = state.get("last_updated", now_str())

        # 1. 计算日衰减
        try:
            last_dt = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M")
            days = min(30, max(0, int((datetime.now() - last_dt).total_seconds() / 86400)))
        except Exception:
            days = 0

        if days > 0:
            for k, rate in DECAY_RATES.items():
                dims[k] = min(100.0, max(0.0, dims.get(k, 0.0) - rate * days))

        # 2. 当前复合分与阻力
        old_comp = calc_composite_score(dims)
        r = calc_resistance(old_comp)

        # 3. 映射各维 delta
        raw = {
            "warmth": warmth_score / 10.0,
            "trust": responsiveness / 10.0,
            "intimacy": (self_disclosure + resonance) / 20.0,
            "intrigue": (self_disclosure + warmth_score) / 20.0,
            "patience": (responsiveness + warmth_score) / 20.0,
        }

        deltas = {}
        for dim, raw_val in raw.items():
            base_delta = max(-2.0, min(2.0, (raw_val - 0.4) * 4.0)) * r
            deltas[dim] = base_delta

        tension_delta = 0.0

        # 4. moments 统计
        moments_adjust = {
            "warmth": 0.0,
            "trust": 0.0,
            "intimacy": 0.0,
            "intrigue": 0.0,
            "patience": 0.0,
            "tension": 0.0,
        }
        for m in moments:
            if m == "伤害行为":
                moments_adjust["trust"] -= 2.0
                moments_adjust["warmth"] -= 1.5
                moments_adjust["tension"] += 3.0
            elif m == "轻浮表白":
                moments_adjust["trust"] -= 1.0
                moments_adjust["intimacy"] -= 0.8
            elif m == "深度共情":
                moments_adjust["intimacy"] += 1.0
                moments_adjust["trust"] += 0.5
            elif m == "分享脆弱":
                moments_adjust["intimacy"] += 1.5
                moments_adjust["trust"] += 1.0

        # 5. EMA 平滑更新常规 delta，moments 直接加减，严格 clamp 在 [0.0, 100.0]
        for dim, a in ALPHA.items():
            old_val = dims.get(dim, 0.0)
            d = deltas.get(dim, 0.0)
            ema_val = a * old_val + (1.0 - a) * (old_val + d)
            new_val = ema_val + moments_adjust.get(dim, 0.0)
            dims[dim] = min(100.0, max(0.0, round(new_val, 2)))

        # tension 独占直接累加，严格 clamp 在 [0.0, 100.0]
        dims["tension"] = min(100.0, max(0.0, round(dims.get("tension", 0.0) + moments_adjust["tension"], 2)))

        # 6. 计算新复合分与阶段
        new_comp = round(calc_composite_score(dims), 2)
        old_stage = state.get("stage", 0)
        new_stage = determine_stage(new_comp)

        if new_stage > old_stage:
            logger.info(f"[Affection] 好感度阶段提升: {old_stage} -> {new_stage} (复合分: {new_comp})")
            await self.db.execute(
                "INSERT INTO milestones (stage, reached_at) VALUES (?, ?)",
                (new_stage, now_str()),
            )

        new_state = {
            "dims": dims,
            "composite": new_comp,
            "stage": new_stage,
            "last_updated": now_str(),
        }
        await self.save_state(new_state)

        # 单次涨幅 > 0.15 时触发情绪脉冲
        trigger_mood_pulse = (new_comp - old_comp) > 0.15
        return new_state, trigger_mood_pulse
