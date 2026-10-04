"""FIXES18 对聊仿真器单测 (tests/test_fixes18.py)

覆盖任务书要求六项：
1. 回合驱动器状态流转（mock 双侧 gateway）
2. 剧情卡收场条件判定
3. 已读不回（S3）的时钟推进与主动消息驱动
4. 指标计算（复读/拖尾/膨胀各一个手造 transcript 用例）
5. 成本熔断触发
6. 临时库隔离（跑完生产库零变化）

纪律：不碰 characters/、config.toml；不发起真实 API 调用（网关在类级别被替换）。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import unittest
from datetime import datetime
from typing import Any, Dict, List
from unittest.mock import patch

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

import duo_sim as D
from companion.gateway import LLMGateway

# ==========================================
# 共用：假网关（两侧都不打真实 API）
# ==========================================

OBSERVER_OK = {
    "self_disclosure": 4, "responsiveness": 5, "warmth_score": 5, "resonance": 5,
    "moments": [], "facts": [], "followups": [], "mood_impact": {"v": 0, "a": 0, "trust": 0},
}


def _fake_payload(purpose: str) -> str:
    if purpose == "observer":
        return json.dumps(OBSERVER_OK, ensure_ascii=False)
    if purpose == "proactive_decision":
        return json.dumps({"should_send": True, "reason": "想接上"}, ensure_ascii=False)
    if purpose == "proactive_message":
        return "你还在吗"
    if purpose == "diary_archive":
        return json.dumps({"content": "今天他来找我聊天了。", "strength": 5}, ensure_ascii=False)
    if purpose == "life_arc":
        return json.dumps([{
            "title": "读书报告", "detail": "导师给了新方向",
            "emotional_stake": "怕做不完", "key_date": "2026-10-12",
        }], ensure_ascii=False)
    return "嗯"


async def _fake_stream_chat(self, messages=None, model=None, temperature=0.7,
                            purpose="main_chat", max_retries=2):
    for ch in _fake_payload(purpose):
        yield ch


async def _fake_chat(self, messages=None, model=None, temperature=0.7, json_mode=False,
                     purpose="chat", max_retries=2):
    return _fake_payload(purpose)


def _user_script(lines: List[str]):
    """构造一个确定性的"他"回复序列（用尽后重复最后一条）。"""
    it = iter(lines)

    async def fn(dialogue, instruction):
        try:
            return next(it)
        except StopIteration:
            return lines[-1] if lines else "嗯"

    return fn


def _turn(idx: int, speaker: str, text: str, **kw) -> Dict[str, Any]:
    """构造一条 transcript 记录（指标函数的输入格式）。"""
    d = {"idx": idx, "speaker": speaker, "text": text, "time": f"2026-10-08 20:{idx:02d}"}
    d.update(kw)
    return d


def _pair(start: int, user_text: str, her_text: str) -> List[Dict[str, Any]]:
    return [
        _turn(start, "user", user_text),
        _turn(start, "her", her_text, bubbles=[her_text] if her_text else []),
    ]


class _FakeGatewayBase(unittest.IsolatedAsyncioTestCase):
    """把 LLMGateway 的两个出口整体换成假的，整类测试零 API 成本。"""

    async def asyncSetUp(self) -> None:
        self._p1 = patch.object(LLMGateway, "stream_chat", _fake_stream_chat)
        self._p2 = patch.object(LLMGateway, "chat", _fake_chat)
        self._p1.start()
        self._p2.start()
        self.run_dir = os.path.join("data", "_t18_test", self.id().rsplit(".", 1)[-1])
        os.makedirs(self.run_dir, exist_ok=True)

    async def asyncTearDown(self) -> None:
        self._p1.stop()
        self._p2.stop()

    async def _make_sim(self, scene_key="S1", turns=None, user_lines=None, **kw):
        config = D.Config.load("config.toml")
        sim = D.DuoSimulator(
            config=config, scene=D.SCENES[scene_key], run_dir=self.run_dir,
            start_time=D.DEFAULT_START_TIME, max_turns=turns,
            user_reply_fn=_user_script(user_lines) if user_lines else None,
            **kw,
        )
        await sim.setup()
        return sim


# ==========================================
# 2. 剧情卡收场条件判定（纯函数）
# ==========================================


class TestSceneEndCondition(unittest.TestCase):
    def test_未到最小回合数不许收场(self):
        """早期一句"算了"不能把整局按死——否则多轮病灶一条都验不到。"""
        s1 = D.SCENES["S1"]
        done, reason = D.should_end(s1, turns_done=2, end_marker_hit_at=2, silence_done=True)
        self.assertFalse(done, f"第 2 轮就收场是错的：{reason}")
        self.assertIn("最小回合数", reason)

    def test_到最小回合数且命中收场语则收场(self):
        s1 = D.SCENES["S1"]
        done, reason = D.should_end(
            s1, turns_done=s1.min_turns, end_marker_hit_at=s1.min_turns, silence_done=True
        )
        self.assertTrue(done)
        self.assertIn("收场语", reason)

    def test_命中收场语后她再回一轮才收场(self):
        """收场语当轮她还没回，要等下一轮判定。"""
        s1 = D.SCENES["S1"]
        done, _ = D.should_end(s1, turns_done=5, end_marker_hit_at=6, silence_done=True)
        self.assertFalse(done)

    def test_硬上限兜底(self):
        s1 = D.SCENES["S1"]
        done, reason = D.should_end(s1, turns_done=s1.max_turns, end_marker_hit_at=None,
                                    silence_done=True)
        self.assertTrue(done)
        self.assertIn("硬上限", reason)

    def test_回合上限可被外部覆盖(self):
        """--turns 15 的冒烟必须真的只跑 15 轮，不能照卡里的 25 轮烧一倍的钱。"""
        s1 = D.SCENES["S1"]
        done, reason = D.should_end(s1, turns_done=15, end_marker_hit_at=None,
                                    silence_done=True, max_turns=15)
        self.assertTrue(done, reason)
        self.assertIn("15", reason)
        # 不传覆盖值时回落到卡里的硬上限
        self.assertFalse(
            D.should_end(s1, 15, None, True)[0],
            "没传 max_turns 时不该提前收场",
        )

    def test_S3静默段未跑完不许收场(self):
        s3 = D.SCENES["S3"]
        done, reason = D.should_end(s3, turns_done=s3.min_turns, end_marker_hit_at=6,
                                    silence_done=False)
        self.assertFalse(done)
        self.assertIn("静默段", reason)

    def test_命中收场语识别(self):
        s1 = D.SCENES["S1"]
        self.assertTrue(D.hit_end_marker("今天太累了 算了", s1))
        self.assertFalse(D.hit_end_marker("今天做了个实验", s1))

    def test_三张卡都带齐任务书要求的字段(self):
        """背景/开场/情绪走向/收场条件/时钟推进规则，一个都不能少。"""
        self.assertEqual(sorted(D.SCENES), ["S1", "S2", "S3"])
        for k, s in D.SCENES.items():
            with self.subTest(scene=k):
                self.assertTrue(s.background.strip(), "缺背景设定")
                self.assertTrue(s.opening.strip(), "缺开场消息")
                self.assertTrue(s.mood_arc.strip(), "缺情绪走向")
                self.assertTrue(s.end_markers, "缺收场条件")
                self.assertGreater(s.clock_step_min, 0, "缺时钟推进规则")
                self.assertGreater(s.min_turns, 0)


# ==========================================
# 3. 时钟推进
# ==========================================


class TestClock(unittest.TestCase):
    def test_advance单调递增(self):
        c = D.Clock(datetime(2026, 10, 8, 19, 30))
        t0 = c.now()
        c.advance(5)
        c.advance(10)
        self.assertEqual(c.now(), datetime(2026, 10, 8, 19, 45))
        self.assertGreater(c.now(), t0)

    def test_时间伪装让全链路看到假现在(self):
        """patch 后 companion 各模块的 datetime.now() 必须返回假时钟。"""
        import companion.db as db_mod
        import companion.assembler as asm_mod
        import companion.proactive as pro_mod
        import companion.turn_handler as th_mod

        fake = datetime(2026, 10, 8, 21, 5)
        c = D.Clock(fake)
        with D._TimePatch(c, []):
            for m in (db_mod, asm_mod, pro_mod, th_mod):
                self.assertEqual(m.datetime.now(), fake, f"{m.__name__} 没被时间伪装")
            self.assertEqual(db_mod.now_str(), "2026-10-08 21:05")

    def test_退出后还原真实时间(self):
        import companion.db as db_mod
        real = db_mod.datetime
        with D._TimePatch(D.Clock(datetime(2020, 1, 1, 0, 0)), []):
            pass
        self.assertIs(db_mod.datetime, real, "时间伪装退出后没还原")


# ==========================================
# 4. 指标计算（手造 transcript）
# ==========================================


class TestMetrics(unittest.TestCase):
    def test_梗复读_同一片段连刷判FAIL(self):
        """她相邻两轮玩同一个梗（4-gram 重复）→ FAIL。"""
        turns: List[Dict[str, Any]] = []
        turns += _pair(1, "在吗", "在呀")
        turns += _pair(2, "今天好累", "我给你捶捶肩好不好呀好不好呀")
        turns += _pair(3, "不想动了", "我给你捶捶肩好不好呀好不好呀")  # 同一句复读
        m = D.metric_meme_repeat(turns)
        self.assertEqual(m["verdict"], "FAIL")
        self.assertGreaterEqual(m["repeat_count"], 1)
        self.assertTrue(any(r["span"] <= D.REPEAT_WINDOW for r in m["detail"]))

    def test_梗复读_无重复判PASS(self):
        turns: List[Dict[str, Any]] = []
        for i, h in enumerate(["在呀", "今天练琴累死了", "你吃饭了吗", "我先睡啦"], 1):
            turns += _pair(i, "嗯", h)
        self.assertEqual(D.metric_meme_repeat(turns)["verdict"], "PASS")

    def test_告别拖尾_收场后还追问判FAIL(self):
        turns: List[Dict[str, Any]] = []
        turns += _pair(1, "在吗", "在呀")
        turns += _pair(2, "困了", "那就睡吧晚安")          # 收场
        turns += _pair(3, "嗯", "你明天几点起呀")          # 收场后还在追问
        turns += _pair(4, "不知道", "那要不要我叫你起床")  # 又追一个
        m = D.metric_farewell_drag(turns, closing_turn=2)
        self.assertEqual(m["verdict"], "FAIL")
        self.assertEqual(m["closing_turn"], 2)
        self.assertGreaterEqual(m["drag_turns"], 2)

    def test_告别拖尾_干净收场判PASS(self):
        turns: List[Dict[str, Any]] = []
        turns += _pair(1, "在吗", "在呀")
        turns += _pair(2, "困了", "睡吧晚安")
        turns += _pair(3, "嗯", "嗯嗯")
        m = D.metric_farewell_drag(turns, closing_turn=2)
        self.assertEqual(m["verdict"], "PASS")
        self.assertEqual(m["drag_turns"], 0)

    def test_告别拖尾_没识别到收场点报WARN而非PASS(self):
        """没识别到收场 ≠ 0 拖尾。必须显式报出来，不能自动成立成 PASS。"""
        turns = _pair(1, "在吗", "在呀") + _pair(2, "嗯", "嗯嗯")
        m = D.metric_farewell_drag(turns, closing_turn=None)
        self.assertEqual(m["verdict"], "WARN")
        self.assertIn("不等于 0 拖尾", m["detail"][0])

    def test_话量膨胀_逐轮变长判FAIL(self):
        turns: List[Dict[str, Any]] = []
        lens = [4, 6, 20, 34, 48, 60, 70, 80]
        for i, n in enumerate(lens, 1):
            turns += _pair(i, "嗯", "啊" * n)
        m = D.metric_length_inflation(turns)
        self.assertEqual(m["verdict"], "FAIL")
        self.assertGreaterEqual(m["slope"], D.INFLATE_FAIL_SLOPE)

    def test_话量膨胀_平稳判PASS(self):
        turns: List[Dict[str, Any]] = []
        for i in range(1, 9):
            turns += _pair(i, "嗯", "还行" if i % 2 else "可以")
        m = D.metric_length_inflation(turns)
        self.assertEqual(m["verdict"], "PASS")
        self.assertLess(abs(m["slope"]), D.INFLATE_WARN_SLOPE)

    def test_话量膨胀_轮次不足报WARN(self):
        m = D.metric_length_inflation(_pair(1, "嗯", "在"))
        self.assertEqual(m["verdict"], "WARN")

    def test_称呼漂移_长称呼出现即FAIL(self):
        turns = _pair(1, "在吗", "阿俊你吃了吗") + _pair(2, "吃了", "嗯嗯")
        m = D.metric_address_drift(turns, stage=1)
        self.assertEqual(m["verdict"], "FAIL")
        self.assertEqual(m["long_name_hits"], 1)

    def test_称呼漂移_零称呼判PASS(self):
        turns = _pair(1, "在吗", "在呀") + _pair(2, "吃饭了吗", "刚吃完")
        self.assertEqual(D.metric_address_drift(turns, stage=1)["verdict"], "PASS")

    def test_沉默合规_白名单语境判PASS(self):
        turns = _pair(1, "困了", "嗯")          # 他纯语气词
        turns += _pair(2, "嗯", "[沉默]")        # 她沉默 = 允许
        m = D.metric_silence_compliance(turns)
        self.assertEqual(m["verdict"], "PASS")
        self.assertEqual(m["silence_n"], 1)

    def test_沉默合规_实质内容后沉默判FAIL(self):
        turns = _pair(1, "今天去做实验了", "[沉默]")  # 她在实质内容后沉默
        m = D.metric_silence_compliance(turns)
        self.assertEqual(m["verdict"], "FAIL")
        self.assertEqual(m["violations"], 1)
        self.assertEqual(m["detail"][0]["context"], "实质内容")

    def test_沉默合规_表情包与收场语属白名单(self):
        self.assertEqual(D._classify_preceding_context("嗯"), "纯语气词")
        self.assertEqual(D._classify_preceding_context("[表情:旺柴]"), "表情包")
        self.assertEqual(D._classify_preceding_context("先睡了"), "收场语")
        self.assertEqual(D._classify_preceding_context("我去做实验了"), "实质内容")

    def test_表情包_统计与重复率(self):
        turns = _pair(1, "在吗", "哈哈[表情:旺柴]")
        turns += _pair(2, "嗯", "好的[表情:旺柴]")
        m = D.metric_sticker_usage(turns)
        self.assertEqual(m["count"], 2)
        self.assertEqual(m["unique"], 1)
        self.assertEqual(m["repeat_rate"], 0.5)

    def test_零观测样本必须报N_A而不是PASS(self):
        """没跑到 ≠ 验过了。

        S1 这类不制造沉默/表情包的卡，一次都不命中是**正常**的。要是按
        "0 违规 == 合规" 报 PASS，报告读起来就像"沉默权验过了"，那是假的。
        """
        turns: List[Dict[str, Any]] = []
        for i, h in enumerate(["在呀", "练琴去了", "睡吧晚安", "嗯嗯"], 1):
            turns += _pair(i, "嗯", h)
        sil = D.metric_silence_compliance(turns)
        stk = D.metric_sticker_usage(turns)
        self.assertEqual(sil["verdict"], "N/A", "全程没用 [沉默] 却报了 PASS")
        self.assertEqual(stk["verdict"], "N/A", "全程没用表情包却报了 PASS")
        self.assertTrue(sil["not_run_reason"] and stk["not_run_reason"])

    def test_汇总里N_A项被单独列出且不污染overall(self):
        turns: List[Dict[str, Any]] = []
        for i, h in enumerate(["在呀", "练琴去了", "睡吧晚安", "嗯嗯"], 1):
            turns += _pair(i, "嗯", h)
        m = D.compute_metrics(turns, stage=1, cost=0.01)
        self.assertEqual(set(m["not_run"]), {"[沉默]合规", "表情包"})
        self.assertEqual(m["overall"], "PASS")
        self.assertEqual(m["judged_metrics"], 4, "N/A 的两项不该计入已判定项数")
        self.assertIn("没跑到", m["not_run_note"])

    def test_单发基线_复用benchmark口径(self):
        turns = _pair(1, "在吗", "小W同学你吃饭了吗")
        turns += _pair(2, "吃了", "你今天上课吗？")
        s = D.metric_bubble_stats(turns)
        self.assertEqual(s["bubble_n"], 2)
        self.assertEqual(len(s["address_hits"]), 1)
        self.assertEqual(s["address_hits"][0]["term"], ["小W同学"])
        self.assertEqual(s["address_hits"][0]["turn"], 1)
        self.assertEqual(s["tail_question_rate"], 0.5)

    def test_汇报腔黑名单命中(self):
        turns = _pair(1, "在吗", "综上，我总结一下今天的安排")
        s = D.metric_bubble_stats(turns)
        self.assertTrue(s["report_tone_hits"], "汇报腔黑名单没命中")
        self.assertIn("综上", s["report_tone_hits"][0][1])

    def test_compute_metrics_汇总与阈值标注(self):
        # 4 轮才够算话量膨胀斜率（<3 轮会报 WARN，那是正确行为不是缺陷）
        turns: List[Dict[str, Any]] = []
        for i, h in enumerate(["在呀", "练琴去了", "睡吧晚安", "嗯嗯"], 1):
            turns += _pair(i, "嗯", h)
        m = D.compute_metrics(turns, stage=1, cost=0.12)
        self.assertEqual(m["overall"], "PASS", m["verdicts"])
        self.assertEqual(m["cost_cny"], 0.12)
        self.assertEqual(m["threshold_note"], D.THRESHOLD_NOTE)
        for k in ("梗复读", "告别拖尾", "话量膨胀", "称呼漂移", "[沉默]合规", "表情包"):
            self.assertIn(k, m["verdicts"])

# ==========================================
# 1. 回合驱动器状态流转
# ==========================================


class TestTurnDriver(_FakeGatewayBase):
    async def test_完整一局_交替发言且产物齐备(self):
        sim = await self._make_sim(
            "S1", turns=6,
            user_lines=["今天做了个实验", "累死了", "还行吧"] * 10,
        )
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                res = await sim.run()
        finally:
            await sim.close()

        turns = [D._rec_to_dict(r) for r in sim.turns]
        self.assertGreaterEqual(len(turns), 4)
        # 交替：他一条、她一条
        for i in range(1, len(turns)):
            if turns[i]["speaker"] == turns[i - 1]["speaker"] and turns[i]["speaker"] == "user":
                self.fail(f"第 {i} 条连续两条都是他的发言，交替被破坏")
        # 首轮必须是卡里的开场白，且不烧 API
        self.assertEqual(turns[0]["text"], D.SCENES["S1"].opening)
        self.assertEqual(sim.raw_turns[0].get("opening"), True)
        # 产物齐备
        for f in ("transcript.md", "raw.json", "metrics.json"):
            self.assertTrue(os.path.exists(os.path.join(self.run_dir, f)), f"缺产物 {f}")
        self.assertGreaterEqual(res["metrics"]["her_turns"], 2)

    async def test_时钟随回合单调前进(self):
        sim = await self._make_sim("S1", turns=5, user_lines=["嗯"] * 30)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()
        times = [t.time for t in sim.turns]
        self.assertEqual(times, sorted(times), "回合时间没有单调递增")
        self.assertGreater(sim.clock.now(), datetime.strptime(D.DEFAULT_START_TIME,
                                                              "%Y-%m-%d %H:%M"))

    async def test_每轮都记录观察者结算(self):
        """observer 评分必须真的落地，不能因为是异步结算就悄悄没有。"""
        sim = await self._make_sim("S1", turns=4, user_lines=["在吗", "好累"] * 10)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()
        her_raw = [r for r in sim.raw_turns if r.get("speaker") == "her"]
        self.assertTrue(her_raw)
        scored = [r for r in her_raw if r.get("observer")]
        self.assertEqual(
            len(scored), len(her_raw),
            f"有 {len(her_raw) - len(scored)} 轮没拿到观察者评分（超时或未落地）",
        )

    async def test_用户侧模型用途单独归集(self):
        """两侧成本要能分开记账，否则成本实报没法对账。"""
        sim = await self._make_sim("S1", turns=3, user_lines=["嗯"] * 30)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()
        self.assertIn("duo_user_sim", D.USER_PURPOSE)

    async def test_模拟器空回复被兜住且留痕(self):
        """空消息会污染下游所有指标（她对空消息沉默 → [沉默]合规 假 FAIL）。

        踩过的坑：S3 冒烟第 14 轮模拟器返回空串，报告读起来像"她违规了"。
        兜底必须发生，而且必须在 raw.json 里留痕，让人知道那不是她的问题。
        """
        sim = await self._make_sim("S1", turns=3, user_lines=[""] * 30)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
            for r in sim.turns:
                if r.speaker == "user":
                    self.assertTrue(r.text.strip(), "空回复没有被兜住")
            notes = [r.get("note", "") for r in sim.raw_turns if r.get("speaker") == "system"]
            self.assertTrue(
                any("模拟器" in n for n in notes),
                f"空回复兜底没有留痕，报告会被误读成青梓的病灶：{notes}",
            )
        finally:
            await sim.close()


# ==========================================
# 3b. S3 已读不回：时钟推进 + 主动消息驱动
# ==========================================


class TestSilencePhase(_FakeGatewayBase):
    async def test_静默段推进时钟且驱动主动消息(self):
        """S3 的已读不回靠推进时钟 + trigger_cycle 验 FIXES15/16，不许伪造用户消息。"""
        sim = await self._make_sim("S3", turns=12, user_lines=["嗯", "还行", "嗯嗯"] * 10)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()

        self.assertTrue(sim.silence_done, "静默段没跑完就结束了，等于没验主动消息")
        # 断言"周期被驱动过"，不是"消息发出来了"——闸门拦截也是有效结论，
        # 但必须和"决策层选择不发"分开记（proactive_log 里区分了 outcome）
        self.assertEqual(
            len(sim.proactive_log), D.SCENES["S3"].silence_turns,
            f"静默段应驱动 {D.SCENES['S3'].silence_turns} 次主动消息周期，"
            f"实际 {len(sim.proactive_log)} 次",
        )
        for entry in sim.proactive_log:
            self.assertIn(
                entry["outcome"], ("已发出", "决策层选择不发", "规则闸门拦截"),
                f"周期留痕没有区分发不出去的原因：{entry}",
            )

        # 静默段内不得有任何伪造的用户发言
        for r in sim.raw_turns:
            if r.get("proactive"):
                self.assertNotEqual(r.get("speaker"), "user")

    async def test_静默周期必须超过主动消息的60分钟闸门(self):
        """闸门是"距上次发言 <60 分钟就拦"，周期太短则三个周期全被拦掉。"""
        for key in ("S1", "S2", "S3"):
            s = D.SCENES[key]
            if s.silence_after_turn is not None:
                self.assertGreater(
                    s.silence_step_min, 60.0,
                    f"{key} 的静默周期 {s.silence_step_min} 分钟过不了 60 分钟闸门",
                )

    async def test_静默段时钟确实前进了(self):
        sim = await self._make_sim("S3", turns=12, user_lines=["嗯"] * 30)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
                end_clock = sim.clock.now()
        finally:
            await sim.close()
        s3 = D.SCENES["S3"]
        lower = datetime.strptime(D.DEFAULT_START_TIME, "%Y-%m-%d %H:%M")
        self.assertGreater(end_clock, lower)
        self.assertGreaterEqual(
            (end_clock - lower).total_seconds() / 60.0,
            s3.silence_step_min,
            "静默段没把时钟往前推",
        )


# ==========================================
# 5. 成本熔断
# ==========================================


class TestCostBreaker(_FakeGatewayBase):
    async def test_超上限立刻停并保留已完成transcript(self):
        sim = await self._make_sim("S1", turns=10, user_lines=["嗯"] * 30, max_cost=0.0)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                res = await sim.run()
        finally:
            await sim.close()
        self.assertIn("成本熔断", res["meta"]["end_reason"])
        # 熔断后仍要落盘，不能因为熔断把已完成的部分丢了
        for f in ("transcript.md", "raw.json", "metrics.json"):
            self.assertTrue(os.path.exists(os.path.join(self.run_dir, f)),
                            f"熔断后丢了产物 {f}")

    async def test_熔断异常类型可被驱动器捕获(self):
        self.assertTrue(issubclass(D.CostBreakerTripped, RuntimeError))
        sim = await self._make_sim("S1", user_lines=["嗯"] * 5, max_cost=0.0)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                with self.assertRaises(D.CostBreakerTripped):
                    await sim._check_budget()
        finally:
            await sim.close()

    async def test_上限未到时不熔断(self):
        sim = await self._make_sim("S1", user_lines=["嗯"] * 5, max_cost=999.0)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim._check_budget()   # 不该抛
        finally:
            await sim.close()


# ==========================================
# 6. 临时库隔离
# ==========================================


class TestSandboxIsolation(_FakeGatewayBase):
    async def test_生产库跑完零变化(self):
        prod = "data/companion.db"
        if not os.path.exists(prod):
            self.skipTest("本地无生产库快照，跳过")
        before = os.stat(prod)
        with open(prod, "rb") as f:
            before_bytes = f.read()

        sim = await self._make_sim("S1", turns=5, user_lines=["嗯"] * 30)
        try:
            self.assertNotEqual(os.path.abspath(sim.db_path), os.path.abspath(prod),
                                "仿真竟然直接用了生产库")
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
        finally:
            await sim.close()

        after = os.stat(prod)
        with open(prod, "rb") as f:
            after_bytes = f.read()
        self.assertEqual(before_bytes, after_bytes, "生产库内容被改动了")
        self.assertEqual(before.st_size, after.st_size)

    async def test_每局临时库从零重建不继承上一局(self):
        """重复用同一个 run 目录时，新一局不能继承上一局的 turns/好感度。"""
        sim1 = await self._make_sim("S1", turns=4, user_lines=["嗯"] * 30)
        try:
            with D._TimePatch(sim1.clock, sim1.sleep_log):
                await sim1.run()
            n1 = await sim1.db.fetchone("SELECT COUNT(*) AS c FROM turns")
        finally:
            await sim1.close()
        first = int(n1["c"])
        self.assertGreater(first, 0)

        sim2 = await self._make_sim("S1", turns=4, user_lines=["嗯"] * 30)
        try:
            self.assertEqual(
                int((await sim2.db.fetchone("SELECT COUNT(*) AS c FROM turns"))["c"]), 0,
                "新一局开局就带着上一局的 turns",
            )
        finally:
            await sim2.close()

    async def test_产物只落在run目录内(self):
        sim = await self._make_sim("S1", turns=4, user_lines=["嗯"] * 30)
        try:
            with D._TimePatch(sim.clock, sim.sleep_log):
                await sim.run()
            self.assertTrue(os.path.abspath(sim.db_path).startswith(
                os.path.abspath(self.run_dir)))
        finally:
            await sim.close()


# ==========================================
# 画像简报存在性（仿真器的硬依赖）
# ==========================================


class TestPersonaBrief(unittest.TestCase):
    def test_画像简报已生成(self):
        self.assertTrue(
            os.path.exists(D.PERSONA_BRIEF),
            f"缺画像简报 {D.PERSONA_BRIEF}，请先跑 scripts/duo_sim_persona.py",
        )

    def test_system_prompt含画像与纪律(self):
        with open(D.PERSONA_BRIEF, "r", encoding="utf-8") as f:
            brief = f.read()
        p = D.build_user_system_prompt(brief, D.SCENES["S1"])
        self.assertIn(brief.strip()[:60], p)
        self.assertIn(D.SCENES["S1"].background[:30], p)
        self.assertIn("硬纪律", p)
        self.assertIn("不许替她接话", p)


if __name__ == "__main__":
    unittest.main()
