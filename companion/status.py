"""快捷查看青梓状态与好感度的 CLI 工具 (companion/status.py)
用法:
  python -m companion.status
"""

from __future__ import annotations

import json
import sqlite3
import sys
from companion.prompts import get_mood_description, get_mood_label, get_trust_description

STAGE_NAMES = {
    0: "初识 (礼貌克制 / 略带距离感 / 简短日常)",
    1: "熟络 (自然随和 / 互相调侃 / 偶尔分享心事)",
    2: "亲近 (深度共鸣 / 情绪流露 / 依赖与关切)",
    3: "深厚羁绊 (独特默契 / 无话不谈 / 彼此生命中的重要存在)",
}

DIM_NAMES = {
    "warmth": "温暖 (Warmth)",
    "trust": "信任 (Trust)",
    "intimacy": "亲密 (Intimacy)",
    "intrigue": "好奇 (Intrigue)",
    "patience": "包容 (Patience)",
    "tension": "紧张 (Tension)",
}


def render_bar(val: float, max_val: float = 100.0, width: int = 20) -> str:
    filled = int(round((val / max_val) * width))
    filled = max(0, min(width, filled))
    return f"[{'■' * filled}{' ' * (width - filled)}] {val:5.1f}"


def main() -> None:
    db_path = "data/companion.db"
    try:
        conn = sqlite3.connect(db_path)
        c = conn.cursor()
    except Exception as e:
        print(f"无法打开数据库 {db_path}: {e}")
        return

    print("=" * 62)
    print("           ✨ 青梓 (沈知予) 实时状态看板 ✨")
    print("=" * 62)

    # 1. 好感度
    c.execute("SELECT value FROM state WHERE key = 'affection'")
    row = c.fetchone()
    if row and row[0]:
        aff = json.loads(row[0])
        composite = float(aff.get("composite", 30.0))
        stage = int(aff.get("stage", 0))
        stage_desc = STAGE_NAMES.get(stage, f"阶段 {stage}")
        dims = aff.get("dims", {})
        print(f"\n【好感度阶段】: 阶段 {stage} - {stage_desc}")
        print(f"【复合好感分】: {composite:.1f} / 100.0")
        print("\n六维好感细分:")
        for k, label in DIM_NAMES.items():
            val = float(dims.get(k, 0.0))
            print(f"  {label:<18}: {render_bar(val)}")
    else:
        print("\n【好感度】: 暂无数据（默认初识阶段 30.0）")

    # 2. 情绪与心境
    c.execute("SELECT value FROM state WHERE key = 'mood'")
    row = c.fetchone()
    if row and row[0]:
        mood = json.loads(row[0])
        v = float(mood.get("v", 2.0))
        a = float(mood.get("a", 1.0))
        t = float(mood.get("t", 7.0))
        lbl = get_mood_label(v, a)
        desc = get_mood_description(v, a)
        trust_desc = get_trust_description(t)
        print(f"\n【当前心境】: {lbl}（{desc}）")
        print(f"【安心程度】: {t:.1f} / 10.0（{trust_desc}）")

    # 3. 统计概览
    c.execute("SELECT value FROM counters WHERE key = 'total_turns'")
    t_row = c.fetchone()
    total_turns = t_row[0] if t_row else 0

    c.execute("SELECT COUNT(*), SUM(cost_estimate) FROM llm_calls")
    cost_row = c.fetchone()
    calls = cost_row[0] if cost_row else 0
    total_cost = cost_row[1] if (cost_row and cost_row[1]) else 0.0

    c.execute("SELECT COUNT(*) FROM stickers")
    s_row = c.fetchone()
    stickers_cnt = s_row[0] if s_row else 0

    print(f"\n【统计指标】:")
    print(f"  • 累计对话轮次: {total_turns} 轮")
    print(f"  • 模型调用总计: {calls} 次 (累计开销: ¥{total_cost:.4f} 元)")
    print(f"  • 收藏表情包数: {stickers_cnt} 个")

    # 4. 记忆事实
    c.execute("SELECT content FROM facts ORDER BY id ASC")
    facts = [r[0] for r in c.fetchall()]
    print(f"\n【记住关于你的事实】({len(facts)} 条):")
    if facts:
        for f in facts:
            print(f"  - {f}")
    else:
        print("  (暂无，多聊聊天就会自动记住你的日常、习惯与喜好)")

    # 5. 最新日记
    c.execute("SELECT created_at, sentiment, content FROM diary ORDER BY id DESC LIMIT 2")
    diaries = c.fetchall()
    print(f"\n【最近日记】:")
    if diaries:
        for d in diaries:
            print(f"  📖 [{d[0]}] 情绪: {d[1]}")
            print(f"     \"{d[2]}\"")
    else:
        print("  (暂无日记，每晚 23:00 后会自动总结一天的相处心事)")

    print("\n" + "=" * 62)
    print("💡 提示: 也可以在浏览器中访问 http://localhost:8080 查看完整 Web 仪表盘")
    print("=" * 62 + "\n")


if __name__ == "__main__":
    main()
