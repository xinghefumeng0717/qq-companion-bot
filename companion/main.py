"""伴侣机器人主入口 (main.py)
装配全部系统模块，启动 OneBot 客户端、主动消息调度器、消息聚合器与状态仪表盘。
支持通过 --status 命令行参数打印当前状态（好感度、PAD、日记、事实）。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from typing import Any, Dict, List, Optional

from companion.admin import AdminServer
from companion.affection import AffectionEngine
from companion.aggregator import MessageAggregator
from companion.arcs import LifeArcManager
from companion.assembler import PromptAssembler
from companion.backup import DailyBackupScheduler
from companion.config import Config
from companion.db import Database
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.onebot import (
    OneBotClient,
    build_face_segment,
    build_image_segment,
    build_record_segment,
    build_reply_segment,
    build_text_segment,
)
from companion.observer import Observer
from companion.persona import Persona
from companion.prompts import get_mood_description, get_mood_label, get_trust_description
from companion.proactive import ProactiveScheduler
from companion.replier import Replier
from companion.stickers import StickerManager
from companion.turn_handler import TurnHandler
from companion.tts import TTSManager
from companion.voice import VoiceProcessor

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("companion")

# 停机时单个组件允许占用的最长时间：组件卡死不能拖死整个进程（systemd 会 SIGKILL）
ADMIN_STOP_TIMEOUT = 5.0
# 后台任务取消的有界等待：真机实测（2026-10-05）出现过某任务拒收取消、
# gather 永不返回、systemd 30 秒 SIGKILL。超时就放弃它、点名留证、继续停机。
TASK_CANCEL_TIMEOUT = 10.0

# 库文件路径唯一来源（构造时可注入，测试/多环境不必改代码）
DEFAULT_DB_PATH = "data/companion.db"


def format_status_text(
    persona: Persona,
    aff_state: Dict[str, Any],
    mood_state: Dict[str, Any],
    facts: Optional[List[str]] = None,
    diaries: Optional[List[str]] = None,
) -> str:
    """统一格式化伴侣机器人的实时状态（好感度、PAD心境、事实与日记）"""
    dims = aff_state.get("dims", {})
    comp = float(aff_state.get("composite", 30.0))
    stage_idx = int(aff_state.get("stage", 0))
    stage_obj = persona.get_stage(stage_idx)

    v = float(mood_state.get("v", 2.0))
    a = float(mood_state.get("a", 1.0))
    t = float(mood_state.get("t", 7.0))
    frustration = float(mood_state.get("frustration", 0.0))

    lines = [
        "=" * 50,
        f"伴侣状态报告：{persona.name}",
        "=" * 50,
        f"【好感度】复合分: {comp:.1f} | 阶段 {stage_idx} ({stage_obj.name}: {stage_obj.tone})",
        f"  - 温暖 (warmth):   {dims.get('warmth', 0.0):.1f}",
        f"  - 信任 (trust):    {dims.get('trust', 0.0):.1f}",
        f"  - 亲密 (intimacy): {dims.get('intimacy', 0.0):.1f}",
        f"  - 好奇 (intrigue): {dims.get('intrigue', 0.0):.1f}",
        f"  - 包容 (patience): {dims.get('patience', 0.0):.1f}",
        f"  - 紧张 (tension):  {dims.get('tension', 0.0):.1f}",
        "-" * 50,
        f"【情绪 (PAD)】{get_mood_label(v, a)} ({get_mood_description(v, a)})",
        f"  - 愉悦度 (Valence):  {v:.1f}",
        f"  - 唤醒度 (Arousal):  {a:.1f}",
        f"  - 安心度 (Trust):    {t:.2f} ({get_trust_description(t)})",
        f"  - 冷落驱力 (Frust):  {frustration:.2f}",
        "-" * 50,
    ]
    if facts is not None:
        lines.append(f"【语义事实 (Facts)】共 {len(facts)} 条:")
        for f in facts:
            lines.append(f"  * {f}")
    if diaries is not None:
        lines.append(f"【记忆日记 (Active Diaries)】共 {len(diaries)} 条:")
        for d in diaries[:5]:
            lines.append(f"  * {d}")
    lines.append("=" * 50)
    return "\n".join(lines)


async def print_status(config: Config) -> None:
    """CLI 打印当前机器人状态 (--status)"""
    db = Database()
    await db.init_tables()
    persona = Persona.load(config.character.path)
    affection = AffectionEngine(db, persona.initial_dims)
    mood = MoodEngine(db)
    memory = MemoryManager(db)

    aff_state = await affection.get_state()
    mood_state = await mood.get_state()
    v = float(mood_state.get("v", 2.0))
    facts = await memory.get_all_facts()
    diaries = await memory.get_active_diaries(current_valence=v)

    report = format_status_text(persona, aff_state, mood_state, facts, diaries)
    print("\n" + report + "\n")
    await db.close()


class CompanionBot:
    def __init__(self, config: Config, db_path: str = DEFAULT_DB_PATH):
        self.config = config
        # 库路径的唯一源头：db / backup_scheduler / admin 三处共用，不再各自硬编码
        self.db_path = db_path
        self.db = Database(self.db_path)
        self.persona = Persona.load(config.character.path)

        stickers_dir = os.path.join(self.persona.base_dir, self.persona.stickers_dir)
        self.stickers = StickerManager(stickers_dir, self.db)

        self.affection = AffectionEngine(self.db, self.persona.initial_dims)
        self.mood = MoodEngine(self.db)
        self.gateway = LLMGateway(config.llm, self.db)
        self.memory = MemoryManager(self.db, self.gateway, self.affection, self.persona)

        # FIXES16 生活主线：只往提示词加事实，与 mood/affection/observer 零耦合
        self.arcs = LifeArcManager(self.db, self.gateway, self.persona)

        self.assembler = PromptAssembler(
            self.persona,
            self.affection,
            self.mood,
            self.memory,
            self.stickers,
            self.db,
            holidays_provider=config.get_holidays,
            arcs=self.arcs,
        )
        self.replier = Replier(config.reply, self.stickers)
        self.observer = Observer(
            self.gateway, self.affection, self.mood, self.memory, self.stickers, self.db
        )

        self.voice_processor = VoiceProcessor(config.voice)
        # FIXES22：语音回复（阶段 A，默认关）。与语音输入并列成一个独立开关，
        # 语音输入关了不影响她说话，语音输出关了也不影响她听。
        # 阶段 B：provider="minimax" 时鉴权复用现有 [models.minimax] 档案
        # （不新增密钥字段；档案不存在就是 None，合成层报缺 key 后降级为文字）。
        self.tts = TTSManager(
            config.tts, self.db, minimax_preset=config.llm.presets.get("minimax")
        )

        self.proactive = ProactiveScheduler(
            config=config.proactive,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            replier=self.replier,
            gateway=self.gateway,
            db=self.db,
            send_msg_fn=self._send_chunk_to_onebot,
            assembler=self.assembler,
            holidays_provider=config.get_holidays,
            set_typing_fn=self._set_typing_to_onebot,
            timing_config=config.timing,
            arcs=self.arcs,
            tts=self.tts,   # FIXES22：与主聊共用同一份语音账目
        )

        self.turn_handler = TurnHandler(
            config=config,
            gateway=self.gateway,
            assembler=self.assembler,
            replier=self.replier,
            memory=self.memory,
            observer=self.observer,
            proactive=self.proactive,
            send_chunk_fn=self._send_chunk_to_onebot,
            set_typing_fn=self._set_typing_to_onebot,
            timing_config=config.timing,
            tts=self.tts,   # FIXES22：语音闸门
        )
        self.aggregator = MessageAggregator(turn_handler=self.turn_handler.handle_turn)

        self.backup_scheduler = DailyBackupScheduler(
            db_path=self.db_path,
            backup_dir="data/backup/daily",
            on_maintenance=self._daily_arcs_topup,
        )

        self._stopping = False
        self._closed = False

        self.onebot = OneBotClient(
            config.onebot,
            allowed_user_id=config.account.allowed_user_id,
            on_message_callback=self._on_raw_message,
            voice_processor=self.voice_processor,
            # FIXES23：对方输入状态直连聚合器（同步回调，见 OneBotClient 注释）
            on_typing_callback=self.aggregator.notify_peer_typing,
        )

        self.admin = AdminServer(
            config=config.admin,
            persona=self.persona,
            affection=self.affection,
            mood=self.mood,
            memory=self.memory,
            stickers=self.stickers,
            proactive=self.proactive,
            assembler=self.assembler,
            db=self.db,
            onebot=self.onebot,
            db_path=self.db_path,
            backup_dir="data/backup/daily",
        )

    async def _daily_arcs_topup(self) -> None:
        """FIXES16 凌晨维护钩子：备份/维护时段跑完后把生活主线补到 3 条。

        这里是"每日补一次"的那个时机（另一个时机是主动消息周期里发现不足 2 条时即时补）。
        异常一律吞掉：备份已经成功了，钩子炸了不该影响主流程。
        """
        try:
            await self.arcs.advance_states()
            await self.arcs.ensure_arcs(min_active=3)
        except Exception as e:
            logger.warning(f"[Bot] 凌晨补充生活主线失败: {e}")

    async def _send_chunk_to_onebot(self, chunk: Dict[str, Any]) -> None:
        """分段发送底层调用

        FIXES20：新增 face（纯表情气泡）与 combo（文字+表情同一条消息）两种段。
        combo 是主形态——机主 65% 的表情是"文字+表情同气泡"（其中 97% 挂句尾），
        拆成两条消息就毁掉了这个语气。OneBot 一条消息本来就支持混合段数组。

        FIXES21：段上有 `_quote` 头时，把它拼成 OneBot `reply` 段放在**最前面**，
        与正文同一条消息出去（"回他第2条 + 内容"是一条气泡，不是两条）。
        reply 段是 NapCat/QQ 侧最可能出岔子的一段（id 过期、部分实现不支持），
        整条发送失败时**去掉 reply 段重发正文**——引用没了顶多指代弱一点，
        她说的话丢了才是事故。
        """
        quote = chunk.get("_quote")
        if chunk["type"] == "text":
            body: List[Dict[str, Any]] = [build_text_segment(chunk["content"])]
        elif chunk["type"] == "sticker":
            body = [build_image_segment(chunk["file"])]
        elif chunk["type"] == "face":
            body = [build_face_segment(chunk["id"])]
        elif chunk["type"] == "combo":
            body = []
            for part in chunk.get("parts", []):
                if part.get("type") == "text":
                    if part.get("content", "").strip():
                        body.append(build_text_segment(part["content"]))
                elif part.get("type") == "face":
                    body.append(build_face_segment(part["id"]))
            if not body:
                return
        elif chunk["type"] == "voice":
            # FIXES22：语音走独立通道（要合成、要删临时文件、要计日额度）
            await self._send_voice_chunk(chunk)
            return
        else:
            return

        segs: List[Dict[str, Any]] = []
        if quote and quote.get("message_id") is not None:
            try:
                segs.append(build_reply_segment(quote["message_id"]))
            except (TypeError, ValueError) as e:
                logger.warning(f"[Bot] 引用 id 非法（{quote.get('message_id')!r}），本条不带引用发出: {e}")
                segs = []
        segs.extend(body)

        ok = await self.onebot.send_private_msg(
            self.config.account.allowed_user_id, segs
        )
        if not ok and segs and segs[0].get("type") == "reply":
            logger.warning(
                f"[Bot] 带引用的消息发送失败（reply 段 id={quote.get('message_id')}），"
                "去掉引用重发正文"
            )
            await self.onebot.send_private_msg(self.config.account.allowed_user_id, body)

    async def _send_voice_chunk(self, chunk: Dict[str, Any]) -> None:
        """语音段：合成 → 发送 → 删临时文件（FIXES22 任务2 第3条）

        合成失败（超时/异常/空文件）时**按普通文字发出去**——语音是锦上添花，
        绝不能因为合成器抽风把她这句话弄丢。删临时文件放 finally，异常也删。
        """
        text = (chunk.get("content") or "").strip()
        if not text:
            return
        tts = getattr(self, "tts", None)
        if tts is None:
            await self.onebot.send_private_msg(
                self.config.account.allowed_user_id,
                [build_text_segment(text)],
            )
            return

        audio_path = None
        try:
            audio_path = await tts.synthesize(text)
            if not audio_path:
                logger.info("[Bot] 语音合成未成功，本条按文字发出")
                await self.onebot.send_private_msg(
                    self.config.account.allowed_user_id,
                    [build_text_segment(text)],
                )
                return
            # "按住说话"的节拍：先短暂停顿再发（≤3s），别让语音和上一条文字粘在一起
            try:
                await asyncio.sleep(min(3.0, 0.6 + 0.04 * len(text)))
            except asyncio.CancelledError:
                raise
            ok = await self.onebot.send_private_msg(
                self.config.account.allowed_user_id,
                [build_record_segment(audio_path)],
            )
            if ok:
                used = await tts.bump_daily()
                logger.info(
                    f"[Bot] 语音已发出（{len(text)} 字，今日第 {used} 条）"
                )
        finally:
            tts.cleanup(audio_path)

    async def _set_typing_to_onebot(self, typing: bool) -> bool:
        """FIXES15 "正在输入"状态。user_id 在这里绑定，处理器/调度器只管开/关。

        NapCat 不支持、未连接或超时时 set_input_status 静默返回 False，
        那只是打字表演没演成，不影响消息发送。
        """
        return await self.onebot.set_input_status(
            self.config.account.allowed_user_id, typing
        )

    async def _on_raw_message(
        self, text: str, image_path: Optional[str], message_id: Any = None
    ) -> None:
        """OneBot 收到机主私聊时交由聚合器（FIXES21：message_id 一起带走）"""
        await self.aggregator.push_message(text, image_path, message_id)

    async def _handle_turn(
        self,
        user_text: str,
        image_path: Optional[str],
        quote_targets: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """兼容保留：委托给 TurnHandler 处理单轮对话（FIXES21：批次透传）"""
        await self.turn_handler.handle_turn(user_text, image_path, quote_targets)

    async def run(self) -> None:
        """主运行循环"""
        await self.db.init_tables()
        await self.stickers.sync_initial_stickers()
        self.aggregator.start()
        self.proactive.start()
        self.backup_scheduler.start()
        await self.admin.start()

        # 启动 OneBot WebSocket 客户端
        try:
            await self.onebot.start()
        finally:
            await self.close()

    async def _cancel_pending_tasks(self) -> None:
        """取消并等待**全部**未完成的后台任务退出（D-4 停机顺序修复）。

        这是 close() 的第一步，必须早于任何资源释放：库连接关掉之后，仍在跑的任务
        （observer.settle_turn、日记归档等 fire-and-forget 的写库协程）再执行一条 SQL
        都会静默重开连接，aiosqlite 的非守护线程会把进程挂住。走 asyncio 的全局任务表
        统一取消，天然覆盖那些没有显式引用的写库任务，无需额外维护引用集合。
        """
        current = asyncio.current_task()
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            # 不能用 wait_for(gather(...))：超时后 wait_for 会先取消 gather 并**等它收尾**，
            # 而 gather 收尾要等子任务退出——遇到拒收取消的任务，wait_for 自己也会挂死。
            # asyncio.wait 超时只返回不取消，才是真有界。
            _, stubborn = await asyncio.wait(pending, timeout=TASK_CANCEL_TIMEOUT)
            if stubborn:
                names = [
                    getattr(t.get_coro(), "__qualname__", repr(t))
                    for t in stubborn
                ]
                logger.warning(
                    f"[Bot] {len(names)} 个后台任务拒收取消（{TASK_CANCEL_TIMEOUT}s 未退出），"
                    f"放弃等待继续停机: {names}"
                )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        logger.info("[Bot] 正在关闭伴侣机器人...")
        # 每一步都打日志：停机若再被 systemd 超时 SIGKILL，日志能直接指出卡在哪一步

        # D-4：第一步先取消全部后台任务（含 observer 结算 / 日记归档等写库任务），
        # 再做任何资源释放——顺序颠倒会让被取消的任务在 db.close() 之后重连挂住进程。
        logger.info("[Bot] 正在取消后台任务")
        await self._cancel_pending_tasks()

        logger.info("[Bot] 正在关闭 后台调度器 (aggregator/proactive/backup)")
        self.backup_scheduler.stop()
        self.aggregator.stop()
        self.proactive.stop()

        logger.info("[Bot] 正在关闭 OneBot 客户端")
        await self.onebot.stop()

        logger.info("[Bot] 正在关闭 Admin 仪表盘")
        try:
            # 有界停机：aiohttp 清理偶尔会卡住，超时就放弃它继续往下走
            await asyncio.wait_for(self.admin.stop(), timeout=ADMIN_STOP_TIMEOUT)
        except asyncio.TimeoutError:
            logger.warning(f"[Bot] Admin 仪表盘关闭超时 ({ADMIN_STOP_TIMEOUT}s)，跳过继续停机")
        except Exception as e:
            logger.warning(f"[Bot] Admin 仪表盘关闭异常，跳过继续停机: {e}")

        logger.info("[Bot] 正在关闭 LLM 网关")
        await self.gateway.close()

        logger.info("[Bot] 正在关闭 数据库")
        await self.db.close()

        logger.info("[Bot] 关闭完成")

    async def stop_gracefully(self) -> None:
        if self._stopping:
            return
        self._stopping = True
        # 全局任务取消已前移进 close() 的第一步（D-4），此处无需再取消一遍
        await self.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ Companion Bot")
    parser.add_argument("--config", default="config.toml", help="配置文件路径")
    parser.add_argument("--status", action="store_true", help="打印当前伴侣状态并退出")
    args = parser.parse_args()

    config = Config.load(args.config)

    if args.status:
        asyncio.run(print_status(config))
        return

    bot = CompanionBot(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def _signal_handler() -> None:
        logger.info("[Bot] 接收到退出信号")
        loop.create_task(bot.stop_gracefully())

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except (NotImplementedError, AttributeError):
            pass

    try:
        loop.run_until_complete(bot.run())
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("[Bot] 进程已中断退出")
    finally:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


if __name__ == "__main__":
    main()
