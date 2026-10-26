"""FIXES16 真实 API 冒烟验证 (scripts/smoke/smoke_fixes16.py)

**全新临时库 + 本地 config.toml 的真实 key，零服务器副作用、零生产库读写。**

本轮要验的不是文笔，是**她的生活有没有连续性**：

A. 事件通道：手工插入一条"今天就该有结果"的主线 → 把 arcs 的时钟钉在 19:00
   （真跑状态机 → 真调 API 生成结果 → 真走事件通道生成消息）→ 贴出她的事件消息原文。
   必查：结果是不是真由模型生成的、消息是不是真从事件通道发出去的
   （防"断言自动成立"——上游没跑到，断言也会 PASS）。
B. 提示词注入：插一条 near + 一条 resolved → 组装 system prompt → 贴【她最近的生活】区块。
   必查：upcoming 确实没被注入。
C. 生成器：真跑一次补线 → 贴生成的主线 JSON。
   必查：key_date 落在未来 3~14 天、与素材池气质一致、没和近 30 天重复。

关于时钟：arcs 模块内所有"现在"都走 `companion.arcs.datetime`，
这里把它钉在今天 19:00，好让"当天 18:00 后出结果"这条规则真的被触发，
而不是因为现在是下午三点就自动不成立。

报告落 data/smoke_fixes16_report.json。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from companion.arcs import (
    DEDUP_THRESHOLD,
    MAX_ACTIVE_ARCS,
    RESOLUTION_HOUR,
    LifeArcManager,
    _char_jaccard,
)
from companion.assembler import PromptAssembler
from companion.config import Config, ProactiveConfig, ReplyConfig
from companion.db import TIME_FORMAT, Database
from companion.gateway import LLMGateway
from companion.persona import Persona
from companion.proactive import ProactiveScheduler
from companion.replier import Replier

logging.basicConfig(level=logging.WARNING)

SANDBOX_DB = "data/smoke_fixes16_sandbox.db"
REPORT_FILE = "data/smoke_fixes16_report.json"

SEGMENTS = ("A_event_channel", "B_prompt_injection", "C_generator")


class _FrozenDatetime(datetime):
    """把 arcs 模块的"现在"钉在今天 19:00（过 18:00 出结果线）。"""

    _frozen: datetime = datetime.now().replace(hour=19, minute=0, second=0, microsecond=0)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003
        return cls._frozen

    @classmethod
    def set(cls, value: datetime) -> None:
        cls._frozen = value


def _rule(ok: bool, name: str, detail: str = "") -> Dict[str, Any]:
    return {"rule": name, "pass": bool(ok), "detail": detail}


def _dump(title: str, text: str, width: int = 78) -> str:
    line = "─" * width
    body = "\n".join(f"  {ln}" for ln in (text or "").splitlines() if ln.strip())
    return f"\n{line}\n{title}\n{line}\n{body}\n{line}"


class Harness:
    """一套独立的生活剧本栈：临时库 + 真实 gateway + 真实组装器/主动消息。"""

    def __init__(self, config: Config, persona: Persona):
        self.config = config
        self.persona = persona
        self.db = Database(SANDBOX_DB)
        self.gateway = LLMGateway(config.llm, self.db)
        self.arcs = LifeArcManager(self.db, self.gateway, persona)
        self.sent: List[str] = []
        self.proactive: Optional[ProactiveScheduler] = None

    async def setup(self) -> None:
        await self.db.init_tables()
        from companion.affection import AffectionEngine
        from companion.memory import MemoryManager
        from companion.mood import MoodEngine
        from companion.stickers import StickerManager

        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)
        self.stickers = StickerManager(stickers_dir, self.db)
        self.replier = Replier(ReplyConfig(5, 0.0, 0.0), self.stickers)
        self.assembler = PromptAssembler(
            self.persona,
            self.affection,
            self.mood,
            self.memory,
            self.stickers,
            self.db,
            holidays_provider=self.config.get_holidays,
            arcs=self.arcs,
        )

    async def build_proactive(self) -> None:
        async def collect(chunk):
            self.sent.append(chunk.get("content", ""))

        async def typing_fake(typing: bool) -> bool:
            self.sent.append(f"[typing]{'开' if typing else '关'}")
            return True

        self.proactive = ProactiveScheduler(
            # 免打扰放开：本段要验证事件通道本身，不让静默时段把整条链零成本拦掉
            config=ProactiveConfig(quiet_hours=[], max_unanswered=5),
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=self.gateway,
            db=self.db,
            send_msg_fn=collect,
            assembler=self.assembler,
            holidays_provider=self.config.get_holidays,
            set_typing_fn=typing_fake,
            timing_config=self.config.timing,
            arcs=self.arcs,
        )

    def install_call_spy(self) -> None:
        """在真实 gateway 外包一层调用记录器。

        为什么要这个：光看"发出去一句什么"分不清它是**事件通道**发的还是
        **常规决策层**发的（两者共用同一条生成管道）。记下每次调用的 purpose
        与实际提示词，才能结构化证明事件通道真的抢在决策层前面跑了。
        """
        real_chat = self.gateway.chat
        self.call_log: List[Dict[str, Any]] = []

        async def spy(**kwargs):
            rec: Dict[str, Any] = {"purpose": kwargs.get("purpose", "")}
            msgs = kwargs.get("messages") or []
            rec["user_prompt"] = str(msgs[-1].get("content", "")) if msgs else ""
            self.call_log.append(rec)
            return await real_chat(**kwargs)

        self.gateway.chat = spy

    def calls_for(self, purpose: str) -> List[Dict[str, Any]]:
        return [c for c in getattr(self, "call_log", []) if c["purpose"] == purpose]

    def event_material_in_prompt(self) -> str:
        """从 proactive_message 调用里抽出话题素材段原文。"""
        for c in self.calls_for("proactive_message"):
            text = c["user_prompt"]
            head = text.find("【话题切入点】")
            tail = text.find("【最近的聊天记录】")
            if head != -1 and tail != -1:
                return text[head:tail]
        return ""

    async def close(self) -> None:
        await self.proactive_stop()
        try:
            await self.gateway.close()
        except Exception:
            pass
        await self.db.close()

    async def proactive_stop(self) -> None:
        pass

    async def llm_rows(self) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT purpose, model, prompt_tokens, completion_tokens FROM llm_calls ORDER BY id"
        )
        return [dict(r) for r in rows]


# ==========================================
# A 段：事件通道
# ==========================================

ARC_A = {
    "title": "乐团节目审查",
    "detail": "下周三晚在玉泉合排之后有个节目审查，大提琴声部要单独过一段，"
              "她这段一直合不齐，怕到时候被指挥当众点名",
    "key_date": None,  # 运行时填"今天"
    "emotional_stake": "紧张，低音部那段到现在还没合齐",
}

# 汇报腔黑名单：事件消息里出现这些就说明她在"汇报"而不是"脱口而出"
REPORT_TONE_WORDS = ["刚刚", "结果", "综上", "总结", "汇报", "如下", "第一", "第二", "总而言之"]


async def segment_a(h: Harness) -> Dict[str, Any]:
    report: Dict[str, Any] = {"segment": "A_event_channel"}
    now = _FrozenDatetime.now()
    today = now.strftime("%Y-%m-%d")

    await h.arcs.insert_arc(
        ARC_A["title"], ARC_A["detail"], today, ARC_A["emotional_stake"]
    )
    rows_before = await h.arcs.fetch_arcs()
    report["seeded"] = {
        "title": rows_before[0]["title"],
        "key_date": rows_before[0]["key_date"],
        "status": rows_before[0]["status"],
        "emotional_stake": rows_before[0]["emotional_stake"],
    }

    # ① 真跑状态机：upcoming/whatever → today →（19:00 已过 18:00 线）resolved
    with patch("companion.arcs.datetime", _FrozenDatetime):
        changed = await h.arcs.advance_states()
    arc = (await h.arcs.fetch_arcs())[0]
    report["after_advance"] = {
        "changed": changed,
        "status": arc["status"],
        "resolution": arc["resolution"],
        "resolved_at": arc["resolved_at"],
        "event_announced": arc["event_announced"],
    }
    report["raw_evidence"] = {}
    report["raw_evidence"]["A_resolution_text"] = arc["resolution"]
    print(_dump("【A-1】状态机推进结果（resolution 由 flash 真生成）", arc["resolution"] or "（空）"))

    # ② 真跑事件通道：走完整 trigger_cycle
    await h.build_proactive()
    h.install_call_spy()
    with patch("companion.arcs.datetime", _FrozenDatetime):
        await h.proactive.trigger_cycle()

    texts = [s for s in h.sent if not s.startswith("[typing]")]
    typings = [s for s in h.sent if s.startswith("[typing]")]
    report["event_message"] = texts[0] if texts else ""
    report["typing_sequence"] = typings
    material = h.event_material_in_prompt()
    decision_calls = h.calls_for("proactive_decision")
    gen_calls = h.calls_for("proactive_message")
    report["call_trace"] = {
        "purposes": [c["purpose"] for c in getattr(h, "call_log", [])],
        "event_material": material,
        "decision_calls": len(decision_calls),
        "generate_calls": len(gen_calls),
    }
    report["raw_evidence"]["A_event_message"] = report["event_message"]
    report["raw_evidence"]["A_event_topic_material"] = material
    print(_dump("【A-2】她的事件消息原文（事件通道实发）", report["event_message"] or "（未发出）"))
    print(_dump("【A-2b】本轮实际注入的话题素材（证明走的是事件通道）", material or "（无）"))
    print(_dump("【A-3】typing 表演序列", " → ".join(typings) or "（未表演）"))
    print(f"【A-4】本轮 LLM 调用序列：{[c['purpose'] for c in getattr(h, 'call_log', [])]}")

    after = (await h.arcs.fetch_arcs())[0]
    unanswered = await h.proactive.get_unanswered_count()
    report["after_event"] = {
        "event_announced": after["event_announced"],
        "unanswered": unanswered,
    }

    msg = report["event_message"]
    # 事件消息与 resolution 的字符相似度：证明她说的确实是这件事，
    # 而不是决策层另编的别的（相似度作为实测数字如实报告，不做模糊断言）
    overlap = _char_jaccard(msg, arc["resolution"])
    report["resolution_overlap"] = round(overlap, 3)

    # ---- 判定：每条都必须是"这条链真的跑到了"，不是自动成立 ----
    report["rules"] = [
        _rule(arc["status"] == "resolved", "状态机推到 resolved", f"status={arc['status']}"),
        _rule(bool(arc["resolution"]), "resolution 由模型真生成", f"{len(arc['resolution'])} 字"),
        _rule(after["event_announced"] == 1, "event_announced 置位为 1"),
        _rule(bool(msg), "事件消息真的发出去（不是空断言）", f"{len(msg)} 字"),
        _rule(
            len(decision_calls) == 0,
            "事件命中时决策层一次都没被调用（事件抢在常规决策前面）",
            f"proactive_decision 调用 {len(decision_calls)} 次",
        ),
        _rule(
            len(gen_calls) == 1 and ARC_A["title"] in material,
            "生成层拿到的是这条主线（结构化取证，不靠字符串猜测）",
            f"素材含标题《{ARC_A['title']}》: {ARC_A['title'] in material}",
        ),
        _rule(
            overlap >= 0.3,
            "消息内容与 resolution 指的是同一件事（实测相似度）",
            f"字符 Jaccard={overlap:.3f}",
        ),
        _rule(
            not any(w in msg for w in REPORT_TONE_WORDS),
            "语气是脱口而出，不是汇报",
            f"命中汇报腔词：{[w for w in REPORT_TONE_WORDS if w in msg]}",
        ),
        _rule(
            unanswered >= 1,
            "事件消息计入现有连续未回闸门",
            f"unanswered={unanswered}",
        ),
        _rule(
            typings[:2] == ["[typing]开", "[typing]关"],
            "typing 表演与常规主动消息同构",
            f"序列={typings}",
        ),
    ]

    # 二次触发：事件已被消费，**不得再走事件通道**。
    # 注意：当天事件额度用完后回落常规决策层是设计行为（任务5 第2条），
    # 所以这里只能断言"没有第二次事件通道调用"，不能断言"总共只发一条"。
    n_gen_before = len(h.calls_for("proactive_message"))
    with patch("companion.arcs.datetime", _FrozenDatetime):
        await h.proactive.trigger_cycle()
    n_gen_after = len(h.calls_for("proactive_message"))
    report["second_trigger"] = {
        "event_channel_generate_calls": n_gen_after - n_gen_before,
        "event_announced": (await h.arcs.fetch_arcs())[0]["event_announced"],
        "purposes": [c["purpose"] for c in getattr(h, "call_log", [])],
    }
    report["rules"].append(
        _rule(
            n_gen_after == n_gen_before
            and (await h.arcs.fetch_arcs())[0]["event_announced"] == 1,
            "同一件事不会连发两遍（第二次触发不再走事件通道）",
            f"第二轮事件通道生成调用增量={n_gen_after - n_gen_before}",
        )
    )
    return report


# ==========================================
# B 段：提示词注入
# ==========================================


async def segment_b(h: Harness) -> Dict[str, Any]:
    report: Dict[str, Any] = {"segment": "B_prompt_injection"}
    now = _FrozenDatetime.now()
    base = now.replace(hour=12, minute=0, second=0, microsecond=0)

    # near：明天的事，她心里挂着
    await h.db.execute(
        """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
           VALUES (?,?,?,'near',?,?)""",
        (
            "紫金港琴房预约",
            "期末前琴房抢不到位置，她每天刷浙大体艺App",
            (base + timedelta(days=1)).strftime("%Y-%m-%d"),
            "怕整周都抢不到",
            base.strftime(TIME_FORMAT),
        ),
    )
    # today：就在今天
    await h.db.execute(
        """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
           VALUES (?,?,?,'today',?,?)""",
        (
            "现代汉语小组 pre",
            "分到讲语气词那组，ppt 还没改完",
            base.strftime("%Y-%m-%d"),
            "怕讲不完被老师打断",
            base.strftime(TIME_FORMAT),
        ),
    )
    # resolved：昨天出的结果
    await h.db.execute(
        """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, resolution,
                                 created_at, resolved_at)
           VALUES (?,?,?,'resolved',?,?,?,?)""",
        (
            "中国古代文学史读书报告",
            "选题卡了两周",
            (base - timedelta(days=1)).strftime("%Y-%m-%d"),
            "怕选题太冷门",
            "交上了，老师说选题有意思",
            (base - timedelta(days=2)).strftime(TIME_FORMAT),
            (base - timedelta(hours=20)).strftime(TIME_FORMAT),
        ),
    )
    # upcoming：还没到她心里，**不该**出现在提示词里
    await h.db.execute(
        """INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
           VALUES (?,?,?,'upcoming',?,?)""",
        (
            "十一月体测预约",
            "800 米，她怕跑不动",
            (base + timedelta(days=8)).strftime("%Y-%m-%d"),
            "怕不及格",
            base.strftime(TIME_FORMAT),
        ),
    )

    prompt = await h.assembler.assemble_system_prompt("")
    block = await h.arcs.build_prompt_block()
    report["block"] = block
    report["raw_evidence"] = {"B_life_block": block}
    print(_dump("【B】system prompt 里的【她最近的生活】区块", block or "（整块省略）"))

    # 区块在完整提示词里的位置
    idx_facts = prompt.find("【事实】")
    idx_block = prompt.find("【她最近的生活】")
    idx_stage = prompt.find("【当前关系阶段")

    report["position"] = {
        "facts": idx_facts,
        "block": idx_block,
        "stage": idx_stage,
    }
    report["rules"] = [
        _rule("【她最近的生活】" in block, "区块标题存在"),
        _rule("（临近）紫金港琴房预约" in block, "near 主线被注入"),
        _rule("（临近）现代汉语小组 pre" in block, "today 主线被注入"),
        _rule("（有结果）中国古代文学史读书报告" in block, "resolved 主线被注入"),
        _rule("交上了，老师说选题有意思" in block, "结果文本进了区块"),
        _rule("十一月体测预约" not in block, "upcoming 主线不注入（还没到她心里）"),
        _rule(
            -1 < idx_facts < idx_block < idx_stage,
            "位置在【事实】区之后、阶段块之前",
            f"facts={idx_facts} block={idx_block} stage={idx_stage}",
        ),
        _rule("【她最近的生活】" in prompt, "区块真的进了 system prompt"),
    ]

    # 反向对照：把 near/resolved 全清掉，整块必须消失
    await h.db.execute("DELETE FROM life_arcs WHERE status IN ('near','today')")
    await h.db.execute("DELETE FROM life_arcs WHERE status = 'resolved'")
    empty_block = await h.arcs.build_prompt_block()
    report["empty_block"] = empty_block
    report["rules"].append(
        _rule(
            empty_block == "",
            "无可注入主线时整块省略（不留空标题）",
            f"空形态返回 {empty_block!r}",
        )
    )
    return report


# ==========================================
# C 段：生成器
# ==========================================

# 素材池里的招牌词：生成的主线应该沾这些气质（浙大实际叫法，而不是百度百科口径）
SEED_FLAVOR_WORDS = [
    "大食堂", "银泉", "临湖", "基图", "琴房", "乐跑", "体测", "pre", "DDL",
    "文琴", "乐团", "选课", "读书报告", "论文", "体艺", "北街", "堕落街",
    "冬学期", "秋学期", "考", "断电", "快递", "银泉", "月牙楼", "选课",
]
# 禁止出现的戏剧性大事件（任务3 结果/任务2 主线都禁）
BANNED_WORDS = ["获奖", "拿奖", "一等奖", "事故", "车祸", "表白", "分手", "住院", "生病住院", "国奖"]


async def segment_c(h: Harness) -> Dict[str, Any]:
    report: Dict[str, Any] = {"segment": "C_generator"}
    # A/B 段留下的主线会污染"活跃上限"与"两两去重"的判定，先清空再验生成器
    await h.db.execute("DELETE FROM life_arcs")
    await h.db.execute("DELETE FROM state WHERE key = 'life_arc_generate'")
    report["cleared_before"] = len(await h.arcs.fetch_arcs())

    now = _FrozenDatetime.now()
    with patch("companion.arcs.datetime", _FrozenDatetime):
        added = await h.arcs.ensure_arcs(min_active=MAX_ACTIVE_ARCS)
    rows = await h.arcs.fetch_arcs()
    report["added"] = added
    report["arcs"] = [
        {
            "title": r["title"],
            "detail": r["detail"],
            "key_date": r["key_date"],
            "emotional_stake": r["emotional_stake"],
            "status": r["status"],
        }
        for r in rows
    ]
    report["raw_evidence"] = {
        "C_generated_json": json.dumps(report["arcs"], ensure_ascii=False, indent=2)
    }
    print(_dump("【C】真跑生成器产出的主线 JSON", report["raw_evidence"]["C_generated_json"]))

    d_min = (now + timedelta(days=3)).strftime("%Y-%m-%d")
    d_max = (now + timedelta(days=14)).strftime("%Y-%m-%d")
    rules = [
        _rule(added >= 1, "生成器真的写入了主线（不是 0 条空过）", f"added={added}"),
        _rule(len(rows) <= MAX_ACTIVE_ARCS, "活跃主线不超过上限", f"{len(rows)} ≤ {MAX_ACTIVE_ARCS}"),
    ]
    for r in report["arcs"]:
        rules.append(_rule(
            d_min <= r["key_date"] <= d_max,
            f"key_date 落在未来 3~14 天 [{r['title']}]",
            f"{r['key_date']} ∈ [{d_min}, {d_max}]",
        ))
        rules.append(_rule(
            all(w not in (r["title"] + r["detail"]) for w in BANNED_WORDS),
            f"无戏剧性大事件 [{r['title']}]",
            f"命中：{[w for w in BANNED_WORDS if w in r['title'] + r['detail']]}",
        ))

    # 气质一致性：至少一条沾上素材池招牌词（软指标，如实报告命中数）
    hits = [
        w for w in SEED_FLAVOR_WORDS
        if any(w in (r["title"] + r["detail"]) for r in report["arcs"])
    ]
    report["seed_flavor_hits"] = hits
    rules.append(_rule(
        bool(hits),
        "生成内容沾上素材池气质（浙大实际叫法）",
        f"命中招牌词：{hits}",
    ))

    # 去重：生成的两条之间、以及与已入库的，不该高度重复
    sims = []
    for i in range(len(report["arcs"])):
        for j in range(i + 1, len(report["arcs"])):
            sims.append(_char_jaccard(
                report["arcs"][i]["title"] + "｜" + report["arcs"][i]["detail"],
                report["arcs"][j]["title"] + "｜" + report["arcs"][j]["detail"],
            ))
    report["pairwise_similarity"] = [round(s, 3) for s in sims]
    rules.append(_rule(
        all(s < DEDUP_THRESHOLD for s in sims) if sims else True,
        f"本轮主线之间无高度重复（阈值 {DEDUP_THRESHOLD}）",
        f"两两相似度：{report['pairwise_similarity']}",
    ))

    # 上限闸门：已经 3 条了，再调一次必须一个 API 都不发
    rows_before = len(await h.db.fetchall("SELECT id FROM llm_calls WHERE purpose='life_arc'"))
    again = await h.arcs.ensure_arcs(min_active=MAX_ACTIVE_ARCS)
    rows_after = len(await h.db.fetchall("SELECT id FROM llm_calls WHERE purpose='life_arc'"))
    report["cap_recheck"] = {
        "added": again,
        "llm_calls_before": rows_before,
        "llm_calls_after": rows_after,
    }
    rules.append(_rule(
        again == 0 and rows_after == rows_before,
        "达到上限后再调不生成、也不发 API",
        f"added={again}, life_arc 调用 {rows_before}→{rows_after}",
    ))

    report["rules"] = rules
    return report


# ==========================================
# 主流程
# ==========================================


async def main() -> int:
    if os.path.exists(SANDBOX_DB):
        os.remove(SANDBOX_DB)

    config = Config.load()
    persona = Persona.load(config.character.path)
    h = Harness(config, persona)
    await h.setup()

    report: Dict[str, Any] = {
        "generated_at": datetime.now().strftime(TIME_FORMAT),
        "frozen_clock_for_arcs": _FrozenDatetime.now().strftime(TIME_FORMAT),
        "segments": {},
        "notes": [
            "全新临时库 data/smoke_fixes16_sandbox.db，零生产库读写、零服务器副作用。",
            "arcs 模块的时钟钉在今天 19:00，为的是让'当天 18:00 后出结果'这条规则真被触发。",
            "ProactiveConfig.quiet_hours 放开为 []：本轮验事件通道本身，不让静默时段零成本拦掉整条链；"
            "免打扰等待行为由 tests/test_fixes16.py 的单测覆盖。",
        ],
    }

    t0 = time.time()
    try:
        for name, fn in (("A_event_channel", segment_a),
                         ("B_prompt_injection", segment_b),
                         ("C_generator", segment_c)):
            print(f"\n{'=' * 78}\n>>> {name}\n{'=' * 78}")
            seg = await fn(h)
            report["segments"][name] = seg
    finally:
        report["llm_calls"] = await h.llm_rows()
        await h.close()
        for suffix in ("", "-wal", "-shm"):
            p = SANDBOX_DB + suffix
            if os.path.exists(p):
                os.remove(p)

    # 汇总
    all_rules = [
        (seg_name, r)
        for seg_name, seg in report["segments"].items()
        for r in seg["rules"]
    ]
    failed = [(s, r) for s, r in all_rules if not r["pass"]]
    report["summary"] = {
        "total": len(all_rules),
        "passed": len(all_rules) - len(failed),
        "failed": len(failed),
        "failed_rules": [{"segment": s, **r} for s, r in failed],
        "elapsed_sec": round(time.time() - t0, 1),
    }

    os.makedirs(os.path.dirname(REPORT_FILE), exist_ok=True)
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n{'=' * 78}")
    print(f"冒烟汇总：{report['summary']['passed']}/{report['summary']['total']} 通过，"
          f"耗时 {report['summary']['elapsed_sec']}s")
    print(f"计费落库（本轮共 {len(report['llm_calls'])} 次调用）：")
    for row in report["llm_calls"]:
        print(f"  • {row['purpose']:<22} {row['model']:<20} in={row['prompt_tokens']} out={row['completion_tokens']}")
    if failed:
        print("\n未通过的判定：")
        for s, r in failed:
            print(f"  ✗ [{s}] {r['rule']} — {r['detail']}")
    else:
        print("\n全部判定通过。")
    print(f"\n报告已写入 {REPORT_FILE}")
    print("=" * 78)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
