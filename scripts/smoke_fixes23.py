"""FIXES23 他打字她等 本机仿真冒烟 (scripts/smoke_fixes23.py)

单测能证明机制，但证明不了**真实时钟下的一整段对话长什么样**。本脚本在
真实 OneBotClient + 真实聚合器上按真实时间轴（真实 6/15/30 秒窗口）灌三段：

  A 有输入状态：他打字慢、句间停顿 7 秒（> 6 秒静默窗）。她等他说完 → 3 条合成一轮
  B 对照组：完全相同的时间轴，但**不发**输入状态事件 → 静默窗到点就插话 → 劈成 3 轮
  C 绝对上限：他打字/停手交替，中途第 17 秒又发一条。**没有输入状态的话
    这一条会当场撞上 15 秒硬上限被立刻冲出去**；有了输入状态则被抬到 30 秒
    绝对上限，到点强制合成一轮

B 段是 A 段的反向对照，缺了它 A 段"她等了"就可能只是因为代码压根没触发。
三段判定全 PASS 才算通过；报告里同时留 `turns`，空轮次要看得见。

全程零真实 API 调用、零服务器副作用、零生产库读写。
报告落 data/smoke_fixes23_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import companion.aggregator as aggmod
from companion.aggregator import MessageAggregator
from companion.config import OneBotConfig
from companion.onebot import OneBotClient

REPORT_FILE = "data/smoke_fixes23_report.json"
OWNER_QQ = 123456789

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def typing_event(event_type: int) -> Dict[str, Any]:
    """按 NapCat 真事件结构造一条输入状态 notice（字段名见 OB11InputStatusEvent.ts）。"""
    return {
        "time": 1759574400,
        "self_id": 10000,
        "post_type": "notice",
        "notice_type": "notify",
        "sub_type": "input_status",
        "status_text": "对方正在输入..." if event_type == 1 else "",
        "event_type": event_type,
        "user_id": OWNER_QQ,
        "group_id": 0,
    }


class _Rig:
    """真实 OneBotClient + 真实聚合器，只把 turn_handler 换成记录器。"""

    def __init__(self, tmp_dir: str) -> None:
        self.turns: List[Dict[str, Any]] = []
        self.t0 = 0.0

        async def handler(text, image_path, batch=None):
            self.turns.append(
                {
                    "at": round(self.now() - self.t0, 2),
                    "text": text,
                    "n": len(batch or []),
                }
            )

        self.agg = MessageAggregator(turn_handler=handler)
        self.agg.start()
        self.client = OneBotClient(
            config=OneBotConfig(),
            allowed_user_id=OWNER_QQ,
            on_message_callback=self._on_message,
            image_save_dir=tmp_dir,
            on_typing_callback=self.agg.notify_peer_typing,
        )
        self.client._running = True

    @staticmethod
    def now() -> float:
        return asyncio.get_event_loop().time()

    def arm(self) -> None:
        self.t0 = self.now()

    async def _on_message(self, text, image_path=None, message_id=None) -> None:
        await self.agg.push_message(text, image_path, message_id)

    async def he_says(self, text: str, mid: int) -> None:
        await self.client._handle_raw_message(
            json.dumps(
                {
                    "post_type": "message",
                    "message_type": "private",
                    "self_id": 10000,
                    "user_id": OWNER_QQ,
                    "message_id": mid,
                    "message": [{"type": "text", "data": {"text": text}}],
                    "raw_message": text,
                    "time": 1759574400,
                }
            )
        )

    async def he_types(self, event_type: int) -> None:
        """走真实报文入口，验的是解析 + 分发 + 联动整条链。"""
        await self.client._handle_raw_message(json.dumps(typing_event(event_type)))

    async def close(self) -> None:
        await self.client.stop()
        self.agg.stop()


async def scenario_a_with_typing() -> Dict[str, Any]:
    """A 段：有输入状态——他打字慢（第 1、2 条之间停 7 秒 > 6 秒静默窗），她等他说完合成一轮。

    时序：t=0 提示"正在输入"（真机常态：**提示先于**消息）+ 第 1 条 →
    t=7 第 2 条（此刻已越过静默窗，她本该插话了）→ t=8 他停手 →
    t=10 第 3 条 → 静默窗到点，**3 条合成一轮**。

    停手刻意排在 15 秒自愈**之前**：自愈一旦先落地，后面那条 typing=0 就成了空操作，
    "他已停手重新起静默窗"这条日志也就永远验不到。
    """
    with tempfile.TemporaryDirectory(prefix="fixes23_") as tmp:
        rig = _Rig(tmp)
        rig.arm()
        try:
            await rig.he_types(1)          # 对方开始输入
            await rig.he_says("我跟你说个事", 9001)
            await asyncio.sleep(7.0)
            await rig.he_says("今天那个会", 9002)   # 已越过静默窗：没输入状态就该插话了
            await asyncio.sleep(1.0)
            await rig.he_types(0)          # 对方停手 → 静默窗重新计时
            await asyncio.sleep(2.0)
            await rig.he_says("可能改时间了", 9003)
            await asyncio.sleep(aggmod.SILENCE_WINDOW + 3.0)
        finally:
            await rig.close()

    # 判据：必须合成**一轮**。若输入状态没起作用，第 1 条会在静默窗点被冲出去。
    ok = len(rig.turns) == 1 and rig.turns[0]["n"] == 3
    return {
        "name": "A 有输入状态：他打字她等，3 条合成一轮",
        "passed": ok,
        "turns": rig.turns,
        "criterion": (
            f"3 条消息合成 1 轮（第 2 条落在第 7 秒、已越过 {aggmod.SILENCE_WINDOW:.0f}s "
            f"静默窗仍未插话）"
        ),
    }


async def scenario_b_without_typing() -> Dict[str, Any]:
    """B 段：对照组——**完全相同**的消息时间轴但没有输入状态事件，必须照旧在静默窗插话。

    没有这条，A 段的"她等了"就可能只是因为代码压根没触发。
    预期被切成 2 轮：第 1 条独自成轮（t≈6 静默窗到点），第 2、3 条合成一轮。
    """
    with tempfile.TemporaryDirectory(prefix="fixes23_") as tmp:
        rig = _Rig(tmp)
        rig.arm()
        try:
            await rig.he_says("我跟你说个事", 9001)
            await asyncio.sleep(7.0)
            await rig.he_says("今天那个会", 9002)
            await asyncio.sleep(3.0)
            await rig.he_says("可能改时间了", 9003)
            await asyncio.sleep(aggmod.SILENCE_WINDOW + 3.0)
        finally:
            await rig.close()

    # 判据：对照组必须真的被切开（第 1 条独自成轮），否则 A 段是空断言
    ok = len(rig.turns) == 2 and rig.turns[0]["n"] == 1
    return {
        "name": "B 对照组：无输入状态，静默窗照常插话",
        "passed": ok,
        "turns": rig.turns,
        "criterion": (
            f"无输入状态事件时行为与改动前一致：第 1 条在 {aggmod.SILENCE_WINDOW:.0f}s "
            f"静默窗点被冲出去独自成轮，共 2 轮"
        ),
    }


async def scenario_c_stale_selfheal() -> Dict[str, Any]:
    """C 段：状态泄漏自愈——他发完就把手机放下了，**永远不发结束事件**。

    时序：t=0 提示"正在输入" + 第 1 条 → t=12 第 2 条（不再有任何输入状态事件）。
    协议层 15 秒自愈应当按"已停手"处理并放行；聚合器此时已等满 15 秒硬上限，立刻回。

    验两件事（缺一不可）：
      1. 她把这一整段话**攒住了**（第 2 条落在第 12 秒、无输入状态的话早被 6 秒静默窗冲出去了）
      2. 她**没有傻等到 30 秒**——结束事件不来也照样回话
    """
    with tempfile.TemporaryDirectory(prefix="fixes23_") as tmp:
        rig = _Rig(tmp)
        rig.arm()
        try:
            await rig.he_types(1)          # 对方开始输入
            await rig.he_says("我跟你说个事，特别长那种", 9001)
            await asyncio.sleep(12.0)
            await rig.he_says("可能改时间了", 9002)  # 之后他再没发过任何输入状态事件
            await asyncio.sleep(6.0)
        finally:
            await rig.close()

    flushed = bool(rig.turns)
    at = rig.turns[0]["at"] if flushed else -1.0
    ok = (
        flushed
        and len(rig.turns) == 1
        and rig.turns[0]["n"] == 2
        # 攒住了：明显晚于 6 秒静默窗
        and at > aggmod.SILENCE_WINDOW + 1.0
        # 没傻等：15 秒自愈一放行就得回，不许拖到 30 秒绝对上限
        and at <= aggmod.HARD_LIMIT + 2.0
    )
    return {
        "name": "C 状态泄漏自愈：结束事件不来也照样回话",
        "passed": ok,
        "turns": rig.turns,
        "criterion": (
            f"2 条合成 1 轮且在 {aggmod.HARD_LIMIT:.0f}s 硬上限附近回话"
            f"（既攒住了整段话，又没傻等到 {aggmod.TYPING_ABSOLUTE_LIMIT:.0f}s 绝对上限）"
        ),
    }


async def main() -> int:
    print("=" * 78)
    print("FIXES23 他打字她等 · 本机仿真冒烟（真实 OneBotClient + 真实聚合器，零真实 API）")
    print("=" * 78)
    print(
        f"窗口参数：静默 {aggmod.SILENCE_WINDOW:.0f}s / 硬上限 {aggmod.HARD_LIMIT:.0f}s "
        f"/ 打字绝对上限 {aggmod.TYPING_ABSOLUTE_LIMIT:.0f}s"
    )
    print("-" * 78)

    results = [
        await scenario_a_with_typing(),
        await scenario_b_without_typing(),
        await scenario_c_stale_selfheal(),
    ]

    for r in results:
        print(f"\n【{r['name']}】{'PASS' if r['passed'] else 'FAIL'}")
        print(f"  判据：{r['criterion']}")
        for t in r["turns"]:
            print(f"  t={t['at']:>5.2f}s  {t['n']} 条  {t['text']!r}")
        if not r["turns"]:
            print("  （没有任何一轮被提交——这本身就是判据失败，别当通过读）")

    passed = all(r["passed"] for r in results)
    report = {
        "generated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "window": {
            "silence_window": aggmod.SILENCE_WINDOW,
            "hard_limit": aggmod.HARD_LIMIT,
            "typing_absolute_limit": aggmod.TYPING_ABSOLUTE_LIMIT,
        },
        "results": results,
        "all_passed": passed,
        "design_note": (
            "30 秒打字绝对上限在真机上基本够不着：协议层 15 秒自愈与聚合器 15 秒硬上限"
            "几乎同时到点，轮次总在 ~15 秒关闭。它是冗余兜底（防自愈失效/事件不来的"
            "极端情况），不是生效路径。真机可见的效果是「她最多把整段话攒住约 15 秒」。"
        ),
        "real_event_capture": "未做——本机无运行中的 NapCat（它在服务器上，任务书禁止本轮碰服务器）。"
        "已列为部署日验证项，见 docs/DEPLOY.md 第 8.1 节。",
    }
    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print("\n" + "=" * 78)
    print(f"结论：{'全部通过' if passed else '存在失败项'}    报告：{REPORT_FILE}")
    print("=" * 78)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
