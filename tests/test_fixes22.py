"""FIXES22 语音回复（阶段 A）测试集 (tests/test_fixes22.py)

盯住四件事：
1. **`[voice:]` 语法与边界**：成对块、可出现在句中、未闭合/空内容当普通文字
2. **四路径降级**逐条验：开关关 / 日上限到 / 作息闸门关 / 合成失败 ——
   **降级永远保消息**（她那句话一个字都不能少），这是任务书最硬的纪律
3. **落库形态** `（语音消息）文本`：与收侧他的语音转写同格式，observer/日记只认这个
4. **日计数跨天清零** + 与 face/sticker/quote 同轮共存 + proactive 通路共用账目

全部本地构造，**零真实合成**（真合成在 scripts/smoke_fixes22.py，
mp3 留给所有者亲耳试听）。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))

from companion.config import ReplyConfig, TTSConfig
from companion.main import CompanionBot
from companion.onebot import build_record_segment
from companion.prompts import (
    SYSTEM_PROMPT_TEMPLATE,
    VOICE_PROMPT_BLOCK,
    VOICE_USAGE_RULES,
)
from companion.replier import (
    Replier,
    chunk_record_text,
    keep_first_voice,
    strip_voice_markers,
    voice_chunk,
)
from companion.tts import (
    BLOCK_KEYWORDS,
    STATE_KEY_TTS_DAILY,
    TTSManager,
    activity_allows_voice,
    truncate_for_voice,
)


class _StubStickers:
    def match_sticker(self, desc: str):
        return f"/fake/stickers/{desc}.png"

    def get_prompt_sticker_list(self):
        return ["猫猫"]


def _replier() -> Replier:
    return Replier(ReplyConfig(), _StubStickers())


def _types(chunks) -> List[str]:
    return [c["type"] for c in chunks]


# ==========================================
# 1. 默认关（拍板：新功能先装死）
# ==========================================


class TestDefaultOff(unittest.TestCase):
    def test_默认配置是关的(self):
        cfg = TTSConfig()
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.daily_limit, 3)
        self.assertEqual(cfg.max_chars, 60)
        self.assertEqual(cfg.voice, "zh-CN-XiaoxiaoNeural")

    def test_现网config_tomll不带tts段也是关的(self):
        """服务器 config.toml 零改动是拍板项：缺段必须安全（= 关着）。"""
        from companion.config import Config

        cfg = Config.load("config.example.toml")
        self.assertFalse(cfg.tts.enabled)

    def test_parse_reply默认voice_allowed为False(self):
        """旧调用方不传这个参数 = 语音一律降级，不改一个字符也安全。"""
        chunks, record = _replier().parse_reply("在呢[voice:睡啦[/voice]")
        self.assertEqual(_types(chunks), ["text"])
        self.assertIn("[voice:睡啦[/voice]", record)


# ==========================================
# 2. [voice:] 语法与边界
# ==========================================


class TestVoiceSyntax(unittest.TestCase):
    def test_句中一段被提成语音段(self):
        chunks, record = _replier().parse_reply(
            "在呢[voice:刚练完琴 手指有点僵[/voice]你说啥", voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["text", "voice", "text"])
        self.assertEqual(chunks[1]["content"], "刚练完琴 手指有点僵")
        self.assertEqual(
            record, "在呢\n（语音消息）刚练完琴 手指有点僵\n你说啥",
            "落库形态必须与收侧他的语音转写同格式",
        )

    def test_整条就是语音(self):
        chunks, record = _replier().parse_reply(
            "[voice:睡啦 明天聊[/voice]", voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["voice"])
        self.assertEqual(record, "（语音消息）睡啦 明天聊")

    def test_未闭合当普通文字(self):
        raw = "在呢[voice:刚练完琴 你说啥"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(record, raw)

    def test_只有闭标记当普通文字(self):
        raw = "在呢[/voice]你说啥"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(record, raw)

    def test_空内容不建语音段(self):
        raw = "在呢[voice:][/voice]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(record, raw)

    def test_中文冒号容错(self):
        chunks, _ = _replier().parse_reply("[voice：睡了[/voice]", voice_allowed=True)
        self.assertEqual(_types(chunks), ["voice"])

    def test_每轮最多一条语音(self):
        chunks, record = _replier().parse_reply(
            "[voice:一[/voice]\n[voice:二[/voice]", voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["voice", "text"])
        self.assertEqual(chunks[0]["content"], "一")
        self.assertIn("二", record, "多余那条按文字降级，不丢弃")

    def test_与face同轮共存(self):
        chunks, record = _replier().parse_reply(
            "在呢[face:憨笑][voice:刚醒[/voice]", voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["combo", "voice"])
        self.assertEqual(record, "在呢[face:憨笑]\n（语音消息）刚醒")

    def test_与sticker同轮共存(self):
        chunks, _ = _replier().parse_reply(
            "[sticker:猫猫]\n[voice:晚安[/voice]", voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["sticker", "voice"])

    def test_与引用同轮共存(self):
        """引用贴在正文头部（FIXES21），语音是独立一条消息（FIXES22）——互不干扰。"""
        batch = [{"index": 1, "text": "在吗", "message_id": 9001}]
        chunks, _rec = _replier().parse_reply(
            "[quote:1]在呢[voice:刚醒[/voice]", quote_targets=batch, voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["text", "voice"])
        self.assertEqual(chunks[0].get("_quote"), {"index": 1, "message_id": 9001})
        self.assertEqual(chunks[1]["content"], "刚醒")

    def test_沉默优先于语音(self):
        """[沉默] 与 [voice:] 同现时沉默赢：沉默是整轮判定，先于任何标记拆分。"""
        chunks, record = _replier().parse_reply("[沉默]", voice_allowed=True)
        self.assertEqual(chunks, [])
        self.assertEqual(record, "")

    def test_记录形态与收侧对称(self):
        from companion.tts import TTSManager as T

        self.assertEqual(T.record_text("睡了"), "（语音消息）睡了")
        self.assertEqual(
            chunk_record_text({"type": "voice", "content": "睡了"}),
            "（语音消息）睡了",
        )
        self.assertEqual(strip_voice_markers("[voice:睡了[/voice]"), "（语音消息）睡了")

    def test_语音不拉长typing时长(self):
        """没人一边打字一边发语音：语音段不该计入打字表演的字数。"""
        self.assertEqual(strip_voice_markers("在呢[voice:睡了[/voice]"), "在呢（语音消息）睡了")

    def test_voice_chunk与上限函数(self):
        self.assertEqual(
            voice_chunk("在吗", True), {"type": "voice", "content": "在吗"}
        )
        self.assertIsNone(voice_chunk("在吗", False))
        self.assertIsNone(voice_chunk("  ", True))
        out = keep_first_voice([
            {"type": "voice", "content": "a"},
            {"type": "voice", "content": "b"},
        ])
        self.assertEqual([c["type"] for c in out], ["voice", "text"])


# ==========================================
# 3. 四路径降级（任务2 第2条）
# ==========================================


class TestDegradation(unittest.TestCase):
    def test_路径1_开关关(self):
        tts = TTSManager(TTSConfig(enabled=False))
        ok, why = asyncio.run(tts.check_gate("回寝室洗漱，看手机聊天"))
        self.assertFalse(ok)
        self.assertIn("开关", why)

    def test_路径2_日上限到(self):
        from helpers import close_db, make_db

        async def run():
            db = await make_db()
            try:
                tts = TTSManager(TTSConfig(enabled=True, daily_limit=2), db)
                ok1, _ = await tts.check_gate("在临湖吃饭")
                await tts.bump_daily()
                await tts.bump_daily()
                ok2, why2 = await tts.check_gate("在临湖吃饭")
                return ok1, ok2, why2
            finally:
                await close_db(db)

        ok1, ok2, why2 = asyncio.run(run())
        self.assertTrue(ok1)
        self.assertFalse(ok2)
        self.assertIn("上限", why2)

    def test_路径3_作息闸门关(self):
        tts = TTSManager(TTSConfig(enabled=True))
        # 本卡作息：合练时她手机静音，不该被打扰
        ok, why = asyncio.run(
            tts.check_gate("蒙民伟楼大排练厅进行文琴交响乐团全团合练，手机静音")
        )
        self.assertFalse(ok)
        self.assertIn("作息", why)

    def test_路径4_合成失败(self):
        """合成器抛异常 → 返回 None → 调用方按文字发，**消息不丢**。"""
        tts = TTSManager(TTSConfig(enabled=True))
        tts._import_edge_tts = lambda: (_ for _ in ()).throw(RuntimeError("网络炸了"))
        self.assertIsNone(asyncio.run(tts.synthesize("在吗")))

    def test_合成超时也不抛(self):
        class _SlowCommunicate:
            def __init__(self, *a, **k):
                pass

            async def save(self, path):
                await asyncio.sleep(30)

        class _FakeEdge:
            Communicate = _SlowCommunicate

        tts = TTSManager(TTSConfig(enabled=True))
        tts._import_edge_tts = lambda: _FakeEdge
        import companion.tts as tts_mod

        old = tts_mod.SYNTH_TIMEOUT
        tts_mod.SYNTH_TIMEOUT = 0.05
        try:
            self.assertIsNone(asyncio.run(tts.synthesize("在吗")))
        finally:
            tts_mod.SYNTH_TIMEOUT = old

    def test_降级后她的原话一字不少(self):
        """四道闸门任一不过，屏幕上都不能少字。"""
        raw = "在呢[voice:刚练完琴 手指有点僵[/voice]你说啥"
        chunks, record = _replier().parse_reply(raw, voice_allowed=False)
        self.assertEqual(_types(chunks), ["text"])
        self.assertEqual(record, raw, "降级必须原样照发，不能改写她的话")

    def test_降级时不开成两个气泡(self):
        """不可用时用不带 voice 分支的正则：原句一行就是一条消息。"""
        raw = "在呢[voice:刚练完琴[/voice]你说啥"
        chunks, _ = _replier().parse_reply(raw, voice_allowed=False)
        self.assertEqual(len(chunks), 1)


# ==========================================
# 4. 作息闸门词表（任务3 第1条）
# ==========================================


class TestActivityGate(unittest.TestCase):
    def test_任务书原词表都在(self):
        for kw in ("上课", "合排", "合练", "排练", "图书馆", "考试", "讲座", "熄灯"):
            with self.subTest(kw=kw):
                self.assertIn(kw, BLOCK_KEYWORDS)

    def test_本卡真实文案会命中(self):
        """闸门必须对本卡真实作息文案真的响，否则等于装了个死开关。"""
        blocked = [
            "教学区西教连上专业必修（古代汉语与文学经典精读），课间看一眼手机",
            "下午安中大楼上通识选修课，在自动贩卖机买乌龙茶",
            "蒙民伟楼大排练厅进行文琴交响乐团全团合练，手机静音",
            "周��四下午独奏练琴时间！独自在蒙民伟楼个人琴房练大提琴独奏",
            "在基础馆自习室刷文献、做笔记",
            "整理谱子上的笔记，准备熄灯休息",
            "已经睡下了，在示例大学宿舍安静的梦乡中",
        ]
        for act in blocked:
            with self.subTest(act=act):
                ok, why = activity_allows_voice(act)
                self.assertFalse(ok, f"这条作息没被拦住：{act}")

    def test_自由活动放行(self):
        for act in (
            "回寝室洗漱，吃点水果，看手机聊天",
            "启真湖边散步吹风，西区海纳食堂吃饭",
            "周末夜晚在宿舍泡杯花果茶看文学书、写手账，最适合深度聊天",
            "迎接收假前的极度松弛：在寝室阳台看夕阳、校内咖啡馆发呆、启真湖边看黑天鹅",
        ):
            with self.subTest(act=act):
                ok, _ = activity_allows_voice(act)
                self.assertTrue(ok, f"这条作息被误伤了：{act}")

    def test_晚自习例外(self):
        """卡里明说"手机在手边较为空闲"，不能被"自习"两个字误伤。"""
        ok, why = activity_allows_voice("晚自习时间，手机在手边较为空闲")
        self.assertTrue(ok)
        self.assertIn("例外", why)

    def test_拿不到活动文案按放行(self):
        ok, _ = activity_allows_voice("")
        self.assertTrue(ok)

    def test_六十字截断切在句读(self):
        """宁可短不可长：切在最近句读上，不把半句话塞进语音。"""
        long = "我今天真的特别特别累，从早到晚没停过。现在只想瘫着。明天还要早起。"
        out = truncate_for_voice(long, 30)
        self.assertLessEqual(len(out), 30)
        self.assertTrue(out.endswith(("，", "。", "！", "？")))
        self.assertLessEqual(len(out), 31)
        self.assertNotIn("明天还要早起", out)

    def test_短文本不截(self):
        self.assertEqual(truncate_for_voice("睡了", 60), "睡了")

    def test_无句读时硬截加省略号(self):
        long = "啊" * 100
        out = truncate_for_voice(long, 60)
        self.assertTrue(out.endswith("…"))
        self.assertLessEqual(len(out), 61)


# ==========================================
# 5. 日计数（任务1 第5条）
# ==========================================


class TestDailyCount(unittest.TestCase):
    def test_跨天自动清零(self):
        from datetime import datetime, timedelta

        from helpers import close_db, make_db

        async def run():
            db = await make_db()
            try:
                tts = TTSManager(TTSConfig(enabled=True, daily_limit=3), db)
                await tts.bump_daily()
                await tts.bump_daily()
                used_before, _ = await tts.daily_status()
                # 往回写一天：模拟"昨天发过两条"
                yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
                await db.set_state_json(
                    STATE_KEY_TTS_DAILY, {"date": yesterday, "count": 2}
                )
                used_after, limit = await tts.daily_status()
                return used_before, used_after, limit
            finally:
                await close_db(db)

        before, after, limit = asyncio.run(run())
        self.assertEqual(before, 2)
        self.assertEqual(after, 0, "跨天没清零：她会被昨天的额度挡住")
        self.assertEqual(limit, 3)

    def test_脏数据不炸(self):
        from helpers import close_db, make_db

        async def run():
            db = await make_db()
            try:
                tts = TTSManager(TTSConfig(enabled=True), db)
                await db.set_state_json(STATE_KEY_TTS_DAILY, {"date": "x", "count": "脏"})
                return await tts.daily_status()
            finally:
                await close_db(db)

        used, _limit = asyncio.run(run())
        self.assertEqual(used, 0)

    def test_无db也能跑(self):
        tts = TTSManager(TTSConfig(enabled=True))
        ok, _ = asyncio.run(tts.check_gate("回寝室洗漱"))
        self.assertTrue(ok)
        self.assertEqual(asyncio.run(tts.bump_daily()), 1)


# ==========================================
# 6. 发送结构（任务2 第3/4条）
# ==========================================


class _RecordingOneBot:
    def __init__(self):
        self.sent: List[Dict[str, Any]] = []

    async def send_private_msg(self, user_id, message_segments, max_retries=2):
        self.sent.append({"user_id": user_id, "message": message_segments})
        return True


class _StubBot(CompanionBot):
    def __init__(self, onebot, tts=None):  # noqa: D107 - 只借发送逻辑
        self.config = type(
            "C", (), {"account": type("A", (), {"allowed_user_id": 10001})()}
        )()
        self.onebot = onebot
        self.tts = tts


class _FakeTTS:
    """假 TTS：产出固定文件，测"发完要删"与计数，不真合成"""

    def __init__(self, path: str, bump: int = 1, fail: bool = False):
        self.path = path
        self.bump = bump
        self.fail = fail
        self.cleaned: List[str] = []

    async def synthesize(self, text):
        if self.fail:
            return None
        with open(self.path, "wb") as f:
            f.write(b"ID3" + b"\0" * 512)
        return self.path

    async def bump_daily(self):
        return self.bump

    def cleanup(self, path):
        self.cleaned.append(path)
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except Exception:
            pass


class TestSendVoice(unittest.TestCase):
    def setUp(self):
        self.dir = "data/test_fixes22_audio"
        os.makedirs(self.dir, exist_ok=True)

    def test_record段是base64(self):
        p = os.path.join(self.dir, "probe.mp3")
        with open(p, "wb") as f:
            f.write(b"ID3-fake-audio")
        seg = build_record_segment(p)
        self.assertEqual(seg["type"], "record")
        self.assertTrue(seg["data"]["file"].startswith("base64://"))
        os.remove(p)

    def test_合成发送删临时文件三步都对(self):
        ob = _RecordingOneBot()
        tts = _FakeTTS(os.path.join(self.dir, "a.mp3"), bump=2)
        bot = _StubBot(ob, tts)
        asyncio.run(bot._send_chunk_to_onebot({"type": "voice", "content": "睡了"}))
        self.assertEqual(len(ob.sent), 1)
        self.assertEqual(ob.sent[0]["message"][0]["type"], "record")
        self.assertEqual(len(tts.cleaned), 1, "临时文件必须删")
        self.assertFalse(os.path.exists(tts.cleaned[0]), "临时文件真的还在")

    def test_合成失败按文字发出(self):
        ob = _RecordingOneBot()
        tts = _FakeTTS(os.path.join(self.dir, "b.mp3"), fail=True)
        bot = _StubBot(ob, tts)
        asyncio.run(bot._send_chunk_to_onebot({"type": "voice", "content": "睡了"}))
        self.assertEqual(ob.sent[0]["message"], [{"type": "text", "data": {"text": "睡了"}}],
                         "合成失败必须把话发出去，不能静默丢消息")

    def test_没有TTSManager时按文字发(self):
        ob = _RecordingOneBot()
        bot = _StubBot(ob, None)
        asyncio.run(bot._send_chunk_to_onebot({"type": "voice", "content": "睡了"}))
        self.assertEqual(ob.sent[0]["message"], [{"type": "text", "data": {"text": "睡了"}}])

    def test_空内容不发(self):
        ob = _RecordingOneBot()
        bot = _StubBot(ob, _FakeTTS(os.path.join(self.dir, "c.mp3")))
        asyncio.run(bot._send_chunk_to_onebot({"type": "voice", "content": "  "}))
        self.assertEqual(ob.sent, [])


# ==========================================
# 7. 提示词（任务3 第2条）
# ==========================================


class TestVoicePrompt(unittest.TestCase):
    def test_模板留注入点(self):
        self.assertIn("{voice_block}", SYSTEM_PROMPT_TEMPLATE)

    def test_纪律句含三个要点(self):
        for kw in ("一小段", "一天最多", "不是把整段回复念出来"):
            with self.subTest(kw=kw):
                self.assertIn(kw, VOICE_USAGE_RULES)

    def test_示范是接新话不是念全文(self):
        """FIXES21 的教训：示范要挑真实对话里她会怎么开口，不能教复读。"""
        self.assertIn("[voice:", VOICE_PROMPT_BLOCK)
        self.assertIn("别连着发好几段", VOICE_PROMPT_BLOCK)

    def test_关着时提示词里没有voice(self):
        from helpers import close_db, make_db, make_engine_stack

        async def run():
            db = await make_db()
            try:
                stack = make_engine_stack(db, persona_path=os.path.join("characters", "qingzi"))
                _msgs, prompt = await stack.assembler.assemble_messages("在吗", None, [], False)
                return prompt
            finally:
                await close_db(db)

        prompt = asyncio.run(run())
        self.assertNotIn("[voice:]", prompt, "默认关时模型不该认识 [voice:]")

    def test_开着时提示词里有voice(self):
        from helpers import close_db, make_db, make_engine_stack

        async def run():
            db = await make_db()
            try:
                stack = make_engine_stack(db, persona_path=os.path.join("characters", "qingzi"))
                _msgs, prompt = await stack.assembler.assemble_messages("在吗", None, [], True)
                return prompt
            finally:
                await close_db(db)

        prompt = asyncio.run(run())
        self.assertIn("[voice:]", prompt)
        self.assertIn("一天最多", prompt)

    def test_她此刻仍独立成行(self):
        """FIXES21 踩过：【她此刻】被插进来的块挤到行中间，既有守卫报过一次。"""
        from helpers import close_db, make_db, make_engine_stack

        async def run():
            db = await make_db()
            try:
                stack = make_engine_stack(db, persona_path=os.path.join("characters", "qingzi"))
                _msgs, prompt = await stack.assembler.assemble_messages("在吗", None, [], True)
                return prompt
            finally:
                await close_db(db)

        prompt = asyncio.run(run())
        self.assertTrue(
            any(ln.startswith("【她此刻】") for ln in prompt.splitlines()),
            "【她此刻】必须仍在行首（开语音时也别被挤掉）",
        )


if __name__ == "__main__":
    unittest.main()
