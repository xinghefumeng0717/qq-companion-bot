"""persona_bench 指标口径单测 (tests/test_persona_bench.py)

跑分工具（scripts/sim/persona_bench.py）的所有指标都是纯正则/词表/计数函数，
不调 API、不读 config.toml。这里把它们钉死：口径写错会**静默地**把 FAIL 变成
PASS（或反过来），所以必须逐项断言。

覆盖：
  1. 词表命中/不命中的边界（直球关心、索要关心、直球承认、傲娇动作、包装词、
     吃醋、找台阶、追问、受伤、乞讨、叮嘱小句、反问链）；
  2. 沉默/气泡/长度/语气词的统计口径（沉默不进长度分母、表情包标记不算字）；
  3. 出戏检测复用 replier 判据（括号旁白、【起】标签、内心旁白、AI 自曝）；
  4. 场景机械判定：喂"好回复"必须全过，喂"漏气回复"必须点名失败；
  5. 综合分权重、成本熔断语义、角色卡 features 重放、--from-raw 复算幂等。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from companion.config import CharacterConfig, ReplyConfig  # noqa: E402
from companion.replier import SILENCE_TOKEN  # noqa: E402
from scripts.sim.persona_bench import (  # noqa: E402
    PROBE_BY_KEY,
    PROBES,
    Budget,
    Probe,
    _cell_seed,
    _sample_agg,
    aggregate_replies,
    apply_card_switches,
    bare_len,
    build_report,
    bubbles_from_chunks,
    caution_clauses,
    chars,
    evaluate_reply,
    load_prev_manual,
    prepare_card_dir,
    probe_from_dict,
    probe_to_dict,
    resolve_card_spec,
    scene_checks,
    score_run,
    score_scene,
    select_probes,
    summarize,
)


# ==========================================
# 构造辅助
# ==========================================
def make_chunks(text: str):
    """把一段实发文本按行拆成 text 段（对齐 replier 的"换行=新气泡"）。"""
    return [
        {"type": "text", "content": line}
        for line in text.split("\n")
        if line.strip()
    ]


def metrics_of(text: str, raw: str = ""):
    chunks = [] if text.strip() == SILENCE_TOKEN else make_chunks(text)
    return evaluate_reply(raw if raw else text, chunks, text)


def agg_of(texts, raws=None):
    items = []
    for index, text in enumerate(texts):
        raw = raws[index] if raws else ""
        items.append(metrics_of(text, raw))
    return aggregate_replies(items)


def checks_by_name(probe: Probe, agg):
    return {check.name: check for check in scene_checks(probe, agg)}


def cell(card_index: int, probe_key: str, sample: int, texts, seed_turns=()):
    """构造一格 raw.json 数据（与真实产物同构，够 summarize/report 用）。"""
    turns = []
    for index, text in enumerate(texts):
        turns.append({
            "user": f"第{index + 1}句",
            "raw": text,
            "reply": text,
            "chunks": make_chunks(text),
            "metrics": metrics_of(text),
        })
    return {
        "card": f"card{card_index}",
        "card_index": card_index,
        "card_dir": "characters/si",
        "probe": probe_key,
        "sample": sample,
        "seed_turns": [list(pair) for pair in seed_turns],
        "turns": turns,
        "initial_state": {"stage": 5, "expected_stage": 5, "composite": 82.6, "turns": 0, "clean": True},
        "cost": {"calls": 3, "cost_cny": 0.01, "by_purpose": {}},
        "error": None,
        "skipped": False,
        "attempt_no": 1,
    }


def synthetic_raw(card_labels=("characters/si",), probe_keys=("care_exposed", "baseline")):
    cells = {}
    texts = {
        "care_exposed": ["咳\n谁担心你了"],
        "baseline": ["在翻小玩意呢", "伙计又偷懒了", "试锅底呢"],
    }
    for card_index, _label in enumerate(card_labels):
        for probe_key in probe_keys:
            for sample in (1, 2):
                cells[f"{card_index}|{probe_key}|{sample}"] = cell(
                    card_index, probe_key, sample, texts[probe_key]
                )
    return {
        "meta": {
            "generated_at": "2026-10-22 21:30:00",
            "fake_time": "2026-10-22 21:30",
            "seed": 20261022,
            "samples": 2,
            "concurrency": 2,
            "fuse": 2.0,
            "stage_override": None,
            "model": "deepseek-v4-pro",
            "total_llm_calls": 30,
            "total_cost_cny": 0.5,
            "failed_cells": 0,
            "skipped_cells": 0,
            "fuse_tripped": False,
            "elapsed_sec": 10,
            "python": "3.13",
            "discipline": ["空库"],
        },
        "cards": [
            {
                "label": label,
                "dir": "characters/si",
                "json_path": "characters/si/character.json",
                "observed_stage": 5,
            }
            for label in card_labels
        ],
        "probes": [probe_to_dict(PROBE_BY_KEY[key]) for key in probe_keys],
        "cells": cells,
    }


# ==========================================
# 1. 基础统计
# ==========================================
class TestTextHelpers(unittest.TestCase):
    def test_chars_ignores_whitespace(self):
        self.assertEqual(chars(" 好 呀\n我 "), 3)
        self.assertEqual(chars(""), 0)

    def test_bare_len_strips_markers(self):
        self.assertEqual(bare_len("[表情:偷笑]嗯"), 1)
        self.assertEqual(bare_len("[face:旺柴]"), 0)
        self.assertEqual(bare_len("好呀"), 2)

    def test_bubbles_from_chunks_text_and_sticker(self):
        chunks = [
            {"type": "text", "content": "好呀"},
            {"type": "sticker", "desc": "偷笑"},
            {"type": "text", "content": "  "},
        ]
        self.assertEqual(bubbles_from_chunks(chunks), ["好呀", "[表情:偷笑]"])

    def test_caution_clauses_counts_non_question_only(self):
        self.assertEqual(caution_clauses("记得吃药\n到了跟我说一声"), ["记得吃药", "到了跟我说一声"])
        self.assertEqual(caution_clauses("药吃了没"), [])

    def test_question_chain_needs_two_marks(self):
        self.assertFalse(metrics_of("今天忙吗")["chain"])
        self.assertTrue(metrics_of("忙吗？累吗？")["chain"])


# ==========================================
# 2. 词表口径
# ==========================================
class TestWordLists(unittest.TestCase):
    def test_direct_care_hits_task_list(self):
        for text in ("给我报个信", "你忙完没", "记得回来", "告诉我一声"):
            self.assertTrue(metrics_of(text)["direct_care"], text)
        self.assertTrue(metrics_of("弄完了跟我说一声")["direct_care"])

    def test_direct_care_ignores_wrapped_care(self):
        # 卡的合法出口：记账/顺手，不是直球要回报
        self.assertEqual(metrics_of("这话我记账上了\n顺手问一句，别想多")["direct_care"], [])
        self.assertEqual(metrics_of("药吃了没")["direct_care"], [])

    def test_care_ask_detection(self):
        for text in ("你是不是不在乎我", "真要关心我", "谁关心我"):
            self.assertTrue(metrics_of(text)["care_ask"], text)
        # 傲娇否认不是索要关心
        self.assertEqual(metrics_of("谁担心你了")["care_ask"], [])

    def test_admit_care_is_failure_line(self):
        self.assertTrue(metrics_of("我担心你")["admit_care"])
        self.assertTrue(metrics_of("好吧，是在担心你")["admit_care"])
        self.assertEqual(metrics_of("谁担心你")["admit_care"], [])

    def test_tsundere_bundle(self):
        result = metrics_of("咳\n我、我才没有\n谁稀罕")["tsundere"]
        self.assertTrue(result["cough"])
        self.assertTrue(result["stutter"])
        self.assertTrue(result["denial"])
        self.assertTrue(metrics_of("哼，随你")["tsundere"]["hum"])
        plain = metrics_of("知道了，你早点睡")["tsundere"]
        self.assertFalse(any(plain.values()))

    def test_wrap_diversity_and_repeats(self):
        wrap = metrics_of("记账\n这笔也记账\n顺手")["wrap"]
        self.assertEqual(wrap["hits"], 3)
        self.assertEqual(wrap["distinct"], 2)
        self.assertEqual(wrap["repeats"], 1)
        self.assertEqual(wrap["labels"], ["记账", "顺手"])

    def test_sour_and_jealous(self):
        self.assertTrue(metrics_of("站着都能睡着那位？")["sour"])
        self.assertTrue(metrics_of("难怪你回消息这么慢")["sour"])
        self.assertEqual(metrics_of("佩丽卡是谁，不认识")["sour"], [])
        self.assertTrue(metrics_of("我吃醋了")["admit_jealous"])
        self.assertEqual(metrics_of("今天被佩丽卡抓去开了一天会")["admit_jealous"], [])

    def test_dodge_pursuit_hurt_beg(self):
        dodge = metrics_of("谁稀罕\n正好我也有事")["dodge"]
        self.assertIn("谁稀罕", dodge)
        self.assertIn("正好", dodge)
        self.assertTrue(metrics_of("那什么时候有空")["pursuit"])
        self.assertTrue(metrics_of("我好失望")["hurt"])
        self.assertTrue(metrics_of("求你陪陪我")["beg"])

    def test_account_and_new_topic(self):
        self.assertTrue(metrics_of("十年前那笔账利滚利一起算")["account"])
        self.assertEqual(metrics_of("知道了")["account"], [])
        self.assertTrue(metrics_of("明天醒了先跟我说一声")["new_topic"])
        self.assertEqual(metrics_of("晚安")["new_topic"], [])

    def test_accept_and_flat_accept(self):
        self.assertTrue(metrics_of("勉强算你会挑")["accept"])
        self.assertTrue(metrics_of("谢谢，我很喜欢")["flat_accept"])
        self.assertEqual(metrics_of("勉强收下")["flat_accept"], [])


# ==========================================
# 3. 出戏检测（复用 replier 判据）
# ==========================================
class TestLeakDetection(unittest.TestCase):
    def test_narration_parentheses(self):
        result = metrics_of("嗯", raw="（别过头）嗯")
        self.assertTrue(result["leaks"]["narration"])
        self.assertEqual(result["leak_total"], 1)

    def test_direction_tag(self):
        result = metrics_of("我去城里", raw="【起】我去城里")
        self.assertTrue(result["leaks"]["direction"])

    def test_inner_narration_line(self):
        # replier 的三条件判据：无"你" + 第三人称"他" + 我+动作
        result = metrics_of("嗯", raw="这人嘴硬，我折回去看看。")
        self.assertTrue(result["leaks"]["inner"])

    def test_inner_narration_released_when_speaking_to_him(self):
        result = metrics_of("嗯", raw="我看他那样，你也不管管")
        self.assertEqual(result["leaks"]["inner"], [])

    def test_self_ai_and_vision_leak(self):
        self.assertTrue(metrics_of("嗯", raw="作为AI我不能这样")["leaks"]["self_ai"])
        self.assertTrue(metrics_of("嗯", raw="【看到：一锅红油】")["leaks"]["vision"])

    def test_clean_reply_has_no_leak(self):
        result = metrics_of("咳\n谁稀罕")
        self.assertEqual(result["leak_total"], 0)
        self.assertEqual(result["leaks"]["narration"], [])


# ==========================================
# 4. 单条回复口径
# ==========================================
class TestEvaluateReply(unittest.TestCase):
    def test_silence(self):
        result = evaluate_reply(SILENCE_TOKEN, [], SILENCE_TOKEN)
        self.assertTrue(result["silent"])
        self.assertEqual(result["chars"], 0)
        self.assertEqual(result["bubbles"], 0)
        self.assertEqual(result["leak_total"], 0)

    def test_bubbles_and_text_length(self):
        chunks = make_chunks("好呀\n我在试锅底")
        result = evaluate_reply("好呀\n我在试锅底", chunks, "好呀\n我在试锅底")
        self.assertEqual(result["bubbles"], 2)
        self.assertEqual(result["text_lens"], [2, 5])

    def test_sticker_bubble_has_no_text_length(self):
        chunks = [{"type": "sticker", "desc": "偷笑"}]
        result = evaluate_reply("[表情:偷笑]", chunks, "[表情:偷笑]")
        self.assertEqual(result["bubbles"], 1)
        self.assertEqual(result["text_lens"], [])

    def test_short_boundary(self):
        self.assertTrue(metrics_of("一二三四五六七八九十")["short"])          # 10 字
        self.assertTrue(metrics_of("一二三四五六七八九十一二")["short"])       # 12 字
        self.assertFalse(metrics_of("一二三四五六七八九十一二三")["short"])     # 13 字

    def test_tone_detection(self):
        self.assertTrue(metrics_of("在翻小玩意呢")["tone"])
        self.assertFalse(metrics_of("知道了")["tone"])

    def test_behavior_uses_sent_text_not_raw(self):
        # 行为词表走实发；出戏走原文
        result = metrics_of("嗯", raw="（我担心你）嗯")
        self.assertEqual(result["admit_care"], [])
        self.assertTrue(result["leaks"]["narration"])


# ==========================================
# 5. 汇总口径
# ==========================================
class TestAggregate(unittest.TestCase):
    def test_silence_stays_in_denominator_but_not_lengths(self):
        agg = agg_of(["好呀", SILENCE_TOKEN, "我在试锅底"])
        self.assertEqual(agg["replies"], 3)
        self.assertEqual(agg["active"], 2)
        self.assertEqual(agg["silent"], 1)
        self.assertAlmostEqual(agg["silent_rate"], 1 / 3, places=6)
        # 沉默不进长度分母：字数为 [2, 5] -> median 3.5
        self.assertEqual(agg["chars_median"], 3.5)

    def test_wrap_union_and_repeats(self):
        agg = agg_of(["记账\n记账", "顺手"])
        self.assertEqual(agg["wrap_types"], 2)
        self.assertEqual(agg["wrap_hits"], 3)
        self.assertEqual(agg["wrap_repeats"], 1)

    def test_leak_replies_count(self):
        agg = agg_of(["嗯", "好呀"], raws=["（别过头）嗯", "好呀"])
        self.assertEqual(agg["leak_replies"], 1)
        self.assertEqual(agg["leak_total"], 1)
        self.assertEqual(agg["leak_kinds"], ["narration"])

    def test_defaults_for_errors_and_skipped(self):
        agg = agg_of(["好呀"])
        self.assertEqual(agg["errors"], 0)
        self.assertEqual(agg["skipped"], 0)


# ==========================================
# 6. 场景机械判定
# ==========================================
GOOD = {
    "care_exposed": ["咳\n谁担心你了"],
    "sick": ["呆货\n药吃了吗", "躺着\n让伙计把药送过去"],
    "flaked": ["记账上了\n十年前那笔利滚利一起算"],
    "other_girl": ["站着都能睡着那位？\n呆货，难怪你回消息这么慢"],
    "invite_dodged": ["谁稀罕\n正好我也有事"],
    "goodnight": ["晚安"],
    "praised": ["咳\n我、我才没有\n勉强算你会挑"],
    "baseline": ["在翻小玩意呢", "伙计又偷懒了", "试锅底呢"],
}

BAD = {
    "care_exposed": ["我担心你\n记得早点睡，忙完跟我说一声"],
    "sick": ["你怎么了？吃了没？还烧吗？"],
    "flaked": ["对不起，我好失望"],
    "other_girl": ["我吃醋了"],
    "invite_dodged": ["你要不要来嘛\n什么时候有空"],
    "goodnight": ["晚安\n明天醒了先跟我说一声"],
    "praised": ["谢谢，我很喜欢"],
    "baseline": [SILENCE_TOKEN, SILENCE_TOKEN, SILENCE_TOKEN],
}

EXPECTED_FAIL = {
    "care_exposed": "不直球承认担心",
    "sick": "先骂/数落",
    "flaked": "不直说失望",
    "other_girl": "不承认吃醋",
    "invite_dodged": "不追问",
    "goodnight": "不拖尾（无问句、不开新话题）",
    "praised": "结巴或否认",
    "baseline": "有回复（沉默 ≤1/3）",
}


class TestSceneChecks(unittest.TestCase):
    def test_all_probes_have_rules(self):
        for probe in PROBES:
            agg = agg_of(GOOD[probe.key])
            checks = scene_checks(probe, agg)
            self.assertTrue(checks, probe.key)
            self.assertNotIn("已知探针", [c.name for c in checks])

    def test_good_replies_pass_every_probe(self):
        for probe in PROBES:
            agg = agg_of(GOOD[probe.key])
            result = score_scene(probe, agg)
            failed = [c["name"] for c in result["checks"] if not c["ok"]]
            self.assertTrue(result["passed"], f"{probe.key} 未通过：{failed}")
            self.assertEqual(result["score"], 100, probe.key)

    def test_bad_replies_fail_named_check(self):
        for probe in PROBES:
            agg = agg_of(BAD[probe.key])
            checks = checks_by_name(probe, agg)
            name = EXPECTED_FAIL[probe.key]
            self.assertIn(name, checks, probe.key)
            self.assertFalse(checks[name].ok, f"{probe.key} 应命中失败项 {name}")
            self.assertFalse(score_scene(probe, agg)["passed"], probe.key)

    def test_error_cell_blocks_pass(self):
        probe = PROBE_BY_KEY["care_exposed"]
        agg = agg_of(GOOD["care_exposed"])
        agg["errors"] = 1
        checks = checks_by_name(probe, agg)
        self.assertFalse(checks["无失败采样"].ok)
        self.assertFalse(score_scene(probe, agg)["passed"])

    def test_fuse_skipped_cell_blocks_pass(self):
        probe = PROBE_BY_KEY["baseline"]
        agg = agg_of(GOOD["baseline"])
        agg["skipped"] = 1
        checks = checks_by_name(probe, agg)
        self.assertFalse(checks["无熔断跳过"].ok)
        self.assertFalse(score_scene(probe, agg)["passed"])

    def test_no_replies_never_passes(self):
        probe = PROBE_BY_KEY["goodnight"]
        result = score_scene(probe, aggregate_replies([]))
        self.assertFalse(result["passed"])
        self.assertEqual(result["score"], 0)

    def test_unknown_probe_fails_loudly(self):
        probe = Probe(key="new_probe", title="新探针", kind="傲娇", brief="", expect="", turns=("hi",))
        checks = scene_checks(probe, agg_of(["好呀"]))
        unknown = [c for c in checks if c.name == "已知探针"]
        self.assertEqual(len(unknown), 1)
        self.assertFalse(unknown[0].ok)
        self.assertFalse(score_scene(probe, agg_of(["好呀"]))["passed"])

    def test_baseline_chain_question_guard(self):
        probe = PROBE_BY_KEY["baseline"]
        agg = agg_of(["你想我了吗？想不想到我了？", "那你呢？说了又怎样？", "好的呀"])
        checks = checks_by_name(probe, agg)
        self.assertFalse(checks["反问链 ≤1 条"].ok)


# ==========================================
# 7. 评分与综合分
# ==========================================
class TestScoring(unittest.TestCase):
    def test_scene_score_is_check_ratio(self):
        probe = PROBE_BY_KEY["care_exposed"]
        agg = agg_of(BAD["care_exposed"])
        result = score_scene(probe, agg)
        total = len(result["checks"])
        ok = sum(1 for c in result["checks"] if c["ok"])
        self.assertEqual(result["score"], round(100.0 * ok / total))

    def test_run_score_weights(self):
        scenes = {
            "a": {"kind": "傲娇", "score": 100, "passed": True},
            "b": {"kind": "傲娇", "score": 80, "passed": False},
            "c": {"kind": "人味", "score": 50, "passed": False},
        }
        overall = score_run(scenes)
        self.assertEqual(overall["tsundere_score"], 90.0)
        self.assertEqual(overall["human_score"], 50.0)
        self.assertEqual(overall["composite_score"], 82.0)
        self.assertEqual(overall["scenes_passed"], 1)
        self.assertEqual(overall["scenes_total"], 3)

    def test_run_score_without_baseline(self):
        overall = score_run({"a": {"kind": "傲娇", "score": 70, "passed": True}})
        self.assertIsNone(overall["human_score"])
        self.assertEqual(overall["composite_score"], 70.0)


# ==========================================
# 8. 成本熔断
# ==========================================
class TestBudget(unittest.IsolatedAsyncioTestCase):
    async def test_charge_trips_at_limit(self):
        budget = Budget(0.5)
        self.assertTrue(await budget.may_start())
        await budget.charge(0.3)
        self.assertTrue(await budget.may_start())
        self.assertFalse(budget.tripped)
        await budget.charge(0.2)
        self.assertTrue(budget.tripped)
        self.assertFalse(await budget.may_start())

    async def test_zero_limit_disables_fuse(self):
        budget = Budget(0)
        await budget.charge(100.0)
        self.assertFalse(budget.tripped)
        self.assertTrue(await budget.may_start())

    async def test_negative_charge_is_ignored(self):
        budget = Budget(1.0)
        await budget.charge(-5.0)
        self.assertEqual(budget.spent, 0.0)


# ==========================================
# 9. 角色卡与开关
# ==========================================
class TestCardSpec(unittest.TestCase):
    def test_resolve_dir_and_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "character.json"), "w", encoding="utf-8") as f:
                f.write('{"name": "测试"}')
            by_dir = resolve_card_spec(tmp)
            by_json = resolve_card_spec(os.path.join(tmp, "character.json"))
            self.assertEqual(by_dir.dir, os.path.abspath(tmp))
            self.assertEqual(by_json.dir, os.path.abspath(tmp))
            self.assertEqual(by_dir.json_path, by_json.json_path)

    def test_missing_card_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                resolve_card_spec(tmp)

    def test_bare_named_json_is_accepted(self):
        # --card 允许直接指向任意命名的角色卡 JSON（如 data/cards/si-old.json）
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "si-old.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"name": "旧卡"}')
            card = resolve_card_spec(path)
            self.assertEqual(card.json_path, os.path.abspath(path))
            self.assertEqual(card.dir, os.path.abspath(tmp))

    def test_prepare_named_json_becomes_standard_card(self):
        with tempfile.TemporaryDirectory() as src, tempfile.TemporaryDirectory() as prep:
            body = '{"name": "旧卡", "features": {"trim_echo": true}, "stickers_dir": "stickers"}'
            json_path = os.path.join(src, "si-old.json")
            with open(json_path, "w", encoding="utf-8") as f:
                f.write(body)
            with open(os.path.join(src, "lorebook.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            os.makedirs(os.path.join(src, "stickers"))
            with open(os.path.join(src, "stickers", "index.json"), "w", encoding="utf-8") as f:
                f.write("{}")
            with open(os.path.join(src, "stickers", "a.png"), "wb") as f:
                f.write(b"png")
            card = resolve_card_spec(json_path)
            prepared = prepare_card_dir(card, 0, base_dir=prep)
            self.assertEqual(prepared.label, card.label)
            self.assertEqual(prepared.json_path, card.json_path)
            self.assertTrue(os.path.isfile(os.path.join(prepared.dir, "character.json")))
            self.assertTrue(os.path.isfile(os.path.join(prepared.dir, "lorebook.json")))
            self.assertTrue(os.path.isfile(os.path.join(prepared.dir, "stickers", "a.png")))
            with open(os.path.join(prepared.dir, "character.json"), encoding="utf-8") as f:
                self.assertEqual(f.read(), body)

    def test_prepare_character_json_is_noop(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "character.json")
            with open(path, "w", encoding="utf-8") as f:
                f.write('{"name": "标准卡"}')
            card = resolve_card_spec(tmp)
            prepared = prepare_card_dir(card, 0, base_dir=os.path.join(tmp, "prep"))
            self.assertEqual(prepared.dir, card.dir)

    def test_feature_switches_follow_target_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "character.json"), "w", encoding="utf-8") as f:
                f.write('{"features": {"self_memory": true, "trim_echo": true, "candidates": 3}}')
            character = CharacterConfig()
            reply = ReplyConfig()
            apply_card_switches(character, reply, tmp)
            self.assertTrue(character.self_memory)
            self.assertTrue(reply.trim_echo)
            self.assertEqual(reply.candidates, 3)

    def test_explicit_config_keys_win_over_card(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "character.json"), "w", encoding="utf-8") as f:
                f.write('{"features": {"self_memory": true, "candidates": 3}}')
            character = CharacterConfig()
            reply = ReplyConfig(candidates=1)
            apply_card_switches(
                character, reply, tmp,
                char_explicit={"self_memory": False},
                reply_explicit={"candidates": 1},
            )
            self.assertFalse(character.self_memory)
            self.assertEqual(reply.candidates, 1)

    def test_ghost_features_are_reset(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "character.json"), "w", encoding="utf-8") as f:
                f.write('{"name": "无 features 的旧卡"}')
            character = CharacterConfig(self_memory=True)
            reply = ReplyConfig(trim_echo=True, candidates=3)
            apply_card_switches(character, reply, tmp)
            self.assertFalse(character.self_memory)
            self.assertFalse(reply.trim_echo)
            self.assertEqual(reply.candidates, 1)


# ==========================================
# 10. 探针集合与命令行选择
# ==========================================
class TestProbes(unittest.TestCase):
    def test_eight_probes_with_unique_keys(self):
        self.assertEqual(len(PROBES), 8)
        self.assertEqual(len({p.key for p in PROBES}), 8)
        for probe in PROBES:
            self.assertTrue(probe.turns, probe.key)
            self.assertIn(probe.kind, ("傲娇", "人味"))

    def test_dodged_invite_probe_has_seed_history(self):
        # 「她先邀约」必须是离线前情，不能靠模型即兴
        probe = PROBE_BY_KEY["invite_dodged"]
        self.assertEqual(len(probe.seed_turns), 1)
        self.assertIn("锅", probe.seed_turns[0][1])

    def test_probe_dict_roundtrip(self):
        for probe in PROBES:
            self.assertEqual(probe_from_dict(probe_to_dict(probe)), probe)

    def test_select_probes_accepts_numbers_and_keys(self):
        picked = select_probes("1,8")
        self.assertEqual([p.key for p in picked], ["care_exposed", "baseline"])
        mixed = select_probes("goodnight,2")
        self.assertEqual([p.key for p in mixed], ["goodnight", "sick"])
        self.assertEqual(len(select_probes("all")), 8)
        with self.assertRaises(ValueError):
            select_probes("不存在的探针")

    def test_cell_seed_is_card_independent(self):
        # A/B 两卡必须看到同一串随机数：种子不含卡序号
        self.assertEqual(_cell_seed(2026, 2, 3, 1), _cell_seed(2026, 2, 3, 1))
        self.assertNotEqual(_cell_seed(2026, 2, 3, 1), _cell_seed(2026, 2, 3, 2))


# ==========================================
# 11. 复算与报告
# ==========================================
class TestSummaryAndReport(unittest.TestCase):
    def test_summarize_scores_and_is_idempotent(self):
        data = synthetic_raw()
        summarize(data)
        overall = data["scores"]["characters/si"]["overall"]
        self.assertIn("composite_score", overall)
        self.assertEqual(overall["scenes_total"], 2)
        first = build_report(data, {})
        summarize(data)
        second = build_report(data, {})
        self.assertEqual(first, second)

    def test_report_contains_required_sections(self):
        data = summarize(synthetic_raw())
        report = build_report(data, {})
        for needed in (
            "# 祀卡傲娇性格跑分报告",
            "沙箱纪律",
            "评分口径",
            "场景总表",
            "逐场景明细",
            "人工裁决区",
            "<!--MANUAL:judge-->",
            "<!--MANUAL:verdict-->",
            "综合分",
        ):
            self.assertIn(needed, report)

    def test_report_quotes_raw_reply(self):
        data = summarize(synthetic_raw())
        report = build_report(data, {})
        self.assertIn("谁担心你了", report)
        self.assertIn("在翻小玩意呢", report)

    def test_manual_block_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "report.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write("<!--MANUAL:judge-->\n这版傲娇没写成泼妇\n<!--/MANUAL-->\n")
            manual = load_prev_manual(path)
            self.assertEqual(manual["judge"], "这版傲娇没写成泼妇")
            data = summarize(synthetic_raw())
            self.assertIn("这版傲娇没写成泼妇", build_report(data, manual))

    def test_ab_comparison_section_when_two_cards(self):
        data = summarize(synthetic_raw(card_labels=("old", "new")))
        report = build_report(data, {})
        self.assertIn("A/B 对照", report)
        self.assertIn("Δ（第 2 卡 − 第 1 卡）", report)

    def test_sample_agg_excludes_error_turns(self):
        data = synthetic_raw()
        cell_data = data["cells"]["0|care_exposed|1"]
        cell_data["turns"].append({
            "user": "[ERROR]",
            "raw": "boom",
            "reply": "[失败] boom",
            "chunks": [],
            "metrics": evaluate_reply("", [], "[失败] boom"),
        })
        agg = _sample_agg(cell_data)
        self.assertEqual(agg["replies"], 1)


if __name__ == "__main__":
    unittest.main()
