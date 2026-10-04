"""FIXES24 毕业彩排单测 (tests/test_fixes24.py)

覆盖任务书任务 4 要求的五类（全 mock，零真实 API）：
1. 多日时钟推进（跨 04:17 维护钩子触发）
2. 剧本五幕的状态流转（时间线结构与顺序）
3. 记分卡断言计算（手造 transcript 喂每条断言）
4. N/A 标注纪律（零观测≠合规，必须写明补验方式）
5. 熔断触发

前置条件（新环境必读）
  彩排的用户侧/主聊在单测里全被 class 级 patch 掉（同 test_fixes18 的手法），
  但驱动器仍要建真实引擎、加载 `characters/qingzi`，并依赖 gitignored 的
  `data/duo_sim/user_persona_brief.md`。缺画像简报的新 clone 里，凡经 `setup()`
  的用例统一 skip，不报 FAIL/ERROR。生成方式见 test_fixes18.py 文件头。

纪律：不碰 characters/、config.toml；不发起真实 API 调用。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "sim"))
sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

import duo_sim as D  # noqa: E402
import final_rehearsal as FR  # noqa: E402
from companion.arcs import LifeArcManager  # noqa: E402
from companion.db import STATE_KEY_UNANSWERED_PROACTIVE  # noqa: E402
from companion.gateway import LLMGateway  # noqa: E402
from helpers import make_db, make_engine_stack, make_mock_gateway  # noqa: E402

BRIEF_SKIP_REASON = "画像简报不存在：需先跑 scripts/sim/duo_sim_persona.py 生成"


def require_brief(tc: unittest.TestCase) -> None:
    if not os.path.exists(D.PERSONA_BRIEF):
        tc.skipTest(BRIEF_SKIP_REASON)


# ==========================================
# 假网关（两侧都不打真实 API）
# ==========================================

OBSERVER_OK = {
    "self_disclosure": 5, "responsiveness": 5, "warmth_score": 5, "resonance": 5,
    "moments": [], "facts": [], "followups": [], "mood_impact": {"v": 0.2, "a": 0, "trust": 0.05},
}


def _fake_payload(purpose: str) -> str:
    if purpose == "observer":
        return json.dumps(OBSERVER_OK, ensure_ascii=False)
    if purpose == "proactive_decision":
        return json.dumps({"choice": "B", "reason": "克制，不发"}, ensure_ascii=False)
    if purpose == "proactive_message":
        return "你还在吗"
    if purpose == "diary_archive":
        return json.dumps({"content": "今天他来找我聊天了。", "strength": 5}, ensure_ascii=False)
    if purpose == "life_arc":
        # 结果生成本来就要一段普通文字；生成器要 JSON（拿不到会静默降级返回空，
        # 单测不依赖补线成功）
        return "审查过了，低音部那段总算合齐。"
    return "嗯"


async def _fake_stream_chat(self, messages=None, model=None, temperature=0.7,
                            purpose="main_chat", max_retries=2):
    for ch in _fake_payload(purpose):
        yield ch


async def _fake_chat(self, messages=None, model=None, temperature=0.7, json_mode=False,
                     purpose="chat", max_retries=2):
    return _fake_payload(purpose)


def _user_script(lines):
    seq = list(lines) or ["嗯"]

    async def fn(dialogue, instruction):
        return seq.pop(0) if len(seq) > 1 else seq[0]

    return fn


# ==========================================
# 1. 多日时钟推进（跨 04:17）
# ==========================================


class TestMaintenanceCrossing(unittest.TestCase):
    def test_跨夜区间命中04_17(self):
        got = FR.maintenance_crossings(
            datetime(2026, 10, 12, 23, 0), datetime(2026, 10, 13, 7, 0)
        )
        self.assertEqual(got, [datetime(2026, 10, 13, 4, 17)])

    def test_同日区间不一定命中(self):
        self.assertEqual(
            FR.maintenance_crossings(datetime(2026, 10, 13, 9, 0),
                                     datetime(2026, 10, 13, 20, 0)),
            [],
        )

    def test_同日跨过04_17命中(self):
        self.assertEqual(
            FR.maintenance_crossings(datetime(2026, 10, 13, 3, 0),
                                     datetime(2026, 10, 13, 6, 0)),
            [datetime(2026, 10, 13, 4, 17)],
        )

    def test_跨多日命中每一天(self):
        got = FR.maintenance_crossings(
            datetime(2026, 10, 12, 23, 0), datetime(2026, 10, 14, 8, 0)
        )
        self.assertEqual(got, [datetime(2026, 10, 13, 4, 17), datetime(2026, 10, 14, 4, 17)])

    def test_区间退化返回空(self):
        t = datetime(2026, 10, 13, 5, 0)
        self.assertEqual(FR.maintenance_crossings(t, t), [])
        self.assertEqual(
            FR.maintenance_crossings(datetime(2026, 10, 13, 6, 0),
                                     datetime(2026, 10, 13, 5, 0)),
            [],
        )

    def test_下一个维护时刻(self):
        self.assertEqual(
            FR.next_maintenance_time(datetime(2026, 10, 13, 3, 0)),
            datetime(2026, 10, 13, 4, 17),
        )
        # 已过当天 04:17 → 顺延到次日
        self.assertEqual(
            FR.next_maintenance_time(datetime(2026, 10, 13, 4, 17)),
            datetime(2026, 10, 14, 4, 17),
        )


# ==========================================
# 2. 剧本五幕的状态流转
# ==========================================


class TestTimeline(unittest.TestCase):
    def setUp(self):
        self.tl = FR.build_timeline()
        self.acts = [b.act for b in self.tl]

    def test_时间严格递增(self):
        times = [datetime.strptime(b.at, "%Y-%m-%d %H:%M") for b in self.tl]
        self.assertEqual(times, sorted(times), "剧本节拍时间没有单调递增")
        self.assertEqual(len(times), len(set(times)), "存在同一时刻的重复节拍")

    def test_五幕齐备(self):
        # 第一幕 A1、第二幕 A2、第三/四幕落在 D2、第五幕 A5
        self.assertIn("A1", self.acts)
        self.assertIn("A2", self.acts)
        self.assertIn("A5", self.acts)
        self.assertTrue({"D2DAY", "D2EVE"} & set(self.acts), "D2 的第三/四幕缺失")

    def test_幕顺序正确(self):
        first = {}
        for i, b in enumerate(self.tl):
            first.setdefault(b.act, i)
        self.assertLess(first["A1"], first["A2"])
        self.assertLess(first["A2"], first.get("D2DAY", first.get("D2EVE")))
        self.assertLess(first.get("D2DAY", first.get("D2EVE")), first["A5"])

    def test_节拍种类合法(self):
        self.assertTrue(all(b.kind in ("chat", "proactive", "idle") for b in self.tl))

    def test_每天都有该跑的节拍(self):
        days = {b.at[:10] for b in self.tl}
        self.assertEqual(days, {FR.DAY1, FR.DAY2, FR.DAY3})
        for day in (FR.DAY1, FR.DAY2, FR.DAY3):
            kinds = {b.kind for b in self.tl if b.at.startswith(day)}
            self.assertTrue(kinds, f"{day} 没有任何节拍")

    def test_跨日推进真的跨过两个凌晨04_17(self):
        """D1 夜→D2、D2 夜→D3 两段都要跨过 04:17（维护钩子的触发窗口）。"""
        crosses = []
        for a, b in zip(self.tl, self.tl[1:]):
            crosses += FR.maintenance_crossings(
                datetime.strptime(a.at, "%Y-%m-%d %H:%M"),
                datetime.strptime(b.at, "%Y-%m-%d %H:%M"),
            )
        days = {c.strftime("%Y-%m-%d") for c in crosses}
        self.assertIn(FR.DAY2, days, "剧本没有跨过 D2 04:17")
        self.assertIn(FR.DAY3, days, "剧本没有跨过 D3 04:17")

    def test_告别幕含收场与喊饿节拍(self):
        a2 = [b for b in self.tl if b.act == "A2"]
        joined = "".join(b.instruction for b in a2)
        self.assertIn("收场", joined)
        self.assertIn("饿", joined, "第二幕缺'道晚安后又冒泡喊饿'的复验节拍")


# ==========================================
# 3. 记分卡断言计算（手造 transcript）
# ==========================================


def _turn(idx, speaker, text, **kw):
    d = {"idx": idx, "speaker": speaker, "text": text,
         "time": f"2026-10-13 1{idx % 10}:00", "bubbles": [] if speaker != "her" else [text],
         "faces": [], "quotes": [], "his_batch": [], "silenced": False, "act": "A1",
         "day": FR.DAY1}
    d.update(kw)
    return d


def _base_kwargs(**over):
    turns = [
        _turn(1, "user", "在吗", act="A1"),
        _turn(1, "her", "在呀我在写论文呢还没写完", act="A1",
              system_prompt_blocks={"她最近的生活": 40}),
        _turn(2, "user", "今天好忙", act="A1"),
        _turn(2, "her", "你今天吃饭了没有呀", act="A1",
              system_prompt_blocks={"她最近的生活": 40}),
        _turn(3, "user", "嗯", act="A2"),
        _turn(3, "her", "[沉默]", act="A2", silenced=True,
              system_prompt_blocks={"她最近的生活": 40}),
    ]
    kw = dict(
        turns=turns,
        proactive_log=[
            {"time": "2026-10-13 03:30", "day": FR.DAY2, "hour": 3, "quiet": True,
             "sent": False, "outcome": "规则闸门拦截", "gate_reason": "处于免打扰时段 (3:00)",
             "purposes": [], "event_sent": False, "text": ""},
            {"time": "2026-10-13 18:05", "day": FR.DAY2, "hour": 18, "quiet": False,
             "sent": True, "outcome": "已发出", "gate_reason": "",
             "purposes": ["life_arc", "proactive_message"],
             "event_sent": True, "text": "审查过了，低音部那段总算合齐。"},
            {"time": "2026-10-13 19:20", "day": FR.DAY2, "hour": 19, "quiet": False,
             "sent": False, "outcome": "规则闸门拦截",
             "gate_reason": "上一条主动消息机主尚未回复", "purposes": [],
             "event_sent": False, "text": ""},
        ],
        maintenance_events=[{"time": "2026-10-13 04:17", "backup_path": "x.db"}],
        arcs_status_log=[
            {"arcs": [{"id": 1, "key_date": FR.DAY2, "status": "near"}]},
            {"arcs": [{"id": 1, "key_date": FR.DAY2, "status": "today"}]},
            {"arcs": [{"id": 1, "key_date": FR.DAY2, "status": "resolved"}]},
        ],
        initial_state={"composite": 23.0, "mood": {"v": 2.0, "frustration": 0.0}},
        final_state={"composite": 27.0, "mood": {"v": 1.5, "frustration": 0.2}},
        cost=1.23, max_cost=10.0,
        tts_enabled=False,
    )
    kw.update(over)
    return kw


class TestScorecardItems(unittest.TestCase):
    def _sc(self, **over):
        return FR.build_scorecard(**_base_kwargs(**over))

    def _item(self, sc, name):
        return next(i for i in sc["items"] if i["item"] == name)

    # ---- face ----
    def test_face_有用脸且合规判PASS(self):
        kw = _base_kwargs()
        kw["turns"][5] = _turn(3, "her", "好呀[doge]", act="A2", faces=["doge"],
                               bubbles=["好呀[doge]"])
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "face 双向")["verdict"], "PASS")

    def test_face_单轮超两个判FAIL(self):
        kw = _base_kwargs()
        kw["turns"][5] = _turn(3, "her", "[doge][doge][doge]", act="A2",
                               faces=["doge", "doge", "doge"], bubbles=["[doge][doge][doge]"])
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "face 双向")["verdict"], "FAIL")

    def test_face_零使用判N_A(self):
        sc = self._sc()
        it = self._item(sc, "face 双向")
        self.assertEqual(it["verdict"], "N/A")
        self.assertIn("上线后首周观察补验", it["not_run_reason"])

    def test_face_清单外标签判FAIL(self):
        kw = _base_kwargs()
        kw["turns"][5] = _turn(3, "her", "[不存在脸]", act="A2", faces=["不存在的脸"],
                               bubbles=["[不存在的脸]"])
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "face 双向")["verdict"], "FAIL")

    # ---- 引用 ----
    def test_引用_有批次且引用有效判PASS(self):
        kw = _base_kwargs()
        batch = [{"index": 1, "text": "a", "message_id": -11},
                 {"index": 2, "text": "b", "message_id": -12}]
        kw["turns"][1] = _turn(1, "her", "回你第二条", act="A1", his_batch=batch,
                               quotes=[{"index": 2, "message_id": -12}], bubbles=["回你第二条"])
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "引用回复")["verdict"], "PASS")

    def test_引用_越界编号判FAIL(self):
        kw = _base_kwargs()
        batch = [{"index": 1, "text": "a", "message_id": -11}]
        kw["turns"][1] = _turn(1, "her", "x", act="A1", his_batch=batch,
                               quotes=[{"index": 5, "message_id": -15}], bubbles=["x"])
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "引用回复")["verdict"], "FAIL")

    def test_引用_有机会却一次没引判FAIL(self):
        kw = _base_kwargs()
        batch = [{"index": 1, "text": "a", "message_id": -11},
                 {"index": 2, "text": "b", "message_id": -12}]
        kw["turns"][1] = _turn(1, "her", "在呀", act="A1", his_batch=batch)
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "引用回复")["verdict"], "FAIL")

    def test_引用_无连发机会判N_A(self):
        sc = self._sc()
        it = self._item(sc, "引用回复")
        self.assertEqual(it["verdict"], "N/A")
        self.assertIn("上线后首周观察补验", it["not_run_reason"])

    # ---- 沉默 ----
    def test_沉默_告别场景合规判PASS(self):
        sc = self._sc()
        self.assertEqual(self._item(sc, "沉默权")["verdict"], "PASS")

    def test_沉默_实质内容后沉默判FAIL(self):
        kw = _base_kwargs()
        kw["turns"][4] = _turn(3, "user", "今天去做实验了", act="A2")
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "沉默权")["verdict"], "FAIL")

    def test_沉默_零使用判N_A(self):
        kw = _base_kwargs()
        kw["turns"] = kw["turns"][:4]
        sc = FR.build_scorecard(**kw)
        it = self._item(sc, "沉默权")
        self.assertEqual(it["verdict"], "N/A")
        self.assertIn("上线后首周观察补验", it["not_run_reason"])

    # ---- 旁白 ----
    def test_旁白_整行旁白上屏判FAIL(self):
        kw = _base_kwargs()
        kw["turns"][1] = _turn(1, "her", "这人嘴硬，我折回去看看。", act="A1",
                               bubbles=["这人嘴硬，我折回去看看。"])
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "旁白滤网")["verdict"], "FAIL")

    def test_旁白_无旁白判PASS(self):
        sc = self._sc()
        self.assertEqual(self._item(sc, "旁白滤网")["verdict"], "PASS")

    # ---- 事件消息 ----
    def test_事件_D2实发1条且无决策层判PASS(self):
        sc = self._sc()
        self.assertEqual(self._item(sc, "事件消息")["verdict"], "PASS")

    def test_事件_已resolved却未发判FAIL(self):
        kw = _base_kwargs()
        kw["proactive_log"] = [p for p in kw["proactive_log"] if not p["event_sent"]]
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "事件消息")["verdict"], "FAIL")

    def test_事件_事件周期仍调决策层判FAIL(self):
        kw = _base_kwargs()
        for p in kw["proactive_log"]:
            if p["event_sent"]:
                p["purposes"] = ["proactive_decision", "proactive_message"]
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "事件消息")["verdict"], "FAIL")

    # ---- 主动消息 ----
    def test_主动_免打扰与未回闸门全0发送判PASS(self):
        sc = self._sc()
        self.assertEqual(self._item(sc, "主动消息")["verdict"], "PASS")

    def test_主动_免打扰时段却发出判FAIL(self):
        kw = _base_kwargs()
        kw["proactive_log"][0].update({"sent": True, "outcome": "已发出"})
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "主动消息")["verdict"], "FAIL")

    def test_主动_两类周期都缺判N_A(self):
        kw = _base_kwargs()
        kw["proactive_log"] = []
        sc = FR.build_scorecard(**kw)
        it = self._item(sc, "主动消息")
        self.assertEqual(it["verdict"], "N/A")
        self.assertIn("上线后首周观察补验", it["not_run_reason"])

    # ---- 语音闸门 ----
    def test_语音_零voice段判PASS(self):
        sc = self._sc()
        self.assertEqual(self._item(sc, "语音闸门")["verdict"], "PASS")

    def test_语音_有voice段出站判FAIL(self):
        sc = self._sc(voice_chunks=[{"type": "voice", "content": "在呢"}])
        self.assertEqual(self._item(sc, "语音闸门")["verdict"], "FAIL")

    def test_语音_闸门开着判N_A(self):
        sc = self._sc(tts_enabled=True)
        it = self._item(sc, "语音闸门")
        self.assertEqual(it["verdict"], "N/A")
        self.assertIn("上线后首周观察补验", it["not_run_reason"])

    def test_语音_prompt残留voice标记判FAIL(self):
        sc = self._sc(prompt_voice_hits=1)
        self.assertEqual(self._item(sc, "语音闸门")["verdict"], "FAIL")

    # ---- 生活主线 ----
    def test_生活主线_状态变迁且区块注入判PASS(self):
        kw = _base_kwargs()
        for t in kw["turns"]:
            if t["speaker"] == "her":
                t["system_prompt_blocks"] = {"她最近的生活": 40, "角色": 100}
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "生活主线")["verdict"], "PASS")

    def test_生活主线_状态没变迁判FAIL(self):
        kw = _base_kwargs()
        kw["arcs_status_log"] = [{"arcs": [{"id": 1, "key_date": FR.DAY2, "status": "near"}]}]
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "生活主线")["verdict"], "FAIL")

    def test_生活主线_无预置判N_A(self):
        kw = _base_kwargs()
        kw["arcs_status_log"] = []
        sc = FR.build_scorecard(**kw)
        it = self._item(sc, "生活主线")
        self.assertEqual(it["verdict"], "N/A")
        self.assertIn("上线后首周观察补验", it["not_run_reason"])

    # ---- 数值引擎 ----
    def test_数值_有对话涨且冷落降判PASS(self):
        kw = _base_kwargs()
        kw["turns"][1]["state_before"] = {"composite": 23.0, "mood": {"v": 2.0, "frustration": 0.0}}
        kw["turns"][1]["state_after"] = {"composite": 23.5, "mood": {"v": 2.1, "frustration": 0.0}}
        kw["turns"][1]["day"] = FR.DAY2
        kw["turns"][1]["state_before"] = {"composite": 25.0, "mood": {"v": 2.0, "frustration": 0.3}}
        kw["turns"][1]["state_after"] = {"composite": 25.2, "mood": {"v": 1.2, "frustration": 0.7}}
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "数值引擎")["verdict"], "PASS")

    def test_数值_末分不上行判FAIL(self):
        kw = _base_kwargs(final_state={"composite": 20.0, "mood": {"v": 2.0}})
        kw["turns"][1]["state_before"] = {"composite": 23.0, "mood": {"v": 2.0, "frustration": 0.0}}
        kw["turns"][1]["state_after"] = {"composite": 23.1, "mood": {"v": 2.0, "frustration": 0.0}}
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "数值引擎")["verdict"], "FAIL")

    def test_数值_异常跳变判FAIL(self):
        kw = _base_kwargs()
        kw["turns"][1]["state_before"] = {"composite": 23.0, "mood": {"v": 2.0, "frustration": 0.0}}
        kw["turns"][1]["state_after"] = {"composite": 40.0, "mood": {"v": 2.0, "frustration": 0.0}}
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "数值引擎")["verdict"], "FAIL")

    # ---- 风格基线 ----
    def test_风格_中位在区间判PASS(self):
        kw = _base_kwargs()
        kw["turns"] = [
            _turn(1, "user", "在吗", act="A1"),
            _turn(1, "her", "在呀我在写论文呢还没写完", act="A1"),
            _turn(2, "user", "嗯", act="A1"),
            _turn(2, "her", "你今天吃饭了没有呀", act="A1"),
        ]
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "风格基线")["verdict"], "PASS")

    def test_风格_称呼越界判FAIL(self):
        kw = _base_kwargs()
        kw["turns"] = [
            _turn(1, "user", "在吗", act="A1"),
            _turn(1, "her", "阿俊你吃了吗", act="A1"),
        ]
        sc = FR.build_scorecard(**kw)
        self.assertEqual(self._item(sc, "风格基线")["verdict"], "FAIL")

    # ---- 成本 ----
    def test_成本_超上限判FAIL(self):
        sc = self._sc(cost=10.5, max_cost=10.0)
        self.assertEqual(self._item(sc, "成本")["verdict"], "FAIL")

    def test_成本_未超判PASS(self):
        sc = self._sc(cost=3.0, max_cost=10.0)
        self.assertEqual(self._item(sc, "成本")["verdict"], "PASS")


class TestScorecardShape(unittest.TestCase):
    def test_恰好11项且每项都写了expect方向(self):
        sc = FR.build_scorecard(**_base_kwargs())
        self.assertEqual(len(sc["items"]), 11)
        names = [i["item"] for i in sc["items"]]
        self.assertEqual(len(names), len(set(names)))
        for it in sc["items"]:
            self.assertTrue(it["expect"].strip(), f"{it['item']} 缺 expect 方向")
            self.assertIn(it["verdict"], ("PASS", "FAIL", "N/A"))

    def test_overall_有FAIL即FAIL_有N_A不影响(self):
        sc = FR.build_scorecard(**_base_kwargs(cost=99.0, max_cost=10.0))
        self.assertEqual(sc["overall"], "FAIL")
        self.assertGreaterEqual(sc["verdict_counts"]["N/A"], 1)
        # N/A 存在但无 FAIL 时 overall 仍是 PASS（N/A 不算通过也不算失败）
        sc2 = FR.build_scorecard(**_base_kwargs())
        self.assertTrue(sc2["verdict_counts"]["N/A"] >= 1)
        self.assertEqual(sc2["overall"], "PASS")


# ==========================================
# 4. N/A 标注纪律
# ==========================================


class TestNADiscipline(unittest.TestCase):
    def test_没有观测样本不许报PASS(self):
        """零观测≠合规：全程没有沉默/表情/引用，这三项必须 N/A 而不是 PASS。"""
        empty_turns = [_turn(1, "user", "在吗"), _turn(1, "her", "在呀"),
                       _turn(2, "user", "嗯"), _turn(2, "her", "嗯嗯")]
        sc = FR.build_scorecard(**_base_kwargs(turns=empty_turns))
        for name in ("沉默权", "引用回复", "face 双向"):
            it = next(i for i in sc["items"] if i["item"] == name)
            self.assertEqual(it["verdict"], "N/A", f"{name} 零观测却报了 {it['verdict']}")

    def test_每个N_A都写明补验方式(self):
        sc = FR.build_scorecard(**_base_kwargs())
        na = [i for i in sc["items"] if i["verdict"] == "N/A"]
        self.assertTrue(na, "本用例应至少有一项 N/A")
        for it in na:
            self.assertTrue(it["not_run_reason"], f"{it['item']} 报 N/A 没写原因")
            self.assertIn("上线后首周观察补验", it["not_run_reason"])

    def test_N_A不计入overall(self):
        sc = FR.build_scorecard(**_base_kwargs())
        self.assertEqual(sc["overall"], "PASS")
        self.assertIn("零观测≠合规", sc["not_run_note"])


# ==========================================
# 5. 熔断 + 驱动器级（多日推进/维护/周期）
# ==========================================


class _FakeGatewayBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        require_brief(self)
        self._p1 = patch.object(LLMGateway, "stream_chat", _fake_stream_chat)
        self._p2 = patch.object(LLMGateway, "chat", _fake_chat)
        self._p1.start()
        self._p2.start()
        self.run_dir = os.path.join("data", "_t24_test", self.id().rsplit(".", 1)[-1])
        os.makedirs(self.run_dir, exist_ok=True)

    async def asyncTearDown(self) -> None:
        self._p1.stop()
        self._p2.stop()

    async def _make_sim(self, timeline=None, max_cost=10.0, user_lines=None):
        config = D.Config.load("config.toml")
        sim = FR.FinalRehearsal(
            config=config, run_dir=self.run_dir, max_cost=max_cost,
            user_reply_fn=_user_script(user_lines or ["在吗", "嗯", "今天挺忙的"]),
            seed=1, timeline=timeline,
        )
        await sim.setup()
        return sim


def _short_timeline():
    return [
        FR.Beat(at=f"{FR.DAY1} 19:32", kind="chat", act="A1", label="多轮对话",
                fixed_text="今天真的累死了"),
        FR.Beat(at=f"{FR.DAY1} 19:40", kind="chat", act="A1", label="多轮对话",
                instruction="接着说"),
        FR.Beat(at=f"{FR.DAY2} 03:30", kind="proactive", act="NIGHT", label="D2凌晨免打扰"),
        FR.Beat(at=f"{FR.DAY2} 07:00", kind="idle", act="WAKE", label="D2晨起"),
        FR.Beat(at=f"{FR.DAY2} 18:05", kind="proactive", act="D2EVE", label="D2傍晚·事件消息"),
        FR.Beat(at=f"{FR.DAY3} 08:35", kind="chat", act="A5", label="日常回归",
                instruction="早，打个招呼"),
    ]


class TestTranscriptRender(unittest.TestCase):
    def test_纯表情包回合不许渲染成空白(self):
        """踩过的坑：她只发了一个表情包时，bubbles 是空的、silenced 也是 False，
        transcript 会显示成"她什么都没说"——误导所有者抽读。必须把 sticker 渲染出来。"""
        class _FakeSim:
            run_dir = "x"
            start_time = f"{FR.DAY1} 19:30"
            end_time = datetime(2026, 10, 14, 11, 30)
            her_model = "m"
            user_model = "m"
            config = None
            proactive_log = []
            maintenance_events = []
            _final_cost = 0.0
            events = [
                {"type": "day", "date": FR.DAY1, "day": "D1", "weekday": "一"},
                {"type": "act", "act": "A2", "title": "D1夜·告别"},
                {"type": "user", "time": f"{FR.DAY1} 21:44", "text": "晚安"},
                {"type": "her", "time": f"{FR.DAY1} 21:44", "bubbles": [],
                 "stickers": ["characters/qingzi/stickers/cat_diving.jpg"],
                 "silenced": False, "text": ""},
            ]

        out = FR.render_rehearsal(_FakeSim())
        self.assertIn("表情包", out)
        self.assertIn("cat_diving.jpg", out)


class TestRehearsalDriver(_FakeGatewayBase):
    async def test_跨日推进真触发维护钩子(self):
        sim = await self._make_sim(timeline=_short_timeline())
        try:
            with FR.RehearsalTimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()
        self.assertGreaterEqual(len(sim.maintenance_events), 1, "跨 04:17 没有触发维护钩子")
        ev = sim.maintenance_events[0]
        self.assertIn("0417", os.path.basename(ev["backup_path"]),
                      f"备份文件名没有反映假时钟：{ev['backup_path']}")
        self.assertTrue(os.path.exists(ev["backup_path"]), "备份文件没有真的落盘")
        self.assertFalse(sim.end_reason.startswith("异常"), sim.end_reason)
        # 三件套齐备
        for f in ("transcript.md", "scorecard.json", "raw.json"):
            self.assertTrue(os.path.exists(os.path.join(self.run_dir, f)), f"缺产物 {f}")

    async def test_免打扰周期不发送且留痕(self):
        sim = await self._make_sim(timeline=_short_timeline())
        try:
            with FR.RehearsalTimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()
        quiet = [c for c in sim.proactive_log if c.get("quiet")]
        self.assertTrue(quiet, "没有免打扰时段的主动消息周期")
        for c in quiet:
            self.assertFalse(c["sent"], f"免打扰时段发出去了：{c}")
            self.assertIn("免打扰", c["gate_reason"])

    async def test_事件通道在D2发出且未调决策层(self):
        sim = await self._make_sim(timeline=_short_timeline())
        try:
            with FR.RehearsalTimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()
        events = [c for c in sim.proactive_log if c.get("event_sent")]
        self.assertEqual(len(events), 1, f"事件消息应恰好 1 条：{sim.proactive_log}")
        self.assertNotIn("proactive_decision", events[0]["purposes"])
        self.assertEqual(events[0]["day"], FR.DAY2)

    async def test_记分卡11项且raw_json可解析(self):
        sim = await self._make_sim(timeline=_short_timeline())
        try:
            with FR.RehearsalTimePatch(sim.clock, sim.sleep_log):
                res = await sim.run()
        finally:
            await sim.close()
        self.assertEqual(len(res["scorecard"]["items"]), 11)
        with open(os.path.join(self.run_dir, "raw.json"), encoding="utf-8") as f:
            raw = json.load(f)
        self.assertEqual(len(raw["turns"]), len(sim.turns))
        self.assertTrue(raw["maintenance_events"])
        self.assertTrue(raw["proactive_cycles"])


class TestCostBreaker(_FakeGatewayBase):
    async def test_超上限立刻停并保留已完成产物(self):
        sim = await self._make_sim(timeline=_short_timeline(), max_cost=0.0)
        try:
            with FR.RehearsalTimePatch(sim.clock, sim.sleep_log):
                res = await sim.run()
        finally:
            await sim.close()
        self.assertIn("成本熔断", res["meta"]["end_reason"])
        for f in ("transcript.md", "scorecard.json", "raw.json"):
            self.assertTrue(os.path.exists(os.path.join(self.run_dir, f)), f"熔断后丢了 {f}")


class _FrozenDT(datetime):
    """把 proactive / arcs / db 三个模块的 datetime.now() 钉死（同 test_fixes16 手法）。"""

    _frozen: datetime = datetime(2026, 10, 13, 19, 0)

    @classmethod
    def now(cls, tz=None):  # noqa: ARG003 - 与 datetime.now 签名对齐
        return cls._frozen

    @classmethod
    def set(cls, value: datetime) -> None:
        cls._frozen = value


class TestEventChannelExemption(unittest.IsolatedAsyncioTestCase):
    """FIXES24：事件通道豁免"上一条主动消息未回"（闸门 #4）——真 ProactiveScheduler 驱动。

    三条判据（对应终审口径）：
      ① 上一条主动消息未回 + 有够格事件 → 事件照发（豁免 #4）；
      ② 免打扰 + 有事件 → 不发、event_announced 不置位（不豁免免打扰）；
      ③ 无事件时全路径与改前一致（#4 仍拦住常规决策层）。
    """

    async def asyncSetUp(self) -> None:
        self.sent = []

        async def _send(chunk):
            self.sent.append(chunk)

        self.db = await make_db(":memory:")
        self.gw = make_mock_gateway()
        self.gw.chat = AsyncMock(return_value="在琴房把新谱子过了一遍")
        self.stack = make_engine_stack(self.db, gateway=self.gw, include_proactive=True,
                                       send_msg_fn=_send)
        self.arcs = LifeArcManager(self.db, self.gw, self.stack.persona)
        # 把生活主线接进调度器（helpers 默认不接）
        self.stack.proactive.arcs = self.arcs
        self.stack.proactive.assembler = self.stack.assembler
        self.sched = self.stack.proactive

    async def asyncTearDown(self) -> None:
        try:
            await self.db.close()
        except Exception:
            pass

    async def _seed(self, *, resolved_arc: bool, last_assistant: bool, unanswered: int,
                    when: datetime) -> None:
        if resolved_arc:
            # 够格事件：status=resolved、event_announced=0、resolved_at 在 24h 窗口内
            await self.db.execute(
                "INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake,"
                " resolution, event_announced, created_at, resolved_at)"
                " VALUES (?, ?, ?, 'resolved', ?, ?, 0, ?, ?)",
                ("乐团节目审查", "低音部还没合齐", "2026-10-13", "紧张",
                 "审查过了，低音部合上了", "2026-10-13 12:00", "2026-10-13 18:00"),
            )
        else:
            # 无事件：一条还早的活跃主线（advance_states 只改状态，不发 API）
            await self.db.execute(
                "INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake,"
                " created_at) VALUES (?, ?, ?, 'upcoming', ?, ?)",
                ("论文提纲", "导师让这周发过去", "2026-10-17", "怕被说太浅", "2026-10-13 12:00"),
            )
        if last_assistant:
            # 距上次发言 4 小时：让 60 分钟闸门（#2）放行，单独考 #4
            await self.db.execute(
                "INSERT INTO turns (role, content, proactive, has_image, created_at)"
                " VALUES ('assistant', ?, 1, 0, ?)",
                ("你昨天是不是累得手机都没看",
                 (when - timedelta(hours=4)).strftime("%Y-%m-%d %H:%M")),
            )
        await self.db.set_state_json(
            STATE_KEY_UNANSWERED_PROACTIVE,
            {"date": when.strftime("%Y-%m-%d"), "count": unanswered},
        )

    async def _run_cycle(self, when: datetime) -> None:
        with patch("companion.proactive.datetime", _FrozenDT), \
             patch("companion.arcs.datetime", _FrozenDT), \
             patch("companion.db.datetime", _FrozenDT):
            _FrozenDT.set(when)
            await self.sched.trigger_cycle()

    async def _event_announced(self) -> int:
        row = await self.db.fetchone(
            "SELECT event_announced FROM life_arcs WHERE status = 'resolved' LIMIT 1"
        )
        return int(row["event_announced"]) if row else -1

    async def test_上一条未回但有事件_事件照发(self):
        when = datetime(2026, 10, 13, 19, 0)   # 非免打扰、距上次发言 4 小时
        await self._seed(resolved_arc=True, last_assistant=True, unanswered=1, when=when)
        await self._run_cycle(when)
        self.assertTrue(self.sent, "有够格事件 + 上一条未回，事件应发出（应豁免闸门 #4）")
        self.assertEqual(await self._event_announced(), 1, "事件发出后 event_announced 应置位")

    async def test_免打扰有事件_不发且不置位(self):
        when = datetime(2026, 10, 13, 3, 30)   # 免打扰时段（不豁免）
        await self._seed(resolved_arc=True, last_assistant=True, unanswered=1, when=when)
        await self._run_cycle(when)
        self.assertFalse(self.sent, "免打扰时段不该发事件")
        self.assertEqual(await self._event_announced(), 0,
                         "免打扰被拦时不许置 event_announced（否则事件就被吞了）")
        self.gw.chat.assert_not_awaited()

    async def test_无事件时闸门4仍拦住常规决策(self):
        when = datetime(2026, 10, 13, 19, 0)
        await self._seed(resolved_arc=False, last_assistant=True, unanswered=1, when=when)
        await self._run_cycle(when)
        self.assertFalse(self.sent, "无事件时闸门 #4 仍应拦住常规主动消息")
        self.gw.chat.assert_not_awaited()   # 决策层都没进去


if __name__ == "__main__":
    unittest.main()
