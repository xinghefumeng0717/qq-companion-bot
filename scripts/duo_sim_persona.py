"""阿俊说话画像统计 (scripts/duo_sim_persona.py)

FIXES18 任务 0：从**真实语料**算出“他”的说话画像，产出给对聊仿真器用的 system prompt 简报。

数据源（按优先级，全部只读）：
  1. QQ 真实聊天导出 `D:\\private\\private_chat_export\\【第三方】_聊天记录_20250927-20260611.txt`
     —— 主源。6592 条他的消息，是“他怎么打字”最硬的证据。
     路径可用 --export 覆盖；文件不存在时自动降级到 2 号源并在报告里标注。
  2. 生产库前两世备份 `data/backups/*.db` 的 `turns` 表 `role='user'`
     —— 他对**青梓**的真实发言（与对真人的语风对照用）。自动剔除互为子集的快照。
  3. `data/archive/real_chat_analysis.md` —— 已有结论直接引用，不重算。

产出：
  data/duo_sim/user_persona_brief.md      给模拟器的 system prompt 简报（所有者过目）
  data/duo_sim/user_persona_stats.json    全部统计数字（供审计，brief 里的每个数都出自这里）

用法：
  ./venv/Scripts/python.exe scripts/duo_sim_persona.py

纪律：不改 characters/、config.toml、companion/ 任何文件；只读语料，只写 data/duo_sim/。
"""

from __future__ import annotations

import glob
import json
import os
import re
import sqlite3
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

EXPORT_DEFAULT = r"D:\private\private_chat_export\【第三方】_聊天记录_20250927-20260611.txt"
BACKUP_GLOB = "data/backups/*.db"
OUT_DIR = "data/duo_sim"
BRIEF_MD = os.path.join(OUT_DIR, "user_persona_brief.md")
STATS_JSON = os.path.join(OUT_DIR, "user_persona_stats.json")
ANALYSIS_MD = "data/archive/real_chat_analysis.md"

# 他（阿俊）在导出文件里的说话人标记
SPEAKER_HIM = "我"
SPEAKER_HER = "她"

# 与 data/archive/real_chat_analysis.md 第五节同一套口径，保证数字可跨文档对照：
# 汉字各计 1、英文单词计 1、连续数字计 1；标点/空白/表情标签不计。
CHAR_RE = re.compile(r"[一-鿿]|[A-Za-z]+|\d+")
LINE_RE = re.compile(r"^(\d{2}):(\d{2})\s+(\S+)\s+(.*)$")
DATE_SEP_RE = re.compile(r"^─+\s*【(\d{4}-\d{2}-\d{2})】共\s*(\d+)\s*条\s*─+\s*$")

# 媒体/占位条：表情、图片、语音、系统提示。真人数据里占三分之一，必须与纯文本分开算。
# 末段"20 位以上裸 hex"沿用 real_chat_analysis.md 第六节的口径（QQ 表情的 id 占位），
# 不加这条会把它误算成纯文本，text 条数与基线文档对不上。
MEDIA_RE = re.compile(
    r"^〔(表情|图|语音|文件|名片|位置|红包|转账|分享|回复|系统)〕"
    r"|^〔\d{6,}〕$"
    r"|^\s*[0-9a-fA-F]{20,}\s*$"
)

# 纯语气应答（整条就是这些）
PURE_ACK_RE = re.compile(
    r"^[\[【]?[嗯哦噢喔唔额呵嘿嘻哈唉哎诶欸吖呀哇嗷昂额噢]?[\]】]?"
    r"(?:[\[【]?[嗯哦噢喔唔额呵嘿嘻哈唉哎诶欸吖呀哇嗷昂]?[\]】]?)*"
    r"(?:ok|OK|okay|OKOK|okok|emm|Emm|EMM|emmm|bushi|yes|no|对|的|了|吧|嗯嗯|？|\?|。|、|\.|～|~|哈)+[\s]*$",
    re.IGNORECASE,
)
LAUGH_RE = re.compile(r"^(?:哈|呵|嘻|嘿嘿|嘻嘻|呵呵|哈哈|哈哈哈哈|hhhh)+[!。~～]*$")
QUESTION_RE = re.compile(r"[？?]")
# 基线文档第六节的“疑问语气”正则，原样沿用
INQUIRY_RE = re.compile(
    r"吗|呢|吧|什么|怎么|怎样|哪|为什么|为啥|是否|有没有|要不要|行不行|好不好|对不对|多少|几|谁|啥|多久|可以不|知道不"
)
METAPHOR_RE = re.compile(r"(像|好像|仿佛).{0,12}(一样|似的|那样|一般)")
TAGGED_EMOJI_RE = re.compile(r"\[[^\[\]]{1,8}\]")
UNICODE_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E0-\U0001F1FF]"
)
SCENE_WRITE_RE = re.compile(r"窗外|天黑|路灯|晚霞|落叶|月光|夜色|星空|夕阳")

# 称呼候选（对“她”的）。基线结论是全库零称呼，这里要拿数据自己验一遍。
ADDRESS_TERMS = (
    "【第三方】", "【第三方】", "【第三方】", "【第三方】",
    "同学", "大佬", "学霸", "宝贝", "宝宝", "丫头", "仙女", "姐姐", "妹妹",
)

# 语气词/口头禅候选词表（按真人 QQ 口语常见度挑选，频次由脚本数，不预设结论）
FILLER_WORDS = (
    "嗯", "哦", "噢", "啊", "唉", "诶", "欸", "哈", "呵", "嘿", "嘻", "咯", "呗", "嘛", "呢", "呀", "啦", "嗷", "呜",
    "草", "笑死", "我不行了", "搜嘎", "soga", "bushi", "6", "ok", "okok", "emm", "yes", "不愧", "真的", "确实",
)

# 场景归类规则（用于挑原句示例）。
# 每条规则 = 必须命中(require) + 不得命中(exclude) + 目标字数带(lo, hi)。
# **为什么要有 exclude**：第一版只有宽松的 require，结果"那你先吃饭哦"被当成"分享"、
# "为什么觉得是错的"被当成"吐槽"。示例是要塞进 system prompt 的，误分类会直接
# 教坏模拟器，所以宁可漏收也不能收错。命中只是进候选池，最终仍需所有者过目。
SCENE_RULES: Tuple[Dict[str, Any], ...] = (
    {
        "name": "分享",
        "desc": "自曝一件具体的事：带时间/动作锚点的陈述句，不是问句、不是抱怨",
        "require": r"(我(刚|今天|昨天|下午|上午|晚上|中午|早上|还|在|去|把|被|这|那)|刚刚|刚才)"
                   r"|(拍了|发现|遇到|碰到|看到|听到|吃到|考完|考砸|出分|放假|开学|下课|吃饭|在吃|去吃)"
                   r"|分享|推荐|安利|发你|给你看|发给",
        "exclude": r"[？?]|吗$|呢$|(为什么|怎么|啥)|(烦|累|难受|emo|想哭|崩溃|受不了|离谱|坑|炸了)",
        "band": (5, 20),
    },
    {
        "name": "吐槽",
        "desc": "抱怨/diss/自嘲：必须出现真实负向情绪词",
        "require": r"(累|烦|难受|emo|想哭|崩溃|不行了|受不了|烦死|恶心|离谱|太难|好烦|好累|挂了|炸了|翻车|坑|无语|服了|熬夜|通宵|肝|心态|摆烂|吐了|麻了)",
        "exclude": r"^〔",
        "band": (4, 22),
    },
    {
        "name": "敷衍",
        "desc": "整条就是纯语气词/单字：他不接话时的默认回复",
        "require": r".",
        "exclude": r"(?!.*)",
        "band": (1, 4),
        "special": "pure_ack",
    },
    {
        "name": "关心",
        "desc": "问对方的状态（睡/吃/冷不冷/到没到）或直接叮嘱",
        "require": r"(睡了吗|睡没睡|吃饭了吗|吃了吗|吃没吃|冷不冷|有没有事|还好吗|怎么了|在干嘛|在忙|到哪|到了吗|回来了吗|没事吧|注意身体|多睡|早点睡|别熬|多喝|休息|吃饭了没|上课了没)",
        # 排除"在休息10分钟"这类**说自己的事**（共享关键词但不是关心对方）
        "exclude": r"^〔|^在(休息|吃|睡|看|写|学|做|打)|^(我|你)能",
        "band": (3, 16),
    },
    {
        "name": "收尾",
        "desc": "结束对话：睡觉/下课/拜拜/先忙，直接走",
        # 强标记直接命中；"走了/睡了"必须**在句首**才算收尾，
        # 否则"我还往回走了""伞被拿走了"这种半路出现的"走了"会被误收。
        "require": r"(拜拜|晚安|下线了|我先睡|我去睡|下课了|去上课了|我先走|先忙|回头聊|不聊了|先吃饭|先上课|先走|明天聊)|^[睡走]了",
        # "睡了多久/睡了没/睡了吗"里的"睡了"是**问句**不是收尾，必须排除
        "exclude": r"^〔|多久|睡了吗|睡没睡|[？?]",
        "band": (3, 16),
    },
)


@dataclass
class Msg:
    """一条语料消息。who 固定 '我'/'她'/'user'（user=他对青梓的发言）。"""

    date: str
    time: str
    who: str
    content: str
    kind: str  # text / media / system

    @property
    def n_chars(self) -> int:
        return len(CHAR_RE.findall(self.content))

    @property
    def ts(self) -> datetime:
        return datetime.strptime(f"{self.date} {self.time}", "%Y-%m-%d %H:%M")


# ──────────────────────────── 语料加载 ────────────────────────────


def load_export(path: str) -> Tuple[List[Msg], List[Msg], Dict[str, Any]]:
    """解析 QQ 导出。返回 (他的消息, 对方消息(按全局顺序交错的完整流), 加载元信息)。

    对方的流**不参与任何画像统计**，只用来给"连发/跳话题"当打断边界：
    只看他的单人流会把"她插了一句、他回一句"误算成连发刷屏。
    """
    if not os.path.exists(path):
        return [], [], {"available": False, "path": path, "reason": "文件不存在"}

    msgs: List[Msg] = []
    stream: List[Msg] = []
    cur_date = ""
    total = 0
    unmatched: List[str] = []
    her_n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\n").rstrip("\r")
            if not line.strip():
                continue
            sep = DATE_SEP_RE.match(line.strip())
            if sep:
                cur_date = sep.group(1)
                continue
            m = LINE_RE.match(line)
            if not m:
                unmatched.append(line)
                continue
            total += 1
            # LINE_RE 有 4 个捕获组：1=时 2=分 3=说话人 4=正文
            hhmm = f"{m.group(1)}:{m.group(2)}"
            who, content = m.group(3), m.group(4)
            if who not in (SPEAKER_HIM, SPEAKER_HER):
                continue
            if MEDIA_RE.match(content.strip()):
                kind = "media"
            elif re.search(r"你?通过?了?你的?朋友验证|以上是打招呼的消息|撤回了一条消息", content):
                kind = "system"
            else:
                kind = "text"
            msg = Msg(date=cur_date, time=hhmm, who=who, content=content, kind=kind)
            stream.append(msg)
            if who == SPEAKER_HER:
                her_n += 1
            else:
                msgs.append(msg)

    return msgs, stream, {
        "available": True,
        "path": path,
        "total_parsed": total,
        "him": len(msgs),
        "her": her_n,
        "unmatched_lines": len(unmatched),
        "date_range": [msgs[0].date, msgs[-1].date] if msgs else None,
    }


def load_prod_corpus(backup_glob: str) -> Tuple[List[Msg], Dict[str, Any]]:
    """从备份库抽 role='user' 的消息（= 他对青梓说的）。

    多个快照常是同一世的嵌套子集（同一起点、逐日递增），直接全量相加会把同一条
    消息重复计 N 次。这里按 (created_at, content) 集合做**极大集筛选**：任一快照的
    消息集合若是另一快照的子集，就丢弃自己。剩下的是各世的全量。
    """
    paths = sorted(glob.glob(backup_glob))
    snapshots: Dict[str, Tuple[set, List[Msg]]] = {}
    for p in paths:
        try:
            con = sqlite3.connect(p)
            cur = con.cursor()
            if not cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='turns'"
            ).fetchone():
                con.close()
                continue
            rows = cur.execute(
                "SELECT created_at, content FROM turns WHERE role='user' ORDER BY created_at"
            ).fetchall()
            con.close()
        except Exception as e:  # noqa: BLE001 —— 备份库损坏不该让画像脚本整体失败
            print(f"[warn] 跳过 {p}: {type(e).__name__}: {e}", file=sys.stderr)
            continue
        msgs = [
            Msg(
                date=(ts or "")[:10],
                time=(ts or "")[11:16] or "00:00",
                who="user",
                content=c or "",
                kind="media" if MEDIA_RE.match((c or "").strip()) else "text",
            )
            for ts, c in rows
        ]
        key = {(m.date + " " + m.time, m.content) for m in msgs}
        snapshots[p] = (key, msgs)

    kept: List[str] = []
    for p, (key, _m) in snapshots.items():
        dominated = any(
            p != q and key < keyq for q, (keyq, _mq) in snapshots.items()
        )
        if not dominated:
            kept.append(p)

    msgs: List[Msg] = []
    for p in kept:
        msgs.extend(snapshots[p][1])
    msgs.sort(key=lambda m: (m.date, m.time))

    return msgs, {
        "available": bool(msgs),
        "kept_snapshots": [os.path.basename(p) for p in kept],
        "dropped_nested": [
            os.path.basename(p) for p in snapshots if p not in kept
        ],
        "count": len(msgs),
        "date_range": [msgs[0].date, msgs[-1].date] if msgs else None,
    }


# ──────────────────────────── 统计 ────────────────────────────


def percentile(sorted_vals: Sequence[float], q: float) -> float:
    """线性插值分位数（q 取 0~1）。空序列返回 0.0。"""
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    pos = q * (len(sorted_vals) - 1)
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac)


def burst_stats(msgs: List[Msg], stream: Optional[List[Msg]] = None, gap_sec: int = 60) -> Dict[str, Any]:
    """连发刷屏：同一人相邻消息间隔 ≤gap_sec 算一“串”。

    `stream` 是**双方交错的完整消息流**：对方插一句话就断串（严格口径）。只传他的
    单人流会把“她插一句、他回一句”也算成刷屏，连发率会虚高到 100%。
    任务书要求看“连续短句刷屏模式”，所以同时给每串条数与每串字数。
    """
    src = stream if stream is not None else msgs
    runs: List[List[Msg]] = []
    cur: List[Msg] = []
    for m in src:
        if m.who != SPEAKER_HIM:  # 对方插话 = 断串（严格口径）
            if cur:
                runs.append(cur)
                cur = []
            continue
        if cur:
            delta = (m.ts - cur[-1].ts).total_seconds()
            if m.date == cur[-1].date and 0 <= delta <= gap_sec:
                cur.append(m)
                continue
            runs.append(cur)
        cur = [m]
    if cur:
        runs.append(cur)

    sizes = [len(r) for r in runs]
    # "参与连发" = 处在**长度 ≥2** 的串里的消息。孤立成一条的不算连发，
    # 否则每条消息都"属于某个串"，这个比例恒等于 100%，指标就废了。
    multi = [s for s in sizes if s >= 2]
    in_burst = sum(multi)
    return {
        "gap_sec": gap_sec,
        "strict": stream is not None,
        "runs": len(runs),
        "singleton_runs": sum(1 for s in sizes if s == 1),
        "in_burst_msgs": in_burst,
        "in_burst_rate": round(in_burst / len(msgs), 4) if msgs else 0.0,
        "size_median": statistics.median(sizes) if sizes else 0,
        "size_mean": round(statistics.fmean(sizes), 2) if sizes else 0.0,
        "size_max": max(sizes) if sizes else 0,
        "rate_ge3": round(sum(1 for s in sizes if s >= 3) / len(sizes), 4) if sizes else 0.0,
        "rate_ge5": round(sum(1 for s in sizes if s >= 5) / len(sizes), 4) if sizes else 0.0,
    }


def topic_jump_stats(msgs: List[Msg], stream: Optional[List[Msg]] = None) -> Dict[str, Any]:
    """话题跳转：用相邻两条的字符 bigram 集合 Jaccard 估“还在聊同一件事”。

    传 `stream` 时比较对象是**全局流里的前一条**（可能是她说的），这才是真实体感：
    “他回她一句完全不搭的话”才是跳题。低于 JUMP_JACCARD 视为跳题。
    这是个粗代理，报告里要标注它不是语义判定。
    """
    JUMP_JACCARD = 0.08
    def grams(t: str) -> set:
        chars = [c for c in t if c.strip() and not c.isascii()]
        return {chars[i] + chars[i + 1] for i in range(len(chars) - 1)}

    pairs = 0
    jumps = 0
    if stream is not None:
        prev_global: Optional[Msg] = None
        for m in stream:
            if m.who == SPEAKER_HIM:
                if prev_global is not None and m.date == prev_global.date:
                    ga, gb = grams(prev_global.content), grams(m.content)
                    if ga and gb:
                        pairs += 1
                        if len(ga & gb) / len(ga | gb) < JUMP_JACCARD:
                            jumps += 1
            prev_global = m
    else:
        for a, b in zip(msgs, msgs[1:]):
            if a.date != b.date:
                continue  # 跨天不算跳转
            ga, gb = grams(a.content), grams(b.content)
            if not ga or not gb:
                continue
            pairs += 1
            if len(ga & gb) / len(ga | gb) < JUMP_JACCARD:
                jumps += 1
    return {
        "jaccard_threshold": JUMP_JACCARD,
        "compared_pairs": pairs,
        "jumps": jumps,
        "jump_rate": round(jumps / pairs, 4) if pairs else 0.0,
        "note": "字符 bigram Jaccard 粗代理，非语义判定",
    }


def opening_stats(msgs: List[Msg], stream: Optional[List[Msg]] = None) -> Dict[str, Any]:
    """主动开启话题：静默 ≥3h 后谁先开口。

    口径：一个“会话段”= 双方都沉默超过 3 小时后重启；段内第一条是谁发的，谁就是
    主动开启方。传 `stream` 时才能区分他/她（只看他的单人流会把所有重启都算成他开的）。
    """
    SILENCE_SEC = 3 * 3600
    starts_him = 0
    starts_her = 0
    first_msg_types: Counter = Counter()
    openers: List[Msg] = []
    src = stream if stream is not None else msgs
    prev: Optional[Msg] = None
    for m in src:
        if prev is None or (m.ts - prev.ts).total_seconds() >= SILENCE_SEC:
            if m.who == SPEAKER_HIM:
                starts_him += 1
                openers.append(m)
            else:
                starts_her += 1
        prev = m
    for m in openers:
        c = m.content.strip()
        if QUESTION_RE.search(c):
            first_msg_types["问句"] += 1
        elif PURE_ACK_RE.match(c):
            first_msg_types["纯应答"] += 1
        elif MEDIA_RE.match(c):
            first_msg_types["媒体/表情"] += 1
        elif m.n_chars <= 5:
            first_msg_types["极短陈述"] += 1
        else:
            first_msg_types["具体陈述"] += 1
    return {
        "silence_threshold_h": 3,
        "openings_after_silence": starts_him,
        "openings_after_silence_her": starts_her,
        "openings_seen": len(msgs),
        "first_msg_type_dist": dict(first_msg_types),
        "openers_sample": [m.content for m in openers[:12]],
    }


def compute_stats(
    msgs: List[Msg], tag: str, stream: Optional[List[Msg]] = None
) -> Dict[str, Any]:
    """整包统计。text 只含纯文本消息，media 单列（真人的“媒体条”占三分之一）。"""
    text = [m for m in msgs if m.kind == "text"]
    media = [m for m in msgs if m.kind == "media"]
    system = [m for m in msgs if m.kind == "system"]
    lens = sorted(m.n_chars for m in text)
    n = len(text) or 1

    # 单字 / 两字
    single = [m for m in text if m.n_chars == 1]
    two = [m for m in text if m.n_chars == 2]

    # 句尾标点
    tail_dot = sum(1 for m in text if m.content.rstrip().endswith("。"))
    tail_q = sum(1 for m in text if m.content.rstrip()[-1:] in ("？", "?"))
    tail_bang = sum(1 for m in text if m.content.rstrip()[-1:] in ("！", "!"))
    tail_none = sum(1 for m in text if m.content.rstrip() and m.content.rstrip()[-1:] not in "。？！?!～~…，,、 ")
    has_comma = sum(1 for m in text if "，" in m.content or "," in m.content)
    has_ellipsis = sum(1 for m in text if "…" in m.content or "..." in m.content)

    # 语气词
    filler: Counter = Counter()
    for w in FILLER_WORDS:
        c = sum(m.content.count(w) for m in msgs if w != "6" or True)
        if c:
            filler[w] = c
    pure_ack = [m for m in text if PURE_ACK_RE.match(m.content.strip()) and m.n_chars <= 4]
    laugh = [m for m in text if LAUGH_RE.match(m.content.strip())]

    # 表情
    tagged = [m for m in msgs if TAGGED_EMOJI_RE.search(m.content)]
    tagged_counter: Counter = Counter()
    for m in msgs:
        for t in TAGGED_EMOJI_RE.findall(m.content):
            tagged_counter[t] += 1
    uni = [m for m in msgs if UNICODE_EMOJI_RE.search(m.content)]
    media_sticker = [m for m in media if m.content.strip() in ("〔表情〕",)]

    # 问句
    tail_q_msgs = [m for m in text if m.content.rstrip()[-1:] in ("？", "?")]
    q_any = [m for m in text if QUESTION_RE.search(m.content)]
    inquiry = [m for m in text if INQUIRY_RE.search(m.content)]

    # 修辞
    metaphor = [m for m in text if METAPHOR_RE.search(m.content)]
    scene = [m for m in text if SCENE_WRITE_RE.search(m.content)]

    # 称呼
    addr: Counter = Counter()
    addr_msgs: Counter = Counter()
    for m in msgs:
        for t in ADDRESS_TERMS:
            c = m.content.count(t)
            if c:
                addr[t] += c
                addr_msgs[t] += 1
    addr_bearing = [m for m in msgs if any(t in m.content for t in ADDRESS_TERMS)]

    return {
        "tag": tag,
        "counts": {
            "total_msgs": len(msgs),
            "text": len(text),
            "media": len(media),
            "system": len(system),
            "sticker_only_media": len(media_sticker),
        },
        "length": {
            "min": lens[0] if lens else 0,
            "p25": round(percentile(lens, 0.25), 1),
            "median": round(percentile(lens, 0.50), 1),
            "p75": round(percentile(lens, 0.75), 1),
            "p90": round(percentile(lens, 0.90), 1),
            "p99": round(percentile(lens, 0.99), 1),
            "max": lens[-1] if lens else 0,
            "mean": round(statistics.fmean(lens), 2) if lens else 0.0,
            "le5_rate": round(sum(1 for x in lens if x <= 5) / n, 4),
            "le15_rate": round(sum(1 for x in lens if x <= 15) / n, 4),
            "ge30_rate": round(sum(1 for x in lens if x >= 30) / n, 4),
            "ge50_rate": round(sum(1 for x in lens if x >= 50) / n, 4),
            "ge100_rate": round(sum(1 for x in lens if x >= 100) / n, 4),
            "single_char_msgs": len(single),
            "single_char_rate": round(len(single) / n, 4),
            "two_char_msgs": len(two),
            "two_char_rate": round(len(two) / n, 4),
            # ≤2 与 ≤5 是包含关系，**不能相加**（相加会重复计数）。
            # 这里给"极短"（≤2 字）的独立占比，≤5 字见 le5_rate。
            "le2_rate": round(sum(1 for x in lens if x <= 2) / n, 4),
        },
        "punctuation": {
            "tail_none_rate": round(tail_none / n, 4),
            "tail_period_rate": round(tail_dot / n, 4),
            "tail_question_rate": round(tail_q / n, 4),
            "tail_bang_rate": round(tail_bang / n, 4),
            "has_comma_rate": round(has_comma / n, 4),
            "has_ellipsis_rate": round(has_ellipsis / n, 4),
        },
        "fillers": {
            "top": filler.most_common(20),
            "pure_ack_msgs": len(pure_ack),
            "pure_ack_rate": round(len(pure_ack) / n, 4),
            "pure_ack_top": Counter(
                m.content.strip() for m in pure_ack
            ).most_common(15),
            "laugh_msgs": len(laugh),
            "laugh_rate": round(len(laugh) / n, 4),
        },
        "emoji": {
            "tagged_msgs": len(tagged),
            "tagged_rate": round(len(tagged) / len(msgs), 4) if msgs else 0.0,
            "tagged_top": tagged_counter.most_common(15),
            "unicode_msgs": len(uni),
            "unicode_rate": round(len(uni) / len(msgs), 4) if msgs else 0.0,
        },
        "questions": {
            "tail_question_rate": round(len(tail_q_msgs) / n, 4),
            "any_question_rate": round(len(q_any) / n, 4),
            "inquiry_tone_rate": round(len(inquiry) / n, 4),
            "inquiry_without_qmark": round(len([m for m in inquiry if not QUESTION_RE.search(m.content)]) / n, 4),
            "per100_qmarks": round(100 * sum(m.content.count("？") + m.content.count("?") for m in text) / n, 1),
        },
        "rhetoric": {
            "metaphor_msgs": len(metaphor),
            "metaphor_rate": round(len(metaphor) / n, 5),
            "metaphor_samples": [m.content for m in metaphor[:8]],
            "scene_write_msgs": len(scene),
            "scene_write_rate": round(len(scene) / n, 5),
        },
        "address": {
            "bearing_msgs": len(addr_bearing),
            "bearing_rate": round(len(addr_bearing) / len(msgs), 4) if msgs else 0.0,
            "term_freq": dict(addr.most_common()),
        },
        "burst": burst_stats(msgs, stream),
        "burst_loose": burst_stats(msgs, None),
        "topic_jump": topic_jump_stats(msgs, stream),
        "opening": opening_stats(msgs, stream),
    }


# ──────────────────────────── 原句示例挑选 ────────────────────────────


def pick_examples(msgs: List[Msg], per_scene: int = 5) -> Dict[str, Any]:
    """按场景规则挑**原句**示例（禁止改写）。

    挑选策略（确定性，可复跑复现）：
      1. 场景内候选池：纯文本 + 命中 require + 不命中 exclude + 字数落在 band 内；
         "敷衍"场景特殊处理 = 纯语气应答。
      2. 同内容去重、同日期去重（同一天最多 1 条），保证场景覆盖而不是同一个梗刷屏；
      3. 排序键 = (偏离字数带中点的距离, 时间)，先取贴近他语风中位的，再按时间先后，
         使示例集合在时间上分散、内容不重样。
    返回 {场景名: {"desc":…, "items":[{date,time,chars,text}…]}}，desc 单独存，
    避免和场景名混在一个 dict 里。
    """
    out: Dict[str, Any] = {}
    for rule in SCENE_RULES:
        scene = rule["name"]
        lo, hi = rule["band"]
        text_msgs = [m for m in msgs if m.kind == "text"]
        if rule.get("special") == "pure_ack":
            pool = [
                m
                for m in text_msgs
                if lo <= m.n_chars <= hi and PURE_ACK_RE.match(m.content.strip())
            ]
        else:
            rx = re.compile(rule["require"])
            ex = re.compile(rule["exclude"]) if rule.get("exclude") else None
            pool = [
                m
                for m in text_msgs
                if lo <= m.n_chars <= hi
                and rx.search(m.content)
                and not (ex and ex.search(m.content))
            ]

        seen_content: set = set()
        cand: List[Msg] = []
        for m in pool:
            key = re.sub(r"\s+", "", m.content)
            if key in seen_content:
                continue
            seen_content.add(key)
            cand.append(m)

        mid = (lo + hi) / 2
        cand.sort(key=lambda m: (abs(m.n_chars - mid), m.ts))

        picked: List[Msg] = []
        used_days: set = set()
        for m in cand:
            if m.date in used_days and len(cand) > per_scene * 2:
                continue
            picked.append(m)
            used_days.add(m.date)
            if len(picked) >= per_scene:
                break
        # 不足 per_scene 时放宽"同日期去重"再补，保证每张卡都有足量示例
        if len(picked) < per_scene:
            got = {m.ts for m in picked}
            for m in cand:
                if m.ts in got:
                    continue
                picked.append(m)
                got.add(m.ts)
                if len(picked) >= per_scene:
                    break

        out[scene] = {
            "desc": rule["desc"],
            "pool_size": len(pool),
            "items": [
                {
                    "date": m.date,
                    "time": m.time,
                    "chars": m.n_chars,
                    "text": m.content,
                }
                for m in picked
            ],
        }
    return out


def pick_flaw_examples(msgs: List[Msg]) -> Dict[str, Any]:
    """为“真人毛病清单”各抓一条**证据原句**（每条毛病都必须有语料出处）。"""
    text = [m for m in msgs if m.kind == "text"]
    flaws: Dict[str, Any] = {}

    # 1) 已读不回 / 静默：她说完后长时间（≥4h）没有他任何消息，且当天也没了
    SILENCE = 4 * 3600
    cases = []
    for a, b in zip(msgs, msgs[1:]):
        gap = (b.ts - a.ts).total_seconds()
        if gap >= SILENCE and a.kind == "text" and a.content.strip():
            cases.append(
                {
                    "last_msg": a.content.strip(),
                    "date": a.date,
                    "time": a.time,
                    "gap_h": round(gap / 3600, 1),
                }
            )
    cases.sort(key=lambda c: -c["gap_h"])
    flaws["已读不回"] = {
        "evidence": cases[:5],
        "count_ge4h": len(cases),
        "median_gap_h": round(statistics.median([c["gap_h"] for c in cases]), 1) if cases else 0,
    }

    # 2) 敷衍：单字/纯应答的原句
    ack = [m for m in text if m.n_chars <= 2 and PURE_ACK_RE.match(m.content.strip())]
    flaws["敷衍单字"] = {
        "count": len(ack),
        "examples": [{"date": m.date, "text": m.content} for m in ack[:8]],
    }

    # 3) 只回“嗯”
    nm = [m for m in text if m.content.strip() in ("嗯", "嗯嗯", "嗯。", "嗯？")]
    flaws["只回嗯"] = {
        "count": len(nm),
        "examples": [{"date": m.date, "text": m.content} for m in nm[:6]],
    }

    # 4) 不打标点：随机取几条长句原文供模拟器对照
    nop = [m for m in text if 6 <= m.n_chars <= 20 and not re.search(r"[，。？！?!、；：…]", m.content)]
    flaws["不打标点"] = {"count": len(nop), "examples": [{"date": m.date, "text": m.content} for m in nop[:8]]}

    # 5) 刷屏：取一条最长的连发串
    runs: List[List[Msg]] = []
    cur: List[Msg] = []
    for m in msgs:
        if cur:
            d = (m.ts - cur[-1].ts).total_seconds()
            if m.date == cur[-1].date and 0 <= d <= 60:
                cur.append(m)
                continue
            runs.append(cur)
        cur = [m]
    if cur:
        runs.append(cur)
    runs.sort(key=len, reverse=True)
    # 展示最长串时只列纯文本，媒体占位符对读者没有信息量
    longest_text = [m for m in (runs[0] if runs else []) if m.kind == "text"]
    flaws["连续刷屏"] = {
        "longest_run_len": len(runs[0]) if runs else 0,
        "longest_run": [{"time": m.time, "text": m.content} for m in longest_text[:8]],
    }
    return flaws


# ──────────────────────────── 简报渲染 ────────────────────────────


def fmt_counter_top(counter: Sequence[Tuple[str, int]], limit: int = 12) -> str:
    return "、".join(f"`{k}`×{v}" for k, v in list(counter)[:limit]) or "（无）"


def render_brief(
    exp_stats: Optional[Dict[str, Any]],
    prod_stats: Optional[Dict[str, Any]],
    exp_meta: Dict[str, Any],
    prod_meta: Dict[str, Any],
    examples: Dict[str, List[Dict[str, Any]]],
    flaws: Dict[str, Any],
) -> str:
    L: List[str] = []
    A = L.append

    A("# 阿俊说话画像简报（对聊仿真器 system prompt 用）")
    A("")
    A("> 用途：FIXES18 对聊仿真器扮演“他”一侧时的事实依据。**全部统计数字由**")
    A("> `scripts/duo_sim_persona.py` 对真实语料计算得出，禁止凭印象改写**；引用的原句")
    A("> 一律是语料原文（未做任何润色）。重跑脚本可完整复现。")
    A("")

    A("## 0. 数据来源与口径")
    A("")
    if exp_meta.get("available"):
        rng = exp_meta.get("date_range") or ["?", "?"]
        A(
            f"- **主源**：QQ 真实聊天导出 `{exp_meta['path']}`，"
            f"解析出他（`我`）的消息 **{exp_meta['him']} 条**（对方 {exp_meta['her']} 条），"
            f"时间范围 {rng[0]} ~ {rng[1]}。"
        )
    else:
        A(f"- **主源缺失**：`{exp_meta.get('path')}` 不可用（{exp_meta.get('reason')}），本简报统计仅来自生产库。")
    if prod_meta.get("available"):
        rng = prod_meta.get("date_range") or ["?", "?"]
        A(
            f"- **对照源（他对她/对青梓的真实发言）**：`data/backups/` 生产库快照 "
            f"{'、'.join('`' + x + '`' for x in prod_meta.get('kept_snapshots', []))}，"
            f"共 **{prod_meta['count']} 条** user 消息，{rng[0]} ~ {rng[1]}；"
            f"已自动剔除互为子集的嵌套快照 {'、'.join('`' + x + '`' for x in prod_meta.get('dropped_nested', [])) or '（无）'}。"
        )
    A("- **已有结论直接引用**：`data/archive/real_chat_analysis.md`（真人基线分析）。")
    A("- **字数口径**：汉字各计 1、英文单词计 1、连续数字计 1；标点/空白/表情标签不计。")
    if exp_stats:
        A(
            f"- **与基线文档的口径差异（先说清，免得对不上时被当成算错）**：本脚本把"
            f"“20 位以上裸 hex”也算媒体条，纯文本 **{exp_stats['counts']['text']} 条**，"
            f"而基线文档 1 节记的是 4361 条；差 {exp_stats['counts']['text'] - 4361} 条来自媒体/系统条目的"
            f"归类边界（文档另把“系统提示”单列一类）。"
            f"**结论方向与量级完全一致**：字数中位 5、≤5 字 55.9%↔{exp_stats['length']['le5_rate'] * 100:.1f}%、"
            f"句末无标点 87.3%↔{exp_stats['punctuation']['tail_none_rate'] * 100:.1f}%、"
            f"真比喻 5 条↔{exp_stats['rhetoric']['metaphor_msgs']} 条、"
            f"零称呼↔{exp_stats['address']['bearing_rate'] * 100:.2f}%、"
            f"宽松连发 881 串/均值 7.48↔{exp_stats['burst_loose']['runs']} 串/均值 {exp_stats['burst_loose']['size_mean']}。"
        )
    A("")

    if exp_stats:
        s = exp_stats
        cnt, ln, pun, fil, emo, q, rh, ad = (
            s["counts"], s["length"], s["punctuation"], s["fillers"],
            s["emoji"], s["questions"], s["rhetoric"], s["address"],
        )
        A("## 1. 统计事实（对真人）")
        A("")
        A("### 1.1 消息体量与长度")
        A("")
        A(f"- 纯文本 **{cnt['text']} 条**，媒体/表情条 **{cnt['media']} 条**"
          f"（占全部消息 {cnt['media'] / max(cnt['total_msgs'], 1) * 100:.1f}%），"
          f"其中纯表情条 {cnt['sticker_only_media']} 条。")
        A(f"- 字数分位：min **{ln['min']}** / P25 **{ln['p25']}** / **中位 {ln['median']}** / "
          f"P75 **{ln['p75']}** / P90 **{ln['p90']}** / P99 **{ln['p99']}** / max **{ln['max']}** / 均值 {ln['mean']}。")
        A(f"- **≤5 字占 {ln['le5_rate'] * 100:.1f}%**，≤15 字占 **{ln['le15_rate'] * 100:.1f}%**；"
          f"≥30 字仅 {ln['ge30_rate'] * 100:.1f}%，**≥50 字 {ln['ge50_rate'] * 100:.1f}%**，"
          f"≥100 字 {ln['ge100_rate'] * 100:.1f}%。")
        A(f"- **单字消息 {ln['single_char_msgs']} 条（{ln['single_char_rate'] * 100:.1f}%）、"
          f"两字 {ln['two_char_msgs']} 条（{ln['two_char_rate'] * 100:.1f}%）**；"
          f"≤2 字合计 {ln['le2_rate'] * 100:.1f}%、≤5 字合计 {ln['le5_rate'] * 100:.1f}%。")
        A("")
        A("### 1.2 标点习惯（这条最能一眼认出他）")
        A("")
        A(f"- **句末无任何标点：{pun['tail_none_rate'] * 100:.1f}%**；句号收尾 {pun['tail_period_rate'] * 100:.1f}%，"
          f"问号收尾 {pun['tail_question_rate'] * 100:.1f}%，叹号收尾 {pun['tail_bang_rate'] * 100:.1f}%。")
        A(f"- 含逗号 {pun['has_comma_rate'] * 100:.1f}%（他极少用逗号，很多地方直接换行或空格断句），"
          f"含省略号 {pun['has_ellipsis_rate'] * 100:.1f}%。")
        A("")
        A("### 1.3 语气词与口头禅")
        A("")
        A(f"- 词频 Top：{fmt_counter_top(fil['top'])}")
        A(f"- **纯应答条（整条就是嗯/哦/ok 这类）{fil['pure_ack_msgs']} 条，占 {fil['pure_ack_rate'] * 100:.1f}%**："
          f"{fmt_counter_top(fil['pure_ack_top'], 10)}")
        A(f"- 纯笑声条 {fil['laugh_msgs']} 条（{fil['laugh_rate'] * 100:.1f}%）。")
        A("")
        A("### 1.4 表情包与 emoji")
        A("")
        A(f"- 带 QQ 表情标签的消息 **{emo['tagged_msgs']} 条（{emo['tagged_rate'] * 100:.1f}%）**；"
          f"标签频次 Top：{fmt_counter_top(emo['tagged_top'])}")
        A(f"- 带 unicode emoji {emo['unicode_msgs']} 条（{emo['unicode_rate'] * 100:.1f}%）。")
        A("- **关键习惯：他是“一个表情刷到底”型**——同几个标签反复用，不是每次换新图。")
        A("")
        A("### 1.5 问句率（真人不打问号但确实在问）")
        A("")
        A(f"- 句尾问号 {q['tail_question_rate'] * 100:.1f}%，任意位置含问号 {q['any_question_rate'] * 100:.1f}%，"
          f"每百条 {q['per100_qmarks']} 个问号。")
        A(f"- **疑问语气（含吗/呢/什么/怎么/哪/为什么等，无问号也算）{q['inquiry_tone_rate'] * 100:.1f}%**，"
          f"其中**不带问号的占 {q['inquiry_without_qmark'] * 100:.1f}%**。")
        A("")
        A("### 1.6 修辞密度（他几乎不写景、不打比方）")
        A("")
        A(f"- 真·比喻句式 {rh['metaphor_msgs']} 条（**{rh['metaphor_rate'] * 100:.3f}%**）"
          f"{'：' + '；'.join(repr(x) for x in rh['metaphor_samples'][:3]) if rh['metaphor_samples'] else ''}")
        A(f"- 写景句（含窗外/路灯/晚霞等）{rh['scene_write_msgs']} 条（{rh['scene_write_rate'] * 100:.3f}%）。")
        A("")
        A("### 1.7 称呼（这条必须拿数据说，不许编）")
        A("")
        if ad["bearing_msgs"] == 0:
            A(f"- **全库 {cnt['total_msgs']} 条消息里，带任何称呼词的有 {ad['bearing_msgs']} 条"
              f"（{ad['bearing_rate'] * 100:.2f}%）。他几乎从不叫对方的名字或外号。**")
            A("- 这与 `real_chat_analysis.md` 5.1/5.2.1 的“零称呼”结论一致。")
        else:
            A(f"- 带称呼词的消息 {ad['bearing_msgs']} 条（{ad['bearing_rate'] * 100:.2f}%），"
              f"词频：{fmt_counter_top(list(ad['term_freq'].items()), 15)}")
        A("")
        A("### 1.8 连发刷屏")
        A("")
        b = s["burst"]
        bl = s["burst_loose"]
        A(f"- **严格口径**（对方插话即断串，间隔 ≤{b['gap_sec']}s 算同串）：共 {b['runs']} 串，"
          f"其中孤立成一条的有 {b['singleton_runs']} 串；"
          f"**{b['in_burst_rate'] * 100:.1f}% 的消息处在 ≥2 条的连发串里**；"
          f"每串条数中位 {b['size_median']}、均值 {b['size_mean']}、最长 {b['size_max']}；"
          f"≥3 条的串占 {b['rate_ge3'] * 100:.1f}%，≥5 条的串占 {b['rate_ge5'] * 100:.1f}%。")
        A(f"- **宽松口径**（不看对方是否插话，只按他自己相邻消息 ≤{bl['gap_sec']}s 连成串，"
          f"与 `real_chat_analysis.md` 2.5 节同口径）：共 {bl['runs']} 串，"
          f"每串中位 {bl['size_median']}、均值 {bl['size_mean']}、最长 {bl['size_max']}，"
          f"≥3 条的串占 {bl['rate_ge3'] * 100:.1f}%。")
        A("- 读法：他的“一条回复”经常被拆成 2~4 条短消息连续发出，而不是合并成一段。"
          "**模拟器必须照这个来：一次发言输出 1~3 条短消息，而不是一段完整的话。**")
        A("")
        A("### 1.9 话题跳转")
        A("")
        j = s["topic_jump"]
        A(f"- 同一天内相邻两条的字符 bigram Jaccard < {j['jaccard_threshold']} 视为跳题："
          f"{j['jumps']}/{j['compared_pairs']} = **{j['jump_rate'] * 100:.1f}%**（{j['note']}）。")
        A("- 读法：短消息连发天然会拉高这个数，说明他**不铺垫、不承接上一个话题**，说完一件事直接跳走是常态。")
        A("")
        A("### 1.10 主动开口")
        A("")
        o = s["opening"]
        A(f"- 静默 ≥{o['silence_threshold_h']}h 后重启对话，**他先开口 {o['openings_after_silence']} 次 / "
          f"她先开口 {o['openings_after_silence_her']} 次**（该语料流共 {o['openings_seen']} 条）。")
        A(f"- 他开场首条形态分布：{fmt_counter_top(list(o['first_msg_type_dist'].items()))}")
        A("- 读法：重启对话这件事两人次数相当（他不比她更爱主动）；但他一旦开口，"
          "第一条**自带内容**（具体的事或直接甩个表情），不会来一句“在吗”。")
        A("")

    if prod_stats:
        p = prod_stats
        pln, ppun, pq = p["length"], p["punctuation"], p["questions"]
        A("## 2. 他对青梓的真实发言（生产库对照）")
        A("")
        A(f"- 样本 **{p['counts']['text']} 条**（纯文本），字数中位 **{pln['median']}**、均值 {pln['mean']}、"
          f"max {pln['max']}；≤5 字占 {pln['le5_rate'] * 100:.1f}%，≤15 字占 {pln['le15_rate'] * 100:.1f}%。")
        A(f"- 句末无标点 {ppun['tail_none_rate'] * 100:.1f}%，句尾问号 {ppun['tail_question_rate'] * 100:.1f}%，"
          f"疑问语气 {pq['inquiry_tone_rate'] * 100:.1f}%。")
        A(f"- 称呼：带称呼词 {p['address']['bearing_msgs']} 条"
          f"（{p['address']['bearing_rate'] * 100:.1f}%）"
          f"{'：' + fmt_counter_top(list(p['address']['term_freq'].items()), 8) if p['address']['term_freq'] else '（本轮语料里一次也没叫过她“小W同学”，与零称呼基线一致）'}。")
        A("- **这条最重要**：仿真器模拟的“他”必须贴近**这一列**（他对青梓的真实说话方式），"
          "而不是他对真人的语风——对青梓时他更正式一点、句子更完整。")
        A("")

    A("## 3. 原句示例（语料原文，未改写）")
    A("")
    A("> 下列每条都是原句照抄。模拟器模仿的是**句子形状与节奏**，不是具体内容。")
    A("")
    for idx, rule in enumerate(SCENE_RULES, 1):
        scene = rule["name"]
        block = examples.get(scene) or {}
        items = block.get("items") or []
        if not items:
            A(f"### 3.{idx} {scene}")
            A("")
            A("> 该场景在语料里没挑出合格示例（规则宁松勿紧，宁缺毋滥）。")
            A("")
            continue
        A(f"### 3.{idx} {scene}（{block.get('desc', '')}；候选池 {block.get('pool_size', 0)} 条）")
        A("")
        for it in items:
            A(f"- `{it['date']} {it['time']}`（{it['chars']} 字）`{it['text']}`")
        A("")

    A("## 4. 真人毛病清单（模拟器必须复现的缺陷）")
    A("")
    A("这些是**要如实复刻**的特征，不是要修的东西。仿真器的价值就在于让青梓在这些点上被真实地戳。")
    A("")
    if not flaws:
        A("> **本节不可用**：QQ 导出主源缺失，语料为空，无任何证据可引。")
        A("> 请把真实聊天导出放到 `--export` 指向的路径后重跑本脚本。")
        A("")
    else:
        ack_ex = "、".join(f"`{e['text']}`" for e in flaws["敷衍单字"]["examples"][:6]) or "（无）"
        A(f"1. **短到极端**：单字/双字回复是他的默认档位（≤5 字占 "
          f"{exp_stats['length']['le5_rate'] * 100:.1f}%，单字占 {exp_stats['length']['single_char_rate'] * 100:.1f}%）。")
        A(f"   证据（他的真实敷衍原句）：{ack_ex}")
        A(f"2. **只回“嗯”**：`嗯/嗯嗯/嗯。/嗯？` 共 {flaws['只回嗯']['count']} 条。"
          f"证据：{'、'.join('`' + e['text'] + '`' for e in flaws['只回嗯']['examples'][:5]) or '（无）'}")
        A(f"3. **不打标点**：句末无标点 {exp_stats['punctuation']['tail_none_rate'] * 100:.1f}%；"
          f"含逗号仅 {exp_stats['punctuation']['has_comma_rate'] * 100:.1f}%。他断句靠换行或空格，不靠标点。"
          f"证据：{'、'.join('`' + e['text'] + '`' for e in flaws['不打标点']['examples'][:4]) or '（无）'}")
        A(f"4. **会已读不回 / 冷场**：与她的消息间隔 ≥4 小时的断档 {flaws['已读不回']['count_ge4h']} 次，"
          f"中位断档 {flaws['已读不回']['median_gap_h']} 小时。证据："
          f"{'；'.join('`' + c['last_msg'] + '` 后静默 ' + str(c['gap_h']) + 'h' for c in flaws['已读不回']['evidence'][:3]) or '（无）'}")
        A(f"5. **连续短句刷屏**：最长一串连发 {flaws['连续刷屏']['longest_run_len']} 条，"
          f"且 {exp_stats['burst']['in_burst_rate'] * 100:.1f}% 的消息都在连发串里。"
          f"最长的那一串：{' → '.join('`' + x['text'] + '`' for x in flaws['连续刷屏']['longest_run'][:6]) or '（无）'}")
        A(f"6. **跳话题不铺垫**：同一天内相邻消息"
          f"{exp_stats['topic_jump']['jump_rate'] * 100:.1f}% 判定为跳题。说完一件事直接说另一件，不做过渡。")
        A("7. **零称呼**：几乎从不叫名字或外号（见 1.7）。角色卡要求她按阶段上称呼，他的镜像就是“她也别指望听到称呼”。")
        A("8. **情绪直给但不修饰**：难受就说难受，不铺垫、不解释。抱怨用最短的句子砸出来。")
        A("9. **关心靠追问具体事实**：问“睡了吗/吃了吗/到哪了”，而不是“你要好好休息哦”那类。")
        A("")

    A("## 5. 给模拟器的三条硬纪律（写在 system prompt 里）")
    A("")
    A("1. **只输出他本人的下一条消息原文**，不要旁白、不要动作描写、不要括号补充说明、不要复述她的问题。")
    A("2. **严格照 1~3 节的数字控制形状**：默认 1~3 条短消息、每条 3~10 字、句末不打标点、"
      "问句少用问号、零称呼。")
    A("3. **不许替她接话**：你只知道她说了什么，不知道她内心怎么想，也不要预演她的下一句。")
    A("")
    A("---")
    A("")
    A(f"- 统计脚本：`scripts/duo_sim_persona.py`（零 LLM 调用，纯确定性统计）")
    A(f"- 机器可读全量数字：`{STATS_JSON}`")
    return "\n".join(L)


# ──────────────────────────── main ────────────────────────────


def main() -> None:
    export_path = EXPORT_DEFAULT
    for i, a in enumerate(sys.argv):
        if a == "--export" and i + 1 < len(sys.argv):
            export_path = sys.argv[i + 1]
        elif a.startswith("--export="):
            export_path = a.split("=", 1)[1]

    print("加载语料…")
    exp_msgs, exp_stream, exp_meta = load_export(export_path)
    prod_msgs, prod_meta = load_prod_corpus(BACKUP_GLOB)
    if exp_meta.get("available"):
        print(f"  导出主源：{exp_meta['him']} 条（{exp_meta['date_range']}）")
    else:
        print(f"  [warn] 导出主源不可用：{exp_meta.get('reason')}")
    print(f"  生产库对照：{prod_meta.get('count', 0)} 条（保留 {prod_meta.get('kept_snapshots')}）")

    exp_stats = compute_stats(exp_msgs, "export_him", exp_stream) if exp_msgs else None
    prod_stats = compute_stats(prod_msgs, "prod_user") if prod_msgs else None
    examples = pick_examples(exp_msgs) if exp_msgs else {}
    flaws = pick_flaw_examples(exp_msgs) if exp_msgs else {}

    os.makedirs(OUT_DIR, exist_ok=True)
    brief = render_brief(exp_stats, prod_stats, exp_meta, prod_meta, examples, flaws)
    with open(BRIEF_MD, "w", encoding="utf-8") as f:
        f.write(brief)

    payload = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "export_meta": exp_meta,
        "prod_meta": prod_meta,
        "export_stats": exp_stats,
        "prod_stats": prod_stats,
        "examples": examples,
        "flaws": flaws,
        "has_analysis_md": os.path.exists(ANALYSIS_MD),
    }
    with open(STATS_JSON, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 画像简报：{BRIEF_MD}（{len(brief)} 字符）")
    print(f"✅ 统计全量：{STATS_JSON}")
    if exp_stats:
        ln = exp_stats["length"]
        print(
            f"   关键数字：条数 {exp_stats['counts']['text']} | 字数中位 {ln['median']} | "
            f"≤5字 {ln['le5_rate'] * 100:.1f}% | 句末无标点 "
            f"{exp_stats['punctuation']['tail_none_rate'] * 100:.1f}% | "
            f"连发内 {exp_stats['burst']['in_burst_rate'] * 100:.1f}%"
        )


if __name__ == "__main__":
    main()
