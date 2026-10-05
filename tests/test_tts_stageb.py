"""TTS 阶段 B 测试集 (tests/test_tts_stageb.py)

盯住三件事：
1. **DEEP_AUDIT B-1 回归**：`[voice:]` 后同一行紧跟 `[face:X]` 时，合并逻辑
   不许把语音整段吃掉（内容丢失 + 落库同步丢失）。危险顺序此前无覆盖。
2. **provider 适配层**：edge（默认，阶段 A 保留）/ minimax（海螺温柔学姐）切换、
   MiniMax t2a_v2 请求体构造、endpoint 拼接、失败一律降级不抛。
3. **配置解析**：`[tts]` 缺 `provider`/minimax 字段时的向后兼容
   （服务器 config.toml 零改动要能起，且默认 edge + 关着）。

全部本地构造，**零真实合成**（真合成在 scripts/smoke/smoke_tts_stageb.py）。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import tomllib
import unittest
from dataclasses import fields
from typing import Any, Dict, List

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if os.path.join(_REPO_ROOT, "tests") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "tests"))
# 测量工具（duo_sim / benchmark_v4）在 scripts/sim 下，不在包路径上
if os.path.join(_REPO_ROOT, "scripts", "sim") not in sys.path:
    sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts", "sim"))

from companion.config import Config, ModelPreset, ReplyConfig, TTSConfig
from companion.replier import Replier, chunk_record_text
from companion.tts import (
    TTSManager,
    build_minimax_payload,
    extract_minimax_audio,
    minimax_endpoint,
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
# 1. DEEP_AUDIT B-1 回归：voice 不许被 face 合并吃掉
# ==========================================


class TestB1VoiceNotEatenByFace(unittest.TestCase):
    """B-1：`[voice:…[/voice]` 后同一行紧跟 `[face:X]` 时语音整段蒸发。

    病灶在 `merge_face_chunks`：它只把 `sticker`/`quote` 排除在合并之外，
    于是 voice 段被当成"已经是 combo 的前段"，`out[-1]` 被一个只含脸的新 combo
    覆盖——她说的那句话既没发出去、也没进落库记录。语音+脸同行不是幻想形态：
    提示词示范就在教"先说一句、再来 [voice:]"，收尾接个表情极自然。
    """

    def test_语音后紧跟同行表情不丢内容(self):
        raw = "[voice:晚安 明天聊[/voice][face:doge]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertIn("voice", _types(chunks), "语音段被吃掉了（B-1 复发）")
        self.assertEqual(chunks[0]["type"], "voice")
        self.assertEqual(chunks[0]["content"], "晚安 明天聊")
        self.assertEqual(
            record, "（语音消息）晚安 明天聊\n[face:doge]",
            "落库记录里她的那句话不能消失（observer/日记靠它认人）",
        )

    def test_语音后紧跟两个脸也不丢内容(self):
        raw = "[voice:刚练完[/voice][face:赞][face:赞]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertEqual(_types(chunks), ["voice", "combo"])
        self.assertEqual(record, "（语音消息）刚练完\n[face:赞][face:赞]")

    def test_安全顺序_脸在前语音在后照旧(self):
        """B-1 修复不许动既有的安全顺序（face 挂句尾是主形态）。"""
        chunks, record = _replier().parse_reply(
            "在呢[face:憨笑][voice:刚醒[/voice]", voice_allowed=True
        )
        self.assertEqual(_types(chunks), ["combo", "voice"])
        self.assertEqual(record, "在呢[face:憨笑]\n（语音消息）刚醒")

    def test_中间隔文字时照旧各自成段(self):
        """中间隔了文字则安全（B-1 的既有对照）——嗯 与脸合成 combo，语音独立。"""
        raw = "[voice:晚安 明天聊[/voice] 嗯[face:doge]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertEqual(_types(chunks), ["voice", "combo"])
        self.assertEqual(
            record, "（语音消息）晚安 明天聊\n嗯[face:doge]"
        )

    def test_气泡数按最终段数算(self):
        """教训 10：段数预算算的是**最终气泡数**，voice 不参与合并故各占一条。

        max_chunks=5 时 "[voice][face]" 是 2 条气泡，两条都要留下。
        """
        raw = "[voice:晚安 明天聊[/voice][face:doge]"
        chunks, _rec = _replier().parse_reply(
            raw, voice_allowed=True,
        )
        self.assertEqual(len(chunks), 2)
        self.assertLessEqual(len(chunks), 5)

    def test_超编时语音不被当填充文本挤掉(self):
        """同族病（B-1 的邻居）：`fit_chunks` 原先把 voice 归进"普通文本段"配额。

        6 条气泡超编时，她**说出口的那句**被当成填充句静默丢弃——
        屏幕上没有、落库也没有，与 B-1 同一种内容丢失。语音是模型明确要求的
        整条内容，应与表情包/表情同列优先保。
        """
        raw = "嗯。\n好。\n行。\n知道了。\n嗯嗯。\n[voice:晚安 明天聊[/voice]"
        chunks, record = _replier().parse_reply(raw, voice_allowed=True)
        self.assertIn("voice", _types(chunks), "超编时语音被当普通文本挤掉了")
        self.assertLessEqual(len(chunks), 5)
        self.assertIn("（语音消息）晚安 明天聊", record)


# ==========================================
# 2. 三个消费方都认得 voice 段（教训 9）
# ==========================================


class TestVoiceConsumers(unittest.TestCase):
    """教训 9：加一种段型要同时改三处消费方——发送端 / 落库记录 / 测量工具。

    语音段在阶段 A 已存在，但阶段 B 修 B-1 时按这条纪律逐一复核，
    发现测量工具 `duo_sim.collect_sent_chunks` 漏了 voice（"她说了话，
    transcript 查无此话"）。这里把三处钉住，防止今后再有第四处漏认。
    """

    def test_落库记录认得voice(self):
        self.assertEqual(
            chunk_record_text({"type": "voice", "content": "晚安 明天聊"}),
            "（语音消息）晚安 明天聊",
        )

    def test_测量工具认得voice(self):
        from duo_sim import collect_sent_chunks

        got = collect_sent_chunks([
            {"type": "text", "content": "在呢"},
            {"type": "voice", "content": "刚练完 手指有点僵"},
            {"type": "face", "tag": "doge", "id": 1},
        ])
        self.assertEqual(got["bubbles"], ["在呢", "（语音消息）刚练完 手指有点僵", "[doge]"],
                         "测量工具漏采 voice → transcript 查无此话，指标偏空")

    def test_测量工具与落库记录形态一致(self):
        from duo_sim import collect_sent_chunks

        chunks = [{"type": "voice", "content": "晚安"}]
        self.assertEqual(
            collect_sent_chunks(chunks)["bubbles"],
            [chunk_record_text(chunks[0])],
        )

    def test_基准工具认得voice(self):
        """benchmark_v4._sent_text 委托 chunk_record_text，故天然认得 voice——
        这里跑一次导入级验证，确认委托关系没被改回去（改回去 = combo/voice 再次失真）。"""
        from benchmark_v4 import _sent_text

        self.assertEqual(
            _sent_text([{"type": "voice", "content": "晚安"}]),
            "（语音消息）晚安",
        )


# ==========================================
# 3. provider 选择与配置解析
# ==========================================


class TestProviderConfig(unittest.TestCase):
    def test_默认是edge(self):
        cfg = TTSConfig()
        self.assertEqual(cfg.provider, "edge")
        self.assertFalse(cfg.enabled)
        self.assertEqual(cfg.voice_id, "Chinese (Mandarin)_Gentle_Senior")
        self.assertEqual(cfg.speed, 1.0)
        self.assertEqual(cfg.model, "speech-2.8-hd")
        self.assertEqual(cfg.group_id, "")
        # 所有者 2026-10-05 二次拍板：日上限 3→8→30（≈感觉不到存在，保险丝保留）
        self.assertEqual(cfg.daily_limit, 30)

    def test_manager归一化provider(self):
        self.assertEqual(TTSManager(TTSConfig(provider="minimax")).provider, "minimax")
        self.assertEqual(TTSManager(TTSConfig(provider="MiniMax")).provider, "minimax")
        self.assertEqual(TTSManager(TTSConfig(provider="")).provider, "edge")
        self.assertEqual(TTSManager(TTSConfig(provider="  EDGE ")).provider, "edge")

    def test_缺provider的旧配置回落到edge且关着(self):
        """服务器 config.toml 零改动兼容：整段缺失 / 有段无 provider 都必须安全。"""
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write('[account]\nallowed_user_id = 1\n[tts]\nenabled = true\n')
            cfg = Config.load(p)
        self.assertEqual(cfg.tts.provider, "edge", "缺 provider 必须回落 edge")
        self.assertEqual(cfg.tts.voice_id, "Chinese (Mandarin)_Gentle_Senior")

    def test_缺tts整段时是关着的(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write('[account]\nallowed_user_id = 1\n')
            cfg = Config.load(p)
        self.assertFalse(cfg.tts.enabled)
        self.assertEqual(cfg.tts.provider, "edge")

    def test_读得出minimax配置(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "c.toml")
            with open(p, "w", encoding="utf-8") as f:
                f.write(
                    '[account]\nallowed_user_id = 1\n[tts]\n'
                    'enabled = true\nprovider = "minimax"\n'
                    'voice_id = "Chinese (Mandarin)_Gentle_Senior"\n'
                    'speed = 1.1\nmodel = "speech-2.6-hd"\ngroup_id = "12345"\n'
                )
            cfg = Config.load(p)
        self.assertTrue(cfg.tts.enabled)
        self.assertEqual(cfg.tts.provider, "minimax")
        self.assertEqual(cfg.tts.speed, 1.1)
        self.assertEqual(cfg.tts.model, "speech-2.6-hd")
        self.assertEqual(cfg.tts.group_id, "12345")

    def test_example与代码默认值不漂移(self):
        """config.example.toml 的 [tts] 是公开仓库里唯一的配置文档，逐字段对账。"""
        example = os.path.join(_REPO_ROOT, "config.example.toml")
        with open(example, "rb") as f:
            data = tomllib.load(f)
        tts = data["tts"]
        defaults = TTSConfig()
        for fld in fields(TTSConfig):
            with self.subTest(field=fld.name):
                self.assertIn(fld.name, tts, f"example 缺字段 {fld.name}")
                self.assertEqual(tts[fld.name], getattr(defaults, fld.name))


# ==========================================
# 4. MiniMax t2a_v2 请求构造与失败降级
# ==========================================


def _fake_resp(audio: bytes = b"ID3-fake-mp3-bytes", code: int = 0) -> Dict[str, Any]:
    return {
        "data": {"audio": audio.hex(), "status": 2},
        "extra_info": {"usage_characters": 18, "audio_length": 2484, "audio_size": 41460},
        "trace_id": "trace-x",
        "base_resp": {"status_code": code, "status_msg": "success" if code == 0 else "boom"},
    }


def _minimax_manager(tmpdir: str, **kw) -> TTSManager:
    preset = ModelPreset(
        provider="minimax",
        base_url="https://api.minimaxi.com/v1",
        api_key="sk-test-fake",
        chat="MiniMax-M3",
    )
    cfg = TTSConfig(enabled=True, provider="minimax", **kw)
    return TTSManager(cfg, None, out_dir=tmpdir, minimax_preset=preset)


class TestMinimaxRequest(unittest.TestCase):
    def test_请求体逐字段(self):
        body = build_minimax_payload("刚练完琴，手有点酸。",
                                     "Chinese (Mandarin)_Gentle_Senior", 1.0, "speech-2.8-hd")
        self.assertEqual(body["model"], "speech-2.8-hd")
        self.assertEqual(body["text"], "刚练完琴，手有点酸。")
        self.assertFalse(body["stream"])
        self.assertEqual(body["output_format"], "hex")
        self.assertEqual(body["language_boost"], "auto", "中英混读要靠它自动识别语种")
        self.assertEqual(body["voice_setting"]["voice_id"], "Chinese (Mandarin)_Gentle_Senior")
        self.assertEqual(body["voice_setting"]["speed"], 1.0)
        self.assertEqual(body["audio_setting"]["format"], "mp3")
        self.assertEqual(body["audio_setting"]["channel"], 1)

    def test_endpoint拼接(self):
        self.assertEqual(
            minimax_endpoint("https://api.minimaxi.com/v1"), "https://api.minimaxi.com/v1/t2a_v2"
        )
        self.assertEqual(
            minimax_endpoint("https://api.minimaxi.com/v1/"), "https://api.minimaxi.com/v1/t2a_v2"
        )
        self.assertEqual(
            minimax_endpoint("https://api.minimaxi.com/v1/t2a_v2"),
            "https://api.minimaxi.com/v1/t2a_v2",
        )
        self.assertEqual(
            minimax_endpoint("", ""), "https://api.minimaxi.com/v1/t2a_v2",
            "base_url 缺省要有兜底",
        )
        self.assertEqual(
            minimax_endpoint("https://api.minimaxi.com/v1", "12345"),
            "https://api.minimaxi.com/v1/t2a_v2?GroupId=12345",
            "group_id 非空时按老接口形态挂查询参数",
        )

    def test_缺key直接降级不联网(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = TTSManager(TTSConfig(enabled=True, provider="minimax"), None, out_dir=td)
            called = {"n": 0}

            async def _boom(*a, **k):
                called["n"] += 1
                raise AssertionError("缺 key 时不该联网")

            mgr._minimax_post = _boom
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))
            self.assertEqual(called["n"], 0)

    def test_未知provider降级且不走edge(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = TTSManager(TTSConfig(enabled=True, provider="volc"), None, out_dir=td)
            mgr._import_edge_tts = lambda: (_ for _ in ()).throw(AssertionError("不该走 edge"))
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))

    def test_合成成功落盘并清理(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)
            seen: Dict[str, Any] = {}

            async def _fake_post(url, payload, api_key):
                seen["url"] = url
                seen["payload"] = payload
                seen["api_key"] = api_key
                return _fake_resp()

            mgr._minimax_post = _fake_post
            path = asyncio.run(mgr.synthesize("刚练完琴，手有点酸。"))
            self.assertIsNotNone(path)
            self.assertEqual(os.path.getsize(path), len(b"ID3-fake-mp3-bytes"))
            self.assertEqual(seen["url"], "https://api.minimaxi.com/v1/t2a_v2")
            self.assertEqual(seen["api_key"], "sk-test-fake", "key 必须取自现有 minimax 档案")
            self.assertEqual(seen["payload"]["voice_setting"]["voice_id"],
                             "Chinese (Mandarin)_Gentle_Senior")
            self.assertEqual(seen["payload"]["voice_setting"]["speed"], 1.0)
            mgr.cleanup(path)
            self.assertFalse(os.path.exists(path), "发完必须删临时文件")

    def test_错误码降级不抛(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _bad(url, payload, api_key):
                return _fake_resp(code=1004)

            mgr._minimax_post = _bad
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))
            self.assertEqual(os.listdir(td), [], "失败不该留下文件")

    def test_没有音频降级不抛(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _empty(url, payload, api_key):
                return {"data": None, "base_resp": {"status_code": 0}}

            mgr._minimax_post = _empty
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))

    def test_网络异常降级不抛(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _raise(url, payload, api_key):
                raise RuntimeError("connection reset")

            mgr._minimax_post = _raise
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))

    def test_超时降级不抛(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _slow(url, payload, api_key):
                await asyncio.sleep(5)

            mgr._minimax_post = _slow
            import companion.tts as tts_mod

            old = tts_mod.SYNTH_TIMEOUT
            tts_mod.SYNTH_TIMEOUT = 0.05
            try:
                self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))
            finally:
                tts_mod.SYNTH_TIMEOUT = old

    def test_音频hex损坏降级不抛(self):
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _bad_hex(url, payload, api_key):
                return {"data": {"audio": "zzzz"}, "base_resp": {"status_code": 0}}

            mgr._minimax_post = _bad_hex
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))

    def test_extract_audio纯函数(self):
        audio, why = extract_minimax_audio(_fake_resp(b"abc"))
        self.assertEqual(audio, b"abc")
        self.assertEqual(why, "")
        for bad in (None, [], {}, {"data": {}}, {"extra_info": {}},
                    {"data": {"audio": ""}}, {"data": {"audio": "xyz"}},
                    {"data": {"audio": "00"}, "base_resp": {"status_code": 1002}}):
            with self.subTest(bad=bad):
                audio, why = extract_minimax_audio(bad)
                self.assertIsNone(audio)
                self.assertTrue(why)

    def test_接口畸形响应也不抛(self):
        """base_resp 不是字典这类畸形响应：解析层要容错，不许把异常抛给调用方。"""
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _weird(url, payload, api_key):
                return {"base_resp": "not-a-dict", "data": {"audio": "0011"}}

            mgr._minimax_post = _weird
            # base_resp 畸形 → 代码按"没有错误码"处理，音频照解
            path = asyncio.run(mgr.synthesize("在吗"))
            self.assertIsNotNone(path)
            mgr.cleanup(path)

    def test_内部未预期异常被安全网兜住(self):
        """合成层任何想不到的错都不许往上冒——调用方没有 except，冒上去会丢她这句话。"""
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td)

            async def _boom(text):
                raise RuntimeError("unexpected")

            mgr._synthesize_minimax = _boom  # type: ignore[assignment]
            self.assertIsNone(asyncio.run(mgr.synthesize("在吗")))

    def test_兜底截断对两家都生效(self):
        """max_chars 是 provider 无关的公共闸门，minimax 也必须在合成前截断。"""
        with tempfile.TemporaryDirectory() as td:
            mgr = _minimax_manager(td, max_chars=10)
            seen: Dict[str, Any] = {}

            async def _fake_post(url, payload, api_key):
                seen["text"] = payload["text"]
                return _fake_resp()

            mgr._minimax_post = _fake_post
            long_text = "我今天真的特别特别累，从早到晚没停过。现在只想瘫着。"
            asyncio.run(mgr.synthesize(long_text))
            # 截断口径与 edge 路径同一份实现（truncate_for_voice）：
            # max_chars 以内，切不到句读时末尾补"…"故最多 max_chars+1 字
            self.assertLessEqual(len(seen["text"]), 11)
            self.assertNotEqual(seen["text"], long_text)


if __name__ == "__main__":
    unittest.main()

