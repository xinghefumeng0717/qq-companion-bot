"""系统替她挑表情：她写完话之后，由一次很小的后台调用决定要不要顺手发一个表情包或小黄脸。

主模型只写话，不输出任何表情标记；这里也不改她的原话，只决定要不要多发一个附件。
"偶尔才发"由代码保证（冷却、不重复），不靠模型自觉。任何异常都按"不发"处理。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional, Sequence

from companion.faces import face_id_by_name, is_sendable_face_id
from companion.gateway import parse_llm_json
from companion.prompts import PROMPT_FACE_LIST

logger = logging.getLogger(__name__)

PICK_SYSTEM_PROMPT = (
    "聊天里的一方刚写完几句回复。请判断：发完这几句之后，要不要再顺手发一个表情。\n"
    "大多数时候不需要。只有某个表情的用法和这几句话的意思明显对得上时才选；拿不准就不选。\n"
    "只能从给出的名字里选一个，不要改写任何文字。\n"
    '只输出 JSON：{"pick": "表情名"}；不选时输出 {"pick": ""}。'
)


class ExpressionPicker:
    def __init__(
        self,
        gateway: Any,
        stickers: Any,
        runtime_events: Any,
        *,
        enabled: bool = True,
        cooldown_turns: int = 5,
        no_repeat: int = 5,
        timeout: float = 6.0,
    ) -> None:
        self.gateway = gateway
        self.stickers = stickers
        self.runtime_events = runtime_events
        self.enabled = enabled
        self.cooldown_turns = max(0, int(cooldown_turns))
        self.no_repeat = max(0, int(no_repeat))
        self.timeout = timeout

    async def _recent(self) -> list[dict[str, Any]]:
        reader = getattr(self.runtime_events, "recent_delivery_actions", None)
        if not callable(reader):
            return []
        return await reader(limit=max(self.no_repeat, 5), channel="sticker")

    def _candidates(self, exclude: set[str]) -> tuple[list[str], dict[str, dict[str, Any]]]:
        """整理可选项：卡里的表情包（名字、含义、用法）和发得出去的小黄脸（名字、含义）。"""
        lines: list[str] = []
        table: dict[str, dict[str, Any]] = {}
        describe = getattr(self.stickers, "describe_all", None)
        sticker_rows = describe() if callable(describe) else []
        if sticker_rows:
            lines.append("表情包：")
        for name, meaning, usage, path in sticker_rows:
            if name in exclude:
                continue
            detail = "｜".join(part for part in (meaning, usage) if part)
            lines.append(f"- {name}｜{detail}" if detail else f"- {name}")
            table[name] = {"desc": name, "file": path}
        face_lines: list[str] = []
        for name, meaning in PROMPT_FACE_LIST:
            face_id = face_id_by_name(name)
            if name in exclude or name in table or not is_sendable_face_id(face_id):
                continue
            face_lines.append(f"- {name}｜{meaning.split('；')[0]}")
            table[name] = {"desc": name, "face_id": face_id, "file": ""}
        if face_lines:
            lines.append("小黄脸：")
            lines.extend(face_lines)
        return lines, table

    async def pick(
        self,
        user_text: str,
        reply_text: str,
        recent_assistant_turn_ids: Sequence[int] = (),
    ) -> Optional[dict[str, Any]]:
        """返回要发的表情（{"desc", "file"} 或 {"desc", "face_id"}）；不发返回 None。"""
        if not self.enabled or not (reply_text or "").strip():
            return None
        try:
            recent = await self._recent()
            cooling = set(list(recent_assistant_turn_ids)[-self.cooldown_turns:]) if self.cooldown_turns else set()
            if any(row.get("evidence_turn_id") in cooling for row in recent):
                return None
            exclude = {str(row.get("content") or "") for row in recent[: self.no_repeat]}
            lines, table = self._candidates(exclude)
            if not table:
                return None
            user_prompt = (
                f"对方说：\n{(user_text or '').strip() or '（没有新消息）'}\n\n"
                f"这一方回：\n{reply_text.strip()}\n\n"
                "可选的表情：\n" + "\n".join(lines)
            )
            raw = await asyncio.wait_for(
                self.gateway.chat(
                    messages=[
                        {"role": "system", "content": PICK_SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=0.3,
                    json_mode=True,
                    purpose="expression_pick",
                    max_retries=0,
                ),
                timeout=self.timeout,
            )
            data = parse_llm_json(raw)
            name = str(data.get("pick") or "").strip() if isinstance(data, dict) else ""
            return dict(table[name]) if name in table else None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.info("[Expression] 这轮不发表情（挑选未完成）: %s: %s", type(exc).__name__, exc)
            return None


__all__ = ["ExpressionPicker"]
