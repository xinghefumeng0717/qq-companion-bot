"""提示词组装器 (assembler.py)
按 §6.2 规定的顺序与规范，实时组装 system prompt 与上下文 messages。
每个分段生成均有 try/except 隔离保护，单块失败不影响主对话。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from companion.affection import AffectionEngine
from companion.db import Database, TIME_FORMAT, parse_dt, now_str
from companion.memory import MemoryManager
from companion.mood import MoodEngine
from companion.persona import Persona
from companion.prompts import (
    SYSTEM_PROMPT_TEMPLATE,
    get_frustration_description,
    get_mood_description,
    get_neglect_description,
    get_trust_description,
)
from companion.safety import SafetyChecker
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"]


class PromptAssembler:
    def __init__(
        self,
        persona: Persona,
        affection: AffectionEngine,
        mood: MoodEngine,
        memory: MemoryManager,
        stickers: StickerManager,
        db: Database,
    ):
        self.persona = persona
        self.affection = affection
        self.mood = mood
        self.memory = memory
        self.stickers = stickers
        self.db = db
        self.last_assembled_prompt: str = ""

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
            return " ".join(parts)
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

    async def _build_facts_block(self) -> str:
        try:
            facts = await self.memory.get_all_facts()
            if not facts:
                return ""
            items = "\n".join([f"  - {f}" for f in facts])
            return f"\n【关于他】\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成语义事实块异常: {e}")
            return ""

    async def _build_diaries_block(self, current_v: float) -> str:
        try:
            diaries = await self.memory.get_active_diaries(current_valence=current_v)
            if not diaries:
                return ""
            items = "\n".join([f"  - {d}" for d in diaries])
            return f"\n【她的记忆】\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成记忆日记块异常: {e}")
            return ""

    async def _build_followups_block(self) -> str:
        try:
            current_time = now_str()
            rows = await self.db.fetchall(
                "SELECT topic FROM followups WHERE done = 0 AND remind_after <= ? ORDER BY id ASC",
                (current_time,),
            )
            if not rows:
                return ""
            items = "\n".join([f"  - {r['topic']}" for r in rows])
            return f"\n【待跟进】\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成待跟进块异常: {e}")
            return ""

    async def _build_suppressed_block(self) -> str:
        try:
            rows = await self.db.fetchall(
                "SELECT id, content, created_at FROM suppressed_desires ORDER BY id DESC LIMIT 5"
            )
            if not rows:
                return ""

            valid_items = []
            now_dt = datetime.now()
            for r in rows:
                c_dt = parse_dt(r["created_at"])
                if c_dt and (now_dt - c_dt).total_seconds() > 48 * 3600:
                    # 超过 48 小时清除
                    await self.db.execute("DELETE FROM suppressed_desires WHERE id = ?", (r["id"],))
                    continue
                valid_items.append(r["content"])

            if not valid_items:
                return ""

            items = "\n".join([f"  - {c}" for c in valid_items])
            return f"\n【欲言又止】曾经想对他说但当时忍住没说的话：\n{items}"
        except Exception as e:
            logger.error(f"[Assembler] 生成欲言又止块异常: {e}")
            return ""

    async def assemble_system_prompt(self, user_message: str) -> str:
        """按 §6.2 组装完整的 System Prompt"""
        now_dt = datetime.now()
        weekday_str = WEEKDAYS[now_dt.weekday()]
        current_time_str = f"{now_dt.strftime(TIME_FORMAT)} 星期{weekday_str}"

        # 1. 好感度与情绪状态
        aff_state = await self.affection.get_state()
        composite_aff = float(aff_state.get("composite", 30.0))
        stage_idx = int(aff_state.get("stage", 0))

        # 组装前先更新一次情绪自然均值回归 (conv=0)
        mood_state = await self.mood.update_mood(composite_affection=composite_aff)
        v = float(mood_state.get("v", 2.0))
        a = float(mood_state.get("a", 1.0))
        t = float(mood_state.get("t", 7.0))
        frustration = float(mood_state.get("frustration", 0.0))
        hours_since_chat = await self.mood.get_hours_since_last_chat()

        # 2. 各文本段渲染
        role_block = self._build_role_block()
        stage_block = self._build_stage_block(stage_idx)

        good_examples = "\n  - " + "\n  - ".join(self.persona.chat_style.good_examples)
        bad_examples = "\n  - " + "\n  - ".join(self.persona.chat_style.bad_examples)
        chat_rules = "\n  - " + "\n  - ".join(self.persona.chat_style.rules)

        stickers_list = "、".join(self.stickers.get_prompt_sticker_list())

        routine_activity = self.persona.get_current_activity(now_dt.hour, now_dt.weekday())
        mood_desc = get_mood_description(v, a)
        trust_desc = get_trust_description(t)
        frustration_desc = get_frustration_description(frustration)
        neglect_desc = get_neglect_description(hours_since_chat, composite_aff)

        last_chat_str = f"；距上次聊天已 {int(hours_since_chat)} 小时" if hours_since_chat >= 1.0 else ""

        facts_block = await self._build_facts_block()
        diaries_block = await self._build_diaries_block(v)
        followups_block = await self._build_followups_block()
        suppressed_block = await self._build_suppressed_block()

        # 安全边界检查 (§13)
        safety_text = SafetyChecker.check_message(user_message)
        safety_block = f"\n【安全边界】\n{safety_text}" if safety_text else ""

        system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
            role_block=role_block,
            user_address=self.persona.user_address,
            good_examples=good_examples,
            bad_examples=bad_examples,
            chat_rules=chat_rules,
            stickers_list=stickers_list,
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
            safety_block=safety_block,
        )

        self.last_assembled_prompt = system_prompt
        return system_prompt

    async def assemble_messages(
        self,
        user_message: str,
        image_data_url: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], str]:
        """组装发送给 LLM 的全套 messages：system + 工作记忆 + 当前用户输入"""
        system_prompt = await self.assemble_system_prompt(user_message)
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system_prompt}]

        # 工作记忆（最近 10 轮）
        recent_turns = await self.memory.get_recent_turns(limit=10)
        for turn in recent_turns:
            messages.append({"role": turn["role"], "content": turn["content"]})

        # 本轮用户输入
        if image_data_url:
            user_text = user_message.strip() or "（发来一张图片）"
            user_content = [
                {"type": "text", "text": user_text},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": user_message})

        return messages, system_prompt
