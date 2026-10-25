"""Voice Turn Pipeline Rebuild 离线集成测试集 (tests/test_voice_turn_rebuild.py)

基于新合同验证 TurnHandler.handle_turn 端到端流程：
1) 普通“给你一条毛巾”“你唱一句试试”不被删除、不走神兜底；
2) 明确请求时 before/voice/after 顺序与原话一致，voice 为原文连续片段；
3) 不调用 voice_normalize/voice_recovery，主生成外无额外 API；
4) 普通【划重点】和 a[0] 原样保留；
5) 讨要保持期内“物资没收到/报告没收到”不触发语音语境，真实 text_fallback 后无宾语“没收到”才触发；
6) 主动消息 last_voice_status 比主聊新时能被读取；
7) runtime_state_v1 true/false 都走同一规划路径。
"""

import os
import sys
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

# 确保 companion 包在搜索路径中
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from companion.config import ReplyConfig
from companion.replier import Replier
from companion.turn_handler import TurnHandler


def _build_test_handler(
    stream_reply: str,
    *,
    voice_allowed: bool = True,
    voice_delivery: str = "sent",
    runtime_state_v1: bool = False,
    proactive_obj: Optional[Any] = None,
    send_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
):
    """构建用于离线集成测试的 TurnHandler 环境。"""
    sent_chunks: List[Dict[str, Any]] = []

    async def fake_send(chunk: Dict[str, Any]):
        if send_callback:
            send_callback(chunk)
        if chunk.get("type") == "voice":
            if voice_delivery == "sent":
                chunk["_voice_delivery"] = "sent"
            elif voice_delivery == "text":
                chunk["type"] = "text"
                chunk["_voice_delivery"] = "text"
            elif voice_delivery == "failed":
                chunk["_voice_delivery"] = "failed"
            else:
                chunk["_voice_delivery"] = voice_delivery
        sent_chunks.append(chunk)

    gateway = MagicMock()
    gateway.config.observer_model = "deepseek-flash"
    gateway.config.text_model = "deepseek-flash"

    async def stream(**_kwargs):
        yield stream_reply

    gateway.stream_chat = stream
    gateway.chat = AsyncMock()

    assembler = MagicMock()
    assembler.assemble_messages = AsyncMock(return_value=([], "sys"))
    assembler.meetups = None
    assembler.persona.get_current_activity = MagicMock(return_value="闲逛")
    assembler.persona.world.face_usage = "normal"
    assembler.persona.world.relative_dates = False
    assembler.get_holidays = MagicMock(return_value=[])

    tts = SimpleNamespace(
        config=SimpleNamespace(enabled=True, max_chars=90),
        check_gate=AsyncMock(return_value=(voice_allowed, "测试放行" if voice_allowed else "测试拦截")),
    )

    reply_config = ReplyConfig(chunk_delay_min=0, chunk_delay_max=0)
    reply_config.runtime_state_v1 = runtime_state_v1
    reply_config.continuity_check = False
    reply_config.candidates = 1

    replier = Replier(reply_config, MagicMock(match_sticker=MagicMock(return_value=None)))

    memory = MagicMock()
    memory.save_turn_pair = AsyncMock()
    memory.reinforce_memories = AsyncMock()
    memory.get_recent_turns = AsyncMock(return_value=[])
    memory.get_last_assistant_turn_time = AsyncMock(return_value=None)
    memory.db = MagicMock()

    proactive = proactive_obj or MagicMock(reset_unanswered_count=AsyncMock())
    if not hasattr(proactive, "reset_unanswered_count"):
        proactive.reset_unanswered_count = AsyncMock()

    config = MagicMock()
    config.reply = reply_config
    config.llm.text_model = "deepseek-flash"

    handler = TurnHandler(
        config=config,
        gateway=gateway,
        assembler=assembler,
        replier=replier,
        memory=memory,
        observer=MagicMock(settle_turn=AsyncMock()),
        proactive=proactive,
        send_chunk_fn=fake_send,
        tts=tts,
    )

    return handler, sent_chunks, gateway, assembler, memory


class TestVoiceTurnRebuild(unittest.IsolatedAsyncioTestCase):
    async def test_1_普通给你一条毛巾不被删除不走神兜底(self):
        """1) 普通‘给你一条毛巾’不被删除、不走神兜底。"""
        raw = "给你一条毛巾"
        handler, sent_chunks, gateway, _, memory = _build_test_handler(raw)

        await handler.handle_turn("帮我拿个东西", None)

        # 验证发送的 chunks
        self.assertEqual(len(sent_chunks), 1)
        self.assertEqual(sent_chunks[0]["type"], "text")
        self.assertEqual(sent_chunks[0]["content"], "给你一条毛巾")

        # 验证记忆落库不是走神兜底
        saved_bot_msg = memory.save_turn_pair.call_args.kwargs["bot_msg"]
        self.assertEqual(saved_bot_msg, "给你一条毛巾")
        self.assertNotIn("刚刚走神了", saved_bot_msg)

    async def test_1_普通你唱一句试试不被删除不走神兜底(self):
        """1) 普通‘你唱一句试试’不被删除、不走神兜底。"""
        raw = "你唱一句试试"
        handler, sent_chunks, _, _, memory = _build_test_handler(raw)

        await handler.handle_turn("这首歌我不大懂", None)

        self.assertEqual(len(sent_chunks), 1)
        self.assertEqual(sent_chunks[0]["type"], "text")
        self.assertEqual(sent_chunks[0]["content"], "你唱一句试试")

        saved_bot_msg = memory.save_turn_pair.call_args.kwargs["bot_msg"]
        self.assertEqual(saved_bot_msg, "你唱一句试试")
        self.assertNotIn("刚刚走神了", saved_bot_msg)

    async def test_2_明确请求时before_voice_after顺序与原话一致且voice为连续片段(self):
        """2) 明确请求时 before/voice/after 顺序与原话一致，voice 为原文连续片段。"""
        raw = "刚交完季度报表。\n今天好想你啊。\n回去路上注意安全。"
        handler, sent_chunks, _, _, memory = _build_test_handler(raw, voice_allowed=True, voice_delivery="sent")

        await handler.handle_turn("发条语音听听嘛", None)

        # 顺序校验：before (text) -> voice -> after (text)
        types = [c["type"] for c in sent_chunks]
        self.assertEqual(types, ["text", "voice", "text"])

        # 各段内容校验
        self.assertEqual(sent_chunks[0]["content"], "刚交完季度报表。")
        self.assertEqual(sent_chunks[1]["content"], "今天好想你啊。")
        self.assertEqual(sent_chunks[2]["content"], "回去路上注意安全。")

        # voice 为原文的连续片段
        voice_text = sent_chunks[1]["content"]
        self.assertIn(voice_text, raw)

        # 记忆落库包含（语音消息）前缀，且语序未错乱
        saved_bot_msg = memory.save_turn_pair.call_args.kwargs["bot_msg"]
        self.assertEqual(
            saved_bot_msg,
            "刚交完季度报表。\n（语音消息）今天好想你啊。\n回去路上注意安全。",
        )

    async def test_3_不调用voice_normalize与voice_recovery主生成外无额外API(self):
        """3) 不调用 voice_normalize/voice_recovery，主生成外无额外 API。"""
        # 测试在语音发送降级场景下，也不会发起任何模型补救或正规化请求
        raw = "刚交完季度报表。\n今天好想你啊。\n回去路上注意安全。"
        handler, sent_chunks, gateway, _, memory = _build_test_handler(
            raw, voice_allowed=True, voice_delivery="text"
        )

        await handler.handle_turn("发条语音听听嘛", None)

        # 验证全过程没有任何 gateway.chat 调用（voice_recovery / voice_normalize 原来使用 gateway.chat）
        self.assertEqual(gateway.chat.call_count, 0)

        # 失败只原地转文字：中间那段直接以 text 形式发出，内容保持原文，无模型改写
        types = [c["type"] for c in sent_chunks]
        self.assertEqual(types, ["text", "text", "text"])
        self.assertEqual(sent_chunks[1]["content"], "今天好想你啊。")
        self.assertEqual(sent_chunks[1]["_voice_delivery"], "text")

        # 投递状态记录为 text_fallback
        self.assertEqual(handler._last_voice_status, "text_fallback")

        # 记忆落库为纯文字，无语音消息前缀
        saved_bot_msg = memory.save_turn_pair.call_args.kwargs["bot_msg"]
        self.assertNotIn("（语音消息）", saved_bot_msg)
        self.assertEqual(saved_bot_msg, raw)

    async def test_4_普通划重点和数组下标原样保留(self):
        """4) 普通【划重点】和 a[0] 原样保留。"""
        raw = "【划重点】今天的安排都在这里\n数组是 a[0] 别写错"
        handler, sent_chunks, _, _, memory = _build_test_handler(raw)

        await handler.handle_turn("今天学什么", None)

        all_sent_text = "\n".join(c["content"] for c in sent_chunks)
        self.assertIn("【划重点】", all_sent_text)
        self.assertIn("a[0]", all_sent_text)

        saved_bot_msg = memory.save_turn_pair.call_args.kwargs["bot_msg"]
        self.assertIn("【划重点】", saved_bot_msg)
        self.assertIn("a[0]", saved_bot_msg)

    async def test_5_讨要保持期内物资报告没收到不触发语音语境_真实降级后无宾语才触发(self):
        """5) 讨要保持期内“物资没收到/报告没收到”不触发语音语境，真实 text_fallback 后无宾语“没收到”才触发。"""
        handler, _, _, assembler, _ = _build_test_handler("先别急嘛，稍后说")

        # 步骤 1：明确索要语音，激活讨要保持期（VOICE_REQUEST_HOLD）
        gate_ok, _ = await handler._check_voice_gate("发条语音听听嘛")
        self.assertTrue(gate_ok)
        self.assertTrue(handler._voice_requested)
        self.assertIsNotNone(handler._voice_request_until)

        # 即使在讨要保持期内，“物资没收到”和“报告没收到”带有具体物品宾语，绝不触发语音追问语境
        self.assertFalse(handler._missing_voice_context("物资没收到"))
        self.assertFalse(handler._missing_voice_context("报告没收到"))
        self.assertFalse(handler._asks_about_missing_voice("物资没收到"))
        self.assertFalse(handler._asks_about_missing_voice("报告没收到"))

        # 且在上一轮未尝试发送语音 (status='none') 时，即使是无宾语的“没收到”，也不触发缺失语音追问
        self.assertEqual(handler._latest_voice_delivery()[0], "none")
        self.assertFalse(handler._missing_voice_context("没收到"))
        self.assertFalse(handler._asks_about_missing_voice("没收到"))

        # 步骤 2：上一轮真实发生 text_fallback 降级
        handler._last_voice_status = "text_fallback"
        handler._last_voice_reason = "TTS合成超时"
        handler._last_voice_at = datetime.now()
        handler._last_turn_voice_status = "text_fallback"
        handler._last_turn_at = handler._last_voice_at

        # 真实降级后：无宾语的“没收到”“没收到诶”精准触发语音语境
        self.assertTrue(handler._missing_voice_context("没收到"))
        self.assertTrue(handler._missing_voice_context("没收到诶"))
        self.assertTrue(handler._missing_voice_context("怎么还没收到？"))
        self.assertTrue(handler._asks_about_missing_voice("没收到"))

        # 真实降级后，带有其它实体宾语的依然不触发
        self.assertFalse(handler._missing_voice_context("物资没收到"))
        self.assertFalse(handler._missing_voice_context("报告没收到"))

        # 进一步通过 handle_turn 检验提示词注入：无宾语“没收到”会注入真实投递说明
        await handler.handle_turn("没收到", None)
        assemble_calls = assembler.assemble_messages.call_args_list
        last_kwargs = assemble_calls[-1].kwargs
        self.assertIn("voice_delivery_note", last_kwargs)
        self.assertEqual(last_kwargs["voice_delivery_note"], "TTS合成超时")

    async def test_6_主动消息last_voice_status比主聊新时能被读取(self):
        """6) 主动消息 last_voice_status 比主聊新时能被读取。"""
        # 主聊最后一次在 10:00 发出成功语音
        main_time = datetime(2026, 10, 24, 10, 0)
        proactive_time = datetime(2026, 10, 24, 11, 0)

        proactive_mock = MagicMock()
        proactive_mock.last_voice_at = proactive_time
        proactive_mock.last_voice_status = "text_fallback"
        proactive_mock.last_voice_reason = "主动语音合成失败"
        proactive_mock.last_turn_voice_status = "text_fallback"
        proactive_mock.last_turn_at = proactive_time
        proactive_mock.reset_unanswered_count = AsyncMock()

        handler, _, _, assembler, _ = _build_test_handler(
            "收到就好", proactive_obj=proactive_mock
        )
        handler._last_voice_at = main_time
        handler._last_voice_status = "sent"
        handler._last_voice_reason = ""

        # 验证读取到的事实来自更新的主动消息
        status, reason = handler._latest_voice_delivery()
        self.assertEqual(status, "text_fallback")
        self.assertEqual(reason, "主动语音合成失败")

        # 此时用户进场说“没收到”，能正确关联到主动消息的 text_fallback
        self.assertTrue(handler._missing_voice_context("没收到"))
        self.assertTrue(handler._asks_about_missing_voice("没收到"))

        await handler.handle_turn("没收到", None)
        assemble_kwargs = assembler.assemble_messages.call_args.kwargs
        self.assertEqual(assemble_kwargs.get("voice_delivery_note"), "主动语音合成失败")

    async def test_6b普通文字不冲掉语音事件_成功语音也可紧接没收到(self):
        from companion.prompts import VOICE_SENT_NOTE

        handler, _, _, _, _ = _build_test_handler("日常文字")
        handler._last_voice_status = "sent"
        handler._last_voice_at = datetime(2026, 10, 24, 10, 0)
        handler._last_turn_voice_status = "sent"
        handler._last_turn_at = handler._last_voice_at
        self.assertTrue(handler._missing_voice_context("没收到"))
        self.assertEqual(handler._voice_delivery_note("没收到"), VOICE_SENT_NOTE)

        # 另一通道后来只发普通文字：最近语音事件仍是 sent，但无宾语追问不再硬猜。
        proactive = handler.proactive
        proactive.last_voice_status = "none"
        proactive.last_voice_at = None
        proactive.last_turn_voice_status = "none"
        proactive.last_turn_at = datetime(2026, 10, 24, 11, 0)
        self.assertEqual(handler._latest_voice_delivery()[0], "sent")
        self.assertTrue(handler._missing_voice_context("刚才那条语音我怎么没收到"))
        self.assertFalse(handler._missing_voice_context("没收到"))
        self.assertEqual(handler._voice_delivery_note("刚才那条语音我怎么没收到"), VOICE_SENT_NOTE)

    async def test_6c引用只挂文字不挂语音(self):
        raw = "[quote:1] 今天好想你啊。\n回去路上注意安全。"
        handler, sent_chunks, _, _, _ = _build_test_handler(raw, voice_allowed=True, voice_delivery="sent")
        targets = [{"index": 1, "message_id": 123, "text": "前一句话"}]

        await handler.handle_turn("发条语音", None, targets)

        self.assertEqual([c["type"] for c in sent_chunks], ["voice", "text"])
        self.assertNotIn("_quote", sent_chunks[0])
        self.assertEqual(sent_chunks[1]["_quote"]["message_id"], 123)
        self.assertEqual(sent_chunks[1]["content"], "回去路上注意安全。")

    async def test_7_runtime_state_v1_true与false都走同一规划路径(self):
        """7) runtime_state_v1 true/false 都走同一规划路径。"""
        raw = "刚交完季度报表。\n今天好想你啊。\n回去路上注意安全。"

        # 运行 runtime_state_v1 = False
        handler_f, sent_f, _, _, _ = _build_test_handler(
            raw, voice_allowed=True, voice_delivery="sent", runtime_state_v1=False
        )
        await handler_f.handle_turn("发条语音听听嘛", None)
        chunks_f = [(c["type"], c["content"]) for c in sent_f]
        status_f = handler_f._last_voice_status

        # 运行 runtime_state_v1 = True
        handler_t, sent_t, _, _, _ = _build_test_handler(
            raw, voice_allowed=True, voice_delivery="sent", runtime_state_v1=True
        )
        await handler_t.handle_turn("发条语音听听嘛", None)
        chunks_t = [(c["type"], c["content"]) for c in sent_t]
        status_t = handler_t._last_voice_status

        # 严格验证：分段类型、内容、顺序、最终投递状态完全一致
        self.assertEqual(chunks_f, chunks_t)
        self.assertEqual(status_f, status_t)
        self.assertEqual(status_f, "sent")
        self.assertEqual(
            chunks_f,
            [
                ("text", "刚交完季度报表。"),
                ("voice", "今天好想你啊。"),
                ("text", "回去路上注意安全。"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
