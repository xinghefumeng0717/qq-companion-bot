"""FIXES10 终检修复的单元测试 (tests/test_fixes10.py)

覆盖的链路/运维层问题：
1. safety.py 危机关键词误伤（"想死你了"/"不想活动"）
2. safety_block 必须排在阶段块之后（PLAN §13：安全提示在 system prompt 末尾）
3. plain_examples 块不得挂在"错误示范（禁止）"标题下
4. replier 超限兜底：换行边界不黏合、表情包段优先保留、落库记录与实发一致
5. voice.py file:// 语音 URI
6. onebot dispatcher 停机守卫
7. admin reset 的 backup_dir 透传
8. main.py 库路径统一 + 停机顺序（先取消任务再关库）+ admin.stop 有界化
10. screenshot_helper 默认演示库与生产库隔离
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from companion.config import Config, OneBotConfig, ReplyConfig, VoiceConfig
from companion.main import CompanionBot
from companion.onebot import OneBotClient
from companion.prompts import CRISIS_PROMPT, WATCH_PROMPT
from companion.replier import Replier, chunk_text_sentences, fit_chunks
from companion.safety import SafetyChecker
from companion.voice import FALLBACK_VOICE_TEXT, VoiceProcessor, file_uri_to_path
from helpers import close_db, make_db, make_engine_stack


# ==========================================================
# 修复 1：safety.py 危机关键词误伤
# ==========================================================


class TestSafetyKeywordExclusions(unittest.TestCase):
    def test_sos_style_flirt_does_not_trigger(self):
        """"想死你了" 是伴侣间最高频的撒娇，绝不能触发自杀干预注入"""
        for message in (
            "我都想死你了",
            "想死我了，一天没见",
            "想死你们啦",
            "她一走我就想死她了",
            "我想死他做的饭了",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    SafetyChecker.check_message(message),
                    f"日常撒娇被误判为危机: {message}",
                )

    def test_no_activity_does_not_trigger(self):
        """"不想活动" 是懒得动弹，不是不想活"""
        for message in (
            "我今天不想活动了",
            "太累了不想活动",
            "吃太饱了不想活动，就想躺着",
        ):
            with self.subTest(message=message):
                self.assertIsNone(
                    SafetyChecker.check_message(message),
                    f"日常表达被误判为危机: {message}",
                )

    def test_real_crisis_still_triggers(self):
        for message in (
            "活着真没意思不想活了",
            "我不想活了",
            "我想死",
            "我有时候真的想死",
            "我最近总想自残",
            "我活不下去了",
            "我想自杀",
        ):
            with self.subTest(message=message):
                self.assertEqual(
                    SafetyChecker.check_message(message),
                    CRISIS_PROMPT,
                    f"真危机句必须命中: {message}",
                )

    def test_second_occurrence_after_excluded_one(self):
        """同一句里前一处是撒娇、后一处是真危机时仍须命中"""
        self.assertEqual(
            SafetyChecker.check_message("我都想死你了，可我今天是真的不想活了"),
            CRISIS_PROMPT,
        )
        self.assertEqual(
            SafetyChecker.check_message("不想活动，其实我是不想活了"),
            CRISIS_PROMPT,
        )

    def test_other_crisis_keywords_unaffected(self):
        for message in ("我活不下去了", "我准备自残", "我想到自杀"):
            with self.subTest(message=message):
                self.assertEqual(SafetyChecker.check_message(message), CRISIS_PROMPT)

    def test_watch_keywords_unaffected(self):
        self.assertEqual(SafetyChecker.check_message("世界上我只有你了"), WATCH_PROMPT)
        self.assertIsNone(SafetyChecker.check_message("今天中午吃了黄焖鸡米饭"))
        self.assertIsNone(SafetyChecker.check_message(""))
        self.assertIsNone(SafetyChecker.check_message(None))


# ==========================================================
# 修复 2 / 3：system prompt 块顺序
# ==========================================================


class TestPromptBlockOrder(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.db = await make_db(":memory:")
        self.stack = make_engine_stack(self.db, "characters/example")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def _stage_block(self) -> str:
        aff_state = await self.stack.affection.get_state()
        return self.stack.assembler._build_stage_block(int(aff_state.get("stage", 0)))

    async def test_safety_block_after_stage_block(self):
        """安全提示必须排在"最高优先级"阶段块之后、收尾句之前"""
        prompt = await self.stack.assembler.assemble_system_prompt("我真的不想活了")

        self.assertIn("【安全边界】", prompt)
        self.assertIn("400-161-9995", prompt)

        stage_block = await self._stage_block()
        safety_idx = prompt.index("【安全边界】")

        self.assertLess(prompt.index(stage_block), safety_idx, "安全块必须在阶段块之后")
        self.assertLess(
            prompt.index("【当前关系阶段·最高优先级】"),
            safety_idx,
            "安全块不得再落在【事实】段里",
        )
        self.assertLess(safety_idx, prompt.index("（收尾固定句）"))

    async def test_no_safety_block_on_normal_message(self):
        prompt = await self.stack.assembler.assemble_system_prompt("今天中午吃了黄焖鸡")
        self.assertNotIn("【安全边界】", prompt)

    async def test_safety_block_not_before_stage_header(self):
        """回归保护：安全块不能退回【事实】段"""
        prompt = await self.stack.assembler.assemble_system_prompt("我想死你了")
        self.assertNotIn("【安全边界】", prompt)

        crisis_prompt = await self.stack.assembler.assemble_system_prompt("我不想活了")
        facts_idx = crisis_prompt.index("【事实】")
        self.assertGreater(crisis_prompt.index("【安全边界】"), facts_idx)


# ==========================================================
# 修复 4：replier 超限兜底
# ==========================================================


class _RecordingStickers:
    """所有描述词都命中，便于构造"文字 + 表情包"混排场景"""

    def __init__(self):
        self.calls = []

    def match_sticker(self, desc: str):
        self.calls.append(desc)
        return f"/stickers/{desc}.png"


def _make_replier(max_chunks: int = 5):
    return Replier(ReplyConfig(max_chunks=max_chunks), _RecordingStickers())


class TestReplierChunkLimit(unittest.TestCase):
    RAW_7_SEGMENTS = (
        "第一句\n第二句\n看看这个[sticker:猫猫]\n"
        "第三句\n第四句\n再来一个[sticker:狗头]\n第五句"
    )

    def test_tail_merge_keeps_newline_boundary(self):
        """兜底合并必须用换行而不是空串黏合"""
        chunks = chunk_text_sentences("甲\n乙\n丙", max_chunks=2)
        self.assertEqual(len(chunks), 2)
        self.assertNotIn("乙丙", chunks)
        self.assertEqual(chunks[1], "乙\n丙")

    def test_over_limit_reply_has_no_glued_segments(self):
        """超限兜底只能丢段或用换行合并，不得黏成"晚点找你记得吃饭别熬夜"这种黏话"""
        lines = ["晚点找你", "记得吃饭", "别熬夜", "早点睡", "想我了随时说", "先忙了", "晚安"]
        replier = _make_replier(max_chunks=5)
        chunks, record = replier.parse_reply("\n".join(lines))

        self.assertLessEqual(len(chunks), 5)
        all_text = "\n".join(c["content"] for c in chunks)
        self.assertNotIn("晚点找你记得吃饭别熬夜", all_text)

        # 每一段的每一行都必须能在原文里原样找到 => 没有被无分隔黏合
        for c in chunks:
            if c["type"] != "text":
                continue
            for part in c["content"].split("\n"):
                self.assertIn(part, lines)

        sent = "\n".join(
            c["content"] if c["type"] == "text" else f"[表情:{c['desc']}]"
            for c in chunks
        )
        self.assertEqual(record, sent)

    def test_stickers_survive_truncation_and_record_matches_sent(self):
        """9 段（含 2 个表情包）超限：最靠前的 sticker 不丢、落库记录与实发一致

        FIXES11 任务5 起 parse_reply 多了"整轮只保留第一个表情包段"的硬上限，
        故第二条表情包 [sticker:狗头] 在进入 fit_chunks 之前就被丢弃；
        "sticker 段不被 fit_chunks 挤掉"这条原语义仍然成立（猫猫保住）。
        """
        replier = _make_replier(max_chunks=5)
        chunks, record = replier.parse_reply(self.RAW_7_SEGMENTS)

        self.assertEqual(len(chunks), 5, "总量必须压到 max_chunks 以内")

        stickers = [c for c in chunks if c["type"] == "sticker"]
        self.assertEqual(len(stickers), 1, "整轮只允许一个表情包段")
        self.assertEqual([c["desc"] for c in stickers], ["猫猫"], "保留最靠前的那个")

        texts = [c["content"] for c in chunks if c["type"] == "text"]
        self.assertEqual(
            texts, ["第一句", "第二句", "看看这个", "第三句"], "先丢普通文本段，且不黏合"
        )
        for t in texts:
            self.assertNotIn("\n", t)

        sent = "\n".join(
            c["content"] if c["type"] == "text" else f"[表情:{c['desc']}]"
            for c in chunks
        )
        self.assertEqual(record, sent, "落库记录必须逐段等于实发内容")
        self.assertIn("[表情:猫猫]", record)
        self.assertNotIn("[表情:狗头]", record, "硬上限丢弃的表情包不得留在记录里")
        self.assertNotIn("第四句", record, "被丢弃的段不得留在记录里")
        self.assertNotIn("第五句", record, "被丢弃的段不得留在记录里")

    def test_fit_chunks_prefers_stickers(self):
        chunks = [
            {"type": "text", "content": "a"},
            {"type": "sticker", "file": "s1.png", "desc": "s1"},
            {"type": "text", "content": "b"},
            {"type": "sticker", "file": "s2.png", "desc": "s2"},
            {"type": "text", "content": "c"},
            {"type": "text", "content": "d"},
        ]
        out = fit_chunks(chunks, 4)
        self.assertEqual([c["type"] for c in out], ["text", "sticker", "text", "sticker"])
        self.assertEqual(out[0]["content"], "a")
        self.assertEqual(out[2]["content"], "b")

    def test_under_limit_untouched(self):
        replier = _make_replier(max_chunks=5)
        chunks, record = replier.parse_reply("行 那你忙")
        self.assertEqual([c["content"] for c in chunks], ["行 那你忙"])
        self.assertEqual(record, "行 那你忙")


# ==========================================================
# 修复 5：voice.py file:// URI
# ==========================================================


class TestFileUriToPath(unittest.TestCase):
    def test_posix_form(self):
        self.assertEqual(
            file_uri_to_path("file:///opt/qq-companion/data/voice/a.silk"),
            "/opt/qq-companion/data/voice/a.silk",
        )

    def test_windows_drive_three_slashes(self):
        self.assertEqual(
            file_uri_to_path("file:///D:/QQ chatter/data/voice/a.silk"),
            "D:/QQ chatter/data/voice/a.silk",
        )

    def test_windows_drive_two_slashes(self):
        self.assertEqual(
            file_uri_to_path("file://D:/QQ chatter/data/voice/a.silk"),
            "D:/QQ chatter/data/voice/a.silk",
        )

    def test_percent_encoding_and_query(self):
        self.assertEqual(
            file_uri_to_path("file:///D:/QQ%20chatter/a.silk?x=1"),
            "D:/QQ chatter/a.silk",
        )

    def test_non_file_uri_untouched(self):
        for value in ("http://example.com/a.silk", "data/voice/a.silk", "C:/x/a.silk"):
            with self.subTest(value=value):
                self.assertEqual(file_uri_to_path(value), value)


class TestProcessVoiceFileUri(unittest.IsolatedAsyncioTestCase):
    TEST_DIR = "data/test_fixes10_voice"

    async def asyncSetUp(self):
        os.makedirs(self.TEST_DIR, exist_ok=True)
        self.processor = VoiceProcessor(
            VoiceConfig(enabled=True, model_dir="data/models/sensevoice"),
            voice_dir=self.TEST_DIR,
        )

    async def asyncTearDown(self):
        for f in os.listdir(self.TEST_DIR):
            try:
                os.remove(os.path.join(self.TEST_DIR, f))
            except OSError:
                pass
        try:
            os.rmdir(self.TEST_DIR)
        except OSError:
            pass

    async def test_file_uri_local_silk_reaches_recognizer(self):
        """file:// 语音必须按本地路径走通，不被整串当路径（旧行为恒 False）"""
        silk = os.path.abspath(os.path.join(self.TEST_DIR, "input.silk"))
        with open(silk, "wb") as f:
            f.write(b"dummy silk content")
        uri = "file://" + silk.replace("\\", "/")

        def mock_transcode(in_file, out_file):
            self.assertEqual(os.path.abspath(in_file), silk, "必须剥掉 scheme 传给 ffmpeg")
            with open(out_file, "wb") as f:
                f.write(b"dummy wav content")
            return True

        download_mock = AsyncMock()
        with patch.object(self.processor, "_download_silk", new=download_mock), \
             patch.object(self.processor, "_transcode_sync", side_effect=mock_transcode), \
             patch.object(self.processor, "_recognize_sync", return_value="今天天气不错"):

            result = await self.processor.process_voice(uri)

        self.assertEqual(result, "（语音消息）今天天气不错")
        download_mock.assert_not_called()
        self.assertTrue(os.path.exists(silk), "file:// 源文件不是下载来的临时文件，不得删除")
        self.assertEqual(
            [f for f in os.listdir(self.TEST_DIR) if f.endswith(".wav")], []
        )

    async def test_missing_file_uri_degrades(self):
        result = await self.processor.process_voice("file:///opt/qq-companion/voice/nope.silk")
        self.assertEqual(result, FALLBACK_VOICE_TEXT)


# ==========================================================
# 修复 6：onebot dispatcher 停机守卫
# ==========================================================


def _message_frame(text: str, user_id: int = 10001) -> str:
    return json.dumps(
        {
            "post_type": "message",
            "message_type": "private",
            "user_id": user_id,
            "message": [{"type": "text", "data": {"text": text}}],
        }
    )


async def _wait_for(predicate, timeout: float = 1.5) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return predicate()


class TestOneBotDispatcherStopGuard(unittest.IsolatedAsyncioTestCase):
    def _client(self):
        got = []

        # FIXES21：回调多第三个参数 message_id
        async def cb(text, img, message_id=None):
            got.append((text, img))

        client = OneBotClient(
            config=OneBotConfig(ws_url="ws://127.0.0.1:3001", access_token=""),
            allowed_user_id=10001,
            on_message_callback=cb,
            image_save_dir="data/test_fixes10_imgs",
        )
        return client, got

    async def test_frames_dropped_before_start(self):
        """start() 之前到达的帧不派发、不创建 dispatcher"""
        client, got = self._client()
        await client._handle_raw_message(_message_frame("在吗"))
        await asyncio.sleep(0.05)
        self.assertEqual(got, [])
        self.assertIsNone(client._dispatcher_task)

    async def test_frames_dropped_after_stop_and_dispatcher_not_restarted(self):
        client, got = self._client()
        client._running = True
        await client._handle_raw_message(_message_frame("在吗"))
        self.assertTrue(await _wait_for(lambda: len(got) == 1))
        self.assertIsNotNone(client._dispatcher_task)

        await client.stop()
        dispatcher_after_stop = client._dispatcher_task
        self.assertTrue(
            await _wait_for(lambda: dispatcher_after_stop.done()), "stop() 后 dispatcher 必须收尾"
        )

        await client._handle_raw_message(_message_frame("还在吗"))
        await asyncio.sleep(0.05)

        self.assertEqual(len(got), 1, "stop() 之后的在途帧不得再触发回调")
        self.assertIs(
            client._dispatcher_task, dispatcher_after_stop, "dispatcher 不得被重新拉起"
        )
        self.assertTrue(client._message_queue.empty(), "停机后不得再入队")


# ==========================================================
# 修复 7：admin reset 的 backup_dir 透传
# ==========================================================


class TestAdminResetBackupDir(unittest.IsolatedAsyncioTestCase):
    class _JsonRequest:
        content_type = "application/json"
        headers = {"Accept": "application/json"}

        async def json(self):
            return {"confirm": "YES"}

    async def asyncSetUp(self):
        self.db = await make_db(":memory:")

    async def asyncTearDown(self):
        await close_db(self.db)

    async def test_reset_passes_injected_backup_dir(self):
        stack = make_engine_stack(
            self.db,
            "characters/example",
            include_admin=True,
            db_path="data/fake_companion.db",
            backup_dir="data/fake_backup/daily",
        )
        with patch(
            "companion.admin.reset_database",
            return_value={"backup_path": "data/fake_backup/daily/x.db", "cleared_counts": {}},
        ) as mocked:
            resp = await stack.admin.handle_admin_reset(self._JsonRequest())

        mocked.assert_called_once_with(
            db_path="data/fake_companion.db",
            backup_dir="data/fake_backup/daily",
            purge_all=False,
        )
        self.assertEqual(resp.status, 200)


# ==========================================================
# 修复 8：main.py 库路径统一
# ==========================================================


class TestBotDbPathUnification(unittest.TestCase):
    def test_single_source_of_truth(self):
        bot = CompanionBot(Config.load("config.example.toml"), db_path="data/zzz_test.db")
        self.assertEqual(bot.db_path, "data/zzz_test.db")
        self.assertEqual(bot.db.db_path, "data/zzz_test.db")
        self.assertEqual(bot.backup_scheduler.db_path, "data/zzz_test.db")
        self.assertEqual(bot.admin.db_path, "data/zzz_test.db")

    def test_default_db_path(self):
        bot = CompanionBot(Config.load("config.example.toml"))
        self.assertEqual(bot.db_path, "data/companion.db")
        self.assertEqual(bot.backup_scheduler.db_path, "data/companion.db")
        self.assertEqual(bot.admin.db_path, "data/companion.db")


# ==========================================================
# 修复 9：停机顺序与 admin.stop 有界化
# ==========================================================


class _FakeScheduler:
    def __init__(self, events, name, **tasks):
        self._events = events
        self._name = name
        for key, value in tasks.items():
            setattr(self, key, value)

    def stop(self):
        self._events.append(f"{self._name}.stop")


class _FakeAsyncComponent:
    def __init__(self, events, name, delay: float = 0.0):
        self._events = events
        self._name = name
        self._delay = delay

    async def stop(self):
        self._events.append(f"{self._name}.stop.start")
        await asyncio.sleep(self._delay)
        self._events.append(f"{self._name}.stop.done")

    async def close(self):
        self._events.append(f"{self._name}.close")


class _FakeDB:
    def __init__(self, events):
        self._events = events

    async def close(self):
        self._events.append("db.close")


def _make_background_task(events):
    async def runner():
        events.append("bg.start")
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events.append("bg.cancelled")
            raise

    return asyncio.create_task(runner())


class TestShutdownSequence(unittest.IsolatedAsyncioTestCase):
    def _build_bot(self, events, admin=None, with_bg_task: bool = True):
        bot = CompanionBot.__new__(CompanionBot)
        bot.config = Config.load("config.example.toml")
        bot._closed = False
        bot._stopping = False

        bg_task = _make_background_task(events) if with_bg_task else None
        bot.aggregator = _FakeScheduler(
            events, "aggregator", _debounce_task=None, _consumer_task=bg_task
        )
        bot.proactive = _FakeScheduler(events, "proactive", _task=None)
        bot.backup_scheduler = _FakeScheduler(events, "backup", _task=None)
        bot.onebot = _FakeAsyncComponent(events, "onebot")
        bot.gateway = _FakeAsyncComponent(events, "gateway")
        bot.admin = admin or _FakeAsyncComponent(events, "admin")
        bot.db = _FakeDB(events)
        return bot, bg_task

    async def test_background_tasks_cancelled_before_db_close(self):
        events = []
        bot, bg_task = self._build_bot(events)
        await asyncio.sleep(0.05)  # 让后台任务真正跑起来
        self.assertIn("bg.start", events)

        with self.assertLogs("companion", level="INFO") as captured:
            await bot.close()

        self.assertIn("bg.cancelled", events, "后台任务必须被取消并等待退出")
        self.assertLess(
            events.index("bg.cancelled"),
            events.index("db.close"),
            "db.close 必须发生在后台任务取消之后",
        )
        self.assertTrue(bg_task.done())

        log_text = "\n".join(captured.output)
        self.assertIn("[Bot] 正在关闭 数据库", log_text)
        self.assertIn("[Bot] 正在关闭 Admin 仪表盘", log_text)
        self.assertIn("[Bot] 正在取消后台任务", log_text)

    async def test_settling_observer_task_is_cancelled_and_shutdown_returns(self):
        """D-4：停机时正在结算的 observer 任务（fire-and-forget、无显式引用）
        必须被统一取消，且取消发生在 db.close 之前——否则它会在库关掉后重连挂住进程。"""
        events = []
        bot, _ = self._build_bot(events, with_bg_task=False)

        async def settle_turn():
            events.append("settle.start")
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                events.append("settle.cancelled")
                raise

        settle_task = asyncio.create_task(settle_turn())
        await asyncio.sleep(0.05)  # 让结算任务真正跑起来
        self.assertIn("settle.start", events)

        # 不许用 wait_for 包 close()：Python ≤3.11 的 wait_for 会把被等协程包成
        # 独立 task，close() 的取消波会把**调用方的 task** 也扫进去反杀自己
        # （CI 3.11 实测 ERROR；3.12+ wait_for 改用 asyncio.timeout 同事执行才没事）
        await bot.close()

        self.assertIn("settle.cancelled", events, "未跟踪的写库任务也必须被取消")
        self.assertTrue(settle_task.done())
        self.assertLess(
            events.index("settle.cancelled"),
            events.index("db.close"),
            "取消结算任务必须发生在 db.close 之前",
        )

    async def test_admin_stop_timeout_does_not_block_shutdown(self):
        events = []
        hanging_admin = _FakeAsyncComponent(events, "admin", delay=30.0)
        bot, _ = self._build_bot(events, admin=hanging_admin, with_bg_task=False)

        with patch("companion.main.ADMIN_STOP_TIMEOUT", 0.05):
            with self.assertLogs("companion", level="WARNING") as captured:
                await bot.close()

        self.assertIn("admin.stop.start", events)
        self.assertNotIn("admin.stop.done", events)
        self.assertIn("db.close", events, "单个组件超时不得拖死停机")
        self.assertIn("gateway.close", events)
        self.assertIn("超时", "\n".join(captured.output))

    async def test_close_is_idempotent(self):
        events = []
        bot, _ = self._build_bot(events, with_bg_task=False)
        await bot.close()
        first = list(events)
        await bot.close()
        self.assertEqual(events, first)

    async def test_stubborn_task_cannot_block_shutdown(self):
        """真机实测（2026-10-05）：某后台任务拒收取消，gather 永不返回，
        systemd 30 秒 SIGKILL。取消等待必须有界，超时就点名放弃、继续停机。"""
        events = []
        bot, _ = self._build_bot(events, with_bg_task=False)

        release = asyncio.Event()

        async def stubborn():
            while not release.is_set():
                try:
                    await asyncio.sleep(0.05)
                except asyncio.CancelledError:
                    continue  # 拒收取消，模拟卡死任务

        task = asyncio.create_task(stubborn())
        await asyncio.sleep(0.05)

        try:
            with patch("companion.main.TASK_CANCEL_TIMEOUT", 0.1):
                with self.assertLogs("companion", level="WARNING") as captured:
                    # 不许用 wait_for 包 close()：Python ≤3.11 的 wait_for 会把被等协程
                    # 包成独立 task，close() 的取消波会反杀调用方 task（CI 3.11 实测
                    # ERROR；3.12+ wait_for 改用 asyncio.timeout 同 task 执行才没事）
                    await bot.close()

            self.assertIn("db.close", events, "任务拒死不得拖死停机")
            log_text = "\n".join(captured.output)
            self.assertIn("拒收取消", log_text)
            self.assertIn("stubborn", log_text, "点名要写出拒死任务的身份，下次直接抓现行")
        finally:
            # 无论断言是否炸掉都必须放出 stubborn——否则它吞取消永生，
            # IsolatedAsyncioTestCase 收尾时被它吊死（CI 3.11 实测卡死 15 分钟）
            release.set()
            await asyncio.wait_for(task, timeout=2.0)


# ==========================================================
# 修复 10：screenshot_helper 演示库隔离
# ==========================================================


class TestScreenshotHelperIsolation(unittest.TestCase):
    def test_default_db_is_demo_db(self):
        """演示脚本默认写独立演示库，绝不碰生产库 data/companion.db"""
        import importlib.util

        cwd = os.getcwd()
        try:
            spec = importlib.util.spec_from_file_location(
                "screenshot_helper_under_test", "scripts/util/screenshot_helper.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        finally:
            os.chdir(cwd)

        self.assertEqual(module.DEMO_DB_PATH, "data/screenshot_demo.db")

        with open("scripts/util/screenshot_helper.py", encoding="utf-8") as f:
            source = f.read()
        self.assertNotIn('db_path = "data/companion.db"', source)
        self.assertNotIn("data/test_screenshot.db", source)
        self.assertIn("绝不使用生产库", source)


if __name__ == "__main__":
    unittest.main()
