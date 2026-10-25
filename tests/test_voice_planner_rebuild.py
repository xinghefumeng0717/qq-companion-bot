"""VoicePlanner 重构定向离线测试集。"""
import unittest

from companion.voice_planner import (
    VOICE_FOLLOWUP_RE,
    VOICE_REQUEST_RE,
    VoicePlan,
    VoicePlanner,
    strip_legacy_voice_markup,
)


class TestVoicePlannerRebuild(unittest.TestCase):
    def test_明确请求识别收窄(self):
        explicit_samples = [
            "发条语音",
            "发个语音",
            "唱一句",
            "唱首",
            "唱首歌",
            "让我听听你",
            "我想听听你的声音",
            "来条语音呗",
            "用语音说",
            "能不能发语音",
            "想听你唱歌",
            "开麦",
            "通个话",
        ]
        for s in explicit_samples:
            self.assertTrue(bool(VOICE_REQUEST_RE.search(s)), f"Should match explicit: {s}")
            self.assertTrue(VoicePlanner.explicit_request(s), f"explicit_request method should match: {s}")

        preference_samples = [
            "你声音真好听",
            "我喜欢你的声音",
            "你的声音好温柔",
            "声音很特别",
            "刚才的声音",
            "声音真好",
            "声音好甜",
        ]
        for s in preference_samples:
            self.assertFalse(bool(VOICE_REQUEST_RE.search(s)), f"Should NOT match preference: {s}")
            self.assertFalse(VoicePlanner.explicit_request(s), f"explicit_request should NOT match: {s}")

    def test_追问正则不误伤普通名词(self):
        followup_samples = [
            "语音呢",
            "怎么没语音",
            "怎么还没发语音",
            "没收到语音",
            "没听到语音",
            "咋没声音",
            "怎么没声音了",
            "语音发了吗",
            "声音去哪了",
        ]
        for s in followup_samples:
            self.assertTrue(bool(VOICE_FOLLOWUP_RE.search(s)), f"Should match followup: {s}")

        normal_samples = [
            "快递呢",
            "怎么没收到快递",
            "验证码没收到",
            "我的外卖怎么还没到",
            "没收到短信",
            "通知收到了吗",
            "没收到货",
        ]
        for s in normal_samples:
            self.assertFalse(bool(VOICE_FOLLOWUP_RE.search(s)), f"Should NOT match normal query: {s}")

    def test_requested显式覆盖识别(self):
        planner = VoicePlanner("explicit")
        plan = planner.plan("在干嘛", "有点累了", voice_allowed=True, requested=True)
        self.assertTrue(plan.has_voice)
        self.assertTrue(plan.requested)
        self.assertEqual(plan.voice_text, "有点累了")

        plan_suppressed = planner.plan("发条语音", "有点累了", voice_allowed=True, requested=False)
        self.assertFalse(plan_suppressed.has_voice)
        self.assertFalse(plan_suppressed.requested)
        self.assertEqual(plan_suppressed.body_text, "有点累了")

    def test_frequency_off永不规划(self):
        planner = VoicePlanner("off")
        self.assertFalse(planner.allows("发条语音"))
        self.assertFalse(planner.allows("发条语音", requested=True))
        plan = planner.plan("发条语音", "晚安", voice_allowed=True, requested=True)
        self.assertFalse(plan.has_voice)
        self.assertEqual(plan.body_text, "晚安")

    def test_frequency_explicit仅在requested时规划(self):
        planner = VoicePlanner("explicit")
        plan_no_req = planner.plan("在干嘛", "我好想你，今天有点累", voice_allowed=True)
        self.assertFalse(plan_no_req.has_voice)
        self.assertEqual(plan_no_req.body_text, "我好想你，今天有点累")

        plan_pref = planner.plan("你声音真好听", "我好想你，今天有点累", voice_allowed=True)
        self.assertFalse(plan_pref.has_voice)

        plan_req = planner.plan("发条语音", "我好想你，今天有点累", voice_allowed=True)
        self.assertTrue(plan_req.has_voice)

    def test_frequency_frequent透明选句与请求强制性(self):
        planner = VoicePlanner("frequent")
        plan_info = planner.plan("在干嘛", "下午三点交报告，材料在桌上", voice_allowed=True)
        self.assertFalse(plan_info.has_voice)

        plan_emo = planner.plan("在干嘛", "材料在桌上\n我有点累了\n明天再说", voice_allowed=True)
        self.assertTrue(plan_emo.has_voice)
        self.assertEqual(plan_emo.voice_text, "我有点累了")
        self.assertEqual(plan_emo.body_text, "材料在桌上\n明天再说")

        plan_must = planner.plan("发条语音", "下午三点交报告，材料在桌上", voice_allowed=True)
        self.assertTrue(plan_must.has_voice)
        self.assertEqual(plan_must.voice_text, "下午三点交报告，材料在桌上")
        self.assertEqual(plan_must.body_text, "")

    def test_原话连续片段不拼散句不改词(self):
        planner = VoicePlanner("frequent")
        reply = "第一句在这。我其实有点想你。第三句在这。"
        plan = planner.plan("在干嘛", reply, voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.voice_text, "我其实有点想你。")
        self.assertEqual(plan.body_text, "第一句在这。\n第三句在这。")
        self.assertEqual(plan.before_text, "第一句在这。")
        self.assertEqual(plan.after_text, "第三句在这。")

    def test_非文字标记保留且不念出(self):
        planner = VoicePlanner("frequent")
        reply = "刚到家[face:12]\n早点睡[sticker:抱抱]\n明天见"
        plan = planner.plan("发条语音", reply, voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.voice_text, "早点睡")
        self.assertIn("[face:12]", plan.body_text)
        self.assertIn("[sticker:抱抱]", plan.body_text)
        self.assertNotIn("face", plan.voice_text)
        self.assertNotIn("sticker", plan.voice_text)

    def test_引用标记quote保留(self):
        planner = VoicePlanner("frequent")
        reply = "[quote:42] 收到啦\n早点睡吧"
        plan = planner.plan("发条语音", reply, voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.quote_index, 42)
        self.assertEqual(plan.voice_text, "早点睡吧")
        self.assertEqual(plan.body_text, "收到啦")

    def test_strip_legacy_voice_markup防御清理(self):
        self.assertEqual(strip_legacy_voice_markup("[voice:]测试语音[/voice]"), "测试语音")
        self.assertEqual(strip_legacy_voice_markup("[voice:整行单标签测试]"), "整行单标签测试")
        self.assertEqual(strip_legacy_voice_markup("（语音消息）历史录音"), "历史录音")
        self.assertEqual(strip_legacy_voice_markup("[语音消息] 消息正文"), "消息正文")
        self.assertEqual(strip_legacy_voice_markup("[语音] 消息正文"), "消息正文")
        self.assertEqual(strip_legacy_voice_markup("【划重点】明天要早起"), "【划重点】明天要早起")
        self.assertEqual(strip_legacy_voice_markup("[笑哭] 太逗了"), "[笑哭] 太逗了")
        self.assertEqual(strip_legacy_voice_markup("var arr = a[0];"), "var arr = a[0];")
        self.assertEqual(strip_legacy_voice_markup("给你一条/唱一句代码"), "给你一条/唱一句代码")
        self.assertEqual(strip_legacy_voice_markup(""), "")

    def test_真实旧写法清理不留半个括号(self):
        cases = {
            "[voice:嗓子还没开呢 将就听]刚睡醒": "嗓子还没开呢 将就听 刚睡醒",
            "好吧\n[voice:你这人真是\n烦死了]": "好吧\n你这人真是\n烦死了",
            "[voice]行吧行吧 就一句[/voice": "行吧行吧 就一句",
            "语音：去吧去吧\n回来再学": "去吧去吧\n回来再学",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(strip_legacy_voice_markup(raw), expected)

    def test_规划器对旧标记的输入清洗(self):
        planner = VoicePlanner("frequent")
        reply = "[voice:]我今天有点累[/voice]\n回去就休息"
        plan = planner.plan("在干嘛", reply, voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertNotIn("voice", plan.voice_text)
        self.assertNotIn("voice", plan.body_text)
        self.assertEqual(plan.voice_text, "我今天有点累")
        self.assertEqual(plan.body_text, "回去就休息")

    def test_否定请求不会触发语音(self):
        for text in ("不发语音", "不要发语音了", "不要给我发语音", "别再给我发语音", "别唱一句", "不想听你的声音"):
            with self.subTest(text=text):
                self.assertFalse(VoicePlanner.explicit_request(text))

    def test_沉默不形成语音(self):
        for reply in ("[沉默]", "[SILENT]", "[quote:2]\n[沉默]", "【沉默】", "［沉默］"):
            with self.subTest(reply=reply):
                plan = VoicePlanner("frequent").plan("发条语音", reply, voice_allowed=True)
                self.assertFalse(plan.has_voice)
                self.assertEqual(plan.body_text, "[沉默]")

    def test_全角表情不进入朗读文本(self):
        plan = VoicePlanner("frequent").plan("发条语音", "早点睡［face:doge］", voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.voice_text, "早点睡")
        self.assertIn("［face:doge］", plan.body_text)

    def test_空文本与无文字标记(self):
        planner = VoicePlanner("frequent")
        plan_empty = planner.plan("发条语音", "", voice_allowed=True)
        self.assertFalse(plan_empty.has_voice)
        plan_tag_only = planner.plan("发条语音", "[sticker:猫猫]", voice_allowed=True)
        self.assertFalse(plan_tag_only.has_voice)
        self.assertEqual(plan_tag_only.body_text, "[sticker:猫猫]")

    def test_max_chars超长截断回填(self):
        planner = VoicePlanner("frequent", max_chars=15)
        long_sentence = "我其实有点想你而且今天遇到了特别多有趣的事情"
        plan = planner.plan("发条语音", long_sentence, voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertLessEqual(len(plan.voice_text), 15)
        self.assertTrue(long_sentence.startswith(plan.voice_text))
        self.assertNotIn("…", plan.voice_text)
        self.assertIn(plan.voice_text, long_sentence)
        self.assertTrue(long_sentence.endswith(plan.body_text))


if __name__ == "__main__":
    unittest.main()
