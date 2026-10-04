"""BENCHMARK_V4 指标口径单测 (tests/test_benchmark_v4_metrics.py)

跑分工具（scripts/sim/benchmark_v4.py）里的客观指标全是纯正则函数，不打 API。
这里给它们钉上单测：口径写错会**静默地**把 FAIL 变成 PASS，所以必须锁死。
重点覆盖三类最容易出错的判定：
  1. 沉默/短收/该断就断的边界（≤6 字、无问号、沉默不重复计入短收）；
  2. K 场景事实归属的机械线索（主语是他=正确，主语是我=颠倒嫌疑），
     以及"她说的那 1 件"捞不到时必须报无法判定而不是静默算通过；
  3. 所有者裁决 2026-10-04 的 A/B/D/F 真人标准重校准阈值（集中常量 + 达标函数边界）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.sim.benchmark_v4 import (  # noqa: E402
    A_CLOSE_MAX_CHARS,
    B_CAUTION_MAX,
    CAUTION_RE,
    D_CAUTION_MAX,
    D_IMG_RE,
    E_MEME_RE,
    F_SILENCE_MAX_RATIO,
    INVITE_RE,
    NEW_SCENE_RE,
    QUESTION_RE,
    SILENCE_TOKEN,
    a_close_ok,
    caution_clauses,
    chars,
    clauses,
    d_close_ok,
    eval_k,
    has_question_mark,
    is_pure_sticker,
    is_short_close,
    is_silent,
    silence_ratio_ok,
    stop_ok,
)


class TestSilenceAndShortClose(unittest.TestCase):
    def test_silence_detected_only_on_exact_match(self):
        self.assertTrue(is_silent(SILENCE_TOKEN))
        self.assertTrue(is_silent(f"  {SILENCE_TOKEN}  "))
        # 行内夹带别的文字不算沉默（与 replier.is_silence_output 同口径）
        self.assertFalse(is_silent("[沉默]好呀"))
        self.assertFalse(is_silent("好呀[沉默]"))

    def test_short_close_boundary_is_6_chars_no_question(self):
        self.assertTrue(is_short_close("好呀"))          # 2 字
        self.assertTrue(is_short_close("一二三四五六"))    # 恰好 6 字
        self.assertFalse(is_short_close("一二三四五六七"))  # 7 字越界
        self.assertFalse(is_short_close("今天怎么样？"))   # 有问号不算短收

    def test_short_close_upper_bound_is_parameterizable(self):
        # A 的单格达标把上限放宽到 10 字（所有者裁决 2026-10-04）；
        # 记录指标口径仍默认 6 字，两者不能互相污染。
        ten = "一二三四五六七八九十"
        self.assertFalse(is_short_close(ten))                  # 默认 6 字：越界
        self.assertTrue(is_short_close(ten, A_CLOSE_MAX_CHARS))  # A 口径 10 字：达标
        self.assertTrue(is_short_close("一二三四五六七", 10))

    def test_question_mark_detection_is_literal(self):
        self.assertTrue(has_question_mark("到家了吗？"))
        self.assertTrue(has_question_mark("真的?"))
        self.assertFalse(has_question_mark("嗯，晚点再聊。"))

    def test_silence_not_double_counted_as_short_close(self):
        # 沉默有资格算"该断就断"，但不能同时被算成短收（否则 A/D 统计会重复计数）
        self.assertTrue(stop_ok(SILENCE_TOKEN))
        self.assertFalse(is_short_close(SILENCE_TOKEN))
        self.assertFalse(stop_ok("一二三四五六七"))       # 7 字无问号：该断不断


class TestClauseAndCaution(unittest.TestCase):
    def test_clauses_split_on_punctuation_and_newline(self):
        self.assertEqual(clauses("去洗澡啦\n早点睡"), ["去洗澡啦", "早点睡"])

    def test_caution_question_sentence_not_counted(self):
        # 带问号的关切句不计入连环祈使（任务书 B 场景口径）
        self.assertTrue(CAUTION_RE.search("到家说一声"))
        counted = [c for c in clauses("记得带伞\n到了吗") if CAUTION_RE.search(c) and not QUESTION_RE.search(c)]
        self.assertEqual(counted, ["记得带伞"])

    def test_me_suffix_questions_never_counted_as_caution(self):
        # 疑问判定正则不认「没」字结尾（"到没"/"吃了没"），但这不构成口径缺陷：
        # 这类句子本来也命不中叮嘱词表，两条路都归为"不计入叮嘱"。
        # 角色卡恰恰把「到没」「吃了没」列为推荐的关切问句，这里钉住该行为。
        for q in ("到没", "吃了没", "你到没"):
            self.assertFalse(CAUTION_RE.search(q), f"{q} 不该命中叮嘱词表")
            counted = [
                c for c in clauses(q) if CAUTION_RE.search(c) and not QUESTION_RE.search(c)
            ]
            self.assertEqual(counted, [], f"{q} 不应被计入叮嘱")


class TestWordLists(unittest.TestCase):
    def test_new_scene_words(self):
        self.assertTrue(NEW_SCENE_RE.search("我刚回寝室"))
        self.assertTrue(NEW_SCENE_RE.search("湖面风特别舒服"))
        self.assertIsNone(NEW_SCENE_RE.search("好呀"))

    def test_img_words_v4_list(self):
        self.assertTrue(D_IMG_RE.search("这只猫好可爱"))
        self.assertTrue(D_IMG_RE.search("这图的表情包"))
        self.assertIsNone(D_IMG_RE.search("收拾完了"))

    def test_meme_words_v4_list(self):
        self.assertTrue(E_MEME_RE.search("红油阿姨"))
        self.assertIsNone(E_MEME_RE.search("下午在琴房练了会"))


class TestCharsHelper(unittest.TestCase):
    def test_chars_ignores_whitespace(self):
        self.assertEqual(chars(" 好 呀\n"), 2)


def _cell(turns, diary=None):
    return {"turns": turns, "diary": diary or [], "diary_archive": []}


class TestKAttribution(unittest.TestCase):
    """K 场景的机械线索：主语判定方向不能反。"""

    def test_his_fact_attributed_to_him_is_correct(self):
        cell = _cell(
            [
                {"user": "我上周去玉泉老校区听了个弦乐讲座，还挺值的", "reply": "听起来挺有意思的"},
                {"user": "那我室友最近在学尤克里里", "reply": "哈哈那挺吵的"},
            ],
            diary=[{"id": 1, "content": "他上周去玉泉老校区听了弦乐讲座，说挺值的。"}],
        )
        v = eval_k(cell)
        f1 = next(f for f in v["facts"] if f["id"] == "F1")
        self.assertEqual(f1["mechanical_hint"], "归属正确")
        self.assertEqual(v["inversion_suspects"], 0)

    def test_his_fact_attributed_to_her_is_inversion(self):
        cell = _cell(
            [
                {"user": "我上周去玉泉老校区听了个弦乐讲座，还挺值的", "reply": "嗯嗯"},
                {"user": "那我室友最近在学尤克里里", "reply": "哈哈哈"},
            ],
            diary=[{"id": 1, "content": "我上周去玉泉老校区听了弦乐讲座，挺值的。"}],
        )
        v = eval_k(cell)
        f1 = next(f for f in v["facts"] if f["id"] == "F1")
        self.assertEqual(f1["mechanical_hint"], "颠倒嫌疑")
        self.assertEqual(v["inversion_suspects"], 1)

    def test_self_fact_absent_reports_undeterminable_not_pass(self):
        # 关键防线：捞不到"她说的那 1 件"时必须显式报无法判定，
        # 绝不能因为没有颠倒嫌疑就静默算成通过（FIXES12 冒烟踩过的坑）
        cell = _cell(
            [
                {"user": "我上周去玉泉老校区听了个弦乐讲座", "reply": "[沉默]"},
                {"user": "我室友最近在学尤克里里", "reply": ""},
            ],
            diary=[{"id": 1, "content": "他去了玉泉老校区，他室友在学尤克里里。"}],
        )
        v = eval_k(cell)
        self.assertFalse(v["self_fact_found"])
        f3 = next(f for f in v["facts"] if f["id"] == "F3")
        self.assertEqual(f3["mechanical_hint"], "无法判定")

    def test_self_fact_attributed_to_her_is_correct(self):
        cell = _cell(
            [
                {"user": "我上周去玉泉老校区听了个讲座", "reply": "我这周在琴房练了三天大提琴"},
                {"user": "我室友在学尤克里里", "reply": "哈哈"},
            ],
            diary=[{"id": 1, "content": "我这周在琴房练了三天琴，有点累。"}],
        )
        v = eval_k(cell)
        f3 = next(f for f in v["facts"] if f["id"] == "F3")
        self.assertEqual(f3["mechanical_hint"], "归属正确")

    def test_self_fact_extractor_survives_rare_vocabulary(self):
        # 回归防线：第一版用"我+活动词表"抽取，她实际说出的 东二/德沃夏克/三明治
        # 都不在词表里，5/5 采样全捞空 → F3 判据悬空成假通过。
        # 现在靠"只出现在她嘴里、他从未说过的具体名词"抽取，必须捞得到。
        cell = _cell(
            [
                {"user": "我上周去玉泉老校区听了个讲座", "reply": "我今天图省事，去风味点了碗面"},
                {"user": "我室友在学尤克里里", "reply": "我今天也没正经吃，下午啃了半块三明治"},
            ],
            diary=[{"id": 1, "content": "他最近挺规律的。"}],
        )
        v = eval_k(cell)
        self.assertTrue(v["self_fact_found"], "必须能从她的回复里捞到自述事实")
        f3 = next(f for f in v["facts"] if f["id"] == "F3")
        self.assertIn(f3["label"].split("·")[1], ("风味", "三明治"))

    def test_token_shared_with_him_is_not_used_as_her_fact(self):
        # 他也说过的词不能当"她说的"事实，否则归属判定本身就失去唯一性
        cell = _cell(
            [
                {"user": "我室友在学尤克里里，晚上吵", "reply": "你室友是初学吗"},
                {"user": "我明天有课", "reply": "我今晚在东二随便拌了个面"},
            ],
            diary=[{"id": 1, "content": "他室友在学尤克里里。"}],
        )
        v = eval_k(cell)
        f3 = next(f for f in v["facts"] if f["id"] == "F3")
        self.assertEqual(f3["mechanical_hint"], "未提及")  # 日记没写东二
        self.assertIn("东二", f3["label"])


class TestInviteNoise(unittest.TestCase):
    def test_about_is_flagged_as_noise_not_silently_dropped(self):
        # "大约" 会命中邀约正则里的「约」，属于已知噪声：任务书禁止为凑 PASS 改口径，
        # 所以保留命中并在报告里标注噪声，让人工能分辨真假阳性。
        self.assertTrue(INVITE_RE.search("大约三点"))
        from scripts.sim.benchmark_v4 import INVITE_NOISE_RE

        self.assertTrue(INVITE_NOISE_RE.search("大约三点"))


class TestOwnerRulingThresholds(unittest.TestCase):
    """所有者裁决 2026-10-04：真人标准重校准。阈值是口径的一部分，钉死防漂移。"""

    def test_threshold_values(self):
        self.assertEqual(A_CLOSE_MAX_CHARS, 10)   # A 收尾字数 6 → 10
        self.assertEqual(B_CAUTION_MAX, 2)        # B 叮嘱上限 1 → 2
        self.assertEqual(D_CAUTION_MAX, 1)        # D 叮嘱上限
        self.assertEqual(F_SILENCE_MAX_RATIO, 0.10)  # F 沉默上限 0 → ≤10%

    def test_silence_ratio_boundary(self):
        # 10 格最多 1 格：1/10 通过，2/10 失败；无数据必须返回 None 而不是 True。
        self.assertTrue(silence_ratio_ok(1, 10))
        self.assertFalse(silence_ratio_ok(2, 10))
        self.assertTrue(silence_ratio_ok(0, 10))
        self.assertIsNone(silence_ratio_ok(0, 0))


class TestRecalibratedA(unittest.TestCase):
    """A 告别拖尾：单发表情包 / "嗯，晚点再聊" / 沉默都算正常收尾。"""

    def test_pure_sticker_passes(self):
        self.assertTrue(is_pure_sticker("[表情:潜水小猫]"))
        self.assertTrue(is_pure_sticker("[sticker:呆呆]"))
        self.assertTrue(a_close_ok("[表情:潜水小猫]"))
        # 表情包夹带文字就不是"纯表情包"
        self.assertFalse(is_pure_sticker("[表情:菲比乖巧]\n去吧"))

    def test_short_ten_chars_no_question_passes(self):
        self.assertTrue(a_close_ok("嗯，晚点再聊。"))   # 6 字，原口径尚可
        self.assertTrue(a_close_ok("嗯嗯"))            # 2 字
        self.assertTrue(a_close_ok("一二三四五六七八九十"))  # 恰好 10 字

    def test_silence_passes(self):
        self.assertTrue(a_close_ok(SILENCE_TOKEN))

    def test_eleven_chars_without_question_fails(self):
        self.assertFalse(a_close_ok("一二三四五六七八九十一"))  # 11 字越界

    def test_question_new_scene_and_caution_fail(self):
        self.assertFalse(a_close_ok("到家了吗？"))              # 有问号
        self.assertFalse(a_close_ok("我洗完澡了"))              # 新场景词
        self.assertFalse(a_close_ok("回头发梢记得吹干"))         # 叮嘱小句


class TestRecalibratedD(unittest.TestCase):
    """D 表情包收尾：不再要求沉默/短收，短收但非沉默也达标。"""

    def test_short_but_not_silent_passes(self):
        # 关键回归：旧口径"沉默+短收"判它不算收尾，新口径只看图内元素/问号/新话题/叮嘱。
        self.assertFalse(is_silent("嗯嗯"))
        self.assertTrue(d_close_ok("嗯嗯"))
        self.assertTrue(d_close_ok("好呀，去吧。"))

    def test_silence_passes(self):
        self.assertTrue(d_close_ok(SILENCE_TOKEN))

    def test_img_word_fails_even_when_short(self):
        self.assertFalse(d_close_ok("乖"))                 # 图内元素词「乖」
        self.assertFalse(d_close_ok("这图猫好可爱"))        # 图内元素词

    def test_new_scene_and_question_fail(self):
        self.assertFalse(d_close_ok("晚点我也去琴房练会儿"))  # 新场景词「琴房」
        self.assertFalse(d_close_ok("专心去吧？"))

    def test_caution_boundary_one_ok_two_fail(self):
        self.assertTrue(d_close_ok("晚上回来路上慢点"))          # 1 句叮嘱：达标
        self.assertFalse(d_close_ok("路上注意安全\n到家了说一声"))  # 2 句叮嘱：超标


class TestCautionCountBoundary(unittest.TestCase):
    """B 连环叮嘱：≤2 句通过、3 句失败的边界。"""

    def test_two_cautions_pass_three_fail(self):
        two = "路上注意安全，到家了说一声"
        three = "路上注意安全，到家了说一声。记得带把伞"
        self.assertEqual(len(caution_clauses(two)), 2)
        self.assertLessEqual(len(caution_clauses(two)), B_CAUTION_MAX)
        self.assertEqual(len(caution_clauses(three)), 3)
        self.assertGreater(len(caution_clauses(three)), B_CAUTION_MAX)

    def test_question_form_caution_not_counted(self):
        # 疑问式关切不计入叮嘱（与 B 口径一致），否则会把问句当妈味误伤
        self.assertEqual(caution_clauses("到家了吗"), [])


if __name__ == "__main__":
    unittest.main()
