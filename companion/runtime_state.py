"""运行时事实、事件状态和轮次追踪（实验开关默认关闭）。"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional

from companion.db import Database, TIME_FORMAT


def _ts(value: Optional[datetime] = None) -> str:
    return (value or datetime.now()).strftime(TIME_FORMAT)


def _chars(text: str) -> str:
    return "".join(re.findall(r"[A-Za-z0-9一-龥]", text or ""))


def runtime_v1_enabled(reply_config: Any, assembler: Any) -> bool:
    """配置与组装器任一显式开启即启用；只有 True 才算，兼容旧 Mock。"""
    return (
        getattr(reply_config, "runtime_state_v1", False) is True
        or getattr(assembler, "runtime_state_v1", False) is True
    )


@dataclass(frozen=True)
class TurnContext:
    now: datetime
    user_text: str
    state_block: str = ""


@dataclass
class TurnTracer:
    directory: str
    max_bytes: int = 2_000_000

    def write(self, context: TurnContext, **payload: Any) -> None:
        os.makedirs(self.directory, exist_ok=True)
        row = dict(payload)
        row.update({"at": _ts(context.now), "user": context.user_text, "state_block": context.state_block})
        path = os.path.join(self.directory, f"{context.now:%Y-%m-%d}.jsonl")
        if os.path.exists(path) and os.path.getsize(path) > self.max_bytes:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


class RuntimeEventStore:
    PLAN_STATUSES = {"planned", "active", "done", "cancelled"}
    VOICE_STATUSES = {"not_attempted", "sent", "failed", "text_fallback"}
    TRANSITIONS = {
        "planned": {"active", "done", "cancelled"},
        "active": {"done", "cancelled"},
        "done": set(),
        "cancelled": set(),
    }
    CLUES = {
        "planned": re.compile(r"打算|准备|计划|想去|要去|约好|约定|明天|后天|届时"),
        "active": re.compile(r"动身|出发|在路上|正在|途中|开始"),
        "done": re.compile(r"到了|抵达|完成|做完|结束|见过|见完|回来了|已经到|已回"),
        "cancelled": re.compile(r"取消|不去了|没去|推掉|作罢|算了"),
    }

    def __init__(self, db: Database):
        self.db = db

    async def init(self) -> None:
        await self.db.execute(
            """
            CREATE TABLE IF NOT EXISTS runtime_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                subject TEXT NOT NULL,
                status TEXT NOT NULL,
                source TEXT NOT NULL,
                content TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                evidence_turn_id INTEGER,
                confidence INTEGER DEFAULT 3,
                metadata TEXT DEFAULT '{}'
            )
            """
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS idx_runtime_events_lookup "
            "ON runtime_events(kind, subject, status, id)"
        )

    async def _insert(self, *, kind: str, subject: str, status: str, source: str,
                      content: str, now: Optional[datetime], evidence_turn_id: Optional[int] = None,
                      confidence: int = 3, metadata: Optional[dict[str, Any]] = None) -> int:
        stamp = _ts(now)
        cur = await self.db.execute(
            """
            INSERT INTO runtime_events
            (kind, subject, status, source, content, occurred_at, recorded_at,
             evidence_turn_id, confidence, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (kind, subject.strip()[:120], status, source, content.strip()[:500],
             stamp, stamp, evidence_turn_id, int(confidence),
             json.dumps(metadata or {}, ensure_ascii=False)),
        )
        return int(cur.lastrowid or 0)

    async def _latest(self, kind: str, subject: str = "") -> Optional[dict[str, Any]]:
        if subject:
            row = await self.db.fetchone(
                "SELECT * FROM runtime_events WHERE kind = ? AND subject = ? ORDER BY id DESC LIMIT 1",
                (kind, subject.strip()[:120]),
            )
        else:
            row = await self.db.fetchone(
                "SELECT * FROM runtime_events WHERE kind = ? ORDER BY id DESC LIMIT 1", (kind,)
            )
        return dict(row) if row else None

    async def transition_plan(self, subject: str, status: str, content: str, *,
                              source: str, now: Optional[datetime] = None,
                              evidence_turn_id: Optional[int] = None, confidence: int = 3) -> bool:
        """只有带语义证据的新状态才入库；时间不直接推进任何计划。"""
        subject, status = subject.strip()[:120], status.strip()
        content = (content or subject).strip()
        if (not subject or status not in self.PLAN_STATUSES or source not in {"user", "assistant"}
                or len(_chars(content)) < 2 or not self.CLUES[status].search(content)):
            return False
        async with self.db.transaction():
            previous = await self._latest("plan", subject)
            if previous:
                old = previous["status"]
                if old == status:
                    return False
                if not (old in {"done", "cancelled"} and status == "planned"):
                    if status not in self.TRANSITIONS.get(old, set()):
                        return False
                if content == str(previous["content"]).strip():
                    return False
            new_id = await self._insert(
                kind="plan", subject=subject, status=status, source=source,
                content=content, now=now, evidence_turn_id=evidence_turn_id,
                confidence=confidence,
            )
            return new_id > 0

    async def apply_observation(self, raw: Any, *, user_text: str, assistant_text: str,
                                now: Optional[datetime] = None,
                                evidence_turn_id: Optional[int] = None) -> int:
        if not isinstance(raw, list):
            return 0
        applied = 0
        for item in raw:
            if not isinstance(item, dict) or item.get("kind") != "plan":
                continue
            source, evidence = item.get("source"), item.get("evidence")
            subject, status = item.get("subject"), item.get("status")
            haystack = user_text if source == "user" else assistant_text if source == "assistant" else ""
            if not all(isinstance(x, str) and x.strip() for x in (evidence, subject, status)):
                continue
            if evidence.strip() not in haystack:
                continue
            if await self.transition_plan(subject, status, evidence, source=source, now=now,
                                          evidence_turn_id=evidence_turn_id):
                applied += 1
        return applied

    async def record_voice(self, status: str, content: str, *, now: Optional[datetime] = None,
                           reason: str = "", evidence_turn_id: Optional[int] = None) -> int:
        if status not in self.VOICE_STATUSES:
            raise ValueError(f"未知语音投递状态: {status}")
        return await self._insert(kind="voice", subject="当前语音投递", status=status,
                                  source="system", content=content, now=now,
                                  evidence_turn_id=evidence_turn_id,
                                  metadata={"reason": reason})

    async def record_delivery_actions(self, actions: list[dict[str, Any]], *,
                                      now: Optional[datetime] = None,
                                      evidence_turn_id: Optional[int] = None) -> int:
        """逐条保存实际送达的语音/贴纸；输入必须由 DeliveryResult.messages 投影。

        evidence_turn_id 是这一轮助手正文在 turns 表里的 id：真实动作挂在它说出口的那一轮上，
        回读历史时才能告诉模型"这一句当时是用语音发的"。"""
        recorded = 0
        for action in actions:
            channel = action.get("channel")
            if channel not in {"voice", "sticker"}:
                raise ValueError(f"未知实际动作类型: {channel}")
            metadata = action.get("metadata")
            if not isinstance(metadata, dict):
                raise ValueError("实际动作必须携带投递明细")
            await self._insert(
                kind="delivery_action",
                subject=channel,
                status="sent",
                source="system",
                content=str(action.get("content") or ""),
                now=now,
                evidence_turn_id=evidence_turn_id,
                metadata=metadata,
            )
            recorded += 1
        return recorded

    async def recent_delivery_actions(self, limit: int = 50, *,
                                      channel: Optional[str] = None) -> list[dict[str, Any]]:
        """读取最近的真实动作，最新在前；metadata 解码后供系统冷却、防重复与审计。"""
        if channel not in {None, "voice", "sticker"}:
            raise ValueError(f"未知实际动作类型: {channel}")
        if channel is None:
            rows = await self.db.fetchall(
                "SELECT * FROM runtime_events WHERE kind = 'delivery_action' "
                "ORDER BY id DESC LIMIT ?",
                (int(limit),),
            )
        else:
            rows = await self.db.fetchall(
                "SELECT * FROM runtime_events WHERE kind = 'delivery_action' AND subject = ? "
                "ORDER BY id DESC LIMIT ?",
                (channel, int(limit)),
            )
        items: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except (TypeError, json.JSONDecodeError):
                item["metadata"] = {}
            items.append(item)
        return items

    async def record_delivery_chunks(self, chunks: list[dict[str, Any]], content: str, *,
                                     now: Optional[datetime] = None, blocked_reason: str = "") -> str:
        planned = [c for c in chunks if c.get("_runtime_voice")]
        if not planned:
            if blocked_reason:
                await self.record_voice("not_attempted", content, now=now, reason=blocked_reason)
                return "not_attempted"
            return ""
        deliveries = [str(c.get("_voice_delivery", "failed")) for c in planned]
        if any(d not in {"sent", "text", "recovered"} for d in deliveries):
            status = "failed"
        elif any(d in {"text", "recovered"} for d in deliveries):
            status = "text_fallback"
        else:
            status = "sent"
        await self.record_voice(status, content, now=now, reason=",".join(deliveries))
        return status

    async def open_plans(self) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM runtime_events WHERE kind = 'plan' ORDER BY id DESC LIMIT 1000")
        latest: dict[str, dict[str, Any]] = {}
        for row in rows:
            if row["subject"] not in latest:
                latest[row["subject"]] = dict(row)
        return sorted(
            (r for r in latest.values() if r["status"] in {"planned", "active"}),
            key=lambda r: r["id"], reverse=True,
        )

    async def latest_voice(self) -> Optional[dict[str, Any]]:
        return await self._latest("voice")

    @staticmethod
    def _voice_prompt_line(voice: dict[str, Any]) -> str:
        labels = {"not_attempted": "未尝试/未获放行", "sent": "已由协议端发出",
                  "failed": "未发出", "text_fallback": "未发出，已转文字"}
        return (
            f"- 最近一次语音：{labels.get(voice['status'], voice['status'])}"
            f"（{voice['recorded_at']}）；这只是系统投递结果，不代表对方已经听到"
        )

    async def delivery_notes_for_turns(self, turn_ids: list[int]) -> dict[int, str]:
        """把真实语音结果整理成挂在具体历史轮次上的一句投递记录。

        事实跟着它发生的那一轮走：模型在自己那句话旁边读到"这句是语音发的／语音没发出去"，
        历史滚出窗口后记录自然消失，不会在无关轮次里长期滞留。
        只写语音：成功的写明是哪一句，失败的写明对方没有收到语音。
        """
        ids = [int(i) for i in turn_ids if i]
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = await self.db.fetchall(
            "SELECT kind, subject, status, content, evidence_turn_id FROM runtime_events "
            f"WHERE evidence_turn_id IN ({marks}) AND "
            "((kind = 'delivery_action' AND subject = 'voice') OR kind = 'voice') "
            "ORDER BY id ASC",
            tuple(ids),
        )
        sent: dict[int, list[str]] = {}
        failed: set[int] = set()
        for row in rows:
            turn_id = int(row["evidence_turn_id"])
            if row["kind"] == "delivery_action":
                text = str(row["content"] or "").strip()
                if text:
                    sent.setdefault(turn_id, []).append(text)
            elif row["status"] in {"failed", "text_fallback"}:
                failed.add(turn_id)
        notes: dict[int, str] = {}
        for turn_id in ids:
            if turn_id in sent:
                quoted = "、".join(f"“{text}”" for text in sent[turn_id])
                notes[turn_id] = f"（投递记录：上面这轮里{quoted}是用语音发出的，其余是文字）"
            elif turn_id in failed:
                notes[turn_id] = (
                    "（投递记录：上面这轮里原本要用语音发的那一条没有发出去，"
                    "已改成文字送到对方那边；对方没有收到语音）"
                )
        return notes

    async def render_prompt(self) -> str:
        lines: list[str] = []
        labels = {"planned": "计划中", "active": "进行中"}
        sources = {"user": "他说过", "assistant": "她说过"}
        for row in (await self.open_plans())[:5]:
            source = sources.get(row["source"], row["source"])
            lines.append(
                f"- {row['subject']}：{labels[row['status']]}（{source}，{row['occurred_at']}；"
                "没有明确新证据时不要改成已完成）"
            )
        voice = await self.latest_voice()
        if voice:
            lines.append(self._voice_prompt_line(voice))
        if not lines:
            return ""
        return (
            "\n【当前对账状态】（来源和时间随记录给出；她说过不等于已发生，"
            "系统已发出不等于对方听到）\n" + "\n".join(lines) + "\n"
        )
