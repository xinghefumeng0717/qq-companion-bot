"""单轮对话流水线处理器 (companion/turn_handler.py)
从 main.py 圈养式解耦：承接消息聚合回调，负责多模态视觉感知、提示词组装、LLM流式生成、分段打字发送、落库、回忆加固与观察者结算触发。
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from companion.assembler import PromptAssembler
from companion.config import Config, TimingConfig
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.observer import MEETUP_HISTORY_TURNS, Observer
from companion.persona import holiday_span
from companion.proactive import ProactiveScheduler
from companion.prompts import VISION_PERCEPTION_PROMPT, VOICE_FAILURE_NOTE, VOICE_NOT_SENT_NOTE, VOICE_SENT_NOTE
from companion.meetups import MeetupManager
from companion.voice_planner import (
    VoicePlanner,
    VOICE_FOLLOWUP_RE as _VOICE_FOLLOWUP_RE,
    strip_legacy_voice_markup,
)
from companion.runtime_state import TurnContext, runtime_v1_enabled
from companion.reply_check import (
    CANDIDATE_GRACE_SECONDS,
    check_continuity,
    check_reply,
    gather_candidates,
    pick_candidate,
    trim_echo_lead,
)
from companion.replier import (
    Replier,
    count_today_faces,
    face_daily_budget,
    fit_chunks,
    is_silence_output,
    quote_chunk,
    record_text_from_chunks,
    sanitize_voice_claims,
    strip_face_markers,
    typing_text_from_chunks_or_record,
)
from companion.stickers import image_to_base64_data_url

logger = logging.getLogger("companion")


def _identities(assembler: Any) -> Dict[str, str]:
    """质检提示词里她/他的身份，取自角色卡 world 与人物档案；拿不到就用默认说法。"""
    persona = getattr(assembler, "persona", None)
    world, user = getattr(persona, "world", None), getattr(persona, "user", None)
    out: Dict[str, str] = {}
    if isinstance(getattr(world, "her_identity", None), str):
        out["her_identity"] = world.her_identity
    if isinstance(getattr(user, "identity", None), str):
        out["his_identity"] = user.identity
    return out


# 一步看图时她回复末尾的那行描述。提示词约定写【看到：…】，实际模型会换括号写法
# （（看到：…）、(看到: …)、『看到：…』），也可能把括号写反（【看到：…)）或漏写闭合符。
# 只认"括号紧跟看到+冒号"这一形态：正文里不带括号的"看到"（"我刚看到你说的"）不命中。
_SEEN_NOTE_START_RE = re.compile(r"[【\[（(『「][ \t]*看到[ \t]*[：:][ \t]*")
_SEEN_NOTE_OPENERS = frozenset("【[（(『「")
_SEEN_NOTE_CLOSERS = frozenset("】]）)』」")

# FIXES27：他这一句是不是在讨要语音（"发个语音""让我听听你""唱一句"）。
# 只给作息闸门开一个口子：开关关着、日上限到了，点名也发不出去（见 TTSManager.check_gate）。


def _seen_note_span(text: str, start: int) -> Tuple[str, int]:
    """从描述正文起点找这一行描述的边界，返回 (描述正文, 描述结束下标)。

    描述里的嵌套括号按深度计数（"红油火锅（微辣），冒着热气"不会在第一个）收口），
    括号写反了（【看到：…)）也在那个闭合符收口；整行一个闭合符都没有 = 漏写，
    按"这行写在回复末尾"的约定一路剥到文本末尾——描述残尾漏到屏幕上就是上屏事故，
    宁可多剥也不漏。收口处紧跟的一串闭合符/空白一并吃掉，避免留下" ）"残渣。
    """
    depth, i = 1, start
    while i < len(text):
        ch = text[i]
        if ch == "\n":
            break
        if ch in _SEEN_NOTE_OPENERS:
            depth += 1
        elif ch in _SEEN_NOTE_CLOSERS:
            depth -= 1
            if depth <= 0:
                end = i + 1
                while end < len(text) and (text[end] in _SEEN_NOTE_CLOSERS or text[end] in " \t"):
                    end += 1
                if end < len(text) and text[end] != "\n":
                    end = i + 1
                return text[start:i], end
        i += 1
    closers = "".join(_SEEN_NOTE_CLOSERS)
    return text[start:].rstrip().rstrip(closers).rstrip(), len(text)


def split_seen_note(text: str) -> Tuple[str, str]:
    """剥掉回复里的【看到：…】描述行，返回 (发给他的回复, 图片描述)。有多行取最后一条。"""
    text = text or ""
    spans: List[Tuple[int, int, str]] = []
    pos = 0
    while True:
        m = _SEEN_NOTE_START_RE.search(text, pos)
        if m is None:
            break
        note, end = _seen_note_span(text, m.end())
        spans.append((m.start(), end, note))
        pos = end
    if not spans:
        return text, ""

    kept, cursor = [], 0
    for start, end, _ in spans:
        kept.append(text[cursor:start])
        cursor = end
    kept.append(text[cursor:])
    # 剥完的行尾空白、只剩空白的行、行首残留缩进都不该上屏
    lines = (line.strip() for line in "".join(kept).split("\n"))
    reply = "\n".join(line for line in lines if line)
    return reply, spans[-1][2].strip()[:120]


def _sync_image_desc_to_batch(batch: List[Dict[str, Any]], user_text: str) -> None:
    """把这一轮补上的图片描述写回批次里**那张被采用的照片**（FIXES21）

    坑（终审抓出来的）：聚合器一轮内多图时**只保留最后一张**（`_image_index`
    指向它，image_data_url 也只有那一张），所以视觉描述必须写回**那一条**。
    早先这里找的是"第一个 has_image 条目"——一轮里他连发两张图时，
    描述会挂到第一张的编号上，而真正被看图识别的是最后一张：
    她会以为第一张是张照片、最后一张是空条目，引用编号也全错位。
    没有 image_index 时（老数据/直接调用的测试）才退回"第一个 has_image"。

    拿不到带图条目就什么都不做：编号块里那条空着也只是少一个可引用目标，
    不该把整轮带崩。
    """
    if not batch:
        return
    marker = "[发来一张照片：" if "发来一张照片" in user_text else (
        "[发来一张图片" if "发来一张图片" in user_text else None
    )
    desc = user_text[user_text.rfind(marker) :] if marker else ""

    target_index = None
    for item in batch:
        if item.get("image_index") is not None:
            target_index = item["image_index"]
            break
    if target_index is None:
        for item in batch:
            if item.get("has_image"):
                target_index = item.get("index")
                break
    if target_index is None:
        return

    for item in batch:
        if item.get("index") != target_index:
            continue
        if desc and not item.get("text"):
            item["text"] = desc
        elif desc:
            item["text"] = f"{item['text']} {desc}".strip()
        return

# 对话进行中（她 5 分钟内回过话）typing 展示的上限：这时候她的打字是快的，
# 真按字数算会出现"回了 5 条后突然卡 20 秒"的假人感。任务书 §三.4 规定收紧到 8 秒。
# 不进 TimingConfig：这是节奏标定常量，不是需要按环境调的部署参数。
TYPING_INLINE_MAX = 8.0

# 他点名要语音后，"讨要"保持多久（期间作息闸门对他让路；开关与日上限照常拦）
VOICE_REQUEST_HOLD = timedelta(minutes=10)
# 他说"没收到/没听到"：只在聊的是语音时（话里提到语音/声音，或讨要还没兑现）才算
_NOT_RECEIVED_RE = re.compile(r"没(?:收到|听到|听见|声音)|收不到|听不到|没有声音")
# 只有无其它宾语的短句，才能结合上一轮真实语音投递记录理解；不靠物品黑名单。
_GENERIC_NOT_RECEIVED_RE = re.compile(
    r"^\s*(?:我|怎么|为什么|为啥)?\s*(?:还|又|都)?\s*"
    r"(?:没(?:有)?(?:收到|听到|听见)|收不到|听不到)"
    r"[\s诶哎唉啊呀呢嘛了？?！!]*$"
)


class TurnHandler:
    def __init__(
        self,
        config: Config,
        gateway: LLMGateway,
        assembler: PromptAssembler,
        replier: Replier,
        memory: MemoryManager,
        observer: Observer,
        proactive: ProactiveScheduler,
        send_chunk_fn: Callable[[Dict[str, Any]], Coroutine[Any, Any, None]],
        set_typing_fn: Optional[Callable[[bool], Coroutine[Any, Any, bool]]] = None,
        timing_config: Optional[TimingConfig] = None,
        tts: Optional[Any] = None,
    ):
        self.config = config
        self.gateway = gateway
        self.assembler = assembler
        self.replier = replier
        self.memory = memory
        self.observer = observer
        self.proactive = proactive
        self.send_chunk_fn = send_chunk_fn
        # FIXES15 注入链：本处理器不 import onebot，只拿一个"开/关正在输入"的回调。
        # 两个新参数默认 None = 整个时机表演关掉，行为与改动前逐格一致（既有测试与
        # 冒烟脚本零改动即得旧行为）；生产由 main.py 显式注入 config.timing。
        self.set_typing_fn = set_typing_fn
        self.timing = timing_config
        # FIXES22：语音闸门问的是它；默认 None = 语音整体关着（既有测试与旧调用方零改动）
        self.tts = tts
        self.voice_planner = VoicePlanner(
            getattr(config.reply, "voice_frequency", "frequent"),
            max_chars=self._voice_max_chars(),
        )
        self.runtime_events = None
        self.turn_tracer = None
        self._observer_tasks = set()
        # 跨轮只保留一份真实投递事实；请求保持与投递投诉语境互不推导。
        self._last_voice_status = "none"
        self._last_voice_reason = ""
        self._last_voice_at: Optional[datetime] = None
        self._last_turn_voice_status = "none"
        self._last_turn_at: Optional[datetime] = None
        self._voice_requested = False
        self._voice_activity = ""
        self._voice_request_until: Optional[datetime] = None
        self._voice_retry_used = False
        self._typing_on = (
            timing_config is not None
            and timing_config.typing_indicator_enabled
            and set_typing_fn is not None
        )

    # ==========================================
    # FIXES15 回复时机人格化
    # ==========================================

    async def _is_first_reply(self) -> bool:
        """本轮是不是"她拿起手机的第一条"。

        判据：turns 表最近一条 assistant 消息距今 >= active_conversation_window。
        从没回过话（None）按首条处理——那正是最该慢的场合。
        """
        window = self.timing.active_conversation_window
        last_dt = await self.memory.get_last_assistant_turn_time()
        if last_dt is None:
            return True
        elapsed = (datetime.now() - last_dt).total_seconds()
        return elapsed >= window

    def _holidays(self) -> List[str]:
        """节假日走 assembler 的门控来源：follow_public_holidays=false 时它返回空表。"""
        getter = getattr(self.assembler, "get_holidays", None)
        if not callable(getter):
            return []
        try:
            holidays = getter()
        except Exception as e:
            logger.warning(f"[Timing] 读取节假日列表失败，按无节假日处理: {e}")
            return []
        if not isinstance(holidays, list):
            return []
        return [d for d in holidays if isinstance(d, str)]

    def current_busy_state(self) -> Tuple[str, bool]:
        """此刻她忙不忙：返回 (活动文案, is_busy)。

        走 persona.get_current_activity_detail —— 命中 daily_routine 的结构化条目
        （上课/练琴/合练/睡觉）算忙，回退文案与长假文案算闲。
        拿不到 persona（assembler 是 mock 等异常构造）时按闲处理：闲=延迟短，最保守。
        """
        persona = getattr(self.assembler, "persona", None)
        if persona is None or not hasattr(persona, "get_current_activity_detail"):
            return "未知（取不到作息表）", False
        now_dt = datetime.now()
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), self._holidays())
        return persona.get_current_activity_detail(
            now_dt.hour, now_dt.weekday(), holiday_span=span
        )

    def choose_first_reply_delay(self, is_busy: bool) -> float:
        """首条延迟 D：忙 1~10 分钟，闲 5~30 秒。非首条不走这里（D=0）。"""
        t = self.timing
        if is_busy:
            return random.uniform(t.first_reply_busy_delay_min, t.first_reply_busy_delay_max)
        return random.uniform(t.first_reply_free_delay_min, t.first_reply_free_delay_max)

    def calc_typing_duration(self, text: str, is_first_reply: bool) -> float:
        """typing 展示时长：每 10 字 2 秒，钳在 typing_min~typing_max；
        非首条（对话中）再收紧一档到 TYPING_INLINE_MAX。"""
        t = self.timing
        raw = len(text) / 10.0 * t.typing_seconds_per_10chars
        duration = max(t.typing_min, min(t.typing_max, raw))
        if not is_first_reply:
            duration = min(duration, TYPING_INLINE_MAX)
        return duration

    async def set_typing(self, typing: bool) -> None:
        """调注入的 typing 回调。任何异常/False 返回都只记日志，绝不影响主流程。"""
        if not self._typing_on:
            return
        try:
            ok = await self.set_typing_fn(typing)
            if ok is False:
                logger.debug(
                    f"[Timing] 正在输入{'开' if typing else '关'}未生效（NapCat 未连接或已降级）"
                )
        except Exception as e:
            logger.warning(
                f"[Timing] 正在输入回调异常（{'开' if typing else '关'}），静默降级: {e}"
            )

    async def close_typing(self) -> None:
        """收掉 typing。沉默分支必须先调它：她选择不回，屏幕上不能留着"正在输入"。"""
        await self.set_typing(False)

    async def prepare_reply_timing(self) -> bool:
        """首条判定 + 静默期等待（本轮的第一段等待）。返回本轮是否首条。

        等待拆两段是任务书 §三.4 的硬要求：先睡整段 D（她"还没看到消息"），
        生成完成后再按实际文字量算 T_typing 演 typing——T_typing 依赖生成结果，
        睡在生成之前就无从算起。
        """
        if self.timing is None:
            return False
        is_first = await self._is_first_reply()
        if not (self.timing.timing_enabled and is_first):
            return is_first

        activity, is_busy = self.current_busy_state()
        delay = self.choose_first_reply_delay(is_busy)
        logger.info(
            f"[Timing] 首条延迟 {delay:.1f} 秒（作息：{activity}，"
            f"{'忙' if is_busy else '闲'}）"
        )
        await asyncio.sleep(delay)
        return is_first

    async def play_typing_indicator(self, text: str, is_first_reply: bool) -> None:
        """"正在输入"视觉签名：开 → 等 T_typing → 关。

        只在确认要发送之后调用（沉默分支不进来）。typing 期间又有新消息进来**不打断**：
        聚合器只作用在接收侧，发送侧的表演会自己演完。
        任一环节异常都降级为"不演了，照发"，绝不因打字状态丢掉一条消息。

        FIXES20：打字时长只按"她真正打出来的字"算——记录里的 [face:标签] 标记先抹掉。
        一个 3 字短句挂个脸，不该因为标记字符把 T_typing 拉长近一倍。
        FIXES22：语音整段不算（语音是"说"出来的，她没在打那行字）。这条的口径在
        **调用方**就已经落实了（传进来的是 `typing_text_from_chunks_or_record` 的结果，
        voice 段压根不在里面），所以这里不必再抹一遍——
        早先这里写的是"抹 [voice:] 标记"，而记录里标记早被 chunk_record_text
        换成了「（语音消息）」前缀，那句抹除是**空转**的（注释与实现不符的经典例子）。
        DEEP_AUDIT B-6：纯语音轮不拿记录文本兜底（否则语音正文被当成打字，实测 10.0 秒），
        此时 text 为空串、时长落在 typing_min 这一档。
        """
        if not self._typing_on:
            return
        duration = self.calc_typing_duration(strip_face_markers(text), is_first_reply)
        if duration <= 0:
            return
        try:
            await self.set_typing(True)
            logger.info(
                f"[Timing] 正在输入 {duration:.1f} 秒（"
                f"{'首条' if is_first_reply else '对话中'}，{len(text)} 字）"
            )
            await asyncio.sleep(duration)
        except Exception as e:
            logger.warning(f"[Timing] 正在输入展示异常，降级为直接发送: {e}")
        finally:
            await self.close_typing()

    def _voice_max_chars(self) -> int:
        """单条语音字数上限（从 TTSManager 的配置取，取不到用默认 60）

        截断必须在解析期做（记录与实发一致），所以上限得在 parse_reply 之前问出来。
        没有 TTSManager（老调用方/测试）时用默认值——反正语音也不可用。
        """
        tts = getattr(self, "tts", None)
        cfg = getattr(tts, "config", None)
        value = getattr(cfg, "max_chars", 60)
        try:
            return int(value)
        except (TypeError, ValueError):
            return 60

    async def _check_voice_gate(self, user_text: str = "") -> Tuple[bool, str]:
        """语音能不能用：开关 → 日上限 → 作息场景（FIXES22 任务3 第1条）

        作息文案取**这一轮提示词里已经算好的那一份**（`last_assembled_prompt` 里的
        【她此刻】行）还是重新算？重新算：assembler 算它时带了 holiday_span 等参数，
        这里再算一遍要复现那些参数才对得上。取不到就按"允许"处理（宁可多试一次，
        合成失败也会降级）。
        拿不到 TTSManager（老调用方/测试）时一律 False：默认关 = 安全。

        FIXES27：`awake_override=True` 是这里的**语义常量**，不是开关——本方法只在
        handle_turn 里被调用，也就是他正在跟她聊的轮次，她不可能是睡着的；卡里写
        "在缘缘庄睡着"只是作息表的措辞，不能当成"她没空说话"（生产上它把语音整夜关死）。
        `user_requested` 看他这句话有没有点名要语音：点名了就再开作息那扇门，
        但开关与日上限照常拦。
        """
        # 他点名要语音以后，这个"讨要"一直有效，直到语音真发出去或过了 VOICE_REQUEST_HOLD
        # （生产实锤：讨价还价三四个来回，"成交""没发！耍赖"这几句里没有"语音"二字，
        # 放行只管一句时，后面几轮闸门全关，她只能用文字假装发了）
        now_dt = datetime.now()
        explicit_request = VoicePlanner.explicit_request(user_text)
        if explicit_request:
            self._voice_request_until = now_dt + VOICE_REQUEST_HOLD
        until = getattr(self, "_voice_request_until", None)
        pending = until is not None and now_dt < until
        self._voice_retry_used = pending and not explicit_request
        requested = explicit_request or pending
        self._voice_requested = requested

        tts = getattr(self, "tts", None)
        if tts is None:
            return False, "没有装配 TTSManager"
        activity = ""
        try:
            activity = await self._current_activity_text()
        except Exception as e:  # 作息查不到不该挡住主流程
            logger.debug(f"[Timing] 取作息文案失败（语音闸门按允许处理）: {e}")
        self._voice_activity = activity
        return await tts.check_gate(
            activity,
            awake_override=True,
            user_requested=requested,
        )

    def _latest_voice_delivery(self) -> Tuple[str, str]:
        """最近一次真实语音事件，不被后来的普通文字消息冲掉。"""
        proactive = getattr(self, "proactive", None)
        events = []
        for at, status, reason in (
            (getattr(self, "_last_voice_at", None), getattr(self, "_last_voice_status", "none"), getattr(self, "_last_voice_reason", "")),
            (getattr(proactive, "last_voice_at", None), getattr(proactive, "last_voice_status", "none"), getattr(proactive, "last_voice_reason", "")),
        ):
            if isinstance(status, str) and status != "none":
                events.append((at if isinstance(at, datetime) else datetime.min, status, reason))
        if not events:
            return "none", ""
        _, status, reason = max(events, key=lambda item: item[0])
        return status, reason if isinstance(reason, str) else ""

    def _latest_turn_voice_status(self) -> str:
        """最近一条助手消息是否涉及语音，用于无宾语“没收到”的紧邻语境。"""
        proactive = getattr(self, "proactive", None)
        own_at = getattr(self, "_last_turn_at", None)
        other_at = getattr(proactive, "last_turn_at", None)
        own_at = own_at if isinstance(own_at, datetime) else datetime.min
        other_at = other_at if isinstance(other_at, datetime) else datetime.min
        status = (
            getattr(proactive, "last_turn_voice_status", "none")
            if other_at > own_at else getattr(self, "_last_turn_voice_status", "none")
        )
        return status if isinstance(status, str) else "none"

    def _missing_voice_context(self, user_text: str) -> bool:
        """只按明确语音词句或上一轮真实投递记录判断，不按讨要保持期猜测。"""
        text = user_text or ""
        if not _NOT_RECEIVED_RE.search(text):
            return False
        explicit = bool(re.search(r"语音|声音", text) or _VOICE_FOLLOWUP_RE.search(text))
        if explicit:
            return True
        attempted = self._latest_turn_voice_status() in {"sent", "text_fallback", "failed", "unconfirmed"}
        return attempted and bool(_GENERIC_NOT_RECEIVED_RE.fullmatch(text))

    def _asks_about_missing_voice(self, user_text: str) -> bool:
        """他在问一条并未确认发出的语音；OneBot 已接受则走另一条事实说明。"""
        return self._missing_voice_context(user_text) and self._latest_voice_delivery()[0] != "sent"

    def _voice_delivery_note(self, user_text: str) -> str:
        if not self._missing_voice_context(user_text):
            return ""
        status, reason = self._latest_voice_delivery()
        if status == "sent":
            return VOICE_SENT_NOTE
        if status in {"text_fallback", "failed", "unconfirmed"}:
            return reason or VOICE_FAILURE_NOTE
        return VOICE_NOT_SENT_NOTE

    async def _current_activity_text(self) -> str:
        """当前活动文案（与 assembler 同一份口径：节假日段长也算进去）"""
        now_dt = datetime.now()
        today = now_dt.strftime("%Y-%m-%d")
        span = holiday_span(today, self._holidays())
        return self.assembler.persona.get_current_activity(
            now_dt.hour, now_dt.weekday(), holiday_span=span
        )

    async def handle_turn(
        self,
        user_text: str,
        image_path: Optional[str],
        quote_targets: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """聚合完毕后，处理完整的一轮对话

        FIXES21：`quote_targets` 是本轮聚合批次的每条消息
        （`{"index": 1, "text": "...", "message_id": 123, "has_image": False}`）。
        它干两件事：①提示词里把这一批编号呈现（`[1] ... / [2] ...`），
        ②她输出 `[quote:N]` 时把编号翻译回真实 message_id。
        取不到/为 None 一律照旧（单条消息的老路径一个字不变），不影响主流程。
        """
        logger.info(
            f"[Bot] 处理新一轮输入: '{user_text}', image={image_path}, "
            f"批次={len(quote_targets) if quote_targets else 0} 条"
        )
        batch = list(quote_targets or [])
        runtime_enabled = runtime_v1_enabled(self.config.reply, self.assembler)
        # 配置与组装器共用同一开关，沙箱只设其中一处也不会半新半旧
        self.assembler.runtime_state_v1 = runtime_enabled
        planned_voice = False
        runtime_plan = None

        # 1. 重置主动消息未回计数
        await self.proactive.reset_unanswered_count()

        # 1.5 FIXES15：首条判定 + 静默期等待（在视觉处理之前，她"还没看到"）
        # 整段包 try/except：时机表演是加分项，出问题一律降级为"立即生成立即发送"
        is_first_reply = False
        try:
            is_first_reply = await self.prepare_reply_timing()
        except Exception as e:
            logger.warning(f"[Timing] 回复时机环节异常，降级为立即发送: {e}")
            is_first_reply = False

        # 首条延迟结束后再冻结本轮时间与状态，避免提示词和账本早取几分钟
        state_block = (
            await self.runtime_events.render_prompt()
            if runtime_enabled and self.runtime_events is not None else ""
        )
        context = TurnContext(datetime.now(), user_text, state_block)

        # 2. 图像多模态处理与降级
        image_data_url = None
        target_model = self.config.llm.text_model

        if image_path:
            data_url, err = image_to_base64_data_url(image_path)
            if err:
                # 超过 10MB 等错误回复
                await self.send_chunk_fn({"type": "text", "content": err})
                return

            if getattr(self.config.reply, "vision_direct", False) is True:
                # 一步看图：图直接给主聊模型，描述等她回完从【看到：…】里取（见第 4.1 步）
                image_data_url = data_url
            elif self.config.llm.vision_model:
                try:
                    logger.info("[Bot] 使用 Flash 视觉模型提取图片场景细节...")
                    desc_resp = await self.gateway.chat(
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": VISION_PERCEPTION_PROMPT},
                                    {"type": "image_url", "image_url": {"url": data_url}},
                                ],
                            }
                        ],
                        model=self.config.llm.vision_model,
                        purpose="vision_perception",
                    )
                    clean_desc = desc_resp.strip()[:120]
                    logger.info(f"[Bot] 视觉提取成功: {clean_desc}")
                    user_text = f"{user_text} [发来一张照片：{clean_desc}]".strip()
                except Exception as e:
                    logger.warning(f"[Bot] 视觉模型提取异常，降级为占位符: {e}")
                    user_text = f"{user_text} [发来一张图片，但没能看清]".strip()
                # 设计承重墙：两段式视觉设计（flash 看图 -> 描述注入主提示词），置空避免激活 assembler 的直接多模态分支
                image_data_url = None
            else:
                user_text = (user_text + " [对方发来一张图片，你看不到内容]").strip()

            # FIXES21：视觉描述也要写回**对应那一条**的编号条目。
            # 不写回的话编号块里那条是空的（她只看到"他发了张图"却不知道是哪一条），
            # 引用一条空条目没有意义。一步看图时描述要等回复出来，到第 4.1 步再写回
            if not image_data_url:
                _sync_image_desc_to_batch(batch, user_text)

        # 2.9 线下见面：到点的约定先补纪要，这样他这句"昨天那锅底"她接得住（没到点不发 API）
        meetups = getattr(self.assembler, "meetups", None)
        if isinstance(meetups, MeetupManager):
            try:
                await meetups.process_due()
            except Exception as e:
                logger.warning(f"[Bot] 见面纪要处理异常: {e}")

        self._active_user_text = user_text

        # 3. 提示词组装（编号块由 quote_targets 生成）
        #    FIXES22：**先问语音闸门再组装**——提示词层要不要写语音能力说明，
        #    取决于这一轮闸门开不开（关着时模型连 [voice:] 都不该认识）。
        #    所以闸门结论同时喂给 assemble_messages 与后面的 parse_reply。
        voice_allowed, voice_why = await self._check_voice_gate(user_text)
        # FIXES27：TTS 装着且开着、闸门却没过（比如真在合练）时，提示词里要告诉她
        # "这会儿只能打字"——否则她会答应了发语音却只发出文字，自己还浑然不觉。
        # getattr 防御：老测试装配的 handler 没有 tts 属性。
        tts_cfg = getattr(getattr(self, "tts", None), "config", None)
        voice_gated_note = bool(getattr(tts_cfg, "enabled", False))
        # 无论是否开启运行时账本，都用同一份真实投递事实回答"没收到"。
        delivery_note = self._voice_delivery_note(user_text)
        assemble_kwargs: Dict[str, Any] = {
            "numbered_batch": batch,
            "voice_allowed": voice_allowed,
            "voice_gated_note": voice_gated_note,
            "now": context.now,
            "runtime_state_block": context.state_block,
        }
        if delivery_note:
            assemble_kwargs["voice_delivery_note"] = delivery_note
        messages, sys_prompt = await self.assembler.assemble_messages(
            user_text, image_data_url, **assemble_kwargs
        )
        logger.info(f"[Bot] 语音闸门: {'可用' if voice_allowed else '不可用'}（{voice_why}）")

        # 4. LLM 流式调用
        # 注意：typing 在这一步之前一定没开过——"生成失败/走神兜底前必须先收掉 typing"
        # 这条要求在本实现里由结构保证（开 typing 的代码在生成之后），不存在开着打字
        # 走进兜底分支的路径。
        reply_parts = []
        candidate_texts: List[str] = []
        picked_index = 0
        # [reply].candidates > 1：并行生成几条，挑没复述/点评/比喻的那条（等待时间≈最慢的那条）
        n_cand = getattr(self.config.reply, "candidates", 1)
        n_cand = n_cand if isinstance(n_cand, int) and n_cand > 1 else 1
        if image_data_url:
            n_cand = 1  # 一步看图：同一张图看三遍费用翻三倍，这一轮只生成一条

        async def _generate_one() -> str:
            parts = []
            async for piece in self.gateway.stream_chat(
                messages=messages,
                model=target_model,
                purpose="main_chat",
            ):
                parts.append(piece)
            return "".join(parts)

        try:
            if n_cand > 1:
                texts = await gather_candidates(
                    _generate_one, n_cand, grace=CANDIDATE_GRACE_SECONDS
                )
                picked, cand_info = pick_candidate(
                    user_text, texts,
                    tiebreak=getattr(self.config.reply, "candidate_tiebreak", "warm"),
                )
                logger.info(
                    f"[Bot] 多候选择优: 收到 {len(texts)}/{n_cand} 条，毛病数 {cand_info['flaws']}"
                    f" → 选第 {cand_info['picked'] + 1} 条"
                )
                candidate_texts = texts
                picked_index = cand_info["picked"]
                reply_parts = [picked]
            else:
                reply_parts = [await _generate_one()]
                candidate_texts = list(reply_parts)
        except Exception as e:
            logger.error(f"[Bot] 调用 LLM 异常: {e}")
            # 兜底文案不能含括号/星号，否则会被旁白剥离正则整块剥除导致用户端静默
            reply_parts = ["刚刚走神了……你再说一次？"]
            candidate_texts = list(reply_parts)
            picked_index = 0

        full_reply = "".join(reply_parts).strip()
        logger.info(f"[Bot] LLM 回复全文: {full_reply}")

        # 4.1 一步看图：剥掉【看到：…】，描述并进他这句话的记录（下一轮和观察者靠它知道他发过什么）
        if image_data_url:
            full_reply, seen = split_seen_note(full_reply)
            if seen:
                user_text = f"{user_text} [发来一张照片：{seen}]".strip()
            else:
                user_text = f"{user_text} [发来一张图片]".strip()
            _sync_image_desc_to_batch(batch, user_text)

        # 4.3 语句连贯性：先保留预选，只有硬伤才换候选或最小修补，失败一律照原稿。
        continuity_info = None
        if (
            getattr(self.config.reply, "continuity_check", False) is True
            and reply_parts != ["刚刚走神了……你再说一次？"]
            and not is_silence_output(full_reply)
        ):
            try:
                continuity_history = await self.memory.get_recent_turns(limit=3)
            except Exception:
                continuity_history = []
            full_reply, continuity_info = await check_continuity(
                self.gateway,
                user_text,
                candidate_texts,
                picked_index,
                continuity_history,
                timeout=getattr(self.config.reply, "continuity_timeout", 8.0),
                tiebreak=getattr(self.config.reply, "candidate_tiebreak", "warm"),
                **_identities(self.assembler),
            )
            reply_parts = [full_reply]
            if continuity_info.get("changed"):
                logger.info(f"[Bot] 连贯性闸门: {continuity_info}")

        # 4.4 首泡回声裁剪（[reply].trim_echo，默认关）：第一个气泡在复述/点评他、后面还有话就删掉它
        if getattr(self.config.reply, "trim_echo", False) is True and reply_parts != ["刚刚走神了……你再说一次？"]:
            full_reply, cut = trim_echo_lead(user_text, full_reply)
            if cut:
                logger.info(f"[Bot] 首泡回声裁剪: 《{cut}》")

        # 4.5 生成后质检（[reply].self_check，默认关）：抓"助手腔"并改写；失败一律照发原稿
        if getattr(self.config.reply, "self_check", False) is True and reply_parts != ["刚刚走神了……你再说一次？"]:
            full_reply, _ = await check_reply(
                self.gateway, user_text, full_reply,
                timeout=getattr(self.config.reply, "self_check_timeout", 10.0),
                **_identities(self.assembler),
            )

        # 她只产正文。旧语音标记仅做确定性清理，普通括号与普通措辞不参与语音判断。
        full_reply = strip_legacy_voice_markup(full_reply)
        requested = bool(getattr(self, "_voice_requested", False))
        runtime_plan = self.voice_planner.plan(
            user_text, full_reply,
            voice_allowed=voice_allowed,
            requested=requested,
        )
        planned_voice = runtime_plan.has_voice
        voice_context = requested or planned_voice or self._missing_voice_context(user_text)
        body_text = runtime_plan.body_text if planned_voice else full_reply
        sanitized_reply = sanitize_voice_claims(
            body_text,
            voice_allowed=planned_voice,
            previous_voice_sent=self._latest_voice_delivery()[0] == "sent",
            voice_context=voice_context,
        )
        if sanitized_reply != body_text:
            logger.warning("[Bot] 明确语音上下文中已删去未确认投递断言")
        full_reply = sanitized_reply

        # 5. 文字由原解析器处理；语音段由规划器直接产生，不再编码回字符串二次解析。
        face_budget = None
        if getattr(self.assembler.persona.world, "face_usage", "normal") == "rare":
            used_faces = await count_today_faces(self.memory.db)
            face_budget = face_daily_budget(self.assembler.persona.world, used_faces)
        chunks, clean_record_text = self.replier.parse_reply(
            full_reply,
            quote_targets=batch,
            voice_allowed=False,
            voice_max_chars=self._voice_max_chars(),
            face_budget=face_budget,
        )
        if planned_voice:
            voice_chunk = {
                "type": "voice", "content": runtime_plan.voice_text,
                "_runtime_voice": True,
                "_voice_activity": getattr(self, "_voice_activity", ""),
                "_voice_requested": requested,
            }
            before_text = getattr(runtime_plan, "before_text", None)
            if before_text is None:
                chunks.append(voice_chunk)
            else:
                before_chunks, _ = self.replier.parse_reply(
                    before_text, quote_targets=batch, voice_allowed=False,
                    voice_max_chars=self._voice_max_chars(), face_budget=face_budget,
                )
                chunks.insert(min(len(before_chunks), len(chunks)), voice_chunk)
            chunks = fit_chunks(chunks, self.replier.config.max_chunks)
        if runtime_plan.quote_index is not None and chunks:
            quote = quote_chunk(runtime_plan.quote_index, batch)
            if quote is not None:
                # QQ 对 reply+record 的组合消息兼容有问题：语音绝不挂引用。
                target = next((c for c in chunks if c.get("type") in ("text", "face", "combo")), None)
                if target is not None:
                    target["_quote"] = quote
                else:
                    logger.info("[Bot] 本轮只有语音，引用不挂到 record 段上")
        for chunk in chunks:
            if chunk.get("type") == "voice":
                chunk["_voice_user_text"] = user_text
        if self.replier.is_silence_decision(full_reply, batch):
            # [沉默]（FIXES13）：她选择不回。不发送、不落 assistant 记录、跳过 observer 结算，
            # 但用户消息照常落库（他确实说了这句），并照常做回忆加固。
            # 沉默优先于 typing（FIXES15 §三.6）：屏幕上不能留着"正在输入"。
            # FIXES21 终审修正：判据必须是**剥掉行首 [quote:N] 之后**的文本，
            # 且"只有引用没正文"同样算沉默；直接拿 full_reply 判会被
            # "[quote:2]\n[沉默]" 甩进下面的 `if not chunks` 兜底，
            # 真的把"刚刚走神了……"发出去（沉默权被架空）。
            await self.close_typing()
            logger.info("[Bot] 本轮她选择沉默：不发送、不落 assistant 记录、跳过 observer 结算")
            await self.memory.save_turn_pair(
                user_msg=user_text, bot_msg=None, has_image=bool(image_path)
            )
            await self.memory.reinforce_memories(user_text)
            return
        clean_record_text = record_text_from_chunks(chunks) or clean_record_text
        if not chunks:
            chunks = [{"type": "text", "content": "刚刚走神了……你再说一次？"}]
            clean_record_text = "刚刚走神了……你再说一次？"

        # 5.5 FIXES15：发送前演"正在输入"，演完再逐段发
        # 打字时长的口径是"她真打出来的字"：从**段**里取，不是从落库记录里抹标记
        # （记录里 voice 已经是「（语音消息）…」形态，标记早没了，抹不掉）。
        # DEEP_AUDIT B-6：纯语音轮**不**回退记录文本——否则语音正文被当成她打的字
        # （实测 10.0 秒）。回退只在"既无文字段也无语音段"时发生。
        typing_text = typing_text_from_chunks_or_record(chunks, clean_record_text)
        await self.play_typing_indicator(typing_text, is_first_reply)
        await self.replier.send_reply_chunks(chunks, self.send_chunk_fn)
        # 语音发送失败时 main 会把 voice 段原地改成 text；落库必须按实发结果重算。
        clean_record_text = record_text_from_chunks(chunks) or clean_record_text
        voice_sent = any(
            c.get("type") == "voice" and c.get("_voice_delivery") == "sent"
            for c in chunks
        )
        # 只有规划器真的形成了语音段才记投递结果；普通聊天绝不产生语音失败。
        if planned_voice:
            if voice_sent:
                self._last_voice_status = "sent"
            elif any(c.get("_runtime_voice") and c.get("_voice_delivery") == "text" for c in chunks):
                self._last_voice_status = "text_fallback"
            elif any(c.get("_runtime_voice") and c.get("_voice_delivery") == "failed" for c in chunks):
                self._last_voice_status = "failed"
            else:
                self._last_voice_status = "unconfirmed"
            self._last_voice_reason = next(
                (c.get("_voice_failure_reason", "") for c in chunks if c.get("_runtime_voice")),
                "",
            ) or ("" if voice_sent else VOICE_FAILURE_NOTE)
            self._last_voice_at = datetime.now()
        self._last_turn_voice_status = self._last_voice_status if planned_voice else "none"
        self._last_turn_at = datetime.now()
        if voice_sent:
            self._voice_request_until = None  # 讨要已兑现
        elif planned_voice:
            logger.warning(f"[Bot] 语音投递状态: {self._last_voice_status}")
        if runtime_enabled and self.runtime_events is not None:
            try:
                blocked = (
                    voice_why
                    if not voice_sent and (planned_voice or requested) else ""
                )
                await self.runtime_events.record_delivery_chunks(
                    chunks, full_reply, now=context.now, blocked_reason=blocked,
                )
            except Exception as e:
                logger.warning(f"[Bot] 运行时语音账本写入失败，不影响本轮回复: {e}")
        if runtime_enabled and self.turn_tracer is not None:
            try:
                self.turn_tracer.write(
                    context,
                    candidates=candidate_texts,
                    picked=picked_index,
                    continuity=continuity_info,
                    reply=full_reply,
                    voice_plan=(
                        {"mode": runtime_plan.mode, "voice_text": runtime_plan.voice_text,
                         "reason": runtime_plan.reason}
                        if runtime_plan is not None else None
                    ),
                    final_chunks=[
                        {"type": c.get("type"), "content": c.get("content"),
                         "delivery": c.get("_voice_delivery"),
                         "runtime_voice": bool(c.get("_runtime_voice"))}
                        for c in chunks
                    ],
                    voice_sent=voice_sent,
                )
            except Exception as e:
                logger.warning(f"[Bot] 轮次追踪写入失败，不影响本轮回复: {e}")

        # 6. 本轮对话落库
        await self.memory.save_turn_pair(
            user_msg=user_text,
            bot_msg=clean_record_text,
            has_image=bool(image_path),
        )

        # 7. 回忆加固
        await self.memory.reinforce_memories(user_text)

        # 8. 观察者异步结算（不阻塞）
        #    见面识别：约定常跨轮说完（她提议地点、他下一轮才定时间），把最近几轮原文一起递过去，
        #    观察者才看得到地点和事。读失败不影响结算（observer 侧拿不到就按本轮两句判）。
        try:
            recent_turns = await self.memory.get_recent_turns(limit=MEETUP_HISTORY_TURNS)
        except Exception as e:
            logger.warning(f"[Bot] 读取近期轮次失败，见面识别按本轮两句判: {e}")
            recent_turns = None
        task = asyncio.create_task(
            self.observer.settle_turn(
                user_message=user_text,
                assistant_reply=clean_record_text,
                user_image_path=image_path,
                recent_turns=recent_turns,
            )
        )
        self._observer_tasks.add(task)
        task.add_done_callback(self._observer_tasks.discard)
