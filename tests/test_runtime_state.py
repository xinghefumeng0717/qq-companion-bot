import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta

from companion.runtime_state import RuntimeEventStore, TurnContext, TurnTracer
from companion.voice_planner import VoicePlanner
from companion.replier import fit_chunks
from helpers import close_db, make_db


class RuntimeEventTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db()
        self.store = RuntimeEventStore(self.db)
        await self.store.init()
        self.now = datetime(2026, 10, 24, 12, 0)

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_时间流逝不会自动完成计划(self):
        await self.store.transition_plan("去越山", "planned", "我打算去越山", source="assistant", now=self.now)
        plans = await self.store.open_plans()
        self.assertEqual(plans[0]["status"], "planned")
        self.assertEqual(plans[0]["occurred_at"], "2026-10-24 12:00")
        later = self.now + timedelta(days=3)
        self.assertEqual((await self.store.open_plans())[0]["status"], "planned")
        await self.store.transition_plan("去越山", "active", "我动身了", source="assistant", now=later)
        await self.store.transition_plan("去越山", "done", "我到了", source="assistant", now=later)
        self.assertEqual(await self.store.open_plans(), [])
        self.assertTrue(await self.store.transition_plan(
            "去越山", "planned", "下次我准备去越山", source="assistant", now=later,
        ))

    async def test_观察者必须给有效语义证据(self):
        changed = await self.store.apply_observation(
            [{"kind": "plan", "subject": "去越山", "status": "planned",
              "source": "assistant", "evidence": "我打算去越山"}],
            user_text="好", assistant_text="我打算去越山", now=self.now,
        )
        self.assertEqual(changed, 1)
        for bad in (
            [{"kind": "plan", "subject": "去越山", "status": "done",
              "source": "assistant", "evidence": "我打算去越山"}],
            [{"kind": "plan", "subject": "去越山", "status": "done",
              "source": "assistant", "evidence": "我已经到了"}],
            [{"kind": "plan", "subject": "去越山", "status": "planned",
              "source": "assistant", "evidence": "好"}],
        ):
            self.assertEqual(await self.store.apply_observation(
                bad, user_text="好", assistant_text="我打算去越山", now=self.now,
            ), 0)
        self.assertEqual((await self.store.open_plans())[0]["status"], "planned")

    async def test_最近计划按最近更新排列并带来源时间(self):
        for i in range(7):
            await self.store.transition_plan(
                f"计划{i}", "planned", f"我打算做计划{i}", source="assistant", now=self.now,
            )
        await self.store.transition_plan("计划0", "active", "我开始做计划0", source="assistant", now=self.now)
        prompt = await self.store.render_prompt()
        self.assertIn("计划0", prompt)
        self.assertNotIn("计划6", prompt.split("计划0")[0])
        self.assertIn("她说过", prompt)
        self.assertIn("2026-10-24 12:00", prompt)
        self.assertTrue(prompt.endswith("\n"))

    async def test_语音已发出不等于对方听到(self):
        await self.store.record_voice("sent", "晚安", now=self.now)
        prompt = await self.store.render_prompt()
        self.assertIn("已由协议端发出", prompt)
        self.assertIn("不代表对方已经听到", prompt)
        await self.store.record_voice("text_fallback", "晚安", now=self.now, reason="recovered")
        self.assertEqual((await self.store.latest_voice())["status"], "text_fallback")

    async def test_最终段决定真实投递(self):
        sent = await self.store.record_delivery_chunks(
            [{"type": "voice", "content": "晚安", "_runtime_voice": True, "_voice_delivery": "sent"}],
            "晚安", now=self.now,
        )
        self.assertEqual(sent, "sent")
        fallback = await self.store.record_delivery_chunks(
            [{"type": "text", "content": "晚安", "_runtime_voice": True, "_voice_delivery": "recovered"}],
            "晚安", now=self.now,
        )
        self.assertEqual(fallback, "text_fallback")
        failed = await self.store.record_delivery_chunks(
            [{"type": "text", "content": "晚安", "_runtime_voice": True}],
            "晚安", now=self.now,
        )
        self.assertEqual(failed, "failed")


class VoicePlannerTests(unittest.TestCase):
    def test_高频语音保留且正文不重复(self):
        plan = VoicePlanner("frequent").plan(
            "在干嘛", "刚逛完摊子\n先别催我\n腿有点酸，回去再跟你说", voice_allowed=True,
        )
        self.assertEqual(plan.mode, "mixed")
        self.assertEqual(plan.body_text, "刚逛完摊子\n先别催我")
        self.assertEqual(plan.voice_text, "腿有点酸，回去再跟你说")

    def test_短句和明确请求都可以发语音(self):
        self.assertTrue(VoicePlanner("frequent").plan("在呢", "晚安", voice_allowed=True).has_voice)
        plan = VoicePlanner("frequent").plan("发条语音", "行啊", voice_allowed=True)
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.mode, "voice")

    def test_闸门关闭只走文字(self):
        plan = VoicePlanner("frequent").plan("发条语音", "这会儿不方便", voice_allowed=False)
        self.assertFalse(plan.has_voice)

    def test_旧标记不进入正文或朗读文本(self):
        plan = VoicePlanner("frequent").plan(
            "在干嘛", "[voice:]刚逛完摊子[/voice]\n[voice:]腿有点酸[/voice]", voice_allowed=True,
        )
        self.assertTrue(plan.has_voice)
        self.assertNotIn("voice", plan.voice_text)
        self.assertNotIn("voice", plan.body_text)
        self.assertNotIn("语音消息", plan.body_text)
        # 旧标记只清理、不额外制造第二条语音：正文仍是没选中的第一句。
        self.assertEqual(plan.body_text, "刚逛完摊子")
        self.assertEqual(plan.voice_text, "腿有点酸")

    def test_高频不无脑且情绪句不在末尾也能选中(self):
        planner = VoicePlanner("frequent")
        for ack in ("在呢", "嗯", "好", "行", "在的", "收到"):
            self.assertFalse(planner.plan("在干嘛", ack, voice_allowed=True).has_voice)
        self.assertFalse(planner.plan(
            "卖什么呢", "玻璃罐子装粉末，还有小灯", voice_allowed=True,
        ).has_voice)
        plan = planner.plan(
            "怎么了", "我今天有点累\n对了\n明天再说吧", voice_allowed=True,
        )
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.voice_text, "我今天有点累")
        # 旧策略取末尾两句会把后两句念出来；新策略选中中间的情绪句，
        # body 必须完整保留前后两段，这就是原先 body 期望过时的地方。
        self.assertEqual(plan.body_text, "对了\n明天再说吧")

    def test_普通信息不会因单字线索误发语音(self):
        planner = VoicePlanner("frequent")
        for info in (
            "酸奶放在冰箱第二层了",
            "我想确认一下明天的会议安排",
            "我明天下午三点交报告吧",
        ):
            self.assertFalse(planner.plan("在干嘛", info, voice_allowed=True).has_voice)
        long_info = "明天上午九点开会，中午和客户吃饭，下午三点交报告。" * 8
        self.assertFalse(planner.plan("在干嘛", long_info, voice_allowed=True).has_voice)

    def test_损他撒娇晚安和个人感受会优先语音(self):
        planner = VoicePlanner("frequent")
        for reply in ("哼，不理你了", "人家不要嘛", "我有点难受", "我有点舍不得", "晚安"):
            self.assertTrue(planner.plan("在干嘛", reply, voice_allowed=True).has_voice, reply)

    def test_明确请求覆盖普通应答且不取尾句(self):
        planner = VoicePlanner("frequent")
        ack = planner.plan("发条语音", "嗯", voice_allowed=True)
        self.assertTrue(ack.has_voice)
        self.assertEqual(ack.voice_text, "嗯")
        plan = planner.plan(
            "发条语音", "我才不要理你呢\n对了\n明天记得带伞", voice_allowed=True,
        )
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.voice_text, "我才不要理你呢")
        self.assertEqual(plan.body_text, "对了\n明天记得带伞")

    def test_引用与明确语音请求可以共存(self):
        plan = VoicePlanner("frequent").plan(
            "发条语音", "[quote:2] 成交\n我有点累", voice_allowed=True,
        )
        self.assertTrue(plan.has_voice)
        self.assertEqual(plan.quote_index, 2)
        self.assertEqual(plan.voice_text, "我有点累")
        self.assertEqual(plan.body_text, "成交")

    def test_引用贴纸与晚安能同时保留(self):
        plan = VoicePlanner("frequent").plan(
            "发条语音", "[quote:2]\n晚安\n[sticker:猫猫抱抱]", voice_allowed=True,
        )
        self.assertEqual(plan.quote_index, 2)
        self.assertEqual(plan.voice_text, "晚安")
        self.assertIn("[sticker:猫猫抱抱]", plan.body_text)
        self.assertNotIn("sticker", plan.voice_text)

    def test_选中表情保留且截断余文回正文(self):
        plan = VoicePlanner("frequent").plan(
            "嗯", "刚逛完摊子\n早点睡[sticker:猫猫抱抱]", voice_allowed=True,
        )
        self.assertIn("[sticker:猫猫抱抱]", plan.body_text)
        self.assertNotIn("sticker", plan.voice_text)

        # 省略号分支：无标点长文本也不能丢尾文，且语音长度必须真 ≤ max_chars。
        long_text = "我有点累但还是想把这句没有标点的话说完整后面还有个末尾关键词测试在这里"
        clipped = VoicePlanner("frequent", max_chars=20).plan("在干嘛", long_text, voice_allowed=True)
        self.assertTrue(clipped.has_voice)
        self.assertLessEqual(len(clipped.voice_text), 20)
        self.assertTrue(clipped.body_text.endswith("测试在这里"))
        self.assertTrue(long_text.endswith(clipped.body_text))
        self.assertTrue(long_text.startswith(clipped.voice_text.rstrip("…")))

        # 明确请求 200 字普通信息：只念到上限，其余正文保留尾部。
        long_info = "明天上午九点开会，中午和客户吃饭，下午三点交报告。" * 8
        asked = VoicePlanner("frequent", max_chars=20).plan(
            "发个语音", long_info, voice_allowed=True,
        )
        self.assertTrue(asked.has_voice)
        self.assertLessEqual(len(asked.voice_text), 20)
        self.assertTrue(long_info.endswith(asked.body_text))

    def test_语音在段数超限时优先保留(self):
        chunks = [{"type": "text", "content": "甲"}, {"type": "voice", "content": "乙"}]
        self.assertEqual(fit_chunks(chunks, 1)[0]["type"], "voice")

    def test_旧整行简写也不会漏语法(self):
        plan = VoicePlanner("frequent").plan(
            "发条语音", "[voice:叫了怎么办 我可真不理你了啊]", voice_allowed=True,
        )
        self.assertTrue(plan.has_voice)
        self.assertNotIn("voice", plan.voice_text)
        self.assertNotIn("]", plan.voice_text)


class TraceTests(unittest.TestCase):
    def test_轮次追踪保留状态与候选且保留字段不可覆盖(self):
        with tempfile.TemporaryDirectory() as directory:
            context = TurnContext(datetime(2026, 10, 24, 12, 0), "在干嘛", "状态")
            TurnTracer(directory).write(context, candidates=["甲", "乙"], picked=1, user="伪造")
            path = os.path.join(directory, "2026-10-24.jsonl")
            with open(path, encoding="utf-8") as f:
                row = json.loads(f.read())
            self.assertEqual(row["candidates"], ["甲", "乙"])
            self.assertEqual(row["picked"], 1)
            self.assertEqual(row["state_block"], "状态")
            self.assertEqual(row["user"], "在干嘛")


if __name__ == "__main__":
    unittest.main()
