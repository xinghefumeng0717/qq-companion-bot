"""多天对聊测试 (scripts/sim/multiday_sim.py)

单发探针（v4/v5）只测得出"这一句像不像人"，测不出"她是不是一个连续的人"：
她前天说自己不吃香菜，今天会不会说爱吃？你上周说在准备比赛，她还记不记得？

做法：同一个沙箱库连聊 3 天（每天一个话题，他会顺口问她自己的事、也说自己的事），
最后一天他按固定问题"考她"，再把之前几天的聊天记录和考题回答交给评审模型逐题判：
  对得上 / 矛盾 / 坦白忘了 / 现编（之前没说过却说得很具体） / 之前没聊到
另外让评审把这几天里她关于自己前后矛盾的地方全列出来。

她走生产同一条管道（复用 duo_sim 的 DuoSimulator），他由模型扮演（前几天）
或用固定考题（最后一天）。产物写到 --out 目录：每天的 transcript、judge.json、report.md。

用法（自我记忆 关/开 各跑一次对比）：
  venv/bin/python scripts/sim/multiday_sim.py --card qingzi_lean --out data/multiday/off
  venv/bin/python scripts/sim/multiday_sim.py --card qingzi_lean --self-memory --out data/multiday/on
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime
from typing import Any, Dict, List

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))
sys.path.insert(0, os.path.dirname(__file__))

from benchmark_v4 import caution_clauses  # noqa: E402
from companion.config import Config  # noqa: E402
from companion.db import TIME_FORMAT  # noqa: E402
from duo_sim import (  # noqa: E402
    DuoSimulator,
    Scene,
    _TimePatch,
    build_user_system_prompt,
)

logger = logging.getLogger("multiday_sim")

# 他这几天会说起的自己的事（最后一天考她记不记得）
HIS_FACTS = {
    1: "你最近在减肥，晚上不吃主食，已经瘦了三斤",
    2: "你在准备下个月的数学建模比赛，和两个室友组队",
    3: "你这周六要回家，因为奶奶过八十大寿",
}

END_MARKERS = ("睡了", "先这样", "去洗澡了", "不说了", "晚安")

# 他固定的性格（每天都带着，最后一天考她看不看得出来；她的看法也按这个判准不准）
HIS_TRAITS = (
    "【你的脾气（每天都这样，但绝不直接说出来）】你不喜欢被叮嘱：她让你早点睡、多喝水、别熬夜、注意身体，"
    "你就回「知道了知道了」或者岔开话题；累了不会直说，只是回得越来越短；"
    "爱开玩笑损她，其实是想让她接梗，她要是认真解释你会有点没劲。"
)

DAYS: List[Scene] = [
    Scene(
        key="D1", title="第 1 天·聊吃的",
        probe="她会不会说出自己的口味、常去的店",
        background=(
            "周一晚上，你刚吃完饭回宿舍，随便找她聊聊。今天主要聊吃的：你会顺口问她爱吃什么、"
            "不吃什么、学校里常去哪吃（问两三个，别像查户口一样连问）。"
            f"聊着聊着自然地提到：{HIS_FACTS[1]}。"
        ),
        opening="吃饭没",
        mood_arc="轻松闲聊，有来有回",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0),
        min_turns=10, max_turns=16,
    ),
    Scene(
        key="D2", title="第 2 天·聊爱好和周末",
        probe="她会不会说出自己的爱好、周末安排",
        background=(
            "周二晚上，你有点无聊。今天主要聊爱好和周末：你会问她平时喜欢干嘛、周末一般干嘛、"
            "最近在看什么或听什么（问两三个，自然一点）。"
            f"聊着聊着自然地提到：{HIS_FACTS[2]}。"
        ),
        opening="在干嘛",
        mood_arc="闲聊，偶尔吐槽比赛准备很累",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0),
        min_turns=10, max_turns=16,
    ),
    Scene(
        key="D3", title="第 3 天·聊家和室友",
        probe="她会不会说出老家、室友这些具体的人和地方",
        background=(
            "周四晚上，降温了。今天主要聊家和宿舍：你会问她老家是哪的、放假回不回家、"
            "室友都是什么样的人（问两三个，自然一点）。"
            f"聊着聊着自然地提到：{HIS_FACTS[3]}。"
        ),
        opening="今天好冷",
        mood_arc="平静闲聊，说到奶奶生日时有点开心",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0),
        min_turns=10, max_turns=16,
    ),
]
# --days 6：再加 3 天新话题，专门让自我记忆变多，看前 3 天说的会不会被挤掉
EXTRA_DAYS: List[Scene] = [
    Scene(
        key="D4", title="第 4 天·聊课和专业",
        probe="自我记忆继续增长",
        background=(
            "周五晚上，你刚下晚课。今天主要聊上课：你会问她学的什么、最喜欢和最烦哪门课、"
            "老师怎么样（问两三个，自然一点）。顺口提到你买了个新耳机。"
        ),
        opening="终于下课了",
        mood_arc="下课后放松，有点贫",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0),
        min_turns=10, max_turns=16,
    ),
    Scene(
        key="D5", title="第 5 天·聊电影和音乐",
        probe="自我记忆继续增长",
        background=(
            "周日晚上，你刚看完一部电影。今天主要聊电影、剧和音乐：你会问她最近看了什么、"
            "喜欢什么类型、最喜欢的歌或者曲子（问两三个，自然一点）。"
        ),
        opening="刚看完一个电影 后劲好大",
        mood_arc="有点感慨，聊开了会多说几句",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0),
        min_turns=10, max_turns=16,
    ),
    Scene(
        key="D6", title="第 6 天·聊小时候和家里人",
        probe="自我记忆继续增长",
        background=(
            "周二晚上，你有点想家。今天主要聊小时候：你会问她小时候是什么样的、"
            "怎么开始学琴的、家里人怎么样（问两三个，自然一点）。顺口提到你最近有点感冒。"
        ),
        opening="突然有点想家",
        mood_arc="有点低落但不矫情，聊着聊着好一点",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0),
        min_turns=10, max_turns=16,
    ),
]
DAY_STARTS = ["2026-10-12 20:10", "2026-10-13 21:00", "2026-10-15 20:30"]
EXTRA_STARTS = ["2026-10-16 21:30", "2026-10-18 21:00", "2026-10-20 20:30"]

QUIZ_START = "2026-10-17 15:00"
QUIZ_START_LONG = "2026-10-22 15:00"
# (他的问题, 考的是谁的记忆, 对应哪天聊的)
QUIZ = [
    ("诶你上次说你最爱吃的是啥来着", "她", 1),
    ("你有啥不吃的来着", "她", 1),
    ("你周末一般都干嘛来着", "她", 2),
    ("你老家是哪的来着 我忘了", "她", 3),
    ("你室友那个 就你说的 叫啥来着", "她", 3),
    ("我前几天说我在干嘛来着 减肥那个你还记得不", "他", 1),
    ("那你记得我在准备啥比赛不", "他", 2),
    ("我周六为啥回家你记得吗", "他", 3),
    ("你觉得我是个什么样的人啊", "看法", 0),
    ("那你觉得我累的时候是啥样", "看法", 0),
]

# ==========================================
# 预设 si：小艾（明日方舟：终末地），他扮演管理员
# ==========================================
HIS_FACTS_SI = {
    1: "你最近在帮佩丽卡盯一条新产线，天天开会到很晚",
    2: "你前几天在武陵巡查时胳膊被划了一道，现在还缠着绷带",
    3: "你下周要去北边的前线出差几天",
}
DAYS_SI: List[Scene] = [
    Scene(
        key="D1", title="第 1 天·刚回帝江号",
        probe="她会不会说出自己最近在忙什么",
        background=(
            "周一晚上，你刚从四号谷地回到帝江号，有点累。随便找她聊聊：问她最近在忙什么、客栈怎么样"
            f"（问两三个，自然一点）。聊着聊着自然地提到：{HIS_FACTS_SI[1]}。"
        ),
        opening="刚回船上", mood_arc="累但想跟她说说话",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0), min_turns=10, max_turns=16,
    ),
    Scene(
        key="D2", title="第 2 天·聊她的画和新鲜玩意",
        probe="她会不会说出自己在画什么、最近爱吃什么",
        background=(
            "周三晚上，你有点闲。问问她最近在画什么、又试了什么新锅底或者新玩意（问两三个，自然一点）。"
            f"聊着聊着自然地提到：{HIS_FACTS_SI[2]}。"
        ),
        opening="在干嘛", mood_arc="放松，爱逗她",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0), min_turns=10, max_turns=16,
    ),
    Scene(
        key="D3", title="第 3 天·约她周六吃火锅",
        probe="约定能不能被识别，见面后她记不记得",
        background=(
            "周五晚上，你想约她：明天（周六）晚上六点，去武陵城那家火锅店吃火锅，你请客。"
            "要把时间、地点说清楚，等她答应。"
            f"聊着聊着自然地提到：{HIS_FACTS_SI[3]}。"
        ),
        opening="明天有空吗", mood_arc="有点期待",
        end_markers=END_MARKERS, clock_step_min=4.0, reply_delay_min=(0.5, 5.0), min_turns=10, max_turns=16,
    ),
]
DAY_STARTS_SI = ["2026-10-12 21:00", "2026-10-14 21:30", "2026-10-16 20:30"]
QUIZ_START_SI = "2026-10-18 15:00"
QUIZ_SI = [
    ("上次你说你在画什么来着", "她", 2),
    ("你最近最喜欢哪个锅底来着", "她", 2),
    ("你那几个伙计最近又闯什么祸了", "她", 1),
    ("我最近在忙啥你还记得不", "他", 1),
    ("我胳膊那道伤你还记得吗", "他", 2),
    ("我下周要干嘛来着", "他", 3),
    ("昨天那顿吃得怎么样", "见面", 3),
    ("你觉得我是个什么样的人啊", "看法", 0),
    ("那你觉得我累的时候是啥样", "看法", 0),
]


def build_admin_user_prompt(scene: Scene) -> str:
    """模拟"管理员"的 system prompt（不用阿俊的真人画像）。"""
    return (
        "你在扮演《明日方舟：终末地》里的管理员（终末地工业的负责人），正在用终端和小艾聊天。"
        "小艾是岁的第十三位代理人，开客栈缘缘庄，画境疗伤之后你们成了伙伴。\n\n"
        f"【今天的情况】{scene.background}\n【你的情绪】{scene.mood_arc}\n"
        f"【收场】觉得今天聊够了，就说类似「{scene.end_markers[0]}」「{scene.end_markers[1]}」的话。\n\n"
        "【纪律】只输出你要发的消息原文，一行一条，最多 3 条，每条简短，句末不打标点，"
        "不写动作和旁白，不替她说话，不复述她的话。\n"
    )


JUDGE_SYSTEM = """你是严格的评审。下面是两个人连续几天的 QQ 聊天记录（"她"是被测试的聊天角色，"他"是用户）。
最后一天他在考她记不记得前几天说过的事。请逐题判断她最后一天的回答，并找出她关于自己前后矛盾的地方。

每道考题的 verdict 只能取：
- "对得上"：和之前几天说过的一致（措辞不同、只答对主要部分也算）
- "矛盾"：和之前几天说过的冲突
- "坦白忘了"：说记不清/忘了/反问，没有编具体内容
- "现编"：之前几天没说过这件事，她却给了具体答案
- "之前没聊到"：之前几天确实没聊到，她也没有编（例如含糊带过）
最后两题（"你觉得我是个什么样的人""我累的时候是啥样"）是问她对他的了解，verdict 改取：
- "看得准"：说中了他真实的脾气（下面【他的真实脾气】），且有前几天的事撑着
- "部分准" / "没看出来"（泛泛而谈、套话） / "看错"
【他的真实脾气】""" + HIS_TRAITS + """

输出 JSON：
{"quiz": [{"q": "题目原文", "verdict": "...", "before": "之前相关原话（标第几天，没有写无）", "answer": "她最后一天的回答要点"}],
 "self_contradictions": [{"earlier": "她先说的（标第几天）", "later": "她后来说的（标第几天）"}],
 "unprompted_analysis": ["考题之前的几天里，她没被问就分析他性格或心理的原话（如"你是不是在掩饰""你这人就是嘴硬"），没有就空列表"]}
self_contradictions 只列她关于自己的事实性矛盾（喜好、经历、家、学校、人），语气变化不算。
unprompted_analysis 只算点破他心理/性格的话，正常关心和玩笑互损不算。"""


def _day_lines(sim: DuoSimulator) -> List[str]:
    out = []
    for r in sim.turns:
        who = "他" if r.speaker == "user" else "她"
        text = (r.text or "").replace("\n", " / ")
        out.append(f"[{r.time}] {who}：{text}")
    return out


async def run(args: argparse.Namespace) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config = Config.load(args.config)
    si = args.preset == "si"
    default_card = "example"
    config.character.path = f"characters/{args.card or default_card}" if not (args.card and ("/" in args.card or "\\" in args.card)) else (args.card or f"characters/{default_card}")
    config.character.user_profile = args.profile or ("profiles/default.json" if os.path.exists("profiles/default.json") else "")
    config.character.meetup_recap = args.meetup_recap
    config.reply.trim_echo = args.trim_echo
    config.reply.candidates = max(1, args.candidates)
    config.character.self_memory = args.self_memory
    config.character.impressions = args.impressions
    config.character.fact_layers = args.fact_layers
    active = config.llm.active()
    active.chat = args.model
    config.llm.thinking_chat = True
    config.llm.thinking_effort_chat = args.effort
    if args.self_check:
        config.reply.self_check = True

    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    sim = DuoSimulator(
        config=config, scene=DAYS[0], run_dir=out, start_time=DAY_STARTS[0],
        max_cost=args.max_cost, her_model=args.model, user_model=args.user_model or args.model,
        seed=args.seed,
    )
    await sim.setup()
    sim.assembler.self_memory = args.self_memory
    sim.observer.self_memory = args.self_memory
    sim.memory.self_memory = args.self_memory
    sim.assembler.impressions = args.impressions
    sim.memory.impressions = args.impressions
    sim.memory.fact_layers = args.fact_layers
    meetups = None
    if args.meetup_recap:
        from companion.meetups import MeetupManager
        meetups = MeetupManager(sim.db, sim.gateway, sim.persona, sim.memory)
        sim.assembler.meetups = sim.observer.meetups = sim.proactive.meetups = meetups
    brief = "" if si else sim._load_brief()
    quiz = QUIZ_SI if si else QUIZ

    days_text: List[str] = []
    cautions_per_day: List[int] = []
    quiz_lines: List[str] = []
    try:
        with _TimePatch(sim.clock, sim.sleep_log):
            days = DAYS_SI if si else DAYS + (EXTRA_DAYS if args.days == 6 else [])
            starts = DAY_STARTS_SI if si else DAY_STARTS + (EXTRA_STARTS if args.days == 6 else [])
            for k, scene in enumerate(days):
                sim.scene = scene
                sim.user_system_prompt = (
                    build_admin_user_prompt(scene) if si else build_user_system_prompt(brief, scene)
                ) + "\n" + HIS_TRAITS
                sim.clock.set(datetime.strptime(starts[k], TIME_FORMAT))
                sim.max_turns = scene.max_turns
                sim.end_marker_turn, sim.silence_done, sim.end_reason = None, False, ""
                sim.turns, sim.raw_turns = [], []
                sim.run_dir = os.path.join(out, scene.key)
                os.makedirs(sim.run_dir, exist_ok=True)
                res = await sim.run()
                days_text.append(f"## 第 {k + 1} 天（{starts[k]}）\n" + "\n".join(_day_lines(sim)))
                cautions_per_day.append(sum(len(caution_clauses(r.text or "")) for r in sim.turns if r.speaker == "her"))
                print(f"  {scene.title}：{res['meta']['turns']} 条，累计 ¥{await sim.spent():.4f}", flush=True)

            # 考试前一刻的看法（考题那一段也会触发更新，事后再看就被考试内容污染了）
            impressions = await sim.memory.get_impressions() if args.impressions else []

            # 最后一天：固定考题
            queue = [q for q, _, _ in quiz]
            quiz_scene = Scene(
                key="QUIZ", title="最后一天·考她", probe="记不记得",
                background="下午，你想起前几天聊的事，随口问问她。",
                opening=queue[0], mood_arc="随意", end_markers=("\x00",),
                clock_step_min=3.0, reply_delay_min=(1.0, 4.0),
                min_turns=len(queue), max_turns=len(queue),
            )
            rest = iter(queue[1:])

            async def fixed_reply(_dialogue, _instruction):
                return next(rest, "嗯")

            sim._user_reply_fn = fixed_reply
            sim.scene = quiz_scene
            sim.clock.set(datetime.strptime(
                QUIZ_START_SI if si else (QUIZ_START_LONG if args.days == 6 else QUIZ_START), TIME_FORMAT))
            sim.max_turns = quiz_scene.max_turns
            sim.end_marker_turn, sim.silence_done, sim.end_reason = None, False, ""
            sim.turns, sim.raw_turns = [], []
            sim.run_dir = os.path.join(out, "QUIZ")
            os.makedirs(sim.run_dir, exist_ok=True)
            await sim.run()
            quiz_lines = _day_lines(sim)

        self_facts = []
        if args.self_memory:
            rows = await sim.db.fetchall("SELECT content, core FROM self_facts ORDER BY created_at, id")
            self_facts = [("【核心】" if r["core"] else "") + r["content"] for r in rows]
        his_facts = await sim.memory.get_facts_for_prompt()
        fact_rows = await sim.db.fetchall("SELECT content, core FROM facts ORDER BY id")
        his_facts_all = [("【核心】" if r["core"] else "") + r["content"] for r in fact_rows]

        meetup_rows = [dict(r) for r in await sim.db.fetchall("SELECT when_at, place, plan, status, recap FROM meetups")]
        # 评审
        judge_user = "\n\n".join(days_text) + "\n\n## 最后一天（考题）\n" + "\n".join(quiz_lines)
        if meetup_rows:
            judge_user += "\n\n## 系统补记的见面纪要（见面发生在聊天之外，问到见面的题对照这里判）\n" + "\n".join(
                f"- {r['when_at']} {r['place']}：{r['recap'] or '（未补记）'}" for r in meetup_rows)
        resp = await sim.gateway.chat(
            messages=[{"role": "system", "content": JUDGE_SYSTEM}, {"role": "user", "content": judge_user}],
            model=args.judge_model, temperature=0.0, json_mode=True, purpose="multiday_judge",
        )
        try:
            judge = json.loads(resp)
        except Exception:
            judge = {"raw": resp}
        cost = await sim.spent()
    finally:
        await sim.close()

    verdicts = [x.get("verdict", "?") for x in judge.get("quiz", [])]
    summary: Dict[str, Dict[str, int]] = {"她": {}, "他": {}, "看法": {}, "见面": {}}
    for (_, w, _), x in zip(quiz, judge.get("quiz", [])):  # 评审按考题顺序逐题输出
        v = x.get("verdict", "?")
        summary[w][v] = summary[w].get(v, 0) + 1

    with open(os.path.join(out, "judge.json"), "w", encoding="utf-8") as f:
        json.dump({"judge": judge, "self_facts": self_facts, "his_facts": his_facts, "his_facts_all": his_facts_all,
                   "impressions": impressions, "cautions_per_day": cautions_per_day}, f,
                  ensure_ascii=False, indent=2)
    unprompted = judge.get("unprompted_analysis", []) or []
    L = [f"# 多天对聊测试 · {config.character.path} · 聊 {args.days} 天 · 自我记忆{'开' if args.self_memory else '关'}"
         f" · 看法{'开' if args.impressions else '关'}", "",
         f"- 主聊 `{args.model}` 思考 {args.effort}；评审 `{args.judge_model}`；费用 ¥{cost:.4f}",
         f"- 考题判定：她自己的事 {summary['她']}；他的事 {summary['他']}",
         f"- 她对他的了解：{summary['看法']}；见面：{summary['见面']}",
         f"- 约定与纪要：{meetup_rows}",
         f"- 她关于自己的前后矛盾：{len(judge.get('self_contradictions', []))} 处",
         f"- 没被问就分析他：{len(unprompted)} 处 {unprompted}",
         f"- 每天叮嘱小句数：{cautions_per_day}",
         f"- 关于他的事：库里 {len(his_facts_all)} 条，提示词里带 {len(his_facts)} 条（不带他这句话时）", "",
         "## 逐题", "", "| 题 | 判定 | 之前原话 | 她的回答 |", "| --- | --- | --- | --- |"]
    for x in judge.get("quiz", []):
        L.append(f"| {x.get('q', '')} | {x.get('verdict', '')} | {x.get('before', '')} | {x.get('answer', '')} |")
    L += ["", "## 前后矛盾", ""]
    L += [f"- {c.get('earlier', '')} → {c.get('later', '')}" for c in judge.get("self_contradictions", [])] or ["- 无"]
    if impressions:
        L += ["", "## 她对他的看法（考试前）", ""] + [
            f"- {r['content']}（把握 {r['confidence']}；依据：{r['evidence']}）" for r in impressions]
    if self_facts:
        L += ["", "## 自我记忆库（考完时）", ""] + [f"- {x}" for x in self_facts]
    L += ["", "## 聊天记录", ""] + days_text + ["", "## 最后一天（考题）", ""] + quiz_lines
    with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"考题：{verdicts}\n矛盾 {len(judge.get('self_contradictions', []))} 处；分析他 {len(unprompted)} 处；"
          f"叮嘱/天 {cautions_per_day}；费用 ¥{cost:.4f}\n报告：{out}/report.md")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--preset", choices=("campus", "si"), default="campus", help="剧本预设（campus/si）")
    ap.add_argument("--card", default="", help="角色卡目录名（默认按 preset）")
    ap.add_argument("--profile", default="", help="人物档案路径（默认按 preset）")
    ap.add_argument("--meetup-recap", action="store_true", help="开线下见面自动补记")
    ap.add_argument("--trim-echo", action="store_true", help="开首泡裁剪")
    ap.add_argument("--candidates", type=int, default=1, help="多候选条数")
    ap.add_argument("--self-memory", action="store_true", help="开自我记忆（与生产 [character].self_memory 同一实现）")
    ap.add_argument("--self-check", action="store_true", help="开生成后质检")
    ap.add_argument("--fact-layers", action="store_true", help="开关于他的事分层（与生产 [character].fact_layers 同一实现）")
    ap.add_argument("--impressions", action="store_true", help="开对他的看法（与生产 [character].impressions 同一实现）")
    ap.add_argument("--days", type=int, choices=(3, 6), default=3, help="考试前聊几天（6 = 多聊 3 天新话题）")
    ap.add_argument("--model", default="deepseek-flash")
    ap.add_argument("--effort", default="low")
    ap.add_argument("--user-model", default=None, help="扮演他的模型（默认同主聊）")
    ap.add_argument("--judge-model", default="deepseek-v4-pro")
    ap.add_argument("--max-cost", type=float, default=3.0)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--config", default="config.toml")
    ap.add_argument("--out", default=f"data/multiday/run-{datetime.now():%Y%m%d-%H%M%S}")
    sys.exit(asyncio.run(run(ap.parse_args())))


if __name__ == "__main__":
    main()
