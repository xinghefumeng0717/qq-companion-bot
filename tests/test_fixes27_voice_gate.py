"""FIXES27：语音闸门的睡眠误判，以及"她不知道自己没发出去"。

两个生产问题（已诊断，本文件只钉修复）：
1. 作息文案「在缘缘庄睡着」命中 voice_busy_words 的「睡着」，语音闸门整夜关死；
   主人 00:24 明确讨要语音，她回得像答应了，语音却永远出不去。
2. 提示词层完全不告诉她语音功能，她不知道自己"没发出去"；且系统当她睡着，
   她却说"我压根没睡"，自相矛盾。

覆盖：对话轮次的醒着例外、点名讨要只越过作息腿（开关/日上限照常）、
提示词三态 voice_block、作息醒着注记、主动消息决策层的"同一叮嘱不重复"。
"""

import asyncio
import inspect
import os
import shutil
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
# 与 test_fixes22 同款：helpers 是 tests/ 下的模块，直接从包外 import
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

from companion.config import TTSConfig  # noqa: E402
from companion.prompts import (  # noqa: E402
    PROACTIVE_DECISION_PROMPT,
    VOICE_PLANNER_RULES,
    VOICE_UNAVAILABLE_NOTE,
)
from companion.tts import TTSManager, activity_allows_voice  # noqa: E402

# 生产上真实出现的那条作息文案：卡里的「睡着」是 voice_busy_words 的忙词。
SLEEP_ACTIVITY = "在缘缘庄睡着，雷打不动"


def _line_before_routine(prompt: str) -> str:
    """【她此刻】上面那一行——模板里它就是 {voice_block} 独占的一行。

    默认参数下 voice_block 是空串，所以这一行必须是空行：这是"默认输出与改造前
    逐字节一致"的结构性证据（注入的字节数不多不少就是 0）。
    """
    lines = prompt.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("【她此刻】"):
            return lines[i - 1] if i else ""
    raise AssertionError("提示词里找不到【她此刻】行，模板结构变了")


# ==========================================
# 1. activity_allows_voice：睡眠词例外只对睡眠词生效
# ==========================================


class TestAwakeOverride(unittest.TestCase):
    def test_默认参数与改造前一致(self):
        """不加 awake_override 时行为逐字节不动（回归）：睡眠词照样拦。"""
        self.assertFalse(inspect.signature(activity_allows_voice)
                         .parameters["awake_override"].default)

        ok, why = activity_allows_voice("已经睡下了，在宿舍安静的梦乡中")
        self.assertFalse(ok)
        self.assertIn("睡下", why)

        ok2, _ = activity_allows_voice("")
        self.assertTrue(ok2)

    def test_睡眠词加醒着override放行(self):
        ok, why = activity_allows_voice(
            SLEEP_ACTIVITY, ("睡着",), awake_override=True
        )
        self.assertTrue(ok, why)
        self.assertIn("对话中轮次", why)

    def test_睡眠词不带override仍拦(self):
        ok, why = activity_allows_voice(SLEEP_ACTIVITY, ("睡着",))
        self.assertFalse(ok, "睡眠词没被拦住，回归了 FIXES27 要修的那个洞")
        self.assertIn("睡着", why)

    def test_非睡眠忙词加override也仍拦(self):
        """"在聊天"不是万能钥匙：上课/合练/对账这些真忙必须继续拦。"""
        ok, why = activity_allows_voice("在教室上课", awake_override=True)
        self.assertFalse(ok, why)
        ok2, why2 = activity_allows_voice(
            "在办公室对账", ("对账",), awake_override=True
        )
        self.assertFalse(ok2, why2)
        self.assertIn("对账", why2)


# ==========================================
# 2. TTSManager.check_gate：只给作息腿开两扇门
# ==========================================


class TestCheckGate(unittest.TestCase):
    def test_醒着override放行睡眠作息(self):
        tts = TTSManager(TTSConfig(enabled=True), voice_busy_words=("睡着",))
        ok, why = asyncio.run(tts.check_gate(SLEEP_ACTIVITY, awake_override=True))
        self.assertTrue(ok, why)

    def test_睡眠作息不override仍然拦(self):
        tts = TTSManager(TTSConfig(enabled=True), voice_busy_words=("睡着",))
        ok, why = asyncio.run(tts.check_gate(SLEEP_ACTIVITY))
        self.assertFalse(ok)
        self.assertIn("作息闸门", why)

    def test_点名要语音越过作息腿(self):
        tts = TTSManager(TTSConfig(enabled=True), voice_busy_words=("睡着",))
        ok, why = asyncio.run(tts.check_gate(SLEEP_ACTIVITY, user_requested=True))
        self.assertTrue(ok, why)
        self.assertIn("点名", why)

    def test_点名不越过总开关(self):
        tts = TTSManager(TTSConfig(enabled=False), voice_busy_words=("睡着",))
        ok, why = asyncio.run(tts.check_gate(SLEEP_ACTIVITY, user_requested=True))
        self.assertFalse(ok, "开关是保险丝，点名也不能绕过")
        self.assertIn("开关", why)

    def test_点名不越过日上限(self):
        from helpers import close_db, make_db

        async def run():
            db = await make_db()
            try:
                tts = TTSManager(
                    TTSConfig(enabled=True, daily_limit=1),
                    db,
                    voice_busy_words=("睡着",),
                )
                await tts.bump_daily()  # 今日额度用满
                return await tts.check_gate(SLEEP_ACTIVITY, user_requested=True)
            finally:
                await close_db(db)

        ok, why = asyncio.run(run())
        self.assertFalse(ok, "日上限是保险丝，点名也不能绕过")
        self.assertIn("上限", why)


# ==========================================
# 3. TurnHandler._check_voice_gate：传参语义
# ==========================================


class _RecordingTTS:
    """只记录 check_gate 收到的参数，不真合成。"""

    class config:  # noqa: N801 - 对齐 TTSManager.config 的鸭子类型
        enabled = True

    def __init__(self):
        self.calls = []

    async def check_gate(self, activity="", awake_override=False, user_requested=False):
        self.calls.append((activity, awake_override, user_requested))
        return True, "ok"


class _StubPersona:
    def get_current_activity(self, hour, weekday=None, holiday_span=0):
        return SLEEP_ACTIVITY


class _StubAssembler:
    persona = _StubPersona()


def _handler(tts=None):
    """只够跑 _check_voice_gate 的 TurnHandler 壳（绕过重型构造函数）。"""
    from companion.turn_handler import TurnHandler

    handler = object.__new__(TurnHandler)
    handler.assembler = _StubAssembler()
    if tts is not None:
        handler.tts = tts  # 不传就模拟"老装配没有 tts 属性"
    return handler


class TestTurnHandlerVoiceGate(unittest.TestCase):
    def test_对话轮次恒按醒着传参(self):
        """handle_turn 是"他正在跟她聊"的轮次，awake_override 必须恒 True。"""
        tts = _RecordingTTS()
        asyncio.run(_handler(tts)._check_voice_gate("在吗"))
        self.assertEqual(tts.calls[-1], (SLEEP_ACTIVITY, True, False))

    def test_点名讨要语音带上user_requested(self):
        tts = _RecordingTTS()
        handler = _handler(tts)
        for text in ("发个语音给我", "让我听听你的声音", "唱一句嘛"):
            with self.subTest(text=text):
                asyncio.run(handler._check_voice_gate(text))
                self.assertEqual(tts.calls[-1][2], True, f"没认出讨要语音：{text}")

    def test_普通聊天不算讨要语音(self):
        tts = _RecordingTTS()
        handler = _handler(tts)
        for text in ("在吗", "今天好累", ""):
            with self.subTest(text=text):
                asyncio.run(handler._check_voice_gate(text))
                self.assertEqual(tts.calls[-1][2], False, f"误判成讨要语音：{text}")

    def test_没有TTSManager仍一律False(self):
        ok, why = asyncio.run(_handler(None)._check_voice_gate("发个语音"))
        self.assertFalse(ok)
        self.assertIn("TTSManager", why)


# ==========================================
# 4. assembler：voice_block 三态 + 作息醒着注记
# ==========================================


def _assemble(persona_path, user_message, **kwargs):
    from helpers import close_db, make_db, make_engine_stack

    async def run():
        db = await make_db()
        try:
            stack = make_engine_stack(db, persona_path=persona_path)
            return await stack.assembler.assemble_system_prompt(user_message, **kwargs)
        finally:
            await close_db(db)

    return asyncio.run(run())


class TestVoiceBlock(unittest.TestCase):
    def setUp(self):
        from helpers import card_path

        self.card = card_path()

    def test_默认参数不注入语音说明(self):
        prompt = _assemble(self.card, "在吗")
        self.assertNotIn(VOICE_UNAVAILABLE_NOTE, prompt)
        self.assertNotIn("这会儿你不方便发语音", prompt)
        self.assertNotIn("[voice:", prompt, "默认关时模型不该认识 [voice:]")
        # 结构性证据：{voice_block} 那一行是空行 → 默认参数注入的字节数为 0
        self.assertEqual(_line_before_routine(prompt), "")

    def test_闸门没过时给不可用说明(self):
        prompt = _assemble(self.card, "在吗", voice_gated_note=True)
        self.assertIn(VOICE_UNAVAILABLE_NOTE, prompt)
        self.assertIn("这轮语音不可用", prompt)
        self.assertIn("别承诺能发", prompt)
        self.assertNotIn("[voice:", prompt, "不可用时不该教她 [voice:] 写法")
        self.assertEqual(_line_before_routine(prompt), VOICE_UNAVAILABLE_NOTE)

    def test_可用时给能力说明且不带不可用说明(self):
        prompt = _assemble(self.card, "在吗", voice_allowed=True)
        self.assertIn(VOICE_PLANNER_RULES, prompt)
        self.assertNotIn("你可以把回复中的一小段用 [voice:]", prompt)
        self.assertNotIn("这会儿你不方便发语音", prompt)


class TestSleepRoutineNote(unittest.TestCase):
    def setUp(self):
        from helpers import make_fixture_card

        self._card_dir = tempfile.mkdtemp(prefix="qqc_fixes27_card_")
        self.addCleanup(shutil.rmtree, self._card_dir, True)
        # 全天一条睡眠作息：不依赖当前几点、也不依赖私有角色卡
        make_fixture_card(
            self._card_dir,
            daily_routine=[{"start": 0, "end": 24, "activity": SLEEP_ACTIVITY}],
        )

    def test_聊天轮次补上醒着的注记(self):
        prompt = _assemble(self._card_dir, "在吗")
        self.assertIn("你其实是醒着的", prompt)

    def test_主动消息传空串不加注记(self):
        prompt = _assemble(self._card_dir, "")
        self.assertNotIn("你其实是醒着的", prompt)


# ==========================================
# 5. 主动消息决策层：同一叮嘱当天不重复
# ==========================================


class TestProactiveDecisionRepeat(unittest.TestCase):
    def test_决策层禁止当天重复叮嘱(self):
        self.assertIn("一天最多主动提一次", PROACTIVE_DECISION_PROMPT)
        self.assertIn("翻旧账", PROACTIVE_DECISION_PROMPT)
        self.assertIn("他没接茬不代表你要追着念", PROACTIVE_DECISION_PROMPT)


# ==========================================
# 6. 轻忙词表（所有者 2026-10-23 拍板「一起开」）：
#    手上有事嘴闲着的场景，对话中轮次放行；主动消息照样拦
# ==========================================


class TestLightBusyWords(unittest.TestCase):
    """light_busy_words 只活在对话中轮次（awake_override=True），且只放轻忙词。"""

    LIGHT = ("对账", "试锅底", "吃火锅", "逛市集", "翻小玩意")
    BUSY = ("作画", "调墨", "讲课", "对账", "试锅底", "出远门", "招待客人")

    def test_轻忙词对话中放行(self):
        ok, why = activity_allows_voice(
            "正在客栈对账，一笔笔对得清清楚楚", self.BUSY,
            awake_override=True, light_busy_words=self.LIGHT,
        )
        self.assertTrue(ok, why)
        self.assertIn("轻忙", why)

    def test_轻忙词主动消息仍拦(self):
        """没有 awake_override（主动消息路径）：轻忙词照拦——
        她不该吃着火锅突然发语音找他。"""
        ok, why = activity_allows_voice(
            "正在客栈对账，一笔笔对得清清楚楚", self.BUSY,
            light_busy_words=self.LIGHT,
        )
        self.assertFalse(ok, "轻忙词在主动消息里被放行了")
        self.assertIn("对账", why)

    def test_硬忙词对话中仍拦(self):
        """作画/讲课/出远门/招待客人是真走不开，对话中也拦。"""
        for activity, kw in [("在客栈后院作画，有灵感时谁叫都不理", "作画"),
                             ("出远门找新鲜事", "出远门")]:
            ok, _ = activity_allows_voice(
                activity, self.BUSY,
                awake_override=True, light_busy_words=self.LIGHT,
            )
            self.assertFalse(ok, f"硬忙词 {kw} 被误放行")

    def test_未声明轻忙词时行为不变(self):
        """light_busy_words=None（卡没写）：对账这类词即使在对话中也照拦。"""
        ok, _ = activity_allows_voice(
            "正在客栈对账，一笔笔对得清清楚楚", self.BUSY, awake_override=True
        )
        self.assertFalse(ok, "没声明轻忙词表时对话中也不该放行")

    def test_签名默认值安全(self):
        """新参数默认 None：老调用方（含 proactive._voice_gate_ok）行为不变。"""
        self.assertIsNone(inspect.signature(activity_allows_voice)
                          .parameters["light_busy_words"].default)
        self.assertIsNone(inspect.signature(TTSManager.__init__)
                          .parameters["voice_light_busy_words"].default)


class TestVoicePlannerPrompt(unittest.TestCase):
    """新合同：她只写正文，系统负责从原话选句和真实投递。"""

    def test_不额外鼓励或束缚她(self):
        self.assertIn("照常聊天", VOICE_PLANNER_RULES)
        self.assertNotIn("多发语音", VOICE_PLANNER_RULES)
        self.assertNotIn("不要写", VOICE_PLANNER_RULES)

    def test_只保留必要的投递事实(self):
        self.assertIn("系统", VOICE_PLANNER_RULES)
        self.assertIn("不要声称语音已经发出", VOICE_PLANNER_RULES)


if __name__ == "__main__":
    unittest.main()
