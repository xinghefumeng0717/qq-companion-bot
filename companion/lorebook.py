"""世界书 (lorebook.py)
她所在世界的设定：一段常驻概要 + 按关键词触发的词条。

模型对新游戏/原创世界知道得很少，不告诉它就会瞎编；但整本世界书塞进提示词又贵、
又会被她照搬。所以只常驻一小段概要（放在提示词靠前的固定位置，能命中缓存），
其余写成带关键词的词条：他这句话或最近几轮聊天提到了才带上，有条数和字数上限。

来源：角色卡的 lorebook 字段，或卡目录下单独的 lorebook.json（后者优先，长文本好维护）。
都没有 = 世界书功能关闭，提示词逐字节不变。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List

logger = logging.getLogger(__name__)

DEFAULT_SCAN_TURNS = 4
DEFAULT_MAX_ENTRIES = 4
DEFAULT_MAX_CHARS = 1200

BLOCK_HEADER = "\n【世界书】（她世界里的设定，聊到时以这里为准；这里没写的不要编，拿不准就含糊带过）"


@dataclass
class LoreEntry:
    keys: List[str]
    content: str
    priority: int = 0


@dataclass
class Lorebook:
    always: str = ""
    entries: List[LoreEntry] = field(default_factory=list)
    scan_turns: int = DEFAULT_SCAN_TURNS
    max_entries: int = DEFAULT_MAX_ENTRIES
    max_chars: int = DEFAULT_MAX_CHARS

    @property
    def enabled(self) -> bool:
        return bool(self.always or self.entries)

    @classmethod
    def from_dict(cls, raw: Any) -> "Lorebook":
        if not isinstance(raw, dict):
            return cls()
        entries = []
        for e in raw.get("entries", []) or []:
            if not isinstance(e, dict):
                continue
            keys = [str(k).strip() for k in (e.get("keys") or []) if str(k).strip()]
            content = str(e.get("content", "")).strip()
            if not keys or not content:
                logger.warning(f"[Lorebook] 词条缺关键词或内容，已跳过：{e!r}"[:200])
                continue
            try:
                priority = int(e.get("priority", 0))
            except (TypeError, ValueError):
                priority = 0
            entries.append(LoreEntry(keys=keys, content=content, priority=priority))

        def _int(name: str, default: int) -> int:
            try:
                return max(0, int(raw.get(name, default)))
            except (TypeError, ValueError):
                return default

        return cls(
            always=str(raw.get("always", "") or "").strip(),
            entries=entries,
            scan_turns=_int("scan_turns", DEFAULT_SCAN_TURNS),
            max_entries=_int("max_entries", DEFAULT_MAX_ENTRIES),
            max_chars=_int("max_chars", DEFAULT_MAX_CHARS),
        )

    @classmethod
    def load(cls, char_dir: str, card_data: Dict[str, Any]) -> "Lorebook":
        """卡目录下的 lorebook.json 优先，其次卡里的 lorebook 字段；读坏了就当没有（打日志）。"""
        path = os.path.join(char_dir, "lorebook.json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return cls.from_dict(json.load(f))
            except Exception as e:
                logger.error(f"[Lorebook] 读取 {path} 失败，世界书不启用: {e}")
                return cls()
        return cls.from_dict(card_data.get("lorebook"))

    def match(self, texts: Iterable[str]) -> List[LoreEntry]:
        """在给定文本里找关键词；按优先级高到低、同级按词条顺序，截到条数和字数上限。"""
        hay = "\n".join(t for t in texts if t).lower()
        if not hay:
            return []
        hits = [(i, e) for i, e in enumerate(self.entries) if any(k.lower() in hay for k in e.keys)]
        hits.sort(key=lambda x: (-x[1].priority, x[0]))
        out: List[LoreEntry] = []
        used = 0
        for _, e in hits:
            if len(out) >= self.max_entries or used + len(e.content) > self.max_chars:
                continue
            out.append(e)
            used += len(e.content)
        return out

    def build_block(self, texts: Iterable[str]) -> str:
        """命中的词条拼成提示词块；没命中返回空串。"""
        picked = self.match(texts)
        if not picked:
            return ""
        return BLOCK_HEADER + "".join(f"\n  - {e.content}" for e in picked)
