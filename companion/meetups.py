"""线下见面 (meetups.py)：约定 → 见面发生在聊天之外 → 自动补一段纪要。

系统只能聊天，见面本身发生在镜头外。可第二天他说"昨天那锅底也太酸了"，她得接得住。
做法：观察者识别"两人明确约好了见面"记一条；约定时间过去 MEETUP_RECAP_AFTER_HOURS 小时后，
按她的口吻写一两句纪要，存成一条"关于他"的事实——他说"不对，那天是……"时，
现有的 updated_facts 通路就能把它改掉。

开关：[character].meetup_recap（默认关）。关着时观察者提示词、主聊提示词逐字节不变。
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from companion.db import Database, TIME_FORMAT, now_str, parse_dt
from companion.prompts import MEETUP_RECAP_PROMPT

logger = logging.getLogger(__name__)

MEETUP_RECAP_AFTER_HOURS = 3      # 约定时间过去几小时后补纪要（默认见面已经结束）
MEETUP_PENDING = 0
MEETUP_DONE = 1
MEETUP_CANCELLED = 2
MEETUP_PROCESSING = 3
MEETUP_STALE_DAYS = 7             # 过了约定时间这么多天还没补上（比如一直没开机）就不补了
MEETUP_SIMILARITY = 0.5
MEETUP_CANCEL_MIN_SCORE = 0.5
# 两次见面的最小间隔（天）：新约定距最近一条已记录的见面（未取消的）不足这么多天就不记，
# 见得太勤会出戏。按日历天算，10-22 与 10-25 正好 3 天算够。
MEETUP_MIN_INTERVAL_DAYS = 3


def _chars(text: str) -> set:
    return set(re.findall(r"[一-龥A-Za-z0-9]", text or ""))


def _similarity(a: str, b: str) -> float:
    """字集相似度：重合字数 / 并集字数（Jaccard）。

    旧实现里"一方的字全包含在另一方里且 ≥2 字"直接返回 1.0，
    于是 `_similarity('武陵', '武陵城火锅店') = 1.0`——同一天里"武陵买奶茶"和
    "武陵城火锅店吃火锅"被当成同一条约定改期，后一条把前一条吞掉，纪要只剩一份。
    子集本身不代表同一件事：这里只按全文重合度给分，短名覆盖不足时自然低于
    MEETUP_SIMILARITY（'武陵' vs '武陵城火锅店' = 2/6 ≈ 0.33，不再命中；
    '武陵城' vs '武陵城火锅店' = 3/6 = 0.5，仍按同一件事处理）。
    """
    ca, cb = _chars(a), _chars(b)
    if not ca or not cb:
        return 0.0
    if ca == cb:
        return 1.0
    return len(ca & cb) / len(ca | cb)


def _describe_score(desc: str, row: Any) -> float:
    target = _chars(desc)
    if not target:
        return 0.0
    scores = []
    for field in (row["place"], row["plan"]):
        chars = _chars(field or "")
        if chars:
            scores.append(len(chars & target) / len(chars))
    return max(scores) if scores else 0.0


class MeetupManager:
    def __init__(self, db: Database, gateway: Any = None, persona: Any = None, memory: Any = None):
        self.db = db
        self.gateway = gateway
        self.persona = persona
        self.memory = memory

    # ---------- 约定 ----------

    async def add(self, when: str, where: str, what: str) -> Optional[int]:
        """记一条约定；时间解析不了就不记（宁可漏记，不记一条没有时间的约定）。
        同一天且地点相似（没地点时看事项）视为同一件事；跨天命中同一条待办视为改期。
        改期不受频率限制；真正新增的约定距最近一条已记录的见面不足
        MEETUP_MIN_INTERVAL_DAYS 天时不记（见面太勤会出戏）。"""
        when_dt = parse_dt((when or "").strip())
        if when_dt is None:
            logger.warning(f"[Meetup] 约定时间解析不了，已忽略：{when!r}")
            return None
        where, what = (where or "").strip(), (what or "").strip()
        day = when_dt.strftime("%Y-%m-%d")
        rows = await self.db.fetchall(
            "SELECT id, when_at, place, plan FROM meetups WHERE status = ? ORDER BY when_at",
            (MEETUP_PENDING,),
        )
        for r in rows:
            same_day = str(r["when_at"] or "")[:10] == day
            if same_day and self._match_score(where, what, r) >= MEETUP_SIMILARITY:
                return await self._reschedule(r, when_dt, where, what)
        best, best_score = None, 0.0
        for r in rows:
            score = self._match_score(where, what, r)
            if score >= MEETUP_SIMILARITY and score > best_score:
                best, best_score = r, score
        if best is not None:
            logger.info(f"[Meetup] 跨天改期：{best['when_at']} → {when_dt.strftime(TIME_FORMAT)}")
            return await self._reschedule(best, when_dt, where, what)
        gap = await self._nearest_gap_days(when_dt)
        if gap is not None and gap < MEETUP_MIN_INTERVAL_DAYS:
            logger.info(
                f"[Meetup] 距最近一次见面只有 {gap} 天（下限 {MEETUP_MIN_INTERVAL_DAYS} 天），"
                f"本次约定不记：{when_dt.strftime(TIME_FORMAT)} {where} {what}".strip()
            )
            return None
        await self.db.execute(
            "INSERT INTO meetups (when_at, place, plan, status, recap, created_at) VALUES (?, ?, ?, ?, '', ?)",
            (when_dt.strftime(TIME_FORMAT), where, what, MEETUP_PENDING, now_str()),
        )
        row = await self.db.fetchone("SELECT MAX(id) AS id FROM meetups")
        return int(row["id"]) if row else None

    async def _nearest_gap_days(self, when_dt: datetime) -> Optional[int]:
        """新约定与最近一条已记录见面（未取消的，含已补纪要的）相隔几个日历天；没有记录返回 None。"""
        rows = await self.db.fetchall(
            "SELECT when_at FROM meetups WHERE status != ?",
            (MEETUP_CANCELLED,),
        )
        gaps = []
        for r in rows:
            other = parse_dt(r["when_at"])
            if other is not None:
                gaps.append(abs((when_dt.date() - other.date()).days))
        return min(gaps) if gaps else None

    @staticmethod
    def _match_score(where: str, what: str, row: Any) -> float:
        if where and row["place"]:
            return _similarity(where, row["place"])
        if what and row["plan"]:
            return _similarity(what, row["plan"])
        return 0.0

    async def _reschedule(self, row: Any, when_dt: datetime, where: str, what: str) -> int:
        await self.db.execute(
            "UPDATE meetups SET when_at = ?, place = ?, plan = ? WHERE id = ?",
            (when_dt.strftime(TIME_FORMAT), where or row["place"], what or row["plan"], row["id"]),
        )
        return int(row["id"])

    async def cancel(self, desc: str) -> bool:
        """取消未完成约定：只有一条时直接取消；多条时按地点/事项相似度取唯一最高。"""
        rows = await self.db.fetchall(
            "SELECT id, when_at, place, plan FROM meetups WHERE status = ? ORDER BY when_at",
            (MEETUP_PENDING,),
        )
        if not rows:
            logger.warning(f"[Meetup] 没有可取消的约定：{desc!r}")
            return False
        if len(rows) == 1:
            target = rows[0]
        else:
            scored = [(_describe_score(desc, r), r) for r in rows]
            best_score = max(score for score, _ in scored)
            top = [r for score, r in scored if score == best_score]
            if best_score < MEETUP_CANCEL_MIN_SCORE or len(top) != 1:
                logger.warning(
                    f"[Meetup] 取消描述匹配不到唯一约定，已放弃：{desc!r}"
                    f"（最高分 {best_score:.2f}，并列 {len(top)} 条）"
                )
                return False
            target = top[0]
        cur = await self.db.execute(
            "UPDATE meetups SET status = ? WHERE id = ? AND status = ?",
            (MEETUP_CANCELLED, target["id"], MEETUP_PENDING),
        )
        if cur.rowcount != 1:
            logger.warning(f"[Meetup] 取消约定失败，状态已变化：id={target['id']}")
            return False
        return True

    async def pending(self, limit: int = 5) -> List[Dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT id, when_at, place, plan FROM meetups WHERE status = ? ORDER BY when_at LIMIT ?",
            (MEETUP_PENDING, limit),
        )
        return [dict(r) for r in rows]

    @staticmethod
    def describe(row: Dict[str, Any]) -> str:
        dt = parse_dt(row.get("when_at"))
        when = dt.strftime("%m-%d %H:%M") if dt else str(row.get("when_at"))
        place = f"在{row['place']}" if row.get("place") else ""
        return f"{when} {place}{('，' + row['plan']) if row.get('plan') else ''}".strip()

    # ---------- 补纪要 ----------

    async def process_due(self, now: Optional[datetime] = None) -> int:
        """把到点的约定补上纪要（存成一条关于他的事实），返回补了几条。失败的留着下次再试。"""
        if not self.gateway or self.memory is None:
            return 0
        now = now or datetime.now()
        done = 0
        for r in await self.pending(limit=20):
            when_dt = parse_dt(r["when_at"])
            if when_dt is None:
                continue
            if now - when_dt > timedelta(days=MEETUP_STALE_DAYS):
                await self.db.execute(
                    "UPDATE meetups SET status = ? WHERE id = ? AND status = ?",
                    (MEETUP_CANCELLED, r["id"], MEETUP_PENDING),
                )
                continue
            if now < when_dt + timedelta(hours=MEETUP_RECAP_AFTER_HOURS):
                continue
            claim = await self.db.execute(
                "UPDATE meetups SET status = ? WHERE id = ? AND status = ?",
                (MEETUP_PROCESSING, r["id"], MEETUP_PENDING),
            )
            if claim.rowcount != 1:
                continue
            recap = await self._write_recap(r)
            if not recap:
                await self.db.execute(
                    "UPDATE meetups SET status = ? WHERE id = ? AND status = ?",
                    (MEETUP_PENDING, r["id"], MEETUP_PROCESSING),
                )
                continue
            try:
                await self.memory.add_fact(f"{when_dt.strftime('%m-%d')} 见面：{recap}")
            except Exception as e:
                logger.warning(f"[Meetup] 纪要存成事实失败，下次再试: {e}")
                await self.db.execute(
                    "UPDATE meetups SET status = ? WHERE id = ? AND status = ?",
                    (MEETUP_PENDING, r["id"], MEETUP_PROCESSING),
                )
                continue
            await self.db.execute(
                "UPDATE meetups SET status = ?, recap = ? WHERE id = ? AND status = ?",
                (MEETUP_DONE, recap, r["id"], MEETUP_PROCESSING),
            )
            logger.info(f"[Meetup] 已补纪要：{recap}")
            done += 1
        return done

    async def _write_recap(self, row: Dict[str, Any]) -> str:
        persona = self.persona
        core = getattr(persona, "core_description", "") or ""
        world = getattr(getattr(persona, "lorebook", None), "always", "") or ""
        lore = ""
        book = getattr(persona, "lorebook", None)
        if book is not None and getattr(book, "entries", None):
            lore = "\n".join(e.content for e in book.match([row.get("place", ""), row.get("plan", "")]))
        try:
            turns = await self.memory.get_recent_turns(limit=6)
        except Exception:
            turns = []
        chat = "\n".join(f"{'他' if t['role'] == 'user' else '她'}：{t['content']}" for t in turns) or "（无）"
        prompt = MEETUP_RECAP_PROMPT.format(
            her=core[:400], world=world or "（无）", lore=lore or "（无）",
            when=row.get("when_at", ""), place=row.get("place") or "（没说）",
            plan=row.get("plan") or "（没说）", chat=chat,
        )
        try:
            resp = await self.gateway.chat(
                messages=[{"role": "user", "content": prompt}],
                model=self.gateway.config.observer_model,
                temperature=0.7,
                purpose="meetup_recap",
            )
        except Exception as e:
            logger.warning(f"[Meetup] 纪要生成失败，下次再试: {e}")
            return ""
        text = (resp or "").strip().strip("「」\"' ")
        return text[:80] if text else ""
