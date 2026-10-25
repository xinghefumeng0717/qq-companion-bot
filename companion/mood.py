"""情绪引擎 (mood.py)
三维连续值 (valence, arousal, trust_mood) + 情感动量 + 冷落驱力 (frustration)。
纯 Python 数学实现，基于 Ornstein-Uhlenbeck 均值回归与对话冲击更新。
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from companion.affection import determine_stage
from companion.db import Database, STATE_KEY_MOOD, TIME_FORMAT, parse_dt

logger = logging.getLogger(__name__)

# ---- 冷落（按真实经过的时间算，与 update_mood 被调多频繁无关） ----
# 冷落从机主最后一次发言后 NEGLECT_GRACE_HOURS 小时开始。
NEGLECT_GRACE_HOURS = 12.0
# 冷落水平 N(h) = min(NEGLECT_CAP, (h - 12) * NEGLECT_SLOPE)：它不是"每次调用扣一次"，
# 而是把 O-U 回归的目标基线往下压 N（valence）/ N*0.5（arousal）。
# 旧实现每次调用直接 v -= N：48h 冷落时每小时调一次 v 钉在 -10，每天调一次只到 -0.4。
# 现在不管调多少次都收敛到同一条曲线；幅度按 FIXES6 标定曲线（6h 步进实测 48h v≈-1.2、
# a≈-1.0、frustration≈2.5，"闹情绪而非抑郁"）重新对齐：composite=25 时 48h v≈-1.5。
NEGLECT_SLOPE = 0.15
NEGLECT_CAP = 4.0
# 安心度磨损：每小时 t -= N * NEGLECT_T_PER_HOUR（= 旧版 6h 步进下每步 0.02*N 的时间等价）
NEGLECT_T_PER_HOUR = 0.02 / 6.0
# 冷落驱力：每小时 += (h - 12) * NEGLECT_FRUSTRATION_PER_HOUR2，按区间精确积分
# （旧版 6h 步进每步 +(h-12)*0.02 的连续版本）：24h ≈0.24、48h ≈2.16、72h ≈6.0、~89h 封顶 10。
NEGLECT_FRUSTRATION_PER_HOUR2 = 0.02 / 6.0


def neglect_level(hours_since_chat: float) -> float:
    """冷落水平 N(h)：12h 内为 0，之后线性上升，封顶 NEGLECT_CAP。"""
    return min(NEGLECT_CAP, max(0.0, (hours_since_chat - NEGLECT_GRACE_HOURS) * NEGLECT_SLOPE))


def _frustration_integral(hours_since_chat: float) -> float:
    """∫_0^h max(0, x-12) * k dx —— 冷落驱力的累计量（与调用频率无关的精确积分）。"""
    over = max(0.0, hours_since_chat - NEGLECT_GRACE_HOURS)
    return over * over * NEGLECT_FRUSTRATION_PER_HOUR2 / 2.0


class MoodEngine:
    def __init__(self, db: Database, now_fn: Optional[Callable[[], datetime]] = None):
        self.db = db
        # 时钟可注入（仿真/测试用），生产默认 datetime.now
        self._now_fn = now_fn

    def _now(self) -> datetime:
        return self._now_fn() if self._now_fn is not None else datetime.now()

    async def get_state(self) -> Dict[str, Any]:
        """获取当前情绪状态"""
        st = await self.db.get_state_json(STATE_KEY_MOOD)
        if st is not None:
            return st

        # 默认初始状态
        init_state = {
            "v": 2.0,
            "a": 1.0,
            "t": 7.0,
            "momentum_v": 0.0,
            "momentum_a": 0.0,
            "frustration": 0.0,
            "last_updated": self._now().strftime(TIME_FORMAT),
        }
        await self.save_state(init_state)
        return init_state

    async def save_state(self, state: Dict[str, Any]) -> None:
        await self.db.set_state_json(STATE_KEY_MOOD, state)

    async def get_hours_since_last_chat(self, now: Optional[datetime] = None) -> float:
        """获取距机主最近一次发言的小时数（只看 user 行，机器人主动消息不计）"""
        row = await self.db.fetchone(
            "SELECT created_at FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1"
        )
        if not row or not row["created_at"]:
            return 0.0
        last_dt = parse_dt(row["created_at"])
        if last_dt:
            delta_sec = ((now or self._now()) - last_dt).total_seconds()
            return max(0.0, delta_sec / 3600.0)
        return 0.0

    async def update_mood(
        self,
        composite_affection: float,
        conv_v: float = 0.0,
        conv_a: float = 0.0,
        conv_trust: float = 0.0,
        stage: Optional[int] = None,
    ) -> Dict[str, Any]:
        """更新情绪状态：O-U均值回归、情感动量、安心度回归、冷落惩罚、对话冲击。

        stage：好感度引擎存的当前阶段（含降阶迟滞、阶段地板、角色卡自带门槛）；
        不传则按默认门槛从复合分推算（旧调用方行为不变）。"""
        state = await self.get_state()
        v = float(state.get("v", 2.0))
        a = float(state.get("a", 1.0))
        t = float(state.get("t", 7.0))
        m_v = float(state.get("momentum_v", 0.0))
        m_a = float(state.get("momentum_a", 0.0))
        frustration = float(state.get("frustration", 0.0))
        last_updated_str = state.get("last_updated")

        now_dt = self._now()
        last_dt = parse_dt(last_updated_str)
        if last_dt:
            real_elapsed = max(0.0, (now_dt - last_dt).total_seconds() / 3600.0)
        else:
            real_elapsed = 1.0

        hours_since_chat = await self.get_hours_since_last_chat()
        # 正在聊（距他上次发言 < 1h，或库里还没有他的发言）：不足 1h 按 1h 参与回归——
        # 这是每条消息的阻尼，防止一串对话冲击在几分钟内无衰减地叠到 clamp。
        # 冷落中（>= 1h 没发言）：按真实经过时间回归，否则调用越密回归越快，冷落曲线随调用频率变形。
        if hours_since_chat >= 1.0:
            elapsed = real_elapsed
        else:
            elapsed = real_elapsed if real_elapsed >= 1.0 else 1.0
        # 本次更新区间的起点（距他上次发言的小时数），用于冷落量按时间积分
        prev_hours_since_chat = max(0.0, hours_since_chat - real_elapsed)
        n_now = neglect_level(hours_since_chat)
        n_prev = neglect_level(prev_hours_since_chat)

        orig_v = v
        orig_a = a

        # 1. O-U 均值回归 + 噪声（θ = 0.12）
        #    冷落时回归目标整体下压 N(h)（arousal 压 N/2）：冷落的"深度"由经过的时间决定，
        #    调用频率只影响逼近这条曲线的步长，不再影响扣了几次。
        baseline_v = 2.0 + min(2.0, composite_affection / 50.0) - n_now
        baseline_a = 1.0 - n_now * 0.5
        # decay 上限 1.0：elapsed > 8.33h 时若任由 decay 超过 1，一次更新会把 v 推过基线
        # 打到反向（隔夜符号反转）。clamp 只影响 > 8.33h 的长间隔，≤ 8.33h 逐点不变。
        decay = min(1.0, 0.12 * elapsed)
        noise_v = random.gauss(0, 0.5) * math.sqrt(decay)
        noise_a = random.gauss(0, 0.4) * math.sqrt(decay)

        v += (baseline_v - v) * decay + noise_v
        a += (baseline_a - a) * decay + noise_a

        # 2. 情感动量（惯性），上限 ±5.0：动量的自激回路会把情绪钉死在极值多轮
        v += m_v * 0.3
        a += m_a * 0.3
        m_v = max(-5.0, min(5.0, m_v * 0.8 + (v - orig_v) * 0.2))
        m_a = max(-5.0, min(5.0, m_a * 0.8 + (a - orig_a) * 0.2))

        # 3. 安心度极慢回归基线（每天回归 5%）
        # 基线随关系阶段走：3.0 + 阶段×0.6（初识只有 3+ 的"小心翼翼"，深爱才到 8+ 的"完全安心"）
        if stage is None:
            stage = determine_stage(composite_affection)
        baseline_t = 3.0 + stage * 0.6
        t += (baseline_t - t) * 0.05 * (elapsed / 24.0)

        # 4. 冷落：安心度磨损与冷落驱力都按"本次区间经过的冷落小时"积分（与调用次数无关）
        if hours_since_chat > NEGLECT_GRACE_HOURS:
            t -= (n_prev + n_now) / 2.0 * NEGLECT_T_PER_HOUR * (hours_since_chat - prev_hours_since_chat)
            frustration = min(
                10.0,
                frustration + _frustration_integral(hours_since_chat) - _frustration_integral(prev_hours_since_chat),
            )
        elif 0 < hours_since_chat < 1.0:
            v += 0.3
            t += 0.02
            frustration = max(0.0, frustration - 0.5)

        # 5. 对话冲击（观察者给出）
        v += conv_v
        a += conv_a
        t += conv_trust

        # 收尾 clamp：v,a in [-10, 10]；t in [0, 10]；frustration in [0, 10]。
        # 存储精度要够细：调用越密单步变化越小（10 分钟一步 v 只动 ~0.03），
        # 旧的 round(,1)/round(,2) 会把小步整个舍掉，曲线随调用频率变形。展示处自己格式化。
        v = round(max(-10.0, min(10.0, v)), 3)
        a = round(max(-10.0, min(10.0, a)), 3)
        t = round(max(0.0, min(10.0, t)), 4)
        frustration = round(max(0.0, min(10.0, frustration)), 4)
        m_v = round(m_v, 3)
        m_a = round(m_a, 3)

        new_state = {
            "v": v,
            "a": a,
            "t": t,
            "momentum_v": m_v,
            "momentum_a": m_a,
            "frustration": frustration,
            "last_updated": now_dt.strftime(TIME_FORMAT),
        }
        await self.save_state(new_state)
        return new_state
