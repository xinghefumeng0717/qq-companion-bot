"""FIXES25：时间感知 + 裸表情标签 + 工程小修（2026-10-19，生产库 10-05~10-19 复盘）

生产证据（data 快照 latest.db，10-19 拉取）：
  E1 10-09 23:12 主动消息 "别又睡到中午[doge]"——裸标签原样发上屏；
  E2 10-10~10-18 他没再回过，她每条主动消息都追问"七点计划还算不算数"
     （followups "明天七点起床" 永远 done=0；facts "打算明天开始好好学习" 无日期）；
  E3 10-08 下午她已到校，当晚/次日的主动消息又说"明天还得背着报告回学校"
     （10-07 存下的欲言又止念头在聊过之后没作废）。
外加四项工程小修：环境变量兜底 api_key、reset 备份进轮转、好感度日衰减零头累计、
冒烟脚本仿真器路径。
"""

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from companion.affection import AffectionEngine
from companion.backup import run_daily_backup
from companion.config import ModelPreset, ProactiveConfig, ReplyConfig, apply_env_api_keys
from companion.db import STATE_KEY_AFFECTION, TIME_FORMAT
from companion.memory import FOLLOWUP_EXPIRED, MemoryManager
from companion.observer import Observer
from companion.replier import Replier, normalize_bare_face_tags
from helpers import close_db, make_db, make_engine_stack, make_mock_gateway


def _ts(**delta) -> str:
    return (datetime.now() - timedelta(**delta)).strftime(TIME_FORMAT)


# ==========================================
# 1. 环境变量兜底 api_key
# ==========================================


class TestEnvApiKey(unittest.TestCase):
    def _p(self, key, provider="deepseek"):
        return ModelPreset(provider=provider, base_url="", api_key=key, chat="x")

    def test_占位key从环境变量补上(self):
        presets = {"deepseek": self._p("REDACTED_API_KEY"), "empty": self._p("")}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}):
            apply_env_api_keys(presets)
        self.assertEqual(presets["deepseek"].api_key, "sk-env")
        self.assertEqual(presets["empty"].api_key, "sk-env")

    def test_配置里的真key绝不被覆盖(self):
        presets = {"deepseek": self._p("sk-real")}
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-env"}):
            apply_env_api_keys(presets)
        self.assertEqual(presets["deepseek"].api_key, "sk-real")

    def test_没有环境变量时保持原样(self):
        presets = {"deepseek": self._p("REDACTED_API_KEY"), "seed": self._p("", "openai")}
        env = {k: v for k, v in os.environ.items() if k != "DEEPSEEK_API_KEY"}
        with patch.dict(os.environ, env, clear=True):
            apply_env_api_keys(presets)
        self.assertEqual(presets["deepseek"].api_key, "REDACTED_API_KEY")
        self.assertEqual(presets["seed"].api_key, "", "openai provider 没有对应环境变量")


# ==========================================
# 2. 裸表情标签（E1）
# ==========================================


class _Stickers:
    def match_sticker(self, desc):
        return None


class TestBareFaceTags(unittest.TestCase):
    def setUp(self):
        self.replier = Replier(ReplyConfig(), _Stickers())

    def test_生产原句的doge变成真表情(self):
        chunks, record = self.replier.parse_reply("你说明天要好好学习，闹钟定没定啊，别又睡到中午[doge]")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["type"], "combo")
        self.assertEqual([p["type"] for p in chunks[0]["parts"]], ["text", "face"])
        self.assertNotIn("[doge]", "".join(p.get("content", "") for p in chunks[0]["parts"]))
        self.assertIn("[face:doge]", record)

    def test_清单外的表情名剥掉标记不留方括号(self):
        for raw in ("好吧[汪汪]", "好吧[[汪汪]]", "好吧[微笑]"):
            chunks, record = self.replier.parse_reply(raw)
            self.assertEqual(record, "好吧", raw)

    def test_不是表情名的方括号一个字节不动(self):
        self.assertEqual(normalize_bare_face_tags("他说[图片]和[沉默]"), "他说[图片]和[沉默]")
        self.assertEqual(normalize_bare_face_tags("[face:doge][sticker:猫]"), "[face:doge][sticker:猫]")

    def test_整条沉默仍然是沉默(self):
        self.assertEqual(self.replier.parse_reply("[沉默]"), ([], ""))


# ==========================================
# 3. 记忆时效（E2 / E3）
# ==========================================


class _MemHarness(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.memory = MemoryManager(self.db)

    async def asyncTearDown(self):
        await close_db(self.db)

    async def add_followup(self, topic, remind_ago_h, created_ago_h=None):
        await self.db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES (?, ?, 0, ?)",
            (topic, _ts(hours=remind_ago_h), _ts(hours=created_ago_h or remind_ago_h + 8)),
        )

    async def add_turn(self, role, content, ago_h, proactive=0):
        await self.db.execute(
            "INSERT INTO turns (role, content, proactive, has_image, created_at) VALUES (?, ?, ?, 0, ?)",
            (role, content, proactive, _ts(hours=ago_h)),
        )


class TestFollowupExpiry(_MemHarness):
    async def test_过期48小时的待跟进不再出现且被标记过期(self):
        await self.add_followup("明天七点起床", remind_ago_h=24 * 9)
        await self.add_followup("周五交报告", remind_ago_h=2)
        rows = await self.memory.get_open_followups(due_only=True)
        self.assertEqual([r["topic"] for r in rows], ["周五交报告"])
        row = await self.db.fetchone("SELECT done FROM followups WHERE topic='明天七点起床'")
        self.assertEqual(row["done"], FOLLOWUP_EXPIRED)

    async def test_提示词形态带记下日期(self):
        await self.add_followup("明天七点起床", remind_ago_h=1, created_ago_h=10)
        rows = await self.memory.get_open_followups(due_only=True)
        day = (datetime.now() - timedelta(hours=10)).strftime("%m-%d")
        self.assertEqual(self.memory.format_followup(rows[0]), f"明天七点起床（{day} 他说的）")

    async def test_主动消息用掉一次就勾掉(self):
        stack = make_engine_stack(self.db, include_proactive=True, proactive_config=ProactiveConfig())
        await self.db.execute(
            "INSERT INTO followups (topic, remind_after, done, created_at) VALUES ('明天要考科目三', ?, 0, ?)",
            (_ts(hours=1), _ts(hours=1)),
        )
        first = await stack.proactive._select_topic_material()
        self.assertIn("考科目三", first)
        second = await stack.proactive._select_topic_material()
        self.assertNotIn("考科目三", second, "同一条待跟进不许天天当素材")


class TestDesireLiveness(_MemHarness):
    async def _add_desire(self, content, ago_h):
        await self.db.execute(
            "INSERT INTO suppressed_desires (content, created_at) VALUES (?, ?)",
            (content, _ts(hours=ago_h)),
        )

    async def test_念头之后他又聊过天就作废(self):
        await self._add_desire("说说自己明天背着报告回学校", ago_h=20)
        await self.add_turn("user", "我回浙大了", ago_h=5)
        self.assertEqual(await self.memory.get_live_desires(), [])

    async def test_他没再说话时念头仍然作数(self):
        await self.add_turn("user", "晚安", ago_h=30)
        await self._add_desire("想问他睡得好不好", ago_h=20)
        live = await self.memory.get_live_desires()
        self.assertEqual([d["content"] for d in live], ["想问他睡得好不好"])

    async def test_超过48小时删除(self):
        await self._add_desire("很久以前的念头", ago_h=60)
        self.assertEqual(await self.memory.get_live_desires(), [])
        row = await self.db.fetchone("SELECT COUNT(*) n FROM suppressed_desires")
        self.assertEqual(row["n"], 0)

    async def test_主动消息素材不取作废念头(self):
        stack = make_engine_stack(self.db, include_proactive=True, proactive_config=ProactiveConfig())
        await self._add_desire("说说自己明天背着报告回学校", ago_h=20)
        await self.add_turn("user", "我回浙大了", ago_h=5)
        material = await stack.proactive._select_topic_material()
        self.assertNotIn("回学校", material)


class TestDatedFacts(_MemHarness):
    async def test_事实注入带记下日期_库里仍存原文(self):
        await self.memory.add_fact("打算明天开始好好学习")
        day = datetime.now().strftime("%m-%d")
        self.assertEqual(await self.memory.get_facts_for_prompt(), [f"打算明天开始好好学习（{day} 记下）"])
        self.assertEqual(await self.memory.get_all_facts(), ["打算明天开始好好学习"])

    async def test_主聊提示词事实块带日期说明(self):
        stack = make_engine_stack(self.db)
        await self.memory.add_fact("打算明天开始好好学习")
        stack.assembler.memory = self.memory
        prompt = await stack.assembler.assemble_system_prompt("在吗")
        self.assertIn("记下）", prompt)
        self.assertIn("按那天算", prompt)


class TestReplyGapNote(_MemHarness):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.stack = make_engine_stack(self.db, include_proactive=True, proactive_config=ProactiveConfig())

    async def test_他多天没回时给出间隔与未回条数(self):
        await self.add_turn("user", "嗯呢", ago_h=24 * 9)
        await self.add_turn("assistant", "七点起了吗", ago_h=24 * 8, proactive=1)
        await self.add_turn("assistant", "七点计划还算数吗", ago_h=24 * 2, proactive=1)
        note = await self.stack.proactive._reply_gap_note()
        self.assertIn("9 天前", note)
        self.assertIn("2 条他都还没回", note)
        self.assertIn("不要换个说法再问一遍", note)

    async def test_他最后说话时不加提示(self):
        await self.add_turn("assistant", "在吗", ago_h=3, proactive=1)
        await self.add_turn("user", "在", ago_h=1)
        self.assertEqual(await self.stack.proactive._reply_gap_note(), "")


class TestObserverKnowsNow(_MemHarness):
    async def test_观察者输入带当前时间与日期写法纪律(self):
        gateway = make_mock_gateway(chat_return_value='{"facts": []}')
        stack = make_engine_stack(self.db, gateway=gateway)
        obs = Observer(gateway, stack.affection, stack.mood, stack.memory, stack.stickers, self.db)
        await obs.settle_turn("明天我七点起", "好")
        msgs = gateway.chat.call_args.kwargs["messages"]
        self.assertIn("【现在】" + datetime.now().strftime("%Y-%m-%d"), msgs[-1]["content"])
        self.assertIn("换算成具体日期", msgs[0]["content"])


# ==========================================
# 4. 好感度日衰减零头累计
# ==========================================


class TestDecayAccumulates(_MemHarness):
    async def _set_state(self, **extra):
        eng = AffectionEngine(self.db)
        st = await eng.get_state()
        st.update(extra)
        await eng.save_state(st)
        return eng

    async def test_每天都聊也会按整天衰减(self):
        # 锚点在 47 小时前、上次结算在 23 小时前：旧实现按 last_updated 算 0 天，
        # 新实现按锚点算 1 天，锚点前移 24h（剩 23h 零头留给下次）
        eng = await self._set_state(last_updated=_ts(hours=23), decay_anchor=_ts(hours=47))
        await eng.update(4.0, 4.0, 4.0, 4.0, [])
        st = await self.db.get_state_json(STATE_KEY_AFFECTION)
        anchor = datetime.strptime(st["decay_anchor"], TIME_FORMAT)
        self.assertAlmostEqual((datetime.now() - anchor).total_seconds() / 3600, 23, delta=0.1)

    async def test_不满一天不动锚点(self):
        eng = await self._set_state(last_updated=_ts(hours=5), decay_anchor=_ts(hours=10))
        before = (await self.db.get_state_json(STATE_KEY_AFFECTION))["decay_anchor"]
        await eng.update(5.0, 5.0, 5.0, 5.0, [])
        self.assertEqual((await self.db.get_state_json(STATE_KEY_AFFECTION))["decay_anchor"], before)

    async def test_老状态没有锚点时按last_updated起算(self):
        eng = await self._set_state(last_updated=_ts(hours=50))
        await eng.update(5.0, 5.0, 5.0, 5.0, [])
        st = await self.db.get_state_json(STATE_KEY_AFFECTION)
        anchor = datetime.strptime(st["decay_anchor"], TIME_FORMAT)
        self.assertAlmostEqual((datetime.now() - anchor).total_seconds() / 3600, 2, delta=0.1)


# ==========================================
# 5. reset 备份进轮转 / 冒烟脚本路径
# ==========================================


class TestBackupRotation(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "c.db")
        open(self.db, "wb").close()
        self.bdir = os.path.join(self.tmp, "daily")
        os.makedirs(self.bdir)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_带秒的reset备份也纳入轮转(self):
        for i in range(5):
            open(os.path.join(self.bdir, f"companion-2020010{i + 1}-183235.db"), "wb").close()
        run_daily_backup(self.db, self.bdir, max_keep=3)
        left = sorted(f for f in os.listdir(self.bdir) if f != "latest.db")
        self.assertEqual(len(left), 3)
        self.assertNotIn("companion-20200101-183235.db", left, "最旧的 reset 备份应被清掉")


class TestSmokeScriptPaths(unittest.TestCase):
    def test_通用沙箱与冒烟路径都存在(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        for rel in ("scripts/sim/full_sandbox.py", "scripts/smoke/smoke_continuity.py"):
            path = os.path.join(root, rel)
            with open(path, encoding="utf-8") as f:
                src = f.read()
            self.assertIn("companion", src, rel)
        self.assertTrue(os.path.exists(os.path.join(root, "scripts", "sim", "full_sandbox.py")))


class TestObserverTopicFacts(_MemHarness):
    async def test_topic键的fact不再被丢_附加日期拼在括号里(self):
        import json as _json
        payload = {"facts": [
            {"topic": "室友最近在学尤克里里"},
            {"topic": "喜欢听弦乐讲座", "time": "2026-10-12 至 2026-10-18 期间"},
        ]}
        gateway = make_mock_gateway(chat_return_value=_json.dumps(payload, ensure_ascii=False))
        stack = make_engine_stack(self.db, gateway=gateway)
        obs = Observer(gateway, stack.affection, stack.mood, stack.memory, stack.stickers, self.db)
        await obs.settle_turn("室友在学尤克里里，我上周去听了弦乐讲座", "挺好")
        facts = await stack.memory.get_all_facts()
        self.assertIn("室友最近在学尤克里里", facts)
        self.assertIn("喜欢听弦乐讲座（2026-10-12 至 2026-10-18 期间）", facts)


class TestOptionalBadExamples(_MemHarness):
    async def test_没有反面教材时整段不渲染(self):
        stack = make_engine_stack(self.db)
        stack.persona.chat_style.bad_examples = []
        prompt = await stack.assembler.assemble_system_prompt("在吗")
        self.assertNotIn("错误示范", prompt)
        self.assertIn("她的具体说话习惯", prompt)


if __name__ == "__main__":
    unittest.main()
