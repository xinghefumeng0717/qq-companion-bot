"""提示词组装器 (assembler.py)
按 §6.2 规定的顺序与规范，实时组装 system prompt 与上下文 messages。
每个分段生成均有 try/except 隔离保护，单块失败不影响主对话。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from companion.affection import AffectionEngine
from companion.datefmt import relativize_dates
from companion.db import Database, TIME_FORMAT, parse_dt
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona, holiday_span, is_sleep_activity
from companion.prompts import (
    face_prompt_block,
    IMAGE_SEEN_INSTRUCTION,
    QUOTE_USAGE_EXAMPLES,
    QUOTE_USAGE_RULES,
    SYSTEM_PROMPT_TEMPLATE,
    VOICE_PROMPT_BLOCK,
    VOICE_PLANNER_RULES,
    VOICE_PLANNER_EXPLICIT_RULES,
    VOICE_PLANNER_OFF_RULES,
    VOICE_UNAVAILABLE_NOTE,
    format_numbered_batch,
    get_frustration_description,
    get_mood_description,
    get_neglect_description,
    get_trust_description,
    holiday_prompt_note,
)
from companion.replier import strip_voice_record_prefix_for_history
from companion.safety import SafetyChecker
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]

# 他的消息和上一句隔了这么久，就在消息前标上时间（历史里没有时间戳，隔两天和隔两分钟
# 在模型眼里一样：多天测试实锤周六下午她说"快去睡"，把两天后的消息当成道完晚安折回来）
GAP_NOTE_HOURS = 3.0


def gap_note(now_dt: datetime, prev_dt: Optional[datetime], show_date: bool = True) -> str:
    """"（10-17 15:01 星期六，离上一句已经过了 1 天 18 小时）"；间隔不够返回空串。
    show_date=False（卡的 world.date_style = relative）：不带月日，只留星期和时刻。"""
    if prev_dt is None:
        return ""
    hours = (now_dt - prev_dt).total_seconds() / 3600
    if hours < GAP_NOTE_HOURS:
        return ""
    days, rest = divmod(int(hours), 24)
    span = f"{days} 天 {rest} 小时" if days and rest else (f"{days} 天" if days else f"{rest} 小时")
    weekday = f"星期{WEEKDAYS[now_dt.weekday()]}"
    if not show_date:
        return f"（{weekday} {now_dt.strftime('%H:%M')}，离上一句已经过了 {span}）\n"
    return f"（{now_dt.strftime('%m-%d %H:%M')} {weekday}，离上一句已经过了 {span}）\n"


class PromptAssembler:
    def __init__(
        self,
        persona: Persona,
        affection: AffectionEngine,
        mood: MoodEngine,
        memory: MemoryManager,
        stickers_manager: StickerManager,
        db: Database,
        holidays_provider: Optional[Callable[[], List[str]]] = None,
        arcs: Optional[Any] = None,
    ):
        self.persona = persona
        self.affection = affection
        self.mood = mood
        self.memory = memory
        self.stickers = stickers_manager
        self.db = db
        # 节假日唯一数据源是 Config.get_holidays()（转发自 [llm.pricing].holidays）。
        # 这里只收一个只读取值函数，脚本/测试不传即视为"无节假日"，行为与旧版一致。
        self._holidays_provider = holidays_provider
        # FIXES16 生活主线：可选注入，不传即整块省略（既有调用方零改动）
        self.arcs = arcs
        # 动态示例（[reply].style_examples，默认关）：由 main 按配置挂上 StyleExampleBank，
        # None 即整块省略，提示词与旧版逐字节一致
        self.style_examples = None
        # 自我记忆（[character].self_memory，默认关）：由 main 按配置置 True
        self.self_memory = False
        # 对他的看法（[character].impressions，默认关）：由 main 按配置置 True
        self.impressions = False
        # runtime_state_v1：默认关；main 负责挂事件账本与语音频率
        self.runtime_state_v1 = False
        self.runtime_events = None
        self.voice_frequency = "frequent"
        # 线下见面（[character].meetup_recap，默认关）：由 main 挂上 MeetupManager
        self.meetups = None
        self.last_assembled_prompt: str = ""

    def get_holidays(self) -> List[str]:
        """取节假日列表（provider 缺失或抛错时退回空列表，绝不因此炸掉组装）"""
        if self._holidays_provider is None:
            return []
        try:
            return list(self._holidays_provider())
        except Exception as e:
            logger.warning(f"[Assembler] 读取节假日列表失败，按无节假日处理: {e}")
            return []

    def _build_role_block(self) -> str:
        try:
            parts = [self.persona.core_description]
            if self.persona.personal_memories:
                mems = "；".join(
                    [f"{m.title}（{m.content}）" for m in self.persona.personal_memories]
                )
                parts.append(f"她心中的重要记忆：{mems}。")
            if self.persona.habits:
                habits = "；".join(self.persona.habits)
                parts.append(f"她的生活习惯与小动作：{habits}。")
            block = " ".join(parts)
            # 世界书常驻概要：固定内容放在靠前位置，前缀缓存照样命中；没有世界书 = 逐字节不变
            always = getattr(getattr(self.persona, "lorebook", None), "always", "")
            if isinstance(always, str) and always:
                block += f"\n【她的世界】{always}"
            return block
        except Exception as e:
            logger.error(f"[Assembler] 生成角色块异常: {e}")
            return self.persona.core_description

    def _build_stage_block(self, stage_idx: int) -> str:
        try:
            stage = self.persona.get_stage(stage_idx)
            insts = "；".join(stage.instructions)
            block = f"{stage.name}。当前态度倾向：{stage.tone}。相处指引：{insts}"
            if stage.examples:
                exs = "\n".join(f"  - {e}" for e in stage.examples)
                block += f"\n这个阶段她说话的样子（语气示范，照此分寸）：\n{exs}"
            return block
        except Exception as e:
            logger.error(f"[Assembler] 生成阶段块异常: {e}")
            return "日常相处阶段"

    async def _build_facts_block(self, user_message: str = "") -> str:
        try:
            facts = await self.memory.get_facts_for_prompt(query=user_message)
            if not facts:
                return ""
            items = "\n".join([f"  - {f}" for f in facts])
            return f"\n【关于他】（括号里是记下的日期，里面的「明天/今天」按那天算）\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成语义事实块异常: {e}")
            return ""

    async def _build_lore_block(self, user_message: str) -> str:
        book = getattr(self.persona, "lorebook", None)
        if not getattr(book, "entries", None):
            return ""
        try:
            turns = await self.memory.get_recent_turns(limit=book.scan_turns) if book.scan_turns else []
            texts = [user_message] + [str(t.get("content") or "") for t in turns]
            return book.build_block(texts)
        except Exception as e:
            logger.error(f"[Assembler] 生成世界书块异常: {e}")
            return ""

    async def _build_self_facts_block(self, user_message: str = "") -> str:
        if self.self_memory is not True:
            return ""
        try:
            items = await self.memory.get_self_facts_for_prompt(query=user_message)
            if not items:
                return ""
            lines = "\n".join(f"  - {x}" for x in items)
            return (
                "\n【你说过的关于自己的事】（再聊到要对得上，不用主动提；"
                f"真变了就自然地说变了，别装作没说过）\n{lines}"
            )
        except Exception as e:
            logger.error(f"[Assembler] 生成自我记忆块异常: {e}")
            return ""

    async def _build_impressions_block(self) -> str:
        if self.impressions is not True:
            return ""
        try:
            rows = await self.memory.get_impressions()
            if not rows:
                return ""
            words = {1: "有点感觉", 2: "比较确定", 3: "很确定"}
            lines = "\n".join(f"  - {r['content']}（{words.get(r['confidence'], '有点感觉')}）" for r in rows)
            return (
                "\n【你对他的看法】（你自己的感觉，可能看错。用来决定怎么接他的话；"
                f"不要说出口，也不要分析他）\n{lines}"
            )
        except Exception as e:
            logger.error(f"[Assembler] 生成看法块异常: {e}")
            return ""

    async def _build_diaries_block(self, current_v: float, now: Optional[datetime] = None) -> str:
        try:
            diaries = await self.memory.get_active_diaries(current_valence=current_v, now=now)
            if not diaries:
                return ""
            items = "\n".join([f"  - {d}" for d in diaries])
            return f"\n【她的记忆】\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成记忆日记块异常: {e}")
            return ""

    async def _build_followups_block(self, now: Optional[datetime] = None) -> str:
        try:
            rows = await self.memory.get_open_followups(due_only=True, limit=5, now=now)
            if not rows:
                return ""
            items = "\n".join([f"  - {self.memory.format_followup(r)}" for r in rows])
            return f"\n【待跟进】\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成待跟进块异常: {e}")
            return ""

    async def _build_meetups_block(self, now: Optional[datetime] = None) -> str:
        if self.meetups is None:
            return ""
        try:
            rows = await self.meetups.pending()
            if not rows:
                return ""
        except Exception as e:
            logger.error(f"[Assembler] 生成约定块异常: {e}")
            return ""
        now = now or datetime.now()
        upcoming, passed = [], []
        for r in rows:
            when_dt = parse_dt(r.get("when_at"))
            if when_dt is not None and when_dt < now:
                passed.append(r)
            else:
                upcoming.append(r)
        lines = []
        # 过了时间但还没补纪要的约定不能和"还没到"混在一起：模型会把"昨天六点"当成未来的事，
        # 追着问"待会儿见？"。两段分开放，措辞直接给出时态。
        if upcoming:
            lines.append("  还没到的（他提起时按将来的事说）：")
            lines.extend(f"    - {self.meetups.describe(r)}" for r in upcoming)
        if passed:
            lines.append("  已经过了的（见面已经发生，提起来用过去时，不要再当成还没到的事）：")
            lines.extend(f"    - {self.meetups.describe(r)}" for r in passed)
        return "\n【约好的见面】（见面本身发生在聊天之外）\n" + "\n".join(lines)

    async def _build_suppressed_block(self, now: Optional[datetime] = None) -> str:
        try:
            # 时效规则（48h + "之后他又跟她聊过就作废"）收口在 memory.get_live_desires
            valid_items = [r["content"] for r in await self.memory.get_live_desires(limit=5, now=now)]
            if not valid_items:
                return ""

            items = "\n".join([f"  - {c}" for c in valid_items])
            return f"\n【欲言又止】曾经想对他说但当时忍住没说的话：\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成欲言又止块异常: {e}")
            return ""

    async def _build_life_arcs_block(self) -> str:
        """FIXES16【她最近的生活】区块。arcs 未注入或无活跃主线时返回空串（整块省略）。"""
        if self.arcs is None:
            return ""
        try:
            return await self.arcs.build_prompt_block()
        except Exception as e:
            logger.error(f"[Assembler] 生成生活主线块异常: {e}")
            return ""

    async def assemble_system_prompt(
        self,
        user_message: str,
        voice_allowed: bool = False,
        voice_gated_note: bool = False,
        voice_delivery_note: str = "",
        now: Optional[datetime] = None,
        runtime_state_block: str = "",
    ) -> str:
        """按 §6.2 组装完整的 System Prompt

        `voice_allowed`（FIXES22）：这一轮能不能发语音。**默认 False = 不注入
        语音能力说明**——所以既有调用方（测试/脚本/benchmark）零改动即得"语音关着"
        的行为，提示词里一个字都不会多。

        `voice_gated_note`（FIXES27）：TTS 开着、但这一轮作息闸门没过时给一段
        "这会儿只能打字"的实情说明（VOICE_UNAVAILABLE_NOTE）。三态优先级：
        voice_allowed 的完整能力说明 > voice_gated_note 的不可用说明 > 空串。
        `voice_delivery_note`：上一轮真实投递结果的补充事实，只有主聊有投递回执时才传。
        默认空串，所以老调用方的输出与改造前逐字节一致。
        """
        now_dt = now or datetime.now()
        weekday_str = WEEKDAYS[now_dt.weekday()]
        current_time_str = f"{now_dt.strftime(TIME_FORMAT)} 星期{weekday_str}"

        # 法定节假日：短假学校放假不上课，长假她不在学校（FIXES11 任务2 / FIXES14 任务1）
        holidays = self.get_holidays()
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), holidays)
        if span:
            current_time_str += holiday_prompt_note(span, self.persona.world)

        # 1. 好感度与情绪状态
        aff_state = await self.affection.get_state()
        composite_aff = float(aff_state.get("composite", 30.0))
        stage_idx = int(aff_state.get("stage", 0))

        # 组装前先更新一次情绪自然均值回归 (conv=0)
        if self.runtime_state_v1:
            # 实验路径：组装提示词是只读操作，情绪只在明确结算阶段推进
            mood_state = await self.mood.get_state()
        else:
            mood_state = await self.mood.update_mood(composite_affection=composite_aff, stage=stage_idx)
        v = float(mood_state.get("v", 2.0))
        a = float(mood_state.get("a", 1.0))
        t = float(mood_state.get("t", 7.0))
        frustration = float(mood_state.get("frustration", 0.0))
        hours_since_chat = await self.mood.get_hours_since_last_chat(now=now_dt)

        # 2. 各文本段渲染
        role_block = self._build_role_block()
        stage_block = self._build_stage_block(stage_idx)

        good_examples = "\n  - " + "\n  - ".join(self.persona.chat_style.good_examples)
        max_stage = self.persona.chat_style.good_examples_max_stage
        if max_stage is not None and stage_idx > max_stage:
            good_examples = "见下方【当前关系阶段】里这个阶段的语气示范"
        # 反面教材可选：卡里没有就整段（含标题）不渲染，不留一个空的"错误示范（禁止）：-"
        bad_examples_block = ""
        if self.persona.chat_style.bad_examples:
            bad_examples = "\n  - " + "\n  - ".join(self.persona.chat_style.bad_examples)
            bad_examples_block = f"  错误示范（禁止）：{bad_examples}\n"
        chat_rules = "\n  - " + "\n  - ".join(self.persona.chat_style.rules)
        # 世界书词条：扫他这句话 + 最近几轮聊天，命中关键词才带
        lore_block = await self._build_lore_block(user_message)
        # 主动消息传入空串，不检索
        style_examples_block = ""
        if self.style_examples is not None and user_message:
            style_examples_block = self.style_examples.build_block(user_message)

        # 日常废话流基线（角色卡可选字段 plain_examples）：为空时整块（含标题）不渲染，
        # 组装结果与旧角色卡逐字节一致。
        # 渲染位置在【正确示范】之后、【错误示范（禁止）】之前，自成一段——
        # 基线是要鼓励的正常语气，不能挂到"禁止"标题下。
        plain_examples_block = ""
        if self.persona.chat_style.plain_examples:
            plain_examples = "\n  - " + "\n  - ".join(self.persona.chat_style.plain_examples)
            plain_examples_block = (
                "\n  日常废话流基线（她大部分消息就是这样平淡的一句，"
                f"不必每条都接话、提问或表达点什么）：{plain_examples}"
            )

        stickers_list = "、".join(self.stickers.get_prompt_sticker_list())

        routine_activity = self.persona.get_current_activity(
            now_dt.hour, now_dt.weekday(), holiday_span=span
        )
        # FIXES27：他在跟她聊的轮次，她不可能真的睡着——作息文案命中睡眠词时补一句
        # 醒着的事实，否则模型会一边"睡着"一边回话（生产上还让语音闸门自相矛盾：
        # 系统当她睡着，她却说"我压根没睡"）。主动消息传空串，所以那条路不受影响。
        if user_message and is_sleep_activity(routine_activity):
            routine_activity += "（他这会儿在跟你聊天，你其实是醒着的）"
        mood_desc = get_mood_description(v, a)
        trust_desc = get_trust_description(t)
        frustration_desc = get_frustration_description(frustration)
        neglect_desc = get_neglect_description(hours_since_chat, composite_aff)

        last_chat_str = f"；距上次聊天已 {int(hours_since_chat)} 小时" if hours_since_chat >= 1.0 else ""

        facts_block = (
            await self._build_facts_block(user_message)
            + await self._build_impressions_block()
            + await self._build_self_facts_block(user_message)
        )
        diaries_block = await self._build_diaries_block(v, now=now_dt)
        followups_block = await self._build_followups_block(now=now_dt) + await self._build_meetups_block(now=now_dt)
        suppressed_block = await self._build_suppressed_block(now=now_dt)
        if self.runtime_state_v1:
            # 生活主线单列为系统创作，不与对账状态混在一个事实块里
            life_arcs_block = ""
            if self.arcs is not None:
                runtime_state_block += await self.arcs.build_prompt_block(as_script=True, now=now_dt)
        else:
            life_arcs_block = await self._build_life_arcs_block()

        # 安全边界检查 (§13)：模板里占位符位于 {stage_block} 之后、收尾句之前，
        # 保证危机/依赖指引排在"最高优先级"阶段规则之后，不被阶段规则压过
        safety_text = SafetyChecker.check_message(user_message)
        safety_block = f"\n【安全边界】\n{safety_text}" if safety_text else ""

        if voice_allowed:
            if not self.runtime_state_v1:
                voice_block = VOICE_PROMPT_BLOCK
            elif self.voice_frequency == "off":
                voice_block = VOICE_PLANNER_OFF_RULES
            elif self.voice_frequency == "explicit":
                voice_block = VOICE_PLANNER_EXPLICIT_RULES
            else:
                voice_block = VOICE_PLANNER_RULES
        else:
            voice_block = VOICE_UNAVAILABLE_NOTE if voice_gated_note else ""
        if voice_delivery_note:
            voice_block = (
                f"{voice_block}\n" if voice_block else ""
            ) + f"  语音投递核对：{voice_delivery_note}"

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            role_block=role_block,
            user_address=self.persona.user_address,
            chat_medium=self.persona.world.chat_medium,
            chat_tail=self.persona.world.chat_tail,
            good_examples=good_examples,
            bad_examples_block=bad_examples_block,
            plain_examples_block=plain_examples_block,
            chat_rules=chat_rules,
            style_examples_block=style_examples_block,
            lore_block=lore_block,
            stickers_list=stickers_list,
            face_block=face_prompt_block(self.persona.world),
            quote_block=QUOTE_USAGE_RULES + "\n" + QUOTE_USAGE_EXAMPLES,
            # FIXES22：空串 = 不注入能力说明。语音默认关，模型就不该认识 [voice:]。
            # voice_allowed 由调用方（TurnHandler / ProactiveScheduler）问过
            # TTSManager 的闸门结论传进来——**显式参数而不是共享属性**：
            # 主动消息与主聊共用一个 assembler，共享可变开关会互相串味。
            # FIXES27：三态。开着但闸门没过（voice_gated_note）时给"只能打字"的实情，
            # 免得她答应了发语音却只发出文字；两个都 False 仍是空串，旧行为不变。
            voice_block=voice_block,
            runtime_state_block=runtime_state_block,
            stage_block=stage_block,
            routine_activity=routine_activity,
            mood_desc=mood_desc,
            trust_desc=trust_desc,
            frustration_desc=frustration_desc,
            neglect_desc=neglect_desc,
            current_time_str=current_time_str,
            last_chat_str=last_chat_str,
            semantic_facts_block=facts_block,
            diaries_block=diaries_block,
            followups_block=followups_block,
            suppressed_block=suppressed_block,
            life_arcs_block=life_arcs_block,
            safety_block=safety_block,
        )

        # 游戏世界不说"10 月 22 号"：给她看的日期最后统一换成相对说法（内部计算不受影响）
        if self.persona.world.relative_dates:
            system_prompt = relativize_dates(system_prompt, now_dt)
        self.last_assembled_prompt = system_prompt
        return system_prompt

    async def assemble_messages(
        self,
        user_message: str,
        image_data_url: Optional[str] = None,
        numbered_batch: Optional[List[Dict[str, Any]]] = None,
        voice_allowed: bool = False,
        voice_gated_note: bool = False,
        voice_delivery_note: str = "",
        now: Optional[datetime] = None,
        runtime_state_block: str = "",
    ) -> Tuple[List[Dict[str, Any]], str]:
        """组装发送给 LLM 的全套 messages：system + 工作记忆 + 当前用户输入

        FIXES21 `numbered_batch`：本轮聚合批次（每条带 index/text/message_id）。
        **≥2 条才渲染编号块**——他只发一条时根本没有"指哪句"的问题，
        硬塞一个 `[1]` 只会诱使她无意义地引用。单条路径的提示词与改动前逐字一致。

        FIXES27 `voice_gated_note`：透传给 assemble_system_prompt（语音开着但闸门
        没过时的"只能打字"说明）；默认 False，既有调用方输出不变。
        """
        system_prompt = await self.assemble_system_prompt(
            user_message,
            voice_allowed,
            voice_gated_note=voice_gated_note,
            voice_delivery_note=voice_delivery_note,
            now=now,
            runtime_state_block=runtime_state_block,
        )
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # 工作记忆（最近 10 轮）
        recent_turns = await self.memory.get_recent_turns(limit=10)
        show_date = not self.persona.world.relative_dates
        prev_dt: Optional[datetime] = None
        for turn in recent_turns:
            content = turn["content"]
            if turn["role"] == "assistant":
                # 存档前缀只用于数据库存储，不能当成她下一次发送语音的示范。
                content = strip_voice_record_prefix_for_history(content)
            turn_dt = parse_dt(turn.get("created_at"))
            if turn["role"] == "user" and turn_dt is not None:
                content = gap_note(turn_dt, prev_dt, show_date) + content
            prev_dt = turn_dt or prev_dt
            messages.append({"role": turn["role"], "content": content})
        now_note = gap_note(now or datetime.now(), prev_dt, show_date)

        # 本轮用户输入
        block = format_numbered_batch(numbered_batch)
        if block:
            # 有编号块就用它当本轮输入（它已经包含这一批的全部文本）
            user_text = block
        elif image_data_url:
            user_text = user_message.strip() or "（发来一张图片）"
        else:
            # 单条消息的老路径：与 FIXES21 之前逐字一致
            user_text = user_message
        user_text = now_note + user_text

        if image_data_url:
            # 只有一步看图（[reply].vision_direct）才会带图进来：让她回完留一行【看到：…】给记录用
            user_content = [
                {"type": "text", "text": f"{user_text}\n{IMAGE_SEEN_INSTRUCTION}"},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_text})

        return messages, system_prompt
