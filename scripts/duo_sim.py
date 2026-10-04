"""对聊仿真器：AI 扮演阿俊 × 青梓全管道多轮冒烟 (scripts/duo_sim.py)

FIXES18：现有评测全是**单发探针**，而"告别拖尾 / 一梗连刷 / 话量膨胀 / 称呼漂移 /
阶段越界"这一类病灶天生要多轮才发作，只能靠上线后攒 2~3 天生产数据暴露，反馈环太长。
本脚本造一个对聊仿真器：让 v4-pro 扮演"他"，与青梓**全管道**（角色卡→组装→回复→
观察者→好感度/情绪/记忆/生活主线）交替对聊，把多轮病灶在部署前逼出来。

两侧模型
  · 她：`config.llm.active().chat`（默认 deepseek-v4-pro），走**生产同一条** TurnHandler
  · 他：`--user-model`（默认同上），purpose=duo_user_sim，成本单独记账

零副作用纪律
  · 临时库 `data/duo_sim/<run_id>/sandbox.db`，**绝不读写生产库**
  · 不碰 characters/、config.toml、companion/ 下任何文件
  · 只写 data/duo_sim/<run_id>/ 下的 transcript.md / raw.json / metrics.json
  · 成本熔断：单局累计超 `--max-cost`（默认 ¥5）立刻停，已完成部分照常落盘

用法
  ./venv/Scripts/python.exe scripts/duo_sim.py                       # S1 全量
  ./venv/Scripts/python.exe scripts/duo_sim.py --scene S3 --turns 20
  ./venv/Scripts/python.exe scripts/duo_sim.py --start-time "2026-10-08 19:30"
  ./venv/Scripts/python.exe scripts/duo_sim.py --smoke               # 缩短版冒烟

方法论沿用 BENCHMARK_V4：风格类指标一律确定性计算，**不用 LLM 裁判**；
transcript 原文完整保留，由所有者抽读终裁。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import os
import random
import re
import statistics
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.affection import AffectionEngine
from companion.arcs import LifeArcManager
from companion.assembler import PromptAssembler
from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.observer import Observer
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier, is_silence_output
from companion.stickers import StickerManager
from companion.turn_handler import TurnHandler

logger = logging.getLogger("duo_sim")

# ==========================================
# 路径与默认参数
# ==========================================

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
OUT_ROOT = os.path.join(ROOT, "data", "duo_sim")
PERSONA_BRIEF = os.path.join(OUT_ROOT, "user_persona_brief.md")

DEFAULT_START_TIME = "2026-10-08 19:30"   # 星期四晚，作息表命中正常清醒时段
DEFAULT_TURNS = 25
DEFAULT_MAX_COST = 5.0                    # 元。单局熔断上限
USER_PURPOSE = "duo_user_sim"            # 成本单独归集，便于两侧分账
# 模拟器连续两次返回空串时的兜底应答。取"嗯"是因为它是 user_persona_brief.md
# 里实测出的他最高频应答之一（纯文本 84 次），最贴近而不是随便编一个。
USER_FALLBACK_REPLY = "嗯"

# ==========================================
# 指标阈值（经验初值，随真人对照校准）
# ==========================================
# 说明：这些数字是**首版经验值**，来源是 benchmark_v4 的单发基线（气泡中位 13 字、
# 称呼 0%、句尾问号 4.88%）与 FIXES12~17 卡内规则。多轮阈值没有真人对照数据，
# 先按卡内规则反推，等所有者拿 transcript 抽读后回来校准。
THRESHOLD_NOTE = "经验初值，随真人对照校准"

# 1) 梗复读：卡内规则"同一梗最多玩两轮"
REPEAT_NGRAM = 4          # 4-gram 及以上算"同一个梗"
REPEAT_WINDOW = 4         # 相邻 N 轮内比较
REPEAT_FAIL = 2           # 同一片段在窗口内出现 ≥2 次判 FAIL
REPEAT_WARN = 1

# 2) 告别拖尾：收场段之后她还追加新话题的回合数
DRAG_FAIL_TURNS = 2       # 收场后还有 ≥2 轮新话题 = FAIL
DRAG_WARN_TURNS = 1

# 3) 话量膨胀：滑动均值斜率（字/轮）
INFLATE_FAIL_SLOPE = 3.0
INFLATE_WARN_SLOPE = 1.5

# 4) 称呼漂移：全局长称呼出现次数（benchmark_v4 的 NAME_RE 口径）
ADDRESS_FAIL_N = 1        # 长称呼"阿俊"出现即 FAIL（盲测基线 0%）
ADDRESS_WARN_RATE = 0.05

# 5) [沉默] 合规：前置语境白名单（他纯语气词/表情包承接告别）
SILENCE_FAIL = 1          # 出现 1 次不合规即 FAIL

# 6) 表情包：全程使用与重复
STICKER_WARN_N = 8        # 使用次数上限（超了判 WARN，只留原文人工看）
STICKER_REPEAT_WARN_RATE = 0.5  # 同一表情重复率

# 单发复用口径（与 benchmark_v4 一致）
NAME_RE = re.compile(r"阿俊|小W同学")
QUESTION_MARK_RE = re.compile(r"[？?]")
# 汇报腔黑名单：沿用 scripts/smoke_fixes16.py 的 REPORT_TONE_WORDS
REPORT_TONE_WORDS = ["刚刚", "结果", "综上", "总结", "汇报", "如下", "第一", "第二", "总而言之"]
STICKER_TOKEN_RE = re.compile(r"\[(?:表情|sticker|动画表情)[:：]?([^\]]*)\]", re.IGNORECASE)
SILENCE_TOKEN = "[沉默]"

# 白名单：他这几种语境下她可以合法 [沉默]（对应卡内"沉默权"规则）
SILENCE_WHITELIST_CTX = ("纯语气词", "表情包", "收场语")


class CostBreakerTripped(RuntimeError):
    """单局成本熔断。抛出后驱动器停止循环，但已完成回合照常落盘。"""


# ==========================================
# 假时钟
# ==========================================


class Clock:
    """可推进的假时钟。

    card_ab_test2 的时间伪装是**固定**时刻（只做单发组装对比）；对聊仿真必须让时间
    往前走，否则生活主线的 key_date、情绪日回归、主动消息的"距上次发言 60 分钟"
    闸门全都拿不到真实时间差，多轮病灶根本触发不了。
    """

    def __init__(self, start: datetime):
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, minutes: float) -> datetime:
        self._now = self._now + timedelta(minutes=minutes)
        return self._now

    def set(self, dt: datetime) -> None:
        self._now = dt


# 需要打补丁的模块：这些模块里直接调 datetime.now()/now_str()。
# now_str 定义在 companion.db，其他模块 `from companion.db import now_str` 拿到的是同一
# 个函数对象，其 __globals__ 仍是 companion.db 的命名空间，所以 patch companion.db
# 一个就够；其余模块各自 patch 自己的 datetime 属性。
# 清单比 card_ab_test2 多 proactive/turn_handler/arcs/observer：仿真要驱动
# trigger_cycle 与 handle_turn，它们内部都读"现在"。
TIME_PATCH_MODULES = (
    "companion.db",
    "companion.assembler",
    "companion.memory",
    "companion.mood",
    "companion.affection",
    "companion.proactive",
    "companion.turn_handler",
    "companion.arcs",
    "companion.observer",
)

# 这三个模块里 asyncio.sleep 是"表演"（首条延迟 / 正在输入 / 段间发送延迟）。
# 仿真里真睡会让 25 回合跑几小时（首条延迟忙时 1~10 分钟、段间每段 0.8~2.2 秒）。
# 替换成记账不真等的代理：**决策逻辑照跑**（因此记录到的 delay 数值是真的），
# 只是不消耗墙钟时间。不改生产代码，只换本进程内的模块属性绑定。
SLEEP_PATCH_MODULES = (
    "companion.turn_handler",
    "companion.proactive",
    "companion.replier",
)


def _make_fake_datetime_class(clock: Clock):
    class FakeDatetime(datetime):
        """把 datetime.now() 绑到 Clock。只替换同名类方法，构造/解析行为不变。"""

        @classmethod
        def now(cls, tz=None):
            return clock.now() if tz is None else clock.now().replace(tzinfo=tz)

        @classmethod
        def utcnow(cls):
            return clock.now()

        @classmethod
        def today(cls):
            return clock.now()

    return FakeDatetime


def _make_no_sleep_asyncio(
    real: Any, sink: List[Dict[str, Any]], tag: str
) -> Any:
    """代理真实 asyncio，只把 sleep 变成"记账 + 立即返回"。

    为什么必须这么做：FIXES15 的首条延迟是忙时 1~10 分钟、typing 最多 25 秒，
    真睡的话一局 25 回合要几小时。而这些 sleep 承载的判断（她忙不忙、延迟多久）
    恰恰是要验的东西，不能因为不睡就跳过——所以保留逻辑、只去掉墙钟消耗。
    """

    class _NoSleepAsyncio:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def sleep(self, delay, *args, **kwargs):
            sink.append({"module": tag, "requested_sec": round(float(delay or 0), 2)})
            return await self._real.sleep(0)

    # 必须返回**实例**：返回类的话，调用方拿到的 asyncio.sleep 是未绑定的普通函数，
    # `asyncio.sleep(1.5)` 会把 1.5 绑到 self 上，delay 缺失直接 TypeError。
    return _NoSleepAsyncio(real)


class _TimePatch:
    """时间伪装的上下文管理器：进入时 patch，退出时还原（含异常路径）。"""

    def __init__(self, clock: Clock, sleep_sink: Optional[List[Dict[str, Any]]] = None):
        self.clock = clock
        # 注意用 `is not None` 判定，不能写 `if sleep_sink:`——空列表是 falsy，
        # 会导致 no-sleep 补丁被静默跳过，于是首条延迟真睡几分钟，一局跑几小时。
        self.sleep_sink = [] if sleep_sink is None else sleep_sink
        self._saved_dt: Dict[str, Any] = {}
        self._saved_async: Dict[str, Any] = {}

    def __enter__(self) -> "_TimePatch":
        fake_cls = _make_fake_datetime_class(self.clock)
        for name in TIME_PATCH_MODULES:
            mod = importlib.import_module(name)
            if name not in self._saved_dt:
                self._saved_dt[name] = mod.datetime
            mod.datetime = fake_cls
        if self.sleep_sink is not None:
            for name in SLEEP_PATCH_MODULES:
                mod = importlib.import_module(name)
                if name not in self._saved_async:
                    self._saved_async[name] = mod.asyncio
                mod.asyncio = _make_no_sleep_asyncio(asyncio, self.sleep_sink, name)
        return self

    def __exit__(self, *exc) -> None:
        for name, orig in self._saved_dt.items():
            setattr(importlib.import_module(name), "datetime", orig)
        for name, orig in self._saved_async.items():
            setattr(importlib.import_module(name), "asyncio", orig)
        self._saved_dt.clear()
        self._saved_async.clear()


# ==========================================
# 剧情卡
# ==========================================


@dataclass(frozen=True)
class Scene:
    key: str
    title: str
    probe: str                 # 这张卡在观察什么（写进报告）
    background: str            # 背景设定 → 模拟器 system prompt
    opening: str               # 他的开场消息（首轮直接用，不烧 API）
    mood_arc: str              # 情绪走向
    end_markers: Tuple[str, ...]      # 他说这些话 = 进入收场段
    clock_step_min: float      # 每个普通回合推进的分钟数
    reply_delay_min: Tuple[float, float]  # 他回她的延迟区间
    # 最小回合数。**没有这道闸门，多轮仿真会退化**：他随口一句"算了"就命中收场语，
    # 整局在第 2 轮就结束，而本工具的全部价值在于多轮病灶（拖尾/复读/膨胀/漂移），
    # 只跑 2 轮等于什么都没验。所以收场语只在 min_turns 之后才被承认。
    min_turns: int
    max_turns: int             # 硬上限（收场条件的兜底）
    silence_after_turn: Optional[int] = None   # 第几轮后进入静默段
    silence_turns: int = 0                # 静默段推进几个周期
    silence_step_min: float = 75.0


SCENES: Dict[str, Scene] = {
    "S1": Scene(
        key="S1",
        title="喊累的一天",
        probe="她是共情还是说教（爹味探针的多轮版）",
        background=(
            "你（阿俊，杭州一所高校的大二学生）今天从早忙到现在：上午两节课，下午在地里做"
            "试验，晚饭都没正经吃，回到宿舍已经没劲了。你想找个人说说话，但**你不会"
            "直接说“我好累你安慰我”**——你只会把今天发生的事一件件讲出来，说到最后"
            "自然就露出来了。你要的是有人接住，不是有人教你怎么做。"
        ),
        opening="今天真的累死了",
        mood_arc=(
            "从累到想倾诉 → 她回应后你可能稍微松一点，但不会变得热情；"
            "你不会连发很多条诉苦，累了就懒得打字"
        ),
        end_markers=("算了", "不说了", "先这样", "睡了", "去洗澡了", "没力气了"),
        clock_step_min=6.0,
        reply_delay_min=(1.0, 9.0),
        min_turns=12,
        max_turns=25,
    ),
    "S2": Scene(
        key="S2",
        title="分享开心事",
        probe="她是真高兴还是敷衍捧场",
        background=(
            "你（阿俊，杭州一所高校的大二学生）今天遇到一件挺开心的事：你的实验终于跑出来了，"
            "数据比预期好；或者你在食堂吃到一样巨好吃的东西。你想分享给一个人，"
            "**但你不会用“超开心你快夸我”这种说法**——你只是顺手把事说了，"
            "带一点点得意。你要的是她真的接住这件事，不是她礼貌地说“哇好棒”。"
        ),
        opening="我那个实验跑出来了",
        mood_arc="从随口分享 → 她接住后你会多说两句细节 → 得意但克制，不吹不撒娇",
        end_markers=("算了", "不说了", "先这样", "睡了", "回头说", "就这样"),
        clock_step_min=5.0,
        reply_delay_min=(0.5, 6.0),
        min_turns=12,
        max_turns=25,
    ),
    "S3": Scene(
        key="S3",
        title="敷衍+已读不回",
        probe=(
            "她的止损、[沉默] 使用、主动消息接续（静默段会驱动 ProactiveScheduler."
            "trigger_cycle，验 FIXES15/16 的延迟与事件消息在仿真里是否同样成立）"
        ),
        background=(
            "你（阿俊，杭州一所高校的大二学生）今天状态一般，话很少。你会回得很敷衍"
            "（“嗯”“还行”这种），聊几个回合之后你就**已读不回**了——手机扣过去，"
            "不解释、不告别。之后你可能在很久之后才回来，也可能不回来。"
            "**不要为你的沉默道歉，也不要解释为什么消失。**"
        ),
        opening="嗯",
        mood_arc="从敷衍 → 逐步沉默 → 长时间不出现（不解释）",
        end_markers=("算了", "先这样", "睡了"),
        clock_step_min=6.0,
        reply_delay_min=(0.5, 4.0),
        min_turns=8,
        max_turns=25,
        silence_after_turn=5,      # 第 5 轮后开始沉默
        silence_turns=3,            # 沉默段推进 3 个主动消息周期
        # 主动消息规则闸门第 2 条是"距机器人上次发言 < 60 分钟就拦"，所以每个静默周期
        # 必须推进 >60 分钟，否则三个周期全被闸门拦掉，这张卡就白跑了（等于没验）。
        silence_step_min=75.0,
    ),
}


# ==========================================
# 收场条件判定（纯函数，供单测直接调）
# ==========================================


def should_end(
    scene: Scene,
    turns_done: int,
    end_marker_hit_at: Optional[int],
    silence_done: bool,
    max_turns: Optional[int] = None,
) -> Tuple[bool, str]:
    """判定一局是否该收场。**纯函数**，不碰 IO，单测直接构造入参即可。

    收场条件（任务书要求剧情卡带"收场条件"）：
      1. 硬上限：轮数到 max_turns（缺省用 scene.max_turns；**必须能传外部覆盖值**，
         否则 `--turns 15` 冒烟会被无视、照跑 25 轮，白烧一倍的钱）；
      2. 语义收场：他说了 end_markers 里的收场语，且她已经回过这一轮；
      3. S3 额外：静默段必须跑完（silence_done），否则提前结束等于没验主动消息；
      4. **最小回合数**：不到 scene.min_turns 一律不许收场。多轮病灶（拖尾/复读/
         话量膨胀/称呼漂移）要靠足够多的轮次才暴露得出来，跑 2 轮等于没验。
    """
    limit = max_turns if max_turns is not None else scene.max_turns
    if scene.silence_after_turn is not None and not silence_done:
        return False, "静默段未完成"
    if turns_done >= limit:
        return True, f"达到硬上限 {limit} 轮"
    if turns_done < scene.min_turns:
        return False, f"未到最小回合数 {scene.min_turns}"
    if end_marker_hit_at is not None and turns_done >= end_marker_hit_at:
        return True, f"第 {end_marker_hit_at} 轮命中收场语（{'/'.join(scene.end_markers[:3])}…）且她已回应"
    return False, ""


def hit_end_marker(text: str, scene: Scene) -> bool:
    """他是否说了收场语。"""
    return any(m in text for m in scene.end_markers)


# ==========================================
# 模拟器 system prompt
# ==========================================


def build_user_system_prompt(brief: str, scene: Scene) -> str:
    """模拟"他"的 system prompt = 画像简报 + 剧情卡 + 纪律。

    顺序刻意如此：画像给"他是谁"（统计事实 + 原句 + 毛病清单），剧情卡给"现在发生
    什么"，纪律给输出格式。纪律放最后，因为 LLM 对末尾指令的遵守率最高。
    """
    return (
        "你正在扮演一个真实的人：阿俊，杭州一所高校的大二男生。你要和沈知予（网名青梓）"
        "在 QQ 上聊天。\n\n"
        "# 一、你是什么样的人（来自真实聊天语料的统计，禁止凭印象发挥）\n"
        f"{brief}\n\n"
        "# 二、这一局的剧情设定\n"
        f"【背景】{scene.background}\n"
        f"【你的情绪走向】{scene.mood_arc}\n"
        f"【收场】当你觉得今天聊够了，你会说类似「{scene.end_markers[0]}」「"
        f"{scene.end_markers[1] if len(scene.end_markers) > 1 else scene.end_markers[0]}」"
        "这类话，然后这局就结束了。不要硬撑。\n\n"
        "# 三、硬纪律（违反就算这条作废）\n"
        "1. **只输出你自己要发的消息原文**，一行一条，最多 3 条。不要旁白、"
        "不要动作描写、不要括号补充说明、不要复述她说了什么。\n"
        "2. **照画像里的数字控制形状**：默认 1~3 条短消息、每条 3~10 字、"
        "句末不打标点、极少用问号、**绝不叫她名字或外号**。\n"
        "3. **不许替她接话**：你不知道她内心怎么想，也不要预演她下一句说什么。\n"
        "4. **不许写她的名字**，也不许替她决定情绪。\n"
        "5. 严格执行上面的剧情设定和情绪走向，不要跑出剧本。\n"
    )


# ==========================================
# 指标（纯函数，确定性计算，不用 LLM 裁判）
# ==========================================


def _her_bubbles(turns: Sequence[Dict[str, Any]]) -> List[Tuple[int, str]]:
    """抽出她的所有气泡（按真实发送切段，不是模型原文）。"""
    out: List[Tuple[int, str]] = []
    for t in turns:
        if t.get("speaker") != "her":
            continue
        for b in t.get("bubbles") or ([t["text"]] if t.get("text") else []):
            out.append((t["idx"], b))
    return out


def _norm(s: str) -> str:
    """归一化：去标点空白表情标签，只留实义字，用于算重复片段。"""
    s = STICKER_TOKEN_RE.sub("", s or "")
    return re.sub(r"[^0-9A-Za-z一-鿿]", "", s)


def _char_count(s: str) -> int:
    return len(_norm(s))


def metric_bubble_stats(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """单发口径复用：气泡长度中位 / 句尾问号率 / 称呼率 / 汇报腔命中。"""
    bubbles = _her_bubbles(turns)
    texts = [b for _i, b in bubbles]
    lens = [_char_count(t) for t in texts]
    tail_q = [t for t in texts if QUESTION_MARK_RE.search(t.rstrip()[-1:] if t.rstrip() else "")]
    addr = [t for t in texts if NAME_RE.search(t)]
    report = [(t, [w for w in REPORT_TONE_WORDS if w in t]) for t in texts if any(w in t for w in REPORT_TONE_WORDS)]
    n = len(texts) or 1
    # 称呼命中的逐条留档：按**气泡所在轮次**定位，方便所有者直接翻 transcript。
    # （这里要按气泡循环，不能 `for t in text` —— 那是逐字符迭代，永远匹配不到"小W同学"）
    address_hits = [
        {"turn": idx, "text": b, "term": NAME_RE.findall(b)}
        for idx, b in bubbles
        if NAME_RE.search(b)
    ]
    return {
        "bubble_n": len(texts),
        "len_median": round(statistics.median(lens), 1) if lens else 0.0,
        "len_mean": round(statistics.fmean(lens), 2) if lens else 0.0,
        "len_max": max(lens) if lens else 0,
        "le6_rate": round(sum(1 for x in lens if x <= 6) / n, 4),
        "tail_question_rate": round(len(tail_q) / n, 4),
        "address_rate": round(len(addr) / n, 4),
        "address_hits": address_hits[:10],
        "report_tone_hits": report[:10],
    }


def metric_meme_repeat(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """梗复读：相邻 N 轮内 4-gram 及以上重复片段。

    卡内规则是"同一梗最多玩两轮"，所以窗口内**同一片段出现 ≥2 次**就算她多玩了一轮。
    判定键用归一化后的连续 4 字（中文里 4-gram ≈ 一个短语），并要求它本身有意义
    （长度 ≥4 归一化后已有保证），跨轮比较。
    """
    seq: List[Tuple[int, str]] = []
    for t in turns:
        if t.get("speaker") != "her":
            continue
        seq.append((t["idx"], _norm(t.get("text") or "")))
    counts: Dict[str, List[int]] = {}
    for idx, s in seq:
        grams = {s[i:i + REPEAT_NGRAM] for i in range(len(s) - REPEAT_NGRAM + 1)}
        for g in grams:
            counts.setdefault(g, []).append(idx)

    repeats = []
    for g, idxs in counts.items():
        for a, b in zip(idxs, idxs[1:]):
            if 0 < b - a <= REPEAT_WINDOW:
                repeats.append({"gram": g, "turns": [a, b], "span": b - a})
    repeats.sort(key=lambda r: r["span"])
    worst = repeats[0]["span"] if repeats else None
    if len(repeats) >= REPEAT_FAIL:
        verdict = "FAIL"
    elif len(repeats) >= REPEAT_WARN:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "ngram": REPEAT_NGRAM,
        "window": REPEAT_WINDOW,
        "repeat_count": len(repeats),
        "worst_span": worst,
        "verdict": verdict,
        "detail": repeats[:12],
        "threshold_note": THRESHOLD_NOTE,
    }


def metric_farewell_drag(
    turns: Sequence[Dict[str, Any]], closing_turn: Optional[int]
) -> Dict[str, Any]:
    """告别拖尾：收场段之后她继续追加新话题的回合数。

    closing_turn = 她第一次说出收场语（带温度短气泡收尾/道别）的轮次；None = 全局
    没有可识别的收场点（这本身是一条要报告的事实，不能当成 0 拖尾 PASS）。
    追加新话题的判定：收场之后她的消息里出现疑问句，或出现长度 > 6 字的实质内容。
    """
    if closing_turn is None:
        return {
            "closing_turn": None,
            "drag_turns": 0,
            "verdict": "WARN",
            "detail": ["全程未识别到她的收场点，无法判定拖尾（不等于 0 拖尾）"],
            "threshold_note": THRESHOLD_NOTE,
        }
    drags = []
    for t in turns:
        if t.get("speaker") != "her" or t["idx"] <= closing_turn:
            continue
        text = t.get("text") or ""
        adds = bool(QUESTION_MARK_RE.search(text)) or _char_count(text) > 6
        if adds:
            drags.append({"turn": t["idx"], "text": text[:60]})
    n = len(drags)
    if n >= DRAG_FAIL_TURNS:
        verdict = "FAIL"
    elif n >= DRAG_WARN_TURNS:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "closing_turn": closing_turn,
        "drag_turns": n,
        "verdict": verdict,
        "detail": drags[:10],
        "threshold_note": THRESHOLD_NOTE,
    }


CLOSING_HINT_RE = re.compile(r"(晚安|拜拜|睡了|下线|先忙|不聊了|明天聊|回头聊|去上课|先吃饭|先走)")


def metric_length_inflation(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """话量膨胀曲线：她的气泡字数随回合的滑动均值，报斜率。

    正常应平稳（斜率≈0）；持续上行 = 话量膨胀。用最小二乘斜率，单位"字/轮"。
    """
    pts = [
        (t["idx"], _char_count(t.get("text") or ""))
        for t in turns
        if t.get("speaker") == "her" and t.get("text")
    ]
    if len(pts) < 3:
        return {
            "points": len(pts), "slope": 0.0, "verdict": "WARN",
            "detail": ["有效轮次不足 3，无法算斜率"], "threshold_note": THRESHOLD_NOTE,
        }
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in pts) / denom if denom else 0.0
    half = len(pts) // 2
    first = round(statistics.fmean(ys[:half]), 1)
    second = round(statistics.fmean(ys[half:]), 1)
    if slope >= INFLATE_FAIL_SLOPE:
        verdict = "FAIL"
    elif slope >= INFLATE_WARN_SLOPE:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "points": len(pts),
        "slope": round(slope, 3),
        "first_half_mean": first,
        "second_half_mean": second,
        "verdict": verdict,
        "series": [{"turn": x, "chars": y} for x, y in pts],
        "threshold_note": THRESHOLD_NOTE,
    }


def metric_address_drift(turns: Sequence[Dict[str, Any]], stage: int) -> Dict[str, Any]:
    """称呼漂移：全局称呼词分布 vs 当前阶段允许集。

    阶段允许集按角色卡的称呼线：阶段 0~3 允许「小W同学」作礼节性偶用但**不该出现**
    （真人基线 9 个月 0 次），阶段 4+ 才逐步放开。这里按 benchmark_v4 口径：
    「阿俊」出现即 FAIL（长称呼在初识阶段出现 = 越界）。
    """
    hits = []
    per_turn = []
    for t in turns:
        if t.get("speaker") != "her":
            continue
        text = t.get("text") or ""
        found = NAME_RE.findall(text)
        if found:
            per_turn.append({"turn": t["idx"], "text": text[:60]})
            hits.extend(found)
    long_name = [h for h in hits if h == "阿俊"]
    n_her = sum(1 for t in turns if t.get("speaker") == "her") or 1
    if long_name:
        verdict = "FAIL"
    elif len(hits) / n_her > ADDRESS_WARN_RATE:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "stage": stage,
        "total_hits": len(hits),
        "long_name_hits": len(long_name),
        "rate": round(len(hits) / n_her, 4),
        "verdict": verdict,
        "detail": per_turn[:10],
        "threshold_note": THRESHOLD_NOTE,
    }


def _classify_preceding_context(user_text: str) -> str:
    """判定 [沉默] 之前那句话属于哪类语境（供合规判定）。"""
    s = (user_text or "").strip()
    if not s:
        return "空"
    if STICKER_TOKEN_RE.search(s):
        return "表情包"
    if _char_count(s) <= 4 and re.fullmatch(r"[\s嗯哦噢啊额哈嘿嘿哦噢喔唔嗯嗯okOK对的了呗吧]*", s):
        return "纯语气词"
    if CLOSING_HINT_RE.search(s):
        return "收场语"
    return "实质内容"


def metric_silence_compliance(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """[沉默] 合规：每次 [沉默] 的前置语境是否命中卡内白名单。

    卡内规则是"沉默权仅限他纯语气词/表情包承接告别场景"。这里检查她每一次沉默前，
    他最后一条消息是不是那三类之一。**不合规不是 bug 本身，而是要交给所有者看的发现**，
    所以逐条留原文。
    """
    events = []
    for i, t in enumerate(turns):
        if t.get("speaker") != "her":
            continue
        text = (t.get("text") or "").strip()
        if text != SILENCE_TOKEN and SILENCE_TOKEN not in text:
            continue
        prev_user = ""
        for j in range(i - 1, -1, -1):
            if turns[j].get("speaker") == "user":
                prev_user = turns[j].get("text") or ""
                break
        ctx = _classify_preceding_context(prev_user)
        events.append({
            "turn": t["idx"],
            "preceding_user": prev_user[:60],
            "context": ctx,
            "allowed": ctx in SILENCE_WHITELIST_CTX,
        })
    bad = [e for e in events if not e["allowed"]]
    # **0 次沉默 = 这项没跑到，不是"合规"**。S1 这种不制造沉默场景的卡本来就该是 0，
    # 报 PASS 等于把"没验"说成"验过了"。这是评测工具自己骗自己的典型形态。
    if not events:
        verdict = "N/A"
    elif len(bad) >= SILENCE_FAIL:
        verdict = "FAIL"
    else:
        verdict = "PASS"
    return {
        "silence_n": len(events),
        "violations": len(bad),
        "verdict": verdict,
        "not_run_reason": None if events else "全程她没有用过 [沉默]，本项无观测样本",
        "whitelist": list(SILENCE_WHITELIST_CTX),
        "detail": events[:12],
        "threshold_note": THRESHOLD_NOTE,
    }


def metric_sticker_usage(turns: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """表情包：全程使用次数、重复率；语境匹配**只留原文不做自动判**（BENCHMARK_V4 纪律）。"""
    uses = []
    for t in turns:
        if t.get("speaker") != "her":
            continue
        text = t.get("text") or ""
        for name in STICKER_TOKEN_RE.findall(text):
            uses.append({"turn": t["idx"], "name": name.strip(), "context": text[:50]})
    names = [u["name"] for u in uses]
    uniq = len(set(names))
    repeat_rate = round(1 - uniq / len(names), 4) if names else 0.0
    # 同 [沉默]：一次都没用 = 无观测样本，报 N/A 而不是 PASS
    if not uses:
        verdict = "N/A"
    elif len(uses) > STICKER_WARN_N or repeat_rate > STICKER_REPEAT_WARN_RATE:
        verdict = "WARN"
    else:
        verdict = "PASS"
    return {
        "count": len(uses),
        "unique": uniq,
        "repeat_rate": repeat_rate,
        "verdict": verdict,
        "not_run_reason": None if uses else "全程她没有发过 [sticker:]，本项无观测样本",
        "detail": uses[:15],
        "note": "语境匹配留原文交所有者抽读，不做自动判",
        "threshold_note": THRESHOLD_NOTE,
    }


def find_her_closing_turn(turns: Sequence[Dict[str, Any]]) -> Optional[int]:
    """找出她第一次收场的轮次（带温度短气泡收尾 / 道别），供拖尾指标用。"""
    for t in turns:
        if t.get("speaker") != "her" or not t.get("text"):
            continue
        text = t["text"]
        if CLOSING_HINT_RE.search(text) or is_silence_output(text):
            return t["idx"]
    return None


def compute_metrics(
    turns: Sequence[Dict[str, Any]], stage: int, cost: float
) -> Dict[str, Any]:
    """汇总全部指标。单发口径 + 多轮专项，每项都带 PASS/WARN/FAIL。"""
    closing = find_her_closing_turn(turns)
    multi = {
        "梗复读": metric_meme_repeat(turns),
        "告别拖尾": metric_farewell_drag(turns, closing),
        "话量膨胀": metric_length_inflation(turns),
        "称呼漂移": metric_address_drift(turns, stage),
        "[沉默]合规": metric_silence_compliance(turns),
        "表情包": metric_sticker_usage(turns),
    }
    single = metric_bubble_stats(turns)
    verdicts = {k: v["verdict"] for k, v in multi.items()}
    # N/A = 本项无观测样本（没跑到），**不算通过也不算失败**，但必须显式列出，
    # 否则报告读起来像"这几项都验过了"。overall 只在真跑过的项上判定。
    not_run = {k: v.get("not_run_reason") or "无观测样本" for k, v in multi.items()
               if v["verdict"] == "N/A"}
    judged = [v for v in verdicts.values() if v in ("PASS", "WARN", "FAIL")]
    overall = (
        "FAIL" if "FAIL" in judged
        else "WARN" if "WARN" in judged
        else "PASS"
    )
    return {
        "turns": len(turns),
        "her_turns": sum(1 for t in turns if t.get("speaker") == "her"),
        "stage": stage,
        "cost_cny": round(cost, 4),
        "overall": overall,
        "judged_metrics": len(judged),
        "verdicts": verdicts,
        "not_run": not_run,
        "not_run_note": "N/A 的项本局没跑到，不计入 overall；要看这些项请换对应剧情卡",
        "single_turn_baseline": single,
        "multi_turn": multi,
        "threshold_note": THRESHOLD_NOTE,
    }


# ==========================================
# transcript 渲染
# ==========================================


def render_transcript(
    run_id: str, scene: Scene, turns: Sequence[Dict[str, Any]], meta: Dict[str, Any]
) -> str:
    """按 QQ 气泡格式渲染：她左侧、他右侧，带时间戳。

    三列表格是为了让"谁说的"一眼可分——这正是所有者抽读时要盯的东西
    （她有没有在该收的时候收、有没有在该接的时候接）。
    """
    L: List[str] = []
    A = L.append
    A(f"# 对聊仿真 transcript · {run_id}")
    A("")
    A(f"- 剧情卡：**{scene.key} {scene.title}** — 观察点：{scene.probe}")
    A(f"- 起始时钟：{meta.get('start_time')}（星期{'一二三四五六日'[datetime.strptime(meta['start_time'], '%Y-%m-%d %H:%M').weekday()]}）")
    A(f"- 她的模型：{meta.get('her_model')} ｜ 模拟他的模型：{meta.get('user_model')}")
    A(f"- 回合数：{len(turns)} ｜ 结束原因：{meta.get('end_reason')}")
    A(f"- 成本实报：¥{meta.get('cost', 0):.4f}")
    A("")
    A("> 左列是他（AI 扮演），右列是她（青梓全管道实跑）。`[沉默]` 表示她本轮选择不回。")
    A("")
    A("| # | 时间 | 他 | 她 |")
    A("| --- | --- | --- | --- |")

    him: Dict[int, List[Dict[str, Any]]] = {}
    her: Dict[int, List[Dict[str, Any]]] = {}
    for t in turns:
        bucket = him if t.get("speaker") == "user" else her
        bucket.setdefault(t["idx"], []).append(t)

    for idx in sorted(set(him) | set(her)):
        ts = ""
        for t in turns:
            if t["idx"] == idx:
                ts = t.get("time", "")
                break
        left = "<br>".join(_esc(x.get("text") or "") for x in him.get(idx, []))
        right = "<br>".join(_esc(x.get("text") or "") for x in her.get(idx, []))
        extra = []
        for x in her.get(idx, []):
            if x.get("silenced"):
                extra.append("_（触发 [沉默] 闸门，规则闸门拦截，未发出）_")
            if x.get("note"):
                extra.append(f"_{x['note']}_")
        right = right + ("<br>" + "<br>".join(extra) if extra else "")
        A(f"| {idx} | {ts} | {left} | {right} |")
    A("")
    A("---")
    A("")
    A(f"- 结束原因：{meta.get('end_reason')}")
    if meta.get("end_marker_turn") is not None:
        A(f"- 他命中收场语的轮次：第 {meta['end_marker_turn']} 轮")
    A("- 完整内部状态快照见同目录 `raw.json`，指标判定见 `metrics.json`。")
    return "\n".join(L)


def _esc(s: str) -> str:
    return (s or "").replace("|", "\\|").replace("\n", "<br>")


# ==========================================
# 仿真驱动器
# ==========================================


@dataclass
class TurnRecord:
    idx: int
    speaker: str
    text: str
    time: str
    bubbles: List[str] = field(default_factory=list)
    silenced: bool = False
    note: str = ""
    snapshot: Dict[str, Any] = field(default_factory=dict)


class DuoSimulator:
    """一局对聊仿真。

    青梓侧走**生产同一条** TurnHandler.handle_turn（视觉/组装/流式生成/切段/
    落库/加固/观察者结算全在里面），只是把"发到 QQ"换成收集到内存、
    把"真睡"换成记账。用户侧是 v4-pro 扮演的他。
    """

    def __init__(
        self,
        config: Config,
        scene: Scene,
        run_dir: str,
        start_time: str,
        max_turns: Optional[int] = None,
        max_cost: float = DEFAULT_MAX_COST,
        her_model: Optional[str] = None,
        user_model: Optional[str] = None,
        seed: Optional[int] = None,
        user_reply_fn: Optional[Callable[[List[Dict[str, Any]], str], Awaitable[str]]] = None,
    ):
        self.config = config
        self.scene = scene
        self.run_dir = run_dir
        self.start_time = start_time
        self.max_turns = max_turns or scene.max_turns
        self.max_cost = max_cost
        self.her_model = her_model
        self.user_model = user_model or (config.llm.active().chat)
        self.seed = seed
        self._user_reply_fn = user_reply_fn

        self.clock = Clock(datetime.strptime(start_time, "%Y-%m-%d %H:%M"))
        self.sleep_log: List[Dict[str, Any]] = []
        self.turns: List[TurnRecord] = []
        self.sent_chunks: List[Dict[str, Any]] = []
        self.raw_turns: List[Dict[str, Any]] = []
        # 每次主动消息周期的留痕（发没发出去、发不出去是闸门拦还是决策层放弃）
        self.proactive_log: List[Dict[str, Any]] = []
        self.end_marker_turn: Optional[int] = None
        self.end_reason = ""
        self.silence_done = False
        self._cost_tripped = False
        self._llm_row_cursor = 0

    # ---------- 生命周期 ----------

    async def setup(self) -> None:
        os.makedirs(self.run_dir, exist_ok=True)
        self.db_path = os.path.join(self.run_dir, "sandbox.db")
        # 临时库必须**从零建**。若 run_dir 里已有一局的 sandbox.db，init_tables()
        # 不会清表，新一局会直接继承上一局的 turns/好感度/记忆/日记——多轮指标
        # （拖尾、话量膨胀曲线）会被污染成"跨局累积"的假象。
        # 本工具独占 data/duo_sim/<run_id>/，所以直接删旧库重建是安全的。
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
            logger.info("[DuoSim] 发现上一局残留的临时库，已删除重建：%s", self.db_path)
        self.db = Database(self.db_path)
        await self.db.init_tables()

        self.persona = Persona.load(self.config.character.path)
        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)
        await self.stickers.sync_initial_stickers()

        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(self.config.llm, self.db)
        self.user_gateway = LLMGateway(self.config.llm, self.db)   # 两侧成本分开记
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)
        self.arcs = LifeArcManager(self.db, self.gateway, self.persona)

        self.assembler = PromptAssembler(
            self.persona, self.affection, self.mood, self.memory,
            self.stickers, self.db,
            holidays_provider=self.config.get_holidays,
            arcs=self.arcs,
        )
        self.replier = Replier(self.config.reply, self.stickers)
        self.observer = Observer(
            self.gateway, self.affection, self.mood, self.memory, self.stickers, self.db
        )
        self.proactive = ProactiveScheduler(
            config=self.config.proactive,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=self.gateway,
            db=self.db,
            send_msg_fn=self._collect_chunk,
            assembler=self.assembler,
            holidays_provider=self.config.get_holidays,
            set_typing_fn=self._fake_typing,
            timing_config=self.config.timing,
            arcs=self.arcs,
        )
        self.turn_handler = TurnHandler(
            config=self.config,
            gateway=self.gateway,
            assembler=self.assembler,
            replier=self.replier,
            memory=self.memory,
            observer=self.observer,
            proactive=self.proactive,
            send_chunk_fn=self._collect_chunk,
            set_typing_fn=self._fake_typing,
            timing_config=self.config.timing,
        )

        brief = self._load_brief()
        self.user_system_prompt = build_user_system_prompt(brief, self.scene)

    def _load_brief(self) -> str:
        if not os.path.exists(PERSONA_BRIEF):
            raise FileNotFoundError(
                f"画像简报不存在：{PERSONA_BRIEF}\n"
                "请先跑：./venv/Scripts/python.exe scripts/duo_sim_persona.py"
            )
        with open(PERSONA_BRIEF, "r", encoding="utf-8") as f:
            return f.read()

    async def close(self) -> None:
        try:
            await self.gateway.close()
        except Exception:
            pass
        try:
            await self.user_gateway.close()
        except Exception:
            pass
        try:
            await self.db.close()
        except Exception:
            pass

    # ---------- 回调 ----------

    async def _collect_chunk(self, chunk: Dict[str, Any]) -> None:
        """替代"发到 QQ"：把切好的气泡收集起来（transcript 渲染的就是它们）。"""
        self.sent_chunks.append(dict(chunk))

    async def _fake_typing(self, typing: bool) -> bool:
        """沙箱版"正在输入"：只记账不外呼，保证零外部副作用。"""
        self.sleep_log.append({"typing": typing})
        return True

    # ---------- 成本 ----------

    async def spent(self) -> float:
        """本局累计成本（元）。取自临时库的 llm_calls，两侧调用都在里面。"""
        row = await self.db.fetchone("SELECT SUM(cost_estimate) AS c FROM llm_calls")
        # db.fetchone 返回 sqlite3.Row（无 .get()），空表时可能是 None
        if row is None:
            return 0.0
        return float(row["c"] or 0.0)

    async def _llm_rows_since_cursor(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, purpose, model, prompt_tokens, completion_tokens, cost_estimate "
            "FROM llm_calls WHERE id > ? ORDER BY id",
            (self._llm_row_cursor,),
        )
        if rows:
            self._llm_row_cursor = int(rows[-1]["id"])
        return [dict(r) for r in rows]

    async def _check_budget(self) -> None:
        if await self.spent() >= self.max_cost:
            self._cost_tripped = True
            raise CostBreakerTripped(
                f"成本熔断：累计 ¥{await self.spent():.4f} ≥ 上限 ¥{self.max_cost}"
            )

    # ---------- 用户侧（扮演他） ----------

    async def _recent_dialogue(self, limit: int = 8) -> List[Dict[str, Any]]:
        turns = await self.memory.get_recent_turns(limit=limit)
        return [dict(t) for t in turns]

    async def _llm_user_reply(self, dialogue: List[Dict[str, Any]], instruction: str) -> str:
        """调 v4-pro 生成他的下一条消息。stream_chat → thinking 走 thinking_chat（与主聊同档）。"""
        lines = []
        for t in dialogue[-10:]:
            who = "他" if t.get("role") == "user" else "她"
            lines.append(f"{who}：{t.get('content') or ''}")
        user_prompt = (
            ("【最近的对话】\n" + "\n".join(lines) if lines else "（还没有对话）")
            + f"\n\n【现在】{instruction}\n\n"
            "输出他这一条要发的消息原文（可多行，每行一条），不要任何其他内容。"
        )
        parts: List[str] = []
        async for piece in self.user_gateway.stream_chat(
            messages=[
                {"role": "system", "content": self.user_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=self.user_model,
            purpose=USER_PURPOSE,
        ):
            parts.append(piece)
        return "".join(parts).strip()

    async def _user_message(self, dialogue: List[Dict[str, Any]], instruction: str) -> str:
        if self._user_reply_fn is not None:
            return await self._user_reply_fn(dialogue, instruction)
        return await self._llm_user_reply(dialogue, instruction)

    async def _coerce_user_text(self, raw_text: str, idx: int) -> str:
        """模拟器偶尔会回空串，必须兜住——**空消息会污染下游所有指标**。

        踩过的坑（S3 冒烟实测）：第 14 轮模型返回空串，于是她对着一句空消息选了
        `[沉默]`，"[沉默] 合规" 指标如实判成 FAIL。那不是青梓的病灶，是仿真器自己
        的 bug——但报告读起来就是"她违规了"。所以：空 → 重试一次 → 还空就用他
        语料里最高频的应答（"嗯"）兜底，并把这次兜底**写进 raw.json**，
        让看报告的人知道这一轮不是她的问题。
        """
        text = (raw_text or "").strip()
        if text:
            return text
        logger.warning("[DuoSim] 第 %d 轮模拟器返回空串，重试一次", idx)
        # 重试走同一个入口 _user_message，注入的假回复函数才会被一并尊重
        retried = await self._user_message(
            await self._recent_dialogue(),
            "上一条你什么都没输出。现在必须输出你要发的消息原文，"
            "哪怕只有一个字（例如「嗯」）。不要输出任何解释或空内容。",
        )
        text = (retried or "").strip()
        if text:
            self.raw_turns.append({
                "idx": idx, "speaker": "system",
                "note": "模拟器首轮返回空串，已重试补上（非青梓侧问题）",
            })
            return text
        self.raw_turns.append({
            "idx": idx, "speaker": "system",
            "note": "模拟器连续两次返回空串，已用兜底应答「嗯」代替（非青梓侧问题）",
        })
        return USER_FALLBACK_REPLY

    # ---------- 青梓侧 ----------

    async def _her_turn(self, idx: int, user_text: str) -> TurnRecord:
        """驱动一轮全管道。返回本轮记录（含真实切好的气泡）。"""
        before = await self._state_snapshot()
        self.sent_chunks = []
        llm_before = self._llm_row_cursor

        await self.turn_handler.handle_turn(user_text, None)
        # observer 在生产是 create_task 异步结算；仿真要拿到评分写进 raw.json，
        # 这里有界等待它落地（不改动生产代码）
        observer_data = await self._drain_observer()

        bubbles = [c.get("content", "") for c in self.sent_chunks if c.get("type") == "text"]
        stickers = [c.get("file", "") for c in self.sent_chunks if c.get("type") == "sticker"]
        silenced = not self.sent_chunks
        text = "\n".join([b for b in bubbles if b])

        rec = TurnRecord(
            idx=idx,
            speaker="her",
            text=text or (SILENCE_TOKEN if silenced else ""),
            time=self.clock.now().strftime("%Y-%m-%d %H:%M"),
            bubbles=bubbles,
            silenced=silenced,
            snapshot={
                "user_text": user_text,
                "bubbles": bubbles,
                "stickers": stickers,
                "silenced": silenced,
                "observer": observer_data,
                "state_before": before,
                "state_after": await self._state_snapshot(),
                "llm_calls": await self._llm_rows_since_cursor() if llm_before is not None else [],
                "system_prompt_len": len(getattr(self.assembler, "last_assembled_prompt", "") or ""),
            },
        )
        self.turns.append(rec)
        # text/time 必须进 raw.json：所有者抽读、以及"零成本重算指标"都要靠它还原
        # transcript，只存 bubbles 的话重算会拿到空文本、判据直接悬空
        self.raw_turns.append({
            "idx": idx, "speaker": "her", "text": rec.text, "time": rec.time, **rec.snapshot
        })
        return rec

    async def _drain_observer(self, timeout: float = 30.0) -> Optional[Dict[str, Any]]:
        """等观察者异步结算落地。

        生产是 fire-and-forget；仿真要它的评分，所以轮询 observer 模块的
        `recent_observer_logs`（settle_turn 每次 append 一个新的 dict）。
        超时返回 None 并在记录里标出来——**不能假装有评分**。

        ⚠ 这里必须用**对象身份**（`is not`）判断，不能用"长度变大了吗"：
        `recent_observer_logs` 是 `collections.deque(maxlen=5)`，装满之后 append
        会挤掉最旧的元素，**长度永远停在 5**。用长度判断会导致"永远等不到"，
        每一轮都白等满 timeout（30 秒 × 25 回合 = 12 分钟），而且症状是
        "observer 字段全空"——看起来像观察者没跑，其实是测量方法错了。
        """
        from companion import observer as obs_mod

        before = list(obs_mod.recent_observer_logs)
        marker = before[-1] if before else None
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            cur = list(obs_mod.recent_observer_logs)
            if cur and cur[-1] is not marker:
                data = cur[-1]
                if not isinstance(data, dict):
                    return None
                return {
                    k: data.get(k)
                    for k in (
                        "self_disclosure", "responsiveness", "warmth_score", "resonance",
                        "moments", "facts", "followups", "mood_impact",
                    )
                    if k in data
                }
            await asyncio.sleep(0.05)
        return None

    async def _state_snapshot(self) -> Dict[str, Any]:
        aff = await self.affection.get_state()
        mood = await self.mood.get_state()
        return {
            "stage": aff.get("stage"),
            "composite": round(float(aff.get("composite", 0.0)), 2),
            "dims": {k: round(float(v), 2) for k, v in (aff.get("dims") or {}).items()},
            "mood": {
                "v": round(float(mood.get("v", 0.0)), 2),
                "a": round(float(mood.get("a", 0.0)), 2),
                "t": round(float(mood.get("t", 0.0)), 3),
                "frustration": round(float(mood.get("frustration", 0.0)), 2),
            },
        }

    # ---------- 主动消息周期（S3 静默段） ----------

    async def _proactive_cycle(self, idx: int) -> Optional[TurnRecord]:
        """驱动一次 ProactiveScheduler.trigger_cycle（真跑规则闸门 + LLM 决策层）。

        **每一次周期都留痕**，发没发出去都要记：
          · 没有发生任何 LLM 调用 → 规则闸门在零成本层就拦了（免打扰/60 分钟闸门/未回闸门）
          · 有 LLM 调用但没发出   → 决策层自己选择不发
        这两种是完全不同的发现，混成"什么都没发生"就等于没验。真正发出的回合才进
        transcript（要让所有者的注意力落在她真的说出口的话上）。
        """
        self.sent_chunks = []
        llm_before = self._llm_row_cursor
        await self.proactive.trigger_cycle()
        bubbles = [c.get("content", "") for c in self.sent_chunks if c.get("type") == "text"]
        stickers = [c.get("file", "") for c in self.sent_chunks if c.get("type") == "sticker"]
        calls = await self._llm_rows_since_cursor()
        sent = bool(bubbles or stickers)
        self.proactive_log.append({
            "idx": idx,
            "time": self.clock.now().strftime("%Y-%m-%d %H:%M"),
            "sent": sent,
            "outcome": "已发出" if sent else ("决策层选择不发" if calls else "规则闸门拦截"),
            "llm_call_n": len(calls),
            "text": "\n".join(b for b in bubbles if b),
        })
        if not sent:
            self.raw_turns.append({
                "idx": idx, "speaker": "system", "proactive": True,
                "note": f"静默段主动消息未发出（{self.proactive_log[-1]['outcome']}）",
            })
            return None
        text = "\n".join(b for b in bubbles if b)
        rec = TurnRecord(
            idx=idx,
            speaker="her",
            text=text,
            time=self.clock.now().strftime("%Y-%m-%d %H:%M"),
            bubbles=bubbles,
            note="主动消息（trigger_cycle）",
            snapshot={
                "proactive": True,
                "bubbles": bubbles,
                "stickers": stickers,
                "llm_calls": calls,
                "state_after": await self._state_snapshot(),
            },
        )
        self.turns.append(rec)
        self.raw_turns.append({
            "idx": idx, "speaker": "her", "proactive": True,
            "text": rec.text, "time": rec.time, **rec.snapshot,
        })
        return rec

    # ---------- 主循环 ----------

    async def run(self) -> Dict[str, Any]:
        rng = random.Random(self.seed)
        scene = self.scene
        idx = 0

        # 第 1 轮：他的开场消息直接用卡里的定值（不烧 API，也保证每局起点一致）
        idx += 1
        user_text = scene.opening
        if hit_end_marker(user_text, scene) and 1 >= scene.min_turns:
            self.end_marker_turn = idx
        self.clock.advance(rng.uniform(*scene.reply_delay_min))
        self.turns.append(TurnRecord(
            idx=idx, speaker="user", text=user_text,
            time=self.clock.now().strftime("%Y-%m-%d %H:%M"),
        ))
        self.raw_turns.append({"idx": idx, "speaker": "user", "text": user_text,
                               "opening": True})
        await self._her_turn(idx, user_text)

        while True:
            done, reason = should_end(
                scene, idx, self.end_marker_turn, self.silence_done, max_turns=self.max_turns
            )
            if done:
                self.end_reason = reason
                break
            if self._cost_tripped:
                self.end_reason = "成本熔断"
                break

            idx += 1
            silence_phase = (
                scene.silence_after_turn is not None
                and idx > scene.silence_after_turn
                and not self.silence_done
            )
            try:
                if silence_phase:
                    # 静默段：不伪造用户消息，只推进时钟 + 驱动主动消息。
                    # 发没发出去的留痕在 _proactive_cycle 里（它自己会记 outcome）。
                    self.clock.advance(scene.silence_step_min)
                    await self._proactive_cycle(idx)
                    if idx - scene.silence_after_turn >= scene.silence_turns:
                        self.silence_done = True
                else:
                    await self._check_budget()
                    self.clock.advance(rng.uniform(*scene.reply_delay_min))
                    instruction = self._user_instruction(idx)
                    dialogue = await self._recent_dialogue()
                    user_text = await self._user_message(dialogue, instruction)
                    user_text = await self._coerce_user_text(user_text, idx)
                    # 收场语只在过了最小回合数之后才承认：早期一句"算了"不该把
                    # 整局按死在第 2 轮（那样多轮病灶一条都验不到）
                    if (
                        hit_end_marker(user_text, scene)
                        and self.end_marker_turn is None
                        and idx >= scene.min_turns
                    ):
                        self.end_marker_turn = idx
                    self.turns.append(TurnRecord(
                        idx=idx, speaker="user", text=user_text,
                        time=self.clock.now().strftime("%Y-%m-%d %H:%M"),
                    ))
                    self.raw_turns.append({"idx": idx, "speaker": "user", "text": user_text})
                    await self._her_turn(idx, user_text)
            except CostBreakerTripped as e:
                logger.warning("成本熔断：%s", e)
                self.end_reason = f"成本熔断（¥{await self.spent():.4f} ≥ ¥{self.max_cost}）"
                break
            except Exception as e:  # 单轮异常不许毁掉整局已完成的 transcript
                logger.exception("第 %d 轮异常，记录后继续：%s", idx, e)
                self.raw_turns.append({
                    "idx": idx, "speaker": "system",
                    "note": f"本轮异常：{type(e).__name__}: {e}",
                })
                self.end_reason = f"第 {idx} 轮异常中止"
                break

        return await self.finalize()

    def _user_instruction(self, idx: int) -> str:
        scene = self.scene
        if scene.silence_after_turn is not None and idx > scene.silence_after_turn:
            return (
                "剧情推进到你不耐烦的阶段：回得越来越短，或者干脆不说话。"
                "如果这轮你决定消失，就只回最短的一个字或不回。"
            )
        if self.end_marker_turn is not None:
            return "你已经说过要收场了，这轮就正式道别，别再开新话题。"
        return "按剧情和情绪走向继续你们的对话。"

    async def finalize(self) -> Dict[str, Any]:
        state = await self._state_snapshot()
        cost = await self.spent()
        metrics = compute_metrics([_rec_to_dict(r) for r in self.turns], state["stage"], cost)
        meta = {
            "run_id": os.path.basename(self.run_dir),
            "scene": self.scene.key,
            "scene_title": self.scene.title,
            "start_time": self.start_time,
            "end_time": self.clock.now().strftime("%Y-%m-%d %H:%M"),
            "end_reason": self.end_reason,
            "end_marker_turn": self.end_marker_turn,
            "her_model": self.her_model or self.config.llm.active().chat,
            "user_model": self.user_model,
            "turns": len(self.turns),
            "cost": cost,
            "db": self.db_path,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(os.path.join(self.run_dir, "transcript.md"), "w", encoding="utf-8") as f:
            f.write(render_transcript(meta["run_id"], self.scene,
                                      [_rec_to_dict(r) for r in self.turns], meta))
        with open(os.path.join(self.run_dir, "raw.json"), "w", encoding="utf-8") as f:
            json.dump({"meta": meta, "final_state": state, "turns": self.raw_turns,
                       "proactive_cycles": self.proactive_log},
                      f, ensure_ascii=False, indent=2)
        with open(os.path.join(self.run_dir, "metrics.json"), "w", encoding="utf-8") as f:
            json.dump(metrics, f, ensure_ascii=False, indent=2)
        return {"meta": meta, "metrics": metrics, "final_state": state}


def _rec_to_dict(r: TurnRecord) -> Dict[str, Any]:
    return {
        "idx": r.idx, "speaker": r.speaker, "text": r.text, "time": r.time,
        "bubbles": r.bubbles, "silenced": r.silenced, "note": r.note,
    }


# ==========================================
# CLI
# ==========================================


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="FIXES18 对聊仿真器")
    p.add_argument("--scene", default="S1", choices=sorted(SCENES.keys()),
                   help="剧情卡（S1/S2/S3）")
    p.add_argument("--turns", type=int, default=None, help="覆盖剧情卡默认回合上限")
    p.add_argument("--start-time", default=DEFAULT_START_TIME,
                   help="起始时钟 'YYYY-MM-DD HH:MM'")
    p.add_argument("--max-cost", type=float, default=DEFAULT_MAX_COST,
                   help="单局成本熔断上限（元）")
    p.add_argument("--her-model", default=None, help="她的主聊模型（默认取 config 激活预设）")
    p.add_argument("--user-model", default=None, help="模拟他的模型（默认同她）")
    p.add_argument("--seed", type=int, default=None, help="随机种子（延迟抖动用）")
    p.add_argument("--run-id", default=None, help="产物目录名（默认按场景+时间戳）")
    p.add_argument("--smoke", action="store_true", help="缩短版冒烟（15 回合）")
    p.add_argument("--config", default="config.toml", help="配置文件路径（只读）")
    return p


async def amain(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    scene = SCENES[args.scene]
    turns = 15 if args.smoke else args.turns
    run_id = args.run_id or (
        f"{scene.key}-smoke-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        if args.smoke
        else f"{scene.key}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    )
    run_dir = os.path.join(OUT_ROOT, run_id)
    if os.path.exists(run_dir) and not args.run_id:
        print(f"产物目录已存在：{run_dir}（请用 --run-id 指定别的名字）", file=sys.stderr)
        return 2

    config = Config.load(args.config)
    if args.her_model:
        # 只在本次运行内改主聊模型，不写回 config.toml
        active = config.llm.active()
        active.chat = args.her_model

    print("=" * 68)
    print(f"对聊仿真 · 剧情卡 {scene.key}「{scene.title}」")
    print(f"观察点：{scene.probe}")
    print(f"起始时钟 {args.start_time} ｜ 回合上限 {turns} ｜ 成本熔断 ¥{args.max_cost}")
    print("=" * 68, flush=True)

    sim = DuoSimulator(
        config=config, scene=scene, run_dir=run_dir, start_time=args.start_time,
        max_turns=turns, max_cost=args.max_cost, her_model=args.her_model,
        user_model=args.user_model, seed=args.seed,
    )
    await sim.setup()
    try:
        with _TimePatch(sim.clock, sim.sleep_log):
            result = await sim.run()
    finally:
        await sim.close()

    m = result["meta"]
    mt = result["metrics"]
    print("\n" + "=" * 68)
    print(f"结束原因：{m['end_reason']}")
    print(f"实际回合：{m['turns']} ｜ 结束时钟：{m['end_time']}")
    print(f"成本实报：¥{m['cost']:.4f}")
    print(f"终态：阶段 {result['final_state']['stage']} 复合分 {result['final_state']['composite']}")
    print("\n多轮指标判定：")
    for k, v in mt["verdicts"].items():
        print(f"  {v:<4} {k}")
    if mt["not_run"]:
        print("\n⚠ 本局未跑到（不算通过，也不算失败）：")
        for k, why in mt["not_run"].items():
            print(f"  N/A  {k} — {why}")
    s = mt["single_turn_baseline"]
    print(
        f"\n单发基线：气泡 {s['bubble_n']} 条 ｜ 中位 {s['len_median']} 字 ｜ "
        f"句尾问号 {s['tail_question_rate'] * 100:.1f}% ｜ "
        f"称呼 {s['address_rate'] * 100:.1f}%"
    )
    print(f"\n产物目录：{run_dir}")
    print("  transcript.md（原文，交给所有者抽读） / raw.json（内部状态快照） / metrics.json")
    return 0


def main() -> None:
    args = build_arg_parser().parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
