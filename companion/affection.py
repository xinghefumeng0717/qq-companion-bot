"""好感度引擎 (affection.py)
纯 Python 数学实现，维护六维关系向量、高值阻力、EMA 平滑、日衰减与阶段判定。
"""

from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.db import Database, STATE_KEY_AFFECTION, TIME_FORMAT, parse_dt

logger = logging.getLogger(__name__)

# 阶段门槛严格映射在 [0, 100] 范围内。
# 标定（FIXES26，真实日衰减下，`python scripts/sim/score_simulation.py` 复现）：
#   基准观察者分 sd=5.0 rs=5.5 wa=6.0 re=5.5、每天 30 轮、聊 6 歇 1、初态示例卡（复合分 22.95）
#   实测首次到达：S2 熟络 6d / S3 同好 20d / S4 知己 45d / S5 微酸 75d / S6 倾心 120d /
#                 S7 依恋 209d / S8 深情 342d（目标 7/21/45/75/120/210/365，容差 ±25%，全部命中）
#   门槛 = 基准曲线在目标日的当日最高复合分；365 天内到达后 0 次掉阶。
# 相守（阶段 9）：设计上不可达，是方向不是终点。基准 10 年复合分渐近 94.4；
#   连满分 10 分 x 每天 40 轮 x 天天聊 10 年也只到 98.6，99.5 永远够不到。
# 顶部门槛刻意拉开（S6->S7 5.9 分、S7->S8 2.3 分），不再挤在 99~99.8 之间靠零点几分定生死。
# （旧表 [.., 93, 97, 99, 99.24, 99.8] 是在"日衰减几乎从不生效"的 bug 下标的，衰减修好后
#   基准只到 S5（96 天），S6 以上一年内到不了，好奇维还会中途掉头把复合分拉下一阶。）
STAGE_THRESHOLDS = [0, 15.0, 37.6, 57.7, 72.4, 79.4, 85.7, 91.6, 93.9, 99.5]

# 降阶迟滞：已到达的阶段，复合分要跌破"门槛 - STAGE_HYSTERESIS"才降。
# 否则刚跨门槛的第二天一扣日衰减就掉回去（阶段在门槛上来回闪、里程碑重复写）。
# 1.5 > 基准每周最大回落（歇一天后两天衰减 ≈0.35）且 > 一周不聊的衰减（≈1.4，深情期出门一周
# 回来不掉阶），< 最窄的阶段间隔（S7->S8 2.3）。一个月不聊（≈-5.3）照样掉阶——衰减是真的。
STAGE_HYSTERESIS = 1.5

ALPHA = {
    "warmth": 0.80,
    "trust": 0.90,
    "intimacy": 0.85,
    "intrigue": 0.70,
    "patience": 0.95,
}

# 每天扣的点数（真实按天扣，见 update() 的 decay_anchor）。
# 各维 衰减/基准日增益 比例统一在 ~0.06（基准日增益 = (1-ALPHA)*delta*30 轮：
# warmth 4.8 / trust 1.8 / intimacy 2.25 / intrigue 5.4 / patience 1.05）。
# 比例一致 => 各维在同一个阻力点达到平衡，复合分单调爬升；旧值 intrigue 2.0（比例 0.37，
# 其他维 ~0.1）会让好奇在中期就先掉头一路衰减到 ~2，持续聊天复合分反而掉阶段。
DECAY_RATES = {
    "warmth": 0.3,
    "trust": 0.1,
    "intimacy": 0.15,
    "intrigue": 0.3,
    "patience": 0.06,
    "tension": 0.8,
}

# 阻力指数：r = ((100-c)/80)^RESISTANCE_EXPONENT。1.0 = 线性归零。
# 旧值 0.45 在高分段太"软"（r 在 99 分还有 0.14），平台被各维撞 100 的 clamp 决定，
# 门槛只能挤在 99 以上；线性阻力让平台由"增益*r = 衰减"决定，基准渐近线落在 ~94.4。
RESISTANCE_EXPONENT = 1.0

# 六维存 4 位小数：高分段单轮增量只有 0.00x，旧的 round(,2) 会把它整个舍掉，
# 形成"舍入平台"（耐心/信任在 r<0.14 时一分都涨不动）。
DIM_DECIMALS = 4


def calc_resistance(composite: float) -> float:
    """计算高值阻力 r。在 [0, 20] 为 1.0，在 100 时平滑归零，无保底杜绝破百。"""
    if composite <= 20.0:
        return 1.0
    if composite >= 100.0:
        return 0.0
    return ((100.0 - composite) / 80.0) ** RESISTANCE_EXPONENT


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


def determine_stage(composite: float, thresholds: Optional[List[float]] = None) -> int:
    """根据门槛判定阶段（默认 STAGE_THRESHOLDS，角色卡可自带），composite >= 门槛[i] 即阶段 i"""
    stage = 0
    for i, threshold in enumerate(thresholds or STAGE_THRESHOLDS):
        if composite >= threshold:
            stage = i
        else:
            break
    return min(9, stage)


def resolve_stage(
    composite: float,
    prev_stage: int,
    max_stage: int,
    stage_max_drop: Optional[int] = None,
    thresholds: Optional[List[float]] = None,
) -> int:
    """在 determine_stage 之上叠两层"不往下掉"的规则，得到要存的阶段：

    1. 降阶迟滞：比上一次阶段低时，复合分 + STAGE_HYSTERESIS 仍够得着旧阶段就保持旧阶段；
    2. 角色卡阶段地板（可选，stage_max_drop）：阶段永远不低于"历史最高阶段 - stage_max_drop"。
       只管阶段标签（提示词/语气按它走），六维与复合分照常衰减；None = 不设地板（旧行为）。
    """
    stage = determine_stage(composite, thresholds)
    if stage < prev_stage:
        stage = max(stage, min(prev_stage, determine_stage(composite + STAGE_HYSTERESIS, thresholds)))
    if stage_max_drop is not None:
        stage = max(stage, max_stage - max(0, int(stage_max_drop)))
    return stage


def dims_for_stage(stage: int, thresholds: Optional[List[float]] = None) -> Dict[str, float]:
    """开局落在指定阶段的六维：五维取该阶段门槛带中点、tension 取 0，复合分即等于中点。"""
    th = thresholds or STAGE_THRESHOLDS
    lower = th[stage]
    upper = th[stage + 1] if stage < len(th) - 1 else 100.0
    v = round((lower + upper) / 2.0, 1)
    return {"warmth": v, "trust": v, "intimacy": v, "intrigue": v, "patience": v, "tension": 0.0}


class AffectionEngine:
    @classmethod
    def from_persona(cls, db: Database, persona: Any, **kw: Any) -> "AffectionEngine":
        """按角色卡的好感度设置建引擎（开局阶段、门槛、衰减、阶段地板；卡里没填的用默认）。"""
        s = getattr(persona, "affection", None)
        return cls(
            db,
            persona.initial_dims,
            stage_max_drop=getattr(persona, "stage_max_drop", None),
            thresholds=getattr(s, "stage_thresholds", None),
            decay_rates=getattr(s, "decay_per_day", None),
            start_stage=getattr(s, "start_stage", None),
            **kw,
        )

    def __init__(
        self,
        db: Database,
        initial_dims: Optional[Dict[str, float]] = None,
        stage_max_drop: Optional[int] = None,
        now_fn: Optional[Callable[[], datetime]] = None,
        thresholds: Optional[List[float]] = None,
        decay_rates: Optional[Dict[str, float]] = None,
        start_stage: Optional[int] = None,
    ):
        self.db = db
        self.initial_dims = initial_dims or {
            "warmth": 40.0,
            "trust": 50.0,
            "intimacy": 35.0,
            "intrigue": 30.0,
            "patience": 50.0,
            "tension": 3.0,
        }
        # 角色卡 stage_max_drop：阶段最多比历史最高低几阶（None = 不设地板）
        self.stage_max_drop = stage_max_drop
        # 角色卡的节奏参数（不填 = 代码默认标定）
        self.thresholds: List[float] = list(thresholds) if thresholds else list(STAGE_THRESHOLDS)
        self.decay_rates: Dict[str, float] = {**DECAY_RATES, **(decay_rates or {})}
        # 开局阶段：卡里给了就按该阶段门槛带中点开局（覆盖 initial_dims）
        if start_stage is not None:
            self.initial_dims = dims_for_stage(int(start_stage), self.thresholds)
        # 时钟可注入：仿真脚本/测试用假时钟跑一年，生产默认 datetime.now
        self._now_fn = now_fn

    def _now(self) -> datetime:
        return self._now_fn() if self._now_fn is not None else datetime.now()

    def _now_str(self) -> str:
        return self._now().strftime(TIME_FORMAT)

    async def get_state(self) -> Dict[str, Any]:
        """获取当前好感度状态（含六维、复合分、阶段、历史最高阶段、上次更新时间）"""
        st = await self.db.get_state_json(STATE_KEY_AFFECTION)
        if st is not None:
            return st

        # 未初始化则写入初始值
        init_comp = calc_composite_score(self.initial_dims)
        init_stage = determine_stage(init_comp, self.thresholds)
        init_state = {
            "dims": dict(self.initial_dims),
            "composite": init_comp,
            "stage": init_stage,
            "max_stage": init_stage,
            "last_updated": self._now_str(),
        }
        await self.save_state(init_state)
        return init_state

    async def save_state(self, state: Dict[str, Any]) -> None:
        await self.db.set_state_json(STATE_KEY_AFFECTION, state)

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
        last_updated_str = state.get("last_updated") or self._now_str()

        # 1. 计算日衰减（按整天扣，零头留给下次）
        #    旧实现以 last_updated 为起点，而 last_updated 每轮都被重置成"现在"——
        #    只要他每天都聊（间隔 < 24h），整数天永远是 0，日衰减从不生效；
        #    隔 30h 聊也只扣 1 天、6h 零头直接丢。现在用独立的 decay_anchor：
        #    它只按"已经扣掉的整天数"往前挪，零头保留到下次累计。
        now_dt = self._now()
        anchor_dt = parse_dt(state.get("decay_anchor")) or parse_dt(last_updated_str)
        if anchor_dt:
            raw_days = max(0, int((now_dt - anchor_dt).total_seconds() / 86400))
            days = min(30, raw_days)
            new_anchor_dt = anchor_dt + timedelta(days=raw_days)
        else:
            days = 0
            new_anchor_dt = now_dt

        if days > 0:
            for k, rate in self.decay_rates.items():
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
            dims[dim] = min(100.0, max(0.0, round(new_val, DIM_DECIMALS)))

        # tension 独占直接累加，严格 clamp 在 [0.0, 100.0]
        dims["tension"] = min(100.0, max(0.0, round(dims.get("tension", 0.0) + moments_adjust["tension"], DIM_DECIMALS)))

        # 6. 计算新复合分与阶段
        new_comp = round(calc_composite_score(dims), 2)
        old_stage = int(state.get("stage", 0))
        # 旧档没有 max_stage：以当时的阶段作为历史最高（不追溯）
        prev_max = max(int(state.get("max_stage", old_stage)), old_stage)
        new_stage = resolve_stage(new_comp, old_stage, prev_max, self.stage_max_drop, self.thresholds)
        new_max = max(prev_max, new_stage)

        # 里程碑只记"历史新高"：降了又回来不重复写
        if new_stage > prev_max:
            logger.info(f"[Affection] 好感度阶段提升: {old_stage} -> {new_stage} (复合分: {new_comp})")
            await self.db.execute(
                "INSERT INTO milestones (stage, reached_at) VALUES (?, ?)",
                (new_stage, self._now_str()),
            )
        elif new_stage != old_stage:
            logger.info(f"[Affection] 好感度阶段变化: {old_stage} -> {new_stage} (复合分: {new_comp})")

        new_state = {
            "dims": dims,
            "composite": new_comp,
            "stage": new_stage,
            "max_stage": new_max,
            "last_updated": self._now_str(),
            "decay_anchor": new_anchor_dt.strftime(TIME_FORMAT),
        }
        await self.save_state(new_state)

        # 单次涨幅 > 0.15 时触发情绪脉冲
        trigger_mood_pulse = (new_comp - old_comp) > 0.15
        return new_state, trigger_mood_pulse
