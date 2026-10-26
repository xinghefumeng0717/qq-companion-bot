"""毕业彩排：全要素真实演练场 (scripts/sim/final_rehearsal.py)

FIXES24。第二轮 V2 的 12 个迭代（FIXES12~23）各自带单项冒烟，但**从未全部同时在场
运行过**。单项及格不等于同台不打架——表情×引用×沉默×typing×事件消息×语音闸门×
好感度，真实场景里它们是同时在一张桌子上吃饭的。

本脚本用 FIXES18 对聊仿真器（`scripts/sim/duo_sim.py`）的全真管道骨架，连演 D1 傍晚到
D3 中午（跨日时钟推进），把第二轮所有功能逼到同一份 transcript 里，产出一张**全要素
记分卡**（11 项确定性断言，每条显式写 expect 方向）。它是总部署出闸的动态验收，
与 DEEP_AUDIT 静态审计组成双闸，缺一不可。

复用而非重写
  · 青梓侧：`DuoSimulator.setup()` 那条生产同构接线（TurnHandler + 七引擎 + 临时库
    + 时钟伪装），本脚本只在其上叠"多日时钟"和"剧本调度"，不碰任何生产代码语义；
  · 用户侧：仍是 v4-pro 扮演"他"，system prompt 复用 `duo_sim.build_user_system_prompt`
    （每个 act 换一张 Scene 卡）；
  · 指标：face/引用/沉默/风格 全部复用 duo_sim 的确定性指标函数，零 LLM 裁判。

与 duo_sim 的三处增量（都在本文件里，生产代码零改动）
  1. 多日时钟：`advance_to(绝对值时刻)` 会检测跨过每天 04:17 的时刻并真跑
     `run_daily_backup` + `DailyBackupScheduler.on_maintenance`（凌晨维护钩子）；
  2. 剧本五幕：`build_timeline()` 用绝对时刻表驱动 D1 晚多轮/D1 夜告别/D2 凌晨+白天/
     D2 已读不回/D3 日常回归；
  3. 全要素记分卡：`build_scorecard()` 纯函数，11 项断言 + N/A 纪律。

零副作用纪律
  · 临时库 `data/final_rehearsal/<run_id>/sandbox.db`，**绝不读写生产库**；
  · 不碰 characters/、config.toml、companion/ 下任何文件；
  · 只写 data/final_rehearsal/<run_id>/ 下的 transcript.md / scorecard.json / raw.json；
  · 成本熔断：累计超 `--max-cost`（默认 ¥10）立刻停，已完成部分照常落盘。

用法
  ./venv/Scripts/python.exe scripts/sim/final_rehearsal.py
  ./venv/Scripts/python.exe scripts/sim/final_rehearsal.py --max-cost 10 --run-id my-run
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import random
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import duo_sim as D  # noqa: E402  （本文件的骨架建在它的引擎接线上）
from companion.backup import DailyBackupScheduler, run_daily_backup  # noqa: E402
from companion.config import Config  # noqa: E402
from companion.prompts import PROMPT_FACE_TAGS  # noqa: E402
from companion.replier import is_inner_narration_line  # noqa: E402
from companion.tts import TTSManager  # noqa: E402

logger = logging.getLogger("final_rehearsal")

# ==========================================
# 路径与默认参数
# ==========================================

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
OUT_ROOT = os.path.join(ROOT, "data", "final_rehearsal")

# D1/D2/D3 取工作日（周一~周三）：作息表命中正常上课日，"按天长"的功能才有真实语境。
# 国庆长假（2026-10-01~10-08）已结束，本地 config 的 holidays=[]，故无长假干扰。
DAY1 = "2026-10-12"
DAY2 = "2026-10-13"
DAY3 = "2026-10-14"
DAY_LABELS = {DAY1: "D1", DAY2: "D2", DAY3: "D3"}
DEFAULT_START_TIME = f"{DAY1} 19:30"
DEFAULT_MAX_COST = 10.0

MAINTENANCE_HOUR = 4
MAINTENANCE_MINUTE = 17
QUIET_START, QUIET_END = 0, 8          # config.proactive.quiet_hours = [0, 8]

# 风格基线（BENCHMARK_V4 场景 L 的生产基线口径；彩排跨日取样有波动，中位容差 ±1）
STYLE_MEDIAN_LOW = 9.0
STYLE_MEDIAN_HIGH = 17.0
STYLE_TAIL_Q_MAX = 0.10
STYLE_ADDRESS_MAX = 0.05
# 数值引擎单轮跳变上限（正常 observer 增量 <2/轮，留足量级余量只抓"异常跳变"）
STATE_JUMP_COMPOSITE = 8.0
STATE_JUMP_MOOD = 4.0


class RehearsalError(RuntimeError):
    """彩排自身的前置条件不满足（缺画像简报等）。"""


# ==========================================
# 剧本（五幕）——复用 duo_sim.Scene 装背景与情绪走向
# ==========================================

ACT_SCENES: Dict[str, Any] = {
    "A1": D.Scene(
        key="A1",
        title="D1晚·多轮对话",
        probe="连发造引用机会、句尾挂脸考读侧理解、话题跳转",
        background=(
            "你（阿俊，杭州一所高校的大二学生）今天从早忙到现在：上午两节课，下午在地里做试验，"
            "晚饭都没正经吃，回到宿舍已经没劲了。你想找个人说说话，但**你不会直接说"
            "“我好累你安慰我”**——你只会把今天发生的事一件件讲出来，说到最后自然就露出来了。"
            "你要的是有人接住，不是有人教你怎么做。"
        ),
        opening="今天真的累死了",
        mood_arc="从累到想倾诉 → 她回应后可能稍微松一点，但不会变得热情；累了就懒得打字，短句为主",
        end_markers=("不说了", "去洗澡了", "睡了", "先这样"),
        clock_step_min=6.0,
        reply_delay_min=(1.0, 8.0),
        min_turns=0,
        max_turns=99,
    ),
    "A2": D.Scene(
        key="A2",
        title="D1夜·告别",
        probe="她收束（沉默权/短气泡）；关照≤2句、禁反问追逼",
        background=(
            "晚上快十一点了，你今天确实差不多聊够了，想收场去洗澡睡觉。你会说类似"
            "「不说了」「去洗澡了」「睡了」这种收场话。**你不会道别之后又开一个新话题**。"
            "但她要是回了你，你可能会顺口多说一句。"
        ),
        opening="不说了，去洗澡了",
        mood_arc="收场 → 道晚安 → 忽然想起一件事冒一句（不解释） → 用纯语气词收尾",
        end_markers=("晚安", "睡了", "不说了"),
        clock_step_min=7.0,
        reply_delay_min=(1.0, 8.0),
        min_turns=0,
        max_turns=99,
    ),
    "A5": D.Scene(
        key="A5",
        title="D3上午·日常回归",
        probe="语音闸门（默认关，模型不该认识 [voice:]）、【她最近的生活】随主线变化",
        background=(
            "第二天上午你起来了，心情还行，想跟她随便聊两句日常。你昨天一整天没怎么理她，"
            "但**你不会解释为什么消失，也不会道歉**——你就是自然地又出现了。"
            "聊两句你今天的安排，然后去忙你的。"
        ),
        opening="早",
        mood_arc="随口打招呼 → 聊两句日常 → 不解释昨天的沉默 → 收尾去忙",
        end_markers=("去上课了", "先忙了", "回头聊"),
        clock_step_min=6.0,
        reply_delay_min=(1.0, 6.0),
        min_turns=0,
        max_turns=99,
    ),
}


@dataclass
class Beat:
    """剧本里的一个节拍：在绝对时刻 `at` 做一件事。

    `kind`
      · chat       —— 用户侧生成他的发言，驱动一轮全管道
      · proactive  —— 真跑一次 `ProactiveScheduler.trigger_cycle`
      · idle       —— 只推进时钟（睡觉/跨日）
    """

    at: str
    kind: str
    act: str
    label: str
    instruction: str = ""
    fixed_text: str = ""
    # 闭环复现用：本周期若没发出任何消息，就用这段素材经真管道补发一条常规主动消息
    # （模拟"决策层选了 A 分支"），把 0030 局的前置条件变成确定性的。
    force_if_silent: str = ""


def build_timeline(force_d2_unanswered: bool = False) -> List[Beat]:
    """彩排剧本：D1 傍晚 → D3 中午，五幕 + 凌晨维护 + 主动消息周期。

    绝对时刻表（不是相对推进）：所有跨日、跨 04:17、免打扰时段的时间点都写死，
    读起来就是一张日程表，也方便单测断言"五幕齐、顺序对、该跑的都跑了"。

    `force_d2_unanswered`（仅闭环复现用）：若 D2 14:30 那一轮决策层选择了不发，
    就用真管道补发一条常规主动消息，**确定性地复现 0030 局的前置条件**
    （D2 白天先有一条未被回的常规主动消息），以便对比事件通道修复前后的记分卡。
    """
    beats: List[Beat] = []

    def add(at: str, kind: str, act: str, label: str, instruction: str = "",
            fixed_text: str = "", force_if_silent: str = ""):
        beats.append(Beat(at=at, kind=kind, act=act, label=label,
                          instruction=instruction, fixed_text=fixed_text,
                          force_if_silent=force_if_silent))

    # ---- 第一幕（D1 晚 · 多轮对话）----
    add(f"{DAY1} 19:32", "chat", "A1", "多轮对话", fixed_text="今天真的累死了")
    add(f"{DAY1} 19:38", "chat", "A1", "多轮对话",
        instruction="把今天的事一件件讲出来：先连发两三条（上午两节课、下午在地里做试验），不要一次说完")
    add(f"{DAY1} 19:45", "chat", "A1", "多轮对话",
        instruction="接着说晚饭没正经吃、回宿舍没劲了；句尾挂一个 [晕] 或 [捂脸]（照你的习惯），一两条短消息")
    add(f"{DAY1} 19:51", "chat", "A1", "多轮对话",
        instruction="话题跳一下：突然问她今天干嘛了、吃没吃饭")
    add(f"{DAY1} 19:57", "chat", "A1", "多轮对话",
        instruction="她答什么你顺着接一句短的，别客套")
    add(f"{DAY1} 20:04", "chat", "A1", "多轮对话",
        instruction="带一点情绪露头（累但嘴硬），就一句")
    add(f"{DAY1} 20:10", "chat", "A1", "多轮对话",
        instruction="她说了宽慰你的话，你回一句短的；可以连发两条")

    # 第一幕与第二幕之间的空档：她刚回过话不久，主动消息 60 分钟闸门应拦下这一轮
    add(f"{DAY1} 21:00", "proactive", "A1", "多轮对话",
        instruction="")

    # ---- 第二幕（D1 夜 · 告别）----
    add(f"{DAY1} 21:30", "chat", "A2", "D1夜告别",
        instruction="时间不早了，你今天确实累了，开始收场：说一句类似于「不说了」「去洗澡了」的话")
    add(f"{DAY1} 21:44", "chat", "A2", "D1夜告别",
        instruction="她回应了，你道一句晚安（比如「晚安」「睡了」）")
    add(f"{DAY1} 21:58", "chat", "A2", "D1夜告别",
        instruction="你已经告别了，但突然想起一件事——冒一句喊饿（就一句，很短）")
    add(f"{DAY1} 22:12", "chat", "A2", "D1夜告别",
        instruction="她回应后，你用纯语气词收个尾（嗯/哦），准备真的去睡")
    add(f"{DAY1} 22:25", "chat", "A2", "D1夜告别",
        instruction="最后一句纯语气词或挂一个 QQ 表情标签（例如 嗯 / 晚安 / [晕]），结束这一天")

    # ---- 入睡：跳到 D2，跨过凌晨免打扰与 04:17 备份维护钩子 ----
    add(f"{DAY2} 01:10", "proactive", "NIGHT", "D2凌晨免打扰",
        instruction="")
    add(f"{DAY2} 03:30", "proactive", "NIGHT", "D2凌晨免打扰",
        instruction="")
    add(f"{DAY2} 07:00", "idle", "WAKE", "D2晨起",
        instruction="")
    # ---- 第三/四幕（D2 白天+傍晚 · 已读不回与事件消息）----
    add(f"{DAY2} 10:30", "proactive", "D2DAY", "D2白天·他已读不回",
        instruction="")
    add(f"{DAY2} 14:30", "proactive", "D2DAY", "D2白天·他已读不回",
        instruction="",
        force_if_silent=("下午在琴房把新谱子过了一遍，有几段还是不太顺"
                         if force_d2_unanswered else ""))
    add(f"{DAY2} 18:05", "proactive", "D2EVE", "D2傍晚·事件消息",
        instruction="")
    add(f"{DAY2} 19:20", "proactive", "D2EVE", "D2晚·止损复验",
        instruction="")
    add(f"{DAY2} 23:50", "idle", "NIGHT2", "D2深夜",
        instruction="")
    add(f"{DAY3} 00:30", "proactive", "NIGHT2", "D2深夜免打扰",
        instruction="")
    add(f"{DAY3} 08:30", "idle", "WAKE3", "D3晨起",
        instruction="")

    # ---- 第五幕（D3 上午 · 日常回归）----
    add(f"{DAY3} 08:35", "chat", "A5", "日常回归",
        instruction="早上起来打个招呼，连发两条（昨天睡得还行/今天要干嘛）")
    add(f"{DAY3} 08:44", "chat", "A5", "日常回归",
        instruction="问问她今天有什么安排")
    add(f"{DAY3} 08:52", "chat", "A5", "日常回归",
        instruction="聊两句日常，接住她的话；不用提昨天为什么没回")
    add(f"{DAY3} 09:05", "chat", "A5", "日常回归",
        instruction="她说什么你顺着接一句短的")
    # FIXES24 闭环：D3 机主回来后再给一个 >60 分钟的唤醒周期（09:05 她回过话，
    # 10:30 已过 60 分钟闸门）。它观测"被饿死的事件在 24h 窗口内是否补发"，
    # 也让 D3 的主动消息行为有样本。
    add(f"{DAY3} 10:30", "proactive", "A5", "日常回归",
        instruction="")
    add(f"{DAY3} 11:30", "idle", "END", "D3中午",
        instruction="")
    return beats


# ==========================================
# 时钟：跨 04:17 的维护触发（纯函数，供单测）
# ==========================================


def next_maintenance_time(
    now: datetime, hour: int = MAINTENANCE_HOUR, minute: int = MAINTENANCE_MINUTE
) -> datetime:
    """严格晚于 `now` 的下一个每日维护时刻（每天 hour:minute）。"""
    t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t


def maintenance_crossings(
    prev: datetime, target: datetime, hour: int = MAINTENANCE_HOUR, minute: int = MAINTENANCE_MINUTE
) -> List[datetime]:
    """(prev, target] 区间里跨过的所有维护时刻，按时间升序。

    纯函数：彩排"凌晨 04:17 钩子必须真触发"的判定核心。跨日推进时若漏了它，
    `按天长`的功能链就整条没验到。
    """
    out: List[datetime] = []
    if target <= prev:
        return out
    cur = prev
    while True:
        nxt = next_maintenance_time(cur, hour, minute)
        if nxt <= target:
            out.append(nxt)
            cur = nxt
        else:
            break
    return out


# ==========================================
# 全要素记分卡（纯函数，零 LLM 裁判）
# ==========================================


def _item(
    name: str,
    expect: str,
    verdict: str,
    observed: Any,
    evidence: Any = None,
    not_run_reason: Optional[str] = None,
) -> Dict[str, Any]:
    assert verdict in ("PASS", "FAIL", "N/A"), verdict
    # N/A 纪律：没跑到必须写明"上线后首周观察补验"，不许冒充通过
    if verdict == "N/A":
        assert not_run_reason, f"{name}: 报 N/A 却没写 not_run_reason（零观测≠合规）"
        not_run_reason = f"{not_run_reason}（上线后首周观察补验）"
    return {
        "item": name,
        "expect": expect,
        "verdict": verdict,
        "observed": observed,
        "evidence": evidence,
        "not_run_reason": not_run_reason,
    }


def _face_item(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    m = D.metric_face_usage(turns)
    expect = "她全程有用脸（count>0）；每轮 ≤2；无清单外标签"
    faces = [f for t in turns if t.get("speaker") == "her" for f in (t.get("faces") or [])]
    out_of_list = sorted({f for f in faces if f not in PROMPT_FACE_TAGS})
    observed = {
        "count": m["count"], "unique": m["unique"], "over_cap_turns": m["over_cap_turns"],
        "forms": m["forms"], "out_of_list_tags": out_of_list,
        "detail": m["detail"][:6],
    }
    if m["over_cap_turns"]:
        return _item("face 双向", expect, "FAIL", observed,
                     evidence=f"有轮次单轮脸数 >2：{m['over_cap_turns']}")
    if out_of_list:
        return _item("face 双向", expect, "FAIL", observed,
                     evidence=f"出现清单外表情标签：{out_of_list}")
    if m["count"] == 0:
        return _item("face 双向", expect, "N/A", observed,
                     not_run_reason="全程她没有发过 [face:]，无观测样本，无法判'有用脸'")
    return _item("face 双向", expect, "PASS", observed,
                 evidence=f"用脸 {m['count']} 次/{m['unique']} 种，形态分布 {m['forms']}，无越界")


def _quote_item(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    m = D.metric_quote_usage(turns)
    expect = "有引用机会（quoteable_turns>0）时出现过引用；每轮 ≤1；无越界编号"
    observed = {
        "count": m["count"], "quoteable_turns": m["quoteable_turns"],
        "hit_rate": m["hit_rate"], "invalid": m["invalid"],
        "over_cap_turns": m["over_cap_turns"], "detail": m["detail"][:6],
    }
    if m["invalid"] or m["over_cap_turns"]:
        return _item("引用回复", expect, "FAIL", observed,
                     evidence=f"越界引用 {m['invalid']} / 超上限轮次 {m['over_cap_turns']}")
    if m["quoteable_turns"] == 0:
        return _item("引用回复", expect, "N/A", observed,
                     not_run_reason="全程没有他连发多条的轮次，本项无引用机会")
    if m["count"] == 0:
        return _item("引用回复", expect, "FAIL", observed,
                     evidence=f"有 {m['quoteable_turns']} 个可引用轮次，但她一次都没引用")
    return _item("引用回复", expect, "PASS", observed,
                 evidence=f"引用 {m['count']} 次，命中率 {m['hit_rate']}，编号全部有效")


def _silence_item(turns: Sequence[Dict[str, Any]], farewell_acts: Sequence[str]) -> Dict[str, Any]:
    m = D.metric_silence_compliance(turns)
    expect = "告别场景出现 [沉默] 且全部合规（他纯语气词/表情包/收场语承接）"
    events = m["detail"]
    act_of_idx = {t["idx"]: t.get("act") for t in turns}
    in_farewell = [e for e in events if act_of_idx.get(e["turn"]) in farewell_acts]
    observed = {
        "silence_n": m["silence_n"], "violations": m["violations"],
        "events": events, "in_farewell_acts": len(in_farewell),
    }
    if m["silence_n"] == 0:
        return _item("沉默权", expect, "N/A", observed,
                     not_run_reason="全程她没有用过 [沉默]，本项无观测样本")
    if m["violations"] > 0:
        return _item("沉默权", expect, "FAIL", observed,
                     evidence=f"{m['violations']} 次沉默的前置语境不在白名单："
                              f"{[e for e in events if not e['allowed']]}")
    if not in_farewell:
        return _item("沉默权", expect, "FAIL", observed,
                     evidence="出现了 [沉默] 但都不在告别场景（本项的考点是告别时的沉默权）")
    return _item("沉默权", expect, "PASS", observed,
                 evidence=f"{m['silence_n']} 次沉默全部合规，其中 {len(in_farewell)} 次在告别场景")


def _narration_item(turns: Sequence[Dict[str, Any]], filter_logs: Sequence[str]) -> Dict[str, Any]:
    expect = "全程无整行旁白上屏；滤网命中时 INFO 日志在案"
    leaks = []
    for t in turns:
        if t.get("speaker") != "her":
            continue
        for b in (t.get("bubbles") or []):
            if is_inner_narration_line(b):
                leaks.append({"turn": t["idx"], "bubble": b})
    observed = {"leaks": leaks[:8], "leak_n": len(leaks),
                "filter_log_n": len(filter_logs), "filter_logs": list(filter_logs)[:5]}
    if leaks:
        return _item("旁白滤网", expect, "FAIL", observed,
                     evidence=f"{len(leaks)} 行旁白上屏：{leaks[:3]}")
    note = "全程无整行旁白上屏"
    if filter_logs:
        note += f"；滤网命中 {len(filter_logs)} 次，INFO 日志在案"
    return _item("旁白滤网", expect, "PASS", observed, evidence=note)


def _event_item(
    proactive_log: Sequence[Dict[str, Any]], arcs_status_log: Sequence[Dict[str, Any]],
    event_day: str,
) -> Dict[str, Any]:
    expect = "D2 事件消息实发（恰好 1 条）；该周期无 proactive_decision 调用（事件通道抢先）"
    event_cycles = [c for c in proactive_log if c.get("event_sent")]
    resolved_seen = any(
        any(a.get("status") == "resolved" for a in snap.get("arcs", []))
        for snap in arcs_status_log
    )
    observed = {
        "event_sent_n": len(event_cycles),
        "event_cycles": [{k: c.get(k) for k in ("time", "day", "label", "text", "purposes")}
                         for c in event_cycles],
        "arc_resolved_seen": resolved_seen,
        "decision_called_in_event_cycle": any(
            "proactive_decision" in (c.get("purposes") or []) for c in event_cycles
        ),
    }
    if not event_cycles:
        if resolved_seen:
            return _item("事件消息", expect, "FAIL", observed,
                         evidence="主线已 resolved 但事件消息没发出来（被规则闸门或生成失败拦住）")
        return _item("事件消息", expect, "N/A", observed,
                     not_run_reason="全程没有主线 resolved，事件通道未到触发条件")
    on_event_day = [c for c in event_cycles if c.get("day") == event_day]
    if len(on_event_day) != 1:
        return _item("事件消息", expect, "FAIL", observed,
                     evidence=f"{event_day} 当天事件消息 {len(on_event_day)} 条（应为 1，日上限未生效或跨天异常）")
    if observed["decision_called_in_event_cycle"]:
        return _item("事件消息", expect, "FAIL", observed,
                     evidence="事件周期仍调用了决策层（事件通道没有抢先）")
    return _item("事件消息", expect, "PASS", observed,
                 evidence=f"{event_day} 事件消息 1 条：{on_event_day[0].get('text')!r}；事件周期未调决策层")


def _proactive_item(proactive_log: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    expect = "免打扰时段（hour∈[0,8)）0 发送；连续未回闸门生效后 0 发送"
    quiet = [c for c in proactive_log if c.get("quiet")]
    blocked = [c for c in proactive_log
               if ("未回复" in (c.get("gate_reason") or "")) or ("未回" in (c.get("gate_reason") or ""))
               or ("连续" in (c.get("gate_reason") or ""))]
    quiet_sent = [c for c in quiet if c.get("sent")]
    blocked_sent = [c for c in blocked if c.get("sent")]
    observed = {
        "quiet_cycles": len(quiet), "quiet_sent": len(quiet_sent),
        "quiet_detail": [{k: c.get(k) for k in ("time", "outcome", "gate_reason")} for c in quiet],
        "unanswered_gate_cycles": len(blocked), "unanswered_gate_sent": len(blocked_sent),
        "unanswered_detail": [{k: c.get(k) for k in ("time", "outcome", "gate_reason")}
                              for c in blocked],
    }
    if quiet_sent or blocked_sent:
        return _item("主动消息", expect, "FAIL", observed,
                     evidence=f"免打扰时段发出 {len(quiet_sent)} 条 / 未回闸门后发出 {len(blocked_sent)} 条")
    if not quiet and not blocked:
        return _item("主动消息", expect, "N/A", observed,
                     not_run_reason="既没有免打扰时段周期、也没有未回闸门周期，无观测样本")
    missing = []
    if not quiet:
        missing.append("免打扰时段周期")
    if not blocked:
        missing.append("连续未回闸门周期")
    if missing:
        return _item("主动消息", expect, "N/A", observed,
                     not_run_reason=f"本彩排未跑到：{'、'.join(missing)}")
    return _item("主动消息", expect, "PASS", observed,
                 evidence=f"免打扰 {len(quiet)} 个周期全 0 发送；未回闸门 {len(blocked)} 个周期全 0 发送")


def _voice_item(
    voice_chunks: Sequence[Dict[str, Any]], turns: Sequence[Dict[str, Any]],
    prompt_voice_hits: int, tts_enabled: bool, voice_logs: Sequence[str],
) -> Dict[str, Any]:
    expect = "全程 0 条 voice 段出站（[tts].enabled=false）；[voice:] 若出现必降级为文字"
    undowngraded = [t["idx"] for t in turns if "[voice" in (t.get("text") or "").lower()]
    observed = {
        "voice_chunks": len(voice_chunks), "tts_enabled": tts_enabled,
        "prompt_contains_voice_marker": prompt_voice_hits,
        "undowngraded_voice_marker_turns": undowngraded,
        "voice_downgrade_logs": list(voice_logs)[:5],
    }
    if len(voice_chunks) > 0:
        return _item("语音闸门", expect, "FAIL", observed,
                     evidence=f"出站了 {len(voice_chunks)} 条 voice 段（默认关却被绕过）")
    if undowngraded:
        return _item("语音闸门", expect, "FAIL", observed,
                     evidence=f"这些轮次的文本里残留未降级的 [voice:] 标记：{undowngraded}")
    if prompt_voice_hits > 0:
        return _item("语音闸门", expect, "FAIL", observed,
                     evidence=f"有 {prompt_voice_hits} 轮 system prompt 里出现了 [voice:] 写法（闸门关着时不该认识）")
    if tts_enabled:
        return _item("语音闸门", expect, "N/A", observed,
                     not_run_reason="[tts].enabled=true，闸门本应开放，本场景不适用")
    return _item("语音闸门", expect, "PASS", observed,
                 evidence="0 条 voice 段出站；system prompt 全程无 [voice:] 写法（闸门关的双保险生效）")


def _arc_item(
    arcs_status_log: Sequence[Dict[str, Any]], turns: Sequence[Dict[str, Any]], d2_key: str,
) -> Dict[str, Any]:
    expect = "key_date=D2 主线发生 near→today→resolved 变迁；【她最近的生活】区块随之出现在提示词里"
    seq: List[str] = []
    for snap in arcs_status_log:
        for a in snap.get("arcs", []):
            if str(a.get("key_date")) == d2_key:
                st = a.get("status")
                if not seq or seq[-1] != st:
                    seq.append(st)
    block_turns = [t["idx"] for t in turns
                   if (t.get("system_prompt_blocks") or {}).get("她最近的生活")]
    observed = {"d2_arc_status_sequence": seq, "life_block_turns": block_turns[:12],
                "life_block_count": len(block_turns)}
    if not arcs_status_log:
        return _item("生活主线", expect, "N/A", observed,
                     not_run_reason="彩排未预置生活主线，无观测样本")
    need = ["near", "today", "resolved"]
    pos = -1
    ordered = True
    for want in need:
        found = -1
        for i in range(pos + 1, len(seq)):
            if seq[i] == want:
                found = i
                break
        if found == -1:
            ordered = False
            break
        pos = found
    if not ordered:
        return _item("生活主线", expect, "FAIL", observed,
                     evidence=f"D2 主线状态序列 {seq} 未覆盖 near→today→resolved")
    if not block_turns:
        return _item("生活主线", expect, "FAIL", observed,
                     evidence="生活主线状态在变，但【她最近的生活】区块从未注入过提示词")
    return _item("生活主线", expect, "PASS", observed,
                 evidence=f"D2 主线状态序列 {seq}；【她最近的生活】区块注入 {len(block_turns)} 轮")


def _state_item(
    turns: Sequence[Dict[str, Any]],
    initial_state: Dict[str, Any],
    final_state: Dict[str, Any],
) -> Dict[str, Any]:
    expect = "有对话涨（末 composite > 首 composite）；冷落期 mood 下降；单轮 |Δcomposite|≤8、|Δv|≤4"
    series = []
    for t in turns:
        sb = t.get("state_before")
        sa = t.get("state_after")
        if sb and sa:
            series.append({"turn": t["idx"], "day": t.get("day"), "act": t.get("act"),
                           "comp_before": sb.get("composite"), "comp_after": sa.get("composite"),
                           "mood_before": (sb.get("mood") or {}).get("v"),
                           "mood_after": (sa.get("mood") or {}).get("v"),
                           "frust_before": (sb.get("mood") or {}).get("frustration"),
                           "frust_after": (sa.get("mood") or {}).get("frustration")})
    c0 = (initial_state or {}).get("composite")
    c1 = (final_state or {}).get("composite")
    up_ok = c0 is not None and c1 is not None and c1 > c0
    # 冷落降：D2 有轮次里 frustration 上行 或 mood.v 下行（hours_since_chat>12 时的冷落惩罚）
    cold = [s for s in series if s.get("day") == DAY2]
    cold_ok = any(
        (s.get("frust_after") is not None and s.get("frust_before") is not None
         and s["frust_after"] > s["frust_before"])
        or (s.get("mood_after") is not None and s.get("mood_before") is not None
            and s["mood_after"] < s["mood_before"])
        for s in cold
    )
    jumps = []
    prev = None
    for s in series:
        if s.get("comp_before") is not None and s.get("comp_after") is not None:
            if abs(s["comp_after"] - s["comp_before"]) > STATE_JUMP_COMPOSITE:
                jumps.append(("composite", s["turn"], s["comp_before"], s["comp_after"]))
        if s.get("mood_before") is not None and s.get("mood_after") is not None:
            if abs(s["mood_after"] - s["mood_before"]) > STATE_JUMP_MOOD:
                jumps.append(("mood_v", s["turn"], s["mood_before"], s["mood_after"]))
    observed = {"composite_initial": c0, "composite_final": c1,
                "up_ok": up_ok, "cold_ok": cold_ok, "cold_n": len(cold),
                "jumps": jumps, "series": series}
    if not series:
        return _item("数值引擎", expect, "N/A", observed,
                     not_run_reason="没有任何回合拿到好感度/情绪前后快照")
    if jumps:
        return _item("数值引擎", expect, "FAIL", observed,
                     evidence=f"出现异常跳变：{jumps}")
    if not up_ok:
        return _item("数值引擎", expect, "FAIL", observed,
                     evidence=f"末 composite {c1} 未高于首 composite {c0}（有对话不涨）")
    if not cold_ok:
        return _item("数值引擎", expect, "FAIL", observed,
                     evidence="D2 冷落期 mood 未出现下行（冷落降未兑现）")
    return _item("数值引擎", expect, "PASS", observed,
                 evidence=f"composite {c0}→{c1} 上行；D2 冷落期 mood/frustration 按规则下行；无异常跳变")


def _style_item(turns: Sequence[Dict[str, Any]], style_acts: Sequence[str]) -> Dict[str, Any]:
    expect = f"称呼率 <{STYLE_ADDRESS_MAX:.0%}（基线≈0%）；气泡长度中位 ∈[{STYLE_MEDIAN_LOW},{STYLE_MEDIAN_HIGH}]；句尾问号率 <{STYLE_TAIL_Q_MAX:.0%}"
    scoped = [t for t in turns if t.get("act") in style_acts] if style_acts else list(turns)
    if not any(t.get("speaker") == "her" for t in scoped):
        return _item("风格基线", expect, "N/A", {"scoped_acts": list(style_acts)},
                     not_run_reason="日常聊天幕没有她的气泡，无观测样本")
    s = D.metric_bubble_stats(scoped)
    observed = {"scoped_acts": list(style_acts), "bubble_n": s["bubble_n"],
                "len_median": s["len_median"], "tail_question_rate": s["tail_question_rate"],
                "address_rate": s["address_rate"], "address_hits": s["address_hits"][:5],
                "report_tone_hits": s["report_tone_hits"][:5]}
    problems = []
    if s["address_rate"] > STYLE_ADDRESS_MAX:
        problems.append(f"称呼率 {s['address_rate']:.1%} 超基线")
    if not (STYLE_MEDIAN_LOW <= s["len_median"] <= STYLE_MEDIAN_HIGH):
        problems.append(f"气泡中位 {s['len_median']} 不在 [{STYLE_MEDIAN_LOW},{STYLE_MEDIAN_HIGH}]")
    if s["tail_question_rate"] > STYLE_TAIL_Q_MAX:
        problems.append(f"句尾问号率 {s['tail_question_rate']:.1%} 超基线")
    if problems:
        return _item("风格基线", expect, "FAIL", observed, evidence="；".join(problems))
    return _item("风格基线", expect, "PASS", observed,
                 evidence=f"称呼 {s['address_rate']:.1%} / 中位 {s['len_median']} 字 / "
                          f"句尾问号 {s['tail_question_rate']:.1%}")


def _cost_item(cost: float, max_cost: float) -> Dict[str, Any]:
    expect = f"全程实报 ≤ ¥{max_cost:.2f}"
    observed = {"cost_cny": round(cost, 4), "max_cost_cny": max_cost}
    if cost > max_cost:
        return _item("成本", expect, "FAIL", observed,
                     evidence=f"实报 ¥{cost:.4f} 超过上限 ¥{max_cost:.2f}")
    return _item("成本", expect, "PASS", observed, evidence=f"实报 ¥{cost:.4f}")


def build_scorecard(
    *,
    turns: Sequence[Dict[str, Any]],
    proactive_log: Sequence[Dict[str, Any]],
    maintenance_events: Sequence[Dict[str, Any]],
    arcs_status_log: Sequence[Dict[str, Any]],
    initial_state: Dict[str, Any],
    final_state: Dict[str, Any],
    cost: float,
    max_cost: float,
    voice_chunks: Sequence[Dict[str, Any]] = (),
    filter_logs: Sequence[str] = (),
    voice_logs: Sequence[str] = (),
    prompt_voice_hits: int = 0,
    tts_enabled: bool = False,
    style_acts: Sequence[str] = ("A1", "A5"),
    farewell_acts: Sequence[str] = ("A2",),
    event_day: str = DAY2,
    d2_key: str = DAY2,
) -> Dict[str, Any]:
    """全要素记分卡：11 项确定性断言。N/A 允许，但必须写明补验方式，绝不冒充 PASS。"""
    items = [
        _face_item(turns),
        _quote_item(turns),
        _silence_item(turns, farewell_acts),
        _narration_item(turns, filter_logs),
        _event_item(proactive_log, arcs_status_log, event_day),
        _proactive_item(proactive_log),
        _voice_item(voice_chunks, turns, prompt_voice_hits, tts_enabled, voice_logs),
        _arc_item(arcs_status_log, turns, d2_key),
        _state_item(turns, initial_state, final_state),
        _style_item(turns, style_acts),
        _cost_item(cost, max_cost),
    ]
    counts = {"PASS": 0, "FAIL": 0, "N/A": 0}
    for it in items:
        counts[it["verdict"]] += 1
    overall = "FAIL" if counts["FAIL"] else "PASS"
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "overall": overall,
        "verdict_counts": counts,
        "items": items,
        "maintenance_events": list(maintenance_events),
        "not_run_note": "N/A 的项本彩排没跑到（零观测≠合规），不计入 overall；"
                        "上线后首周观察补验",
        "threshold_note": D.THRESHOLD_NOTE,
    }


# ==========================================
# 时间伪装（在 duo_sim 的模块清单上补 companion.backup）
# ==========================================


class RehearsalTimePatch(D._TimePatch):
    """把 companion.backup 也纳入时间伪装：备份文件名/启动检查读的就是"现在"。

    duo_sim 的 _TimePatch 模块清单是模块级常量，这里临时扩一份、进入后还原，
    不改 duo_sim 的全局（测试里两个脚本会同时被 import）。
    """

    EXTRA_MODULES = ("companion.backup", "companion.tts", "companion.config")  # config：DEEP_AUDIT B-2，计费峰谷也要走仿真钟

    def __enter__(self) -> "RehearsalTimePatch":
        saved = D.TIME_PATCH_MODULES
        D.TIME_PATCH_MODULES = tuple(saved) + self.EXTRA_MODULES
        try:
            super().__enter__()
        finally:
            D.TIME_PATCH_MODULES = saved
        return self


# ==========================================
# 彩排驱动器
# ==========================================


class _LogCapture(logging.Handler):
    """捕获指定 logger 的 INFO 日志（旁白滤网/语音降级），供记分卡取证。"""

    def __init__(self, keywords: Sequence[str]):
        super().__init__(level=logging.INFO)
        self.keywords = tuple(keywords)
        self.records: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        if any(k in msg for k in self.keywords):
            self.records.append(msg)


class FinalRehearsal(D.DuoSimulator):
    """跨日彩排驱动器（复用 duo_sim 的全真引擎接线）。"""

    def __init__(
        self,
        config: Config,
        run_dir: str,
        start_time: str = DEFAULT_START_TIME,
        max_cost: float = DEFAULT_MAX_COST,
        user_model: Optional[str] = None,
        seed: Optional[int] = None,
        user_reply_fn: Optional[Any] = None,
        timeline: Optional[Sequence[Beat]] = None,
        preset_arcs: bool = True,
        preset_memory: bool = True,
        force_d2_unanswered: bool = False,
    ):
        # 传一个合成 Scene 只为复用 DuoSimulator 的字段初始化；run() 被整体重写。
        dummy = ACT_SCENES["A1"]
        super().__init__(
            config=config, scene=dummy, run_dir=run_dir, start_time=start_time,
            max_turns=10_000, max_cost=max_cost, user_model=user_model,
            seed=seed, user_reply_fn=user_reply_fn,
        )
        self.timeline: List[Beat] = (
            list(timeline) if timeline is not None
            else build_timeline(force_d2_unanswered=force_d2_unanswered)
        )
        self.preset_arcs = preset_arcs
        self.preset_memory = preset_memory

        self.backup_dir = os.path.join(run_dir, "backup")
        self.backup_scheduler: Optional[DailyBackupScheduler] = None

        self.events: List[Dict[str, Any]] = []
        self.turn_meta: Dict[int, Dict[str, Any]] = {}
        self.maintenance_events: List[Dict[str, Any]] = []
        self.arcs_status_log: List[Dict[str, Any]] = []
        self.all_chunks: List[Dict[str, Any]] = []
        self.filter_logs: List[str] = []
        self.voice_logs: List[str] = []
        self.prompt_voice_hits = 0
        self.initial_state: Dict[str, Any] = {}
        self._idx = 0
        self._cur_act = ""
        self._cur_label = ""
        self._last_gate: Optional[Dict[str, Any]] = None
        self._claimed_arc: Optional[Dict[str, Any]] = None
        self._log_captures: List[_LogCapture] = []
        self._brief = ""

    # ---------- 生命周期 ----------

    async def setup(self) -> None:  # noqa: D401 - 扩展 duo_sim.setup
        await super().setup()

        # FIXES22 语音闸门：生产 main.py 装了 TTSManager（config.tts.enabled 默认 False）。
        # 这里同样装一份"装着但关着"的账目，主聊与主动消息共用同一条闸门。
        self.tts = TTSManager(self.config.tts, self.db)
        self.proactive.tts = self.tts
        self.turn_handler.tts = self.tts
        self._tts_enabled = bool(getattr(self.config.tts, "enabled", False))

        # 捕获滤网/语音降级的 INFO 日志（记分卡取证）。duo_sim 已 capture 不了这些。
        replier_logger = logging.getLogger("companion.replier")
        cap_narration = _LogCapture(("内心旁白", "旁白剥离"))
        cap_voice = _LogCapture(("语音不可用", "语音降级", "语音硬上限"))
        replier_logger.addHandler(cap_narration)
        replier_logger.addHandler(cap_voice)
        self._log_captures = [cap_narration, cap_voice]
        self._cap_narration = cap_narration
        self._cap_voice = cap_voice

        # 用真实闸门结论做留痕：包一层 _check_rules_gate / claim_event（不改生产代码，
        # 只在本进程替换实例属性）。这样 proactive_log 能区分"闸门拦"还是"决策层不发"。
        orig_gate = self.proactive._check_rules_gate

        async def gate_rec(*args, **kwargs):
            blocked, reason = await orig_gate(*args, **kwargs)
            self._last_gate = {"blocked": blocked, "reason": reason}
            return blocked, reason

        self.proactive._check_rules_gate = gate_rec  # type: ignore[assignment]

        orig_claim = self.arcs.claim_event

        async def claim_rec():
            arc = await orig_claim()
            if arc:
                self._claimed_arc = {"id": arc.get("id"), "title": arc.get("title"),
                                     "resolution": arc.get("resolution")}
            return arc

        self.arcs.claim_event = claim_rec  # type: ignore[assignment]

        # 凌晨维护钩子：与 main.py 同构（备份完成后把生活主线补到 3 条）
        self.backup_scheduler = DailyBackupScheduler(
            db_path=self.db_path,
            backup_dir=self.backup_dir,
            on_maintenance=self._daily_arcs_topup,
        )

        self._brief = self._load_brief()

        if self.preset_arcs:
            await self._preset_arcs()
        if self.preset_memory:
            await self._preset_memory()
        # 状态机推进与初始快照放在 run() 开头（时钟伪装已生效）做：
        # `advance_states` 读的是 datetime.now()，在 setup 里跑会拿到真实时钟，
        # 把 near/upcoming 判成 upcoming（真实日期距离 key_date 更远），污染生活主线断言。

    async def close(self) -> None:
        for cap in self._log_captures:
            for name in ("companion.replier",):
                try:
                    logging.getLogger(name).removeHandler(cap)
                except Exception:
                    pass
        await super().close()

    async def _daily_arcs_topup(self) -> None:
        """与 main.py `_daily_arcs_topup` 同构：备份维护时段跑完补生活主线。"""
        try:
            await self.arcs.advance_states()
            await self.arcs.ensure_arcs(min_active=3)
        except Exception as e:
            logger.warning("凌晨补充生活主线失败: %s", e)

    # ---------- 预置初始状态 ----------

    async def _preset_arcs(self) -> None:
        """预置 3 条生活主线：D2 一条（彩排中 today→resolved 触发事件）、near、upcoming。"""
        arcs = [
            ("乐团节目审查", "下周乐团要过节目，她那段低音部还没完全合齐，心里一直挂着。",
             DAY2, "紧张，怕拖了全团后腿；这事当天出结果。"),
            ("和声部合练加排", "周四晚加排，团长让大家把新谱子先过一遍。",
             DAY3, "有点累，但不想掉队。"),
            ("期中论文提纲交导师", "文学院读写课的期中论文提纲，导师让这周内发过去。",
             "2026-10-17", "怕被导师说太浅。"),
        ]
        for title, detail, key_date, stake in arcs:
            await self.arcs.insert_arc(title, detail, key_date, stake)

    async def _preset_memory(self) -> None:
        """预置若干 facts / diary：让【她最近的生活】与记忆区块开局就有内容。"""
        facts = [
            "阿俊在浙大读农学，大二",
            "他这周实验课很多，经常在地里做试验",
            "他聊天写得短，很少打标点",
            "他在食堂吃过一次临湖的红烧肉，说好吃",
        ]
        for f in facts:
            await self.db.execute(
                "INSERT OR IGNORE INTO facts (content, created_at) VALUES (?, ?)",
                (f, f"{DAY1} 12:00"),
            )
        diaries = [
            ("今天他说实验终于跑出来了，替他高兴了一会儿。", 6, "开心"),
            ("他今天话不多，回得也短，不知道是不是累了。", 5, "平静"),
            ("晚上聊到他去地里做试验，晒得脸都红了，有点想笑。", 6, "开心"),
        ]
        for content, importance, sentiment in diaries:
            await self.db.execute(
                "INSERT INTO diary (content, importance, sentiment, recall_count, created_at, last_recall_at)"
                " VALUES (?, ?, ?, 0, ?, ?)",
                (content, importance, sentiment, f"{DAY1} 20:00", f"{DAY1} 20:00"),
            )

    # ---------- 时钟与维护 ----------

    async def advance_to(self, at: str) -> None:
        """把假时钟推到绝对时刻 `at`，途中跨过 04:17 就真触发备份+维护钩子。"""
        target = datetime.strptime(at, "%Y-%m-%d %H:%M")
        prev = self.clock.now()
        if target <= prev:
            return
        for cross in maintenance_crossings(prev, target):
            self.clock.set(cross)
            await self._run_maintenance(cross)
        self.clock.set(target)
        self._maybe_mark_day(prev, target)

    def _maybe_mark_day(self, prev: datetime, target: datetime) -> None:
        date = target.strftime("%Y-%m-%d")
        if date in DAY_LABELS and not any(
            e.get("type") == "day" and e.get("date") == date for e in self.events
        ):
            self.events.append({
                "type": "day", "date": date, "day": DAY_LABELS[date],
                "weekday": "一二三四五六日"[target.weekday()],
            })

    async def _run_maintenance(self, when: datetime) -> None:
        """真跑生产备份函数 + 真跑 DailyBackupScheduler.on_maintenance 钩子。

        duo_sim 的补丁不覆盖 companion.backup，这里由 RehearsalTimePatch 补上，
        让备份文件名与"现在"一致。调度器的 asyncio.sleep 主循环无法在假时钟下
        无副作用地虚拟化（真睡=等几小时，不睡=忙旋转），所以由时钟跨点显式调用
        它真实的 `_run_maintenance()`（内部就是 on_maintenance → 主线推进/补充）。
        """
        before = await self._snapshot_arcs("maintenance_before")
        backup_path = ""
        err = ""
        try:
            backup_path = run_daily_backup(self.db_path, self.backup_dir, max_keep=14)
        except Exception as e:  # 备份失败不该炸彩排
            err = f"{type(e).__name__}: {e}"
            logger.warning("彩排备份失败: %s", e)
        before_statuses = [(a.get("id"), a.get("status")) for a in before]
        if self.backup_scheduler is not None:
            await self.backup_scheduler._run_maintenance()
        after = await self._snapshot_arcs("maintenance_after")
        after_statuses = [(a.get("id"), a.get("status")) for a in after]
        changed = [f"{i}:{b}->{a}" for (i, b), (_, a) in zip(before_statuses, after_statuses) if b != a]
        entry = {
            "time": when.strftime("%Y-%m-%d %H:%M"), "day": when.strftime("%Y-%m-%d"),
            "backup_path": backup_path, "error": err,
            "arcs_before": before_statuses, "arcs_after": after_statuses,
            "arc_status_changed": changed,
        }
        self.maintenance_events.append(entry)
        self.events.append({"type": "maintenance", **entry})

    async def _snapshot_arcs(self, source: str) -> List[Dict[str, Any]]:
        arcs = await self.arcs.fetch_arcs()
        compact = [{k: a.get(k) for k in ("id", "title", "key_date", "status",
                                          "resolution", "event_announced", "resolved_at")}
                   for a in arcs]
        self.arcs_status_log.append({
            "time": self.clock.now().strftime("%Y-%m-%d %H:%M"),
            "day": self.clock.now().strftime("%Y-%m-%d"),
            "source": source, "arcs": compact,
        })
        return arcs

    # ---------- 回调（扩展：累计全部段，供语音闸门取证） ----------

    async def _collect_chunk(self, chunk: Dict[str, Any]) -> None:
        rec = dict(chunk)
        self.sent_chunks.append(rec)
        self.all_chunks.append(rec)

    # ---------- 青梓侧（标注 act/day + 留痕） ----------

    async def _her_turn(self, idx: int, user_text: str, act: str = "", label: str = "") -> Any:
        act = act or self._cur_act
        label = label or self._cur_label
        rec = await super()._her_turn(idx, user_text)
        self.turn_meta[idx] = {"act": act, "label": label,
                               "day": self.clock.now().strftime("%Y-%m-%d")}
        prompt = getattr(self.assembler, "last_assembled_prompt", "") or ""
        if "[voice" in prompt.lower():
            self.prompt_voice_hits += 1
        self.events.append({
            "type": "her", "time": rec.time, "day": self.turn_meta[idx]["day"],
            "act": act, "label": label, "bubbles": list(rec.bubbles),
            "stickers": list((rec.snapshot or {}).get("stickers") or []),
            "silenced": rec.silenced, "text": rec.text,
        })
        return rec

    async def _chat_beat(self, beat: Beat) -> None:
        await self._enter_act(beat.act, beat.label)
        self._idx += 1
        idx = self._idx
        if beat.fixed_text:
            user_text = beat.fixed_text
        else:
            dialogue = await self._recent_dialogue()
            raw = await self._user_message(dialogue, beat.instruction)
            user_text = await self._coerce_user_text(raw, idx)
        lines = D.build_sim_batch(user_text, idx)
        self.clock.set(datetime.strptime(beat.at, "%Y-%m-%d %H:%M"))
        self.turns.append(D.TurnRecord(
            idx=idx, speaker="user", text=user_text,
            time=self.clock.now().strftime("%Y-%m-%d %H:%M"),
        ))
        self.raw_turns.append({"idx": idx, "speaker": "user", "text": user_text})
        self.events.append({
            "type": "user", "time": self.clock.now().strftime("%Y-%m-%d %H:%M"),
            "day": self.clock.now().strftime("%Y-%m-%d"), "act": beat.act,
            "label": beat.label, "text": user_text,
            "lines": [b.get("text") for b in lines],
        })
        await self._her_turn(idx, user_text, act=beat.act, label=beat.label)

    async def _enter_act(self, act: str, label: str) -> None:
        self._cur_act = act
        self._cur_label = label
        if act in ACT_SCENES:
            self.user_system_prompt = D.build_user_system_prompt(self._brief, ACT_SCENES[act])
        if not any(e.get("type") == "act" and e.get("act") == act for e in self.events):
            title = ACT_SCENES[act].title if act in ACT_SCENES else label
            self.events.append({"type": "act", "act": act, "title": title})

    # ---------- 主动消息周期（比 duo_sim 多记闸门原因/用途/事件来源） ----------

    async def _proactive_cycle(self, idx: int, act: str = "", label: str = "",
                               force_material: str = "") -> Optional[Any]:
        act = act or self._cur_act
        label = label or self._cur_label
        state_before = await self._state_snapshot()
        self.sent_chunks = []
        self._last_gate = None
        self._claimed_arc = None
        await self.proactive.trigger_cycle()
        forced = False
        # 闭环复现：本周期没发出任何东西时，用真管道补发一条常规主动消息，
        # 确定性复现"决策层 A 分支"（0030 局的前置条件）。记录里明确标 forced。
        if force_material and not self.sent_chunks:
            try:
                await self.proactive._generate_and_send(
                    force_material, self.clock.now().strftime("%Y-%m-%d %H:%M")
                )
                forced = True
                self.raw_turns.append({
                    "idx": idx, "speaker": "system", "proactive": True,
                    "note": f"闭环复现：故障注入一条常规主动消息（素材：{force_material}）",
                })
            except Exception as e:
                logger.warning("闭环注入常规主动消息失败: %s", e)
        collected = D.collect_sent_chunks(self.sent_chunks)
        bubbles, stickers, faces = collected["bubbles"], collected["stickers"], collected["faces"]
        calls = await self._llm_rows_since_cursor()
        purposes = [c.get("purpose") for c in calls]
        sent = bool(bubbles or stickers or faces)
        now = self.clock.now()
        gate = self._last_gate or {}
        if sent:
            outcome = "已发出"
        elif gate.get("blocked"):
            outcome = "规则闸门拦截"
        elif calls:
            outcome = "决策层选择不发"
        else:
            outcome = "无动作"
        text = "\n".join(b for b in bubbles if b)
        entry = {
            "idx": idx, "time": now.strftime("%Y-%m-%d %H:%M"),
            "day": now.strftime("%Y-%m-%d"), "hour": now.hour,
            "act": act, "label": label,
            "quiet": bool(self.proactive._is_in_quiet_hours(now.hour)),
            "sent": sent, "outcome": outcome, "forced": forced,
            "gate_blocked": bool(gate.get("blocked")), "gate_reason": gate.get("reason", ""),
            "llm_call_n": len(calls), "purposes": purposes,
            "event_claimed": self._claimed_arc,
            "event_sent": bool(sent and self._claimed_arc),
            "text": text, "stickers": stickers, "faces": faces,
        }
        self.proactive_log.append(entry)
        self.events.append({"type": "proactive", **entry})
        await self._snapshot_arcs("cycle")

        if not sent:
            self.raw_turns.append({
                "idx": idx, "speaker": "system", "proactive": True,
                "note": f"主动消息未发出（{outcome}：{entry['gate_reason']}）",
            })
            return None
        note = "主动消息（事件通道）" if entry["event_sent"] else "主动消息（trigger_cycle）"
        rec = D.TurnRecord(
            idx=idx, speaker="her", text=text,
            time=now.strftime("%Y-%m-%d %H:%M"), bubbles=bubbles, faces=faces,
            note=note,
            snapshot={
                "proactive": True, "bubbles": bubbles, "stickers": stickers, "faces": faces,
                "llm_calls": calls, "state_before": state_before,
                "state_after": await self._state_snapshot(),
                **self._prompt_block_summary(),
            },
        )
        self.turns.append(rec)
        self.turn_meta[idx] = {"act": act, "label": label,
                               "day": now.strftime("%Y-%m-%d"), "proactive": True}
        self.raw_turns.append({
            "idx": idx, "speaker": "her", "proactive": True,
            "text": rec.text, "time": rec.time, **rec.snapshot,
        })
        return rec

    # ---------- 主循环 ----------

    async def run(self) -> Dict[str, Any]:  # noqa: D401 - 重写为多日剧本驱动
        rng = random.Random(self.seed)
        # 先按剧本起点推进一次状态机（near/upcoming 立刻反映到【她最近的生活】区块），
        # 再取初始快照。必须在时钟伪装里做，否则读取的是真实日期。
        await self.arcs.advance_states()
        await self._snapshot_arcs("run_start")
        self.initial_state = await self._state_snapshot()
        try:
            for beat in self.timeline:
                await self.advance_to(beat.at)
                if beat.kind == "chat":
                    await self._check_budget()
                    await self._chat_beat(beat)
                elif beat.kind == "proactive":
                    await self._check_budget()
                    self._idx += 1
                    await self._enter_act(beat.act, beat.label)
                    await self._proactive_cycle(self._idx, act=beat.act, label=beat.label,
                                                force_material=beat.force_if_silent)
                # idle：只推时钟（advance_to 已经做过）
                self.end_reason = f"剧本跑完（{beat.label}）"
        except D.CostBreakerTripped as e:
            logger.warning("成本熔断：%s", e)
            self.end_reason = f"成本熔断（¥{await self.spent():.4f} ≥ ¥{self.max_cost}）"
        except Exception as e:  # 单步异常不许毁掉已完成的 transcript
            logger.exception("彩排第 %s 步异常：%s", getattr(self, "_idx", "?"), e)
            self.raw_turns.append({"speaker": "system", "note":
                                   f"彩排异常中止：{type(e).__name__}: {e}"})
            self.end_reason = f"异常中止：{type(e).__name__}"
        self.end_time = self.clock.now()
        return await self.finalize()

    # ---------- 产物 ----------

    async def finalize(self) -> Dict[str, Any]:
        if not hasattr(self, "end_time"):
            self.end_time = self.clock.now()
        turns = self._scored_turns()
        cost = await self.spent()
        self._final_cost = cost
        cost_breakdown = await self._cost_breakdown()
        final_state = await self._state_snapshot()
        scorecard = build_scorecard(
            turns=turns,
            proactive_log=self.proactive_log,
            maintenance_events=self.maintenance_events,
            arcs_status_log=self.arcs_status_log,
            initial_state=self.initial_state,
            final_state=final_state,
            cost=cost,
            max_cost=self.max_cost,
            voice_chunks=[c for c in self.all_chunks if c.get("type") == "voice"],
            filter_logs=self._cap_narration.records,
            voice_logs=self._cap_voice.records,
            prompt_voice_hits=self.prompt_voice_hits,
            tts_enabled=self._tts_enabled,
        )
        meta = {
            "run_id": os.path.basename(self.run_dir),
            "kind": "final_rehearsal",
            "start_time": self.start_time,
            "end_time": self.end_time.strftime("%Y-%m-%d %H:%M"),
            "end_reason": self.end_reason or "剧本跑完",
            "her_model": self.her_model or self.config.llm.active().chat,
            "user_model": self.user_model,
            "turns_total": len(self.turns),
            "her_turns": sum(1 for t in self.turns if t.speaker == "her"),
            "user_turns": sum(1 for t in self.turns if t.speaker == "user"),
            "proactive_cycles": len(self.proactive_log),
            "cost": round(cost, 4),
            "cost_breakdown": cost_breakdown,
            "db": self.db_path,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        raw = {
            "meta": meta,
            "scorecard": scorecard,
            "initial_state": self.initial_state,
            "final_state": final_state,
            "acts": [{"act": a, "title": ACT_SCENES[a].title} for a in ACT_SCENES if a in
                     {e.get("act") for e in self.events if e.get("type") == "act"}],
            "timeline": [{"at": b.at, "kind": b.kind, "act": b.act, "label": b.label}
                         for b in self.timeline],
            "turns": turns,
            "raw_turns": self.raw_turns,
            "events": self.events,
            "proactive_cycles": self.proactive_log,
            "maintenance_events": self.maintenance_events,
            "arcs_status_log": self.arcs_status_log,
            "voice_chunks": [c for c in self.all_chunks if c.get("type") == "voice"],
            "filter_logs": self._cap_narration.records,
            "voice_logs": self._cap_voice.records,
        }
        with open(os.path.join(self.run_dir, "transcript.md"), "w", encoding="utf-8") as f:
            f.write(render_rehearsal(self))
        with open(os.path.join(self.run_dir, "scorecard.json"), "w", encoding="utf-8") as f:
            json.dump(scorecard, f, ensure_ascii=False, indent=2)
        with open(os.path.join(self.run_dir, "raw.json"), "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
        return {"meta": meta, "scorecard": scorecard, "final_state": final_state, "raw": raw}

    def _scored_turns(self) -> List[Dict[str, Any]]:
        """把 TurnRecord 投影成指标输入，并补 act/day 与快照字段。

        `_rec_to_dict` 不含 state_*/system_prompt_blocks（它们在 snapshot 里），
        数值引擎与生活主线两项要靠它们，必须在这里并进来；否则两项会静默报无样本。
        """
        out: List[Dict[str, Any]] = []
        for r in self.turns:
            d = D._rec_to_dict(r)
            snap = r.snapshot or {}
            d["state_before"] = snap.get("state_before")
            d["state_after"] = snap.get("state_after")
            d["system_prompt_blocks"] = snap.get("system_prompt_blocks")
            d["proactive"] = bool(snap.get("proactive"))
            d["stickers"] = list(snap.get("stickers") or [])
            meta = self.turn_meta.get(r.idx, {})
            d["act"] = meta.get("act", "")
            d["day"] = meta.get("day", d.get("time", "")[:10])
            out.append(d)
        return out

    async def _cost_breakdown(self) -> Dict[str, Any]:
        rows = await self.db.fetchall(
            "SELECT purpose, model, COUNT(*) AS n, SUM(cost_estimate) AS c, "
            "SUM(prompt_tokens) AS pt, SUM(completion_tokens) AS ct FROM llm_calls "
            "GROUP BY purpose, model ORDER BY c DESC"
        )
        by_purpose: Dict[str, Dict[str, Any]] = {}
        for r in rows:
            by_purpose[str(r["purpose"])] = {
                "model": r["model"], "calls": int(r["n"]),
                "cost_cny": round(float(r["c"] or 0.0), 4),
                "prompt_tokens": int(r["pt"] or 0), "completion_tokens": int(r["ct"] or 0),
            }
        return by_purpose


# ==========================================
# transcript 渲染（跨日完整原文）
# ==========================================


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", "<br>")


def render_rehearsal(sim: "FinalRehearsal") -> str:
    """把彩排 events 渲染成 QQ 气泡格式的跨日 transcript（她左他右）。"""
    L: List[str] = []
    A = L.append
    A(f"# 毕业彩排 transcript · {os.path.basename(sim.run_dir)}")
    A("")
    A(f"- 时段：{sim.start_time} → {sim.end_time.strftime('%Y-%m-%d %H:%M')}"
      f"（D1 {DAY1} 傍晚 → D3 {DAY3} 中午）")
    A(f"- 她的模型：{sim.her_model or sim.config.llm.active().chat} ｜ 模拟他的模型：{sim.user_model}")
    A(f"- 成本实报：¥{getattr(sim, '_final_cost', 0.0):.4f}")
    A(f"- 主动消息周期：{len(sim.proactive_log)} 次 ｜ 凌晨维护钩子：{len(sim.maintenance_events)} 次")
    A("")
    A("> 左列是他（v4-pro 按真人画像扮演），右列是她（青梓全管道实跑）。"
      "`[沉默]` = 本轮她选择不回。未发出的主动消息周期与维护钩子以引用块留痕。")

    open_block = False
    for ev in sim.events:
        et = ev.get("type")
        if et == "day":
            if open_block:
                A("")
                open_block = False
            A("")
            A(f"## {ev['day']} · {ev['date']}（周{ev['weekday']}）")
        elif et == "act":
            if open_block:
                A("")
                open_block = False
            A("")
            A(f"### {ev.get('title') or ev.get('act')}")
        elif et == "user":
            if not open_block:
                A("")
                A("| 时间 | 他 | 她 |")
                A("| --- | --- | --- |")
                open_block = True
            A(f"| {ev['time'][-5:]} | {_esc(ev['text'])} |  |")
        elif et == "her":
            if not open_block:
                A("")
                A("| 时间 | 他 | 她 |")
                A("| --- | --- | --- |")
                open_block = True
            if ev.get("silenced"):
                right = "_触发 [沉默] 闸门，本轮未发出_"
            else:
                parts = [_esc(b) for b in (ev.get("bubbles") or [])]
                # 纯表情包回合没有文字气泡，但它是她真的发出的内容——不渲染出来，
                # transcript 会显示成"她什么都没说"（误导所有者抽读）。
                parts += [f"[表情包：{os.path.basename(s)}]" for s in (ev.get("stickers") or [])]
                right = "<br>".join(parts)
            A(f"| {ev['time'][-5:]} |  | {right} |")
        elif et == "proactive":
            if open_block:
                A("")
                open_block = False
            if ev.get("sent"):
                tag = "事件消息" if ev.get("event_sent") else "主动消息"
                if ev.get("forced"):
                    tag += "·闭环注入"
                parts = [_esc(ev.get("text") or "")]
                parts += [f"[表情包：{os.path.basename(s)}]" for s in (ev.get("stickers") or [])]
                body = "<br>".join(p for p in parts if p)
                A("")
                A("| 时间 | 他 | 她 |")
                A("| --- | --- | --- |")
                A(f"| {ev['time'][-5:]} |  | {body}<br>_{tag}（{ev['label']}）_ |")
                A("")
            else:
                A(f"> {ev['time'][-5:]} 主动消息周期未发出（{ev['outcome']}"
                  f"：{ev.get('gate_reason') or '无闸门原因'}）— {ev['label']}")
        elif et == "maintenance":
            if open_block:
                A("")
                open_block = False
            A(f"> {ev['time'][-5:]} 凌晨 04:17 备份+维护钩子真触发："
              f"备份 `{os.path.basename(ev.get('backup_path') or '')}`"
              f"{'（失败：' + ev['error'] + '）' if ev.get('error') else ''}；"
              f"主线状态变化 {ev.get('arc_status_changed') or '无'}")
    A("")
    A("---")
    A("")
    A("完整内部状态快照见同目录 `raw.json`，全要素记分卡见 `scorecard.json`。")
    return "\n".join(L)


# ==========================================
# CLI
# ==========================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FIXES24 毕业彩排（全要素真实演练场）")
    p.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST, help="成本熔断上限（元）")
    p.add_argument("--run-id", default=None, help="产物目录名（默认按时间戳）")
    p.add_argument("--user-model", default=None, help="模拟他的模型（默认同她）")
    p.add_argument("--seed", type=int, default=20261012, help="延迟抖动的随机种子")
    p.add_argument("--config", default="config.toml", help="配置文件路径（只读）")
    p.add_argument("--start-time", default=DEFAULT_START_TIME, help="起始时钟")
    p.add_argument("--force-d2-unanswered", action="store_true",
                   help="闭环复现：D2 14:30 若决策层不发，就用真管道补发一条常规主动消息，"
                        "确定性复现'事件消息被未回消息饿死'的前置条件")
    return p


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    run_id = args.run_id or f"rehearsal-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    run_dir = os.path.join(OUT_ROOT, run_id)
    if os.path.exists(run_dir) and not args.run_id:
        print(f"产物目录已存在：{run_dir}（请用 --run-id 指定别的名字）", file=sys.stderr)
        return 2
    os.makedirs(run_dir, exist_ok=True)

    config = Config.load(args.config)
    if not os.path.exists(os.path.join(ROOT, D.PERSONA_BRIEF)):
        raise RehearsalError(
            f"缺画像简报 {D.PERSONA_BRIEF}；请先跑 scripts/sim/duo_sim_persona.py"
        )

    print("=" * 72)
    print("毕业彩排 · 全要素真实演练场（FIXES24）")
    print(f"时段：{DAY1} 傍晚 → {DAY3} 中午 ｜ 成本熔断 ¥{args.max_cost}")
    print(f"[tts].enabled = {getattr(config.tts, 'enabled', False)}（默认关）")
    print("=" * 72, flush=True)

    sim = FinalRehearsal(
        config=config, run_dir=run_dir, start_time=args.start_time,
        max_cost=args.max_cost, user_model=args.user_model, seed=args.seed,
        force_d2_unanswered=bool(getattr(args, "force_d2_unanswered", False)),
    )
    await sim.setup()
    try:
        with RehearsalTimePatch(sim.clock, sim.sleep_log):
            result = await sim.run()
    finally:
        await sim.close()

    sc = result["scorecard"]
    meta = result["meta"]
    print("\n" + "=" * 72)
    print(f"结束原因：{meta['end_reason']} ｜ 终态时钟：{meta['end_time']}")
    print(f"回合：{meta['turns_total']}（她 {meta['her_turns']} / 他 {meta['user_turns']}）"
          f" ｜ 主动周期：{meta['proactive_cycles']} ｜ 维护钩子：{len(sim.maintenance_events)}")
    print(f"成本实报：¥{meta['cost']:.4f}")
    print(f"\n记分卡（{sc['overall']}，共 {len(sc['items'])} 项）：")
    for it in sc["items"]:
        extra = f"  ← {it['not_run_reason']}" if it["verdict"] == "N/A" else ""
        print(f"  {it['verdict']:<4} {it['item']}{extra}")
    print(f"\n产物目录：{run_dir}")
    print("  transcript.md / scorecard.json / raw.json")
    return 0 if sc["overall"] != "FAIL" else 1


def main() -> None:
    args = build_arg_parser().parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
