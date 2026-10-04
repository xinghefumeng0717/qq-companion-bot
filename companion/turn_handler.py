"""单轮对话流水线处理器 (companion/turn_handler.py)
从 main.py 圈养式解耦：承接消息聚合回调，负责多模态视觉感知、提示词组装、LLM流式生成、分段打字发送、落库、回忆加固与观察者结算触发。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime
from typing import Any, Callable, Coroutine, Dict, List, Optional, Tuple

from companion.assembler import PromptAssembler
from companion.config import Config, TimingConfig
from companion.gateway import LLMGateway
from companion.memory import MemoryManager
from companion.observer import Observer
from companion.persona import holiday_span
from companion.proactive import ProactiveScheduler
from companion.prompts import VISION_PERCEPTION_PROMPT
from companion.replier import (
    Replier,
    is_silence_output,
    strip_face_markers,
    strip_voice_markers,
)
from companion.stickers import image_to_base64_data_url

logger = logging.getLogger("companion")


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
        try:
            holidays = self.config.get_holidays()
        except Exception as e:
            logger.warning(f"[Timing] 读取节假日列表失败，按无节假日处理: {e}")
            holidays = []
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), holidays)
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
        FIXES22：语音同理——没人一边打字一边发语音，语音段不参与打字表演的时长计算
        （它会走自己的"按住说话"停顿）。
        """
        if not self._typing_on:
            return
        duration = self.calc_typing_duration(
            strip_voice_markers(strip_face_markers(text)), is_first_reply
        )
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

    async def _check_voice_gate(self) -> Tuple[bool, str]:
        """语音能不能用：开关 → 日上限 → 作息场景（FIXES22 任务3 第1条）

        作息文案取**这一轮提示词里已经算好的那一份**（`last_assembled_prompt` 里的
        【她此刻】行）还是重新算？重新算：assembler 算它时带了 holiday_span 等参数，
        这里再算一遍要复现那些参数才对得上。取不到就按"允许"处理（宁可多试一次，
        合成失败也会降级）。
        拿不到 TTSManager（老调用方/测试）时一律 False：默认关 = 安全。
        """
        tts = getattr(self, "tts", None)
        if tts is None:
            return False, "没有装配 TTSManager"
        activity = ""
        try:
            activity = await self._current_activity_text()
        except Exception as e:  # 作息查不到不该挡住主流程
            logger.debug(f"[Timing] 取作息文案失败（语音闸门按允许处理）: {e}")
        return await tts.check_gate(activity)

    async def _current_activity_text(self) -> str:
        """当前活动文案（与 assembler 同一份口径：节假日段长也算进去）"""
        now_dt = datetime.now()
        today = now_dt.strftime("%Y-%m-%d")
        span = holiday_span(today, self.config.get_holidays())
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

        # 2. 图像多模态处理与降级
        image_data_url = None
        target_model = self.config.llm.text_model

        if image_path:
            data_url, err = image_to_base64_data_url(image_path)
            if err:
                # 超过 10MB 等错误回复
                await self.send_chunk_fn({"type": "text", "content": err})
                return

            if self.config.llm.vision_model:
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
            # 引用一条空条目没有意义。
            _sync_image_desc_to_batch(batch, user_text)

        # 3. 提示词组装（编号块由 quote_targets 生成）
        #    FIXES22：**先问语音闸门再组装**——提示词层要不要写语音能力说明，
        #    取决于这一轮闸门开不开（关着时模型连 [voice:] 都不该认识）。
        #    所以闸门结论同时喂给 assemble_messages 与后面的 parse_reply。
        voice_allowed, voice_why = await self._check_voice_gate()
        messages, sys_prompt = await self.assembler.assemble_messages(
            user_text, image_data_url, numbered_batch=batch, voice_allowed=voice_allowed
        )
        logger.info(f"[Bot] 语音闸门: {'可用' if voice_allowed else '不可用'}（{voice_why}）")

        # 4. LLM 流式调用
        # 注意：typing 在这一步之前一定没开过——"生成失败/走神兜底前必须先收掉 typing"
        # 这条要求在本实现里由结构保证（开 typing 的代码在生成之后），不存在开着打字
        # 走进兜底分支的路径。
        reply_parts = []
        try:
            async for piece in self.gateway.stream_chat(
                messages=messages,
                model=target_model,
                purpose="main_chat",
            ):
                reply_parts.append(piece)
        except Exception as e:
            logger.error(f"[Bot] 调用 LLM 异常: {e}")
            # 兜底文案不能含括号/星号，否则会被旁白剥离正则整块剥除导致用户端静默
            reply_parts = ["刚刚走神了……你再说一次？"]

        full_reply = "".join(reply_parts).strip()
        logger.info(f"[Bot] LLM 回复全文: {full_reply}")

        # 5. 回复管道切段与打字延迟发送
        #    FIXES21：把批次传下去，编号才能翻译成 message_id
        #    FIXES22：voice_allowed 是第 3 步问出来的同一份结论（两次问会各读一次
        #    日计数；同一轮内不会变，用缓存的那份）。
        chunks, clean_record_text = self.replier.parse_reply(
            full_reply, quote_targets=batch, voice_allowed=voice_allowed
        )
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
        if not chunks:
            chunks = [{"type": "text", "content": "刚刚走神了……你再说一次？"}]
            clean_record_text = "刚刚走神了……你再说一次？"

        # 5.5 FIXES15：发送前演"正在输入"，演完再逐段发
        await self.play_typing_indicator(clean_record_text, is_first_reply)
        await self.replier.send_reply_chunks(chunks, self.send_chunk_fn)

        # 6. 本轮对话落库
        await self.memory.save_turn_pair(
            user_msg=user_text,
            bot_msg=clean_record_text,
            has_image=bool(image_path),
        )

        # 7. 回忆加固
        await self.memory.reinforce_memories(user_text)

        # 8. 观察者异步结算（不阻塞）
        asyncio.create_task(
            self.observer.settle_turn(
                user_message=user_text,
                assistant_reply=clean_record_text,
                user_image_path=image_path,
            )
        )
