"""情绪引擎 (mood.py)
三维连续值 (valence, arousal, trust_mood) + 情感动量 + 冷落驱力 (frustration)。
纯 Python 数学实现，基于 Ornstein-Uhlenbeck 均值回归与对话冲击更新。
"""

from __future__ import annotations

import json
import logging
import math
import random
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from companion.affection import determine_stage
from companion.db import Database, STATE_KEY_MOOD, now_str

logger = logging.getLogger(__name__)


class MoodEngine:
    def __init__(self, db: Database):
        self.db = db

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
            "last_updated": now_str(),
        }
        await self.save_state(init_state)
        return init_state

    async def save_state(self, state: Dict[str, Any]) -> None:
        await self.db.set_state_json(STATE_KEY_MOOD, state)

    async def get_hours_since_last_chat(self) -> float:
        """获取距工作记忆最后一轮的小时数"""
        row = await self.db.fetchone("SELECT created_at FROM turns ORDER BY id DESC LIMIT 1")
        if not row or not row["created_at"]:
            return 0.0
        try:
            last_dt = datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M")
            delta_sec = (datetime.now() - last_dt).total_seconds()
            return max(0.0, delta_sec / 3600.0)
        except Exception:
            return 0.0

    async def update_mood(
        self,
        composite_affection: float,
        conv_v: float = 0.0,
        conv_a: float = 0.0,
        conv_trust: float = 0.0,
    ) -> Dict[str, Any]:
        """更新情绪状态：O-U均值回归、情感动量、安心度回归、冷落惩罚、对话冲击。"""
        state = await self.get_state()
        v = float(state.get("v", 2.0))
        a = float(state.get("a", 1.0))
        t = float(state.get("t", 7.0))
        m_v = float(state.get("momentum_v", 0.0))
        m_a = float(state.get("momentum_a", 0.0))
        frustration = float(state.get("frustration", 0.0))
        last_updated_str = state.get("last_updated", now_str())

        try:
            last_dt = datetime.strptime(last_updated_str, "%Y-%m-%d %H:%M")
            real_elapsed = max(0.0, (datetime.now() - last_dt).total_seconds() / 3600.0)
        except Exception:
            real_elapsed = 1.0

        # >=1 按实际，<1 按 1 参与回归计算
        elapsed = real_elapsed if real_elapsed >= 1.0 else 1.0
        hours_since_chat = await self.get_hours_since_last_chat()

        orig_v = v
        orig_a = a

        # 1. O-U 均值回归 + 噪声（θ = 0.12）
        baseline_v = 2.0 + min(2.0, composite_affection / 50.0)
        decay = 0.12 * elapsed
        noise_v = random.gauss(0, 0.5) * math.sqrt(min(decay, 2.0))
        noise_a = random.gauss(0, 0.4) * math.sqrt(min(decay, 2.0))

        v += (baseline_v - v) * decay + noise_v
        a += (1.0 - a) * decay + noise_a

        # 2. 情感动量（惯性）
        v += m_v * 0.3
        a += m_a * 0.3
        m_v = m_v * 0.8 + (v - orig_v) * 0.2
        m_a = m_a * 0.8 + (a - orig_a) * 0.2

        # 3. 安心度极慢回归基线（每天回归 5%）
        # 基线随关系阶段走：3.0 + 阶段×0.6（初识只有 3+ 的"小心翼翼"，深爱才到 8+ 的"完全安心"）
        stage = determine_stage(composite_affection)
        baseline_t = 3.0 + stage * 0.6
        t += (baseline_t - t) * 0.05 * (elapsed / 24.0)

        # 4. 冷落惩罚
        if hours_since_chat > 12.0:
            neglect = min(3.0, (hours_since_chat - 12.0) * 0.1)
            v -= neglect
            a -= neglect * 0.5
            t -= neglect * 0.02
            frustration = min(10.0, frustration + (hours_since_chat - 12.0) * 0.02)
        elif 0 < hours_since_chat < 1.0:
            v += 0.3
            t += 0.02
            frustration = max(0.0, frustration - 0.5)

        # 5. 对话冲击（观察者给出）
        v += conv_v
        a += conv_a
        t += conv_trust

        # 收尾 clamp：v,a in [-10, 10] 保留1位小数；t in [0, 10] 保留2位；frustration in [0, 10] 保留2位
        v = round(max(-10.0, min(10.0, v)), 1)
        a = round(max(-10.0, min(10.0, a)), 1)
        t = round(max(0.0, min(10.0, t)), 2)
        frustration = round(max(0.0, min(10.0, frustration)), 2)
        m_v = round(m_v, 2)
        m_a = round(m_a, 2)

        new_state = {
            "v": v,
            "a": a,
            "t": t,
            "momentum_v": m_v,
            "momentum_a": m_a,
            "frustration": frustration,
            "last_updated": now_str(),
        }
        await self.save_state(new_state)
        return new_state
