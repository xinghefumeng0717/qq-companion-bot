"""生活主线管理器 (companion/arcs.py)
FIXES16 生活剧本：给她的"生活"一张时间表，让生活有连续性。

解决的病：她的生活是每条消息即兴编的，说完就蒸发——提过的审查没有结果，
说过的考试没有下文。这里让主线按 key_date 自动向前滚动，节点当天出结果，
结果直接驱动一条"脱口而出"的主动消息。

【与既有引擎的边界】本模块只往提示词里加事实，**不写** mood/affection/observer，
不读它们的输出做判断（FIXES16 拍板决策 3：零耦合，防"输出刻度 vs 输入预期"错位，
本项目祖传病种）。本模块唯一的外部依赖是 db 与 gateway（生成用）。

【状态机】
    upcoming  key_date 距今 > 2 天
    near      距今 1~2 天
    today     key_date 当天
    resolved  当天 18:00 后生成结果，写 resolved_at
    faded     resolved 超过 2 天（不再注入，行保留供去重）

【并发/失败纪律】所有对外方法都自带 try/except 静默降级：生活剧本是氛围加成，
它坏掉绝不能连带炸掉主对话或主动消息。
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from companion.db import (
    STATE_KEY_ARC_EVENT_DAILY,
    STATE_KEY_ARC_GENERATE,
    Database,
    TIME_FORMAT,
    parse_dt,
)
from companion.gateway import LLMGateway
from companion.persona import Persona, calendar_anchor_note
from companion.prompts import (
    LIFE_ARC_GENERATE_PROMPT,
    LIFE_ARC_RESOLUTION_PROMPT,
    LIFE_ARC_SEED_POOL,
)

logger = logging.getLogger(__name__)


def _now_str() -> str:
    """本模块的时间戳一律用**模块内**的 datetime.now()。

    不用 db.now_str()：那样 created_at/resolved_at 走真实时钟，而状态机走本模块的
    datetime，两处时间源不一致会让"刚 resolved 的行"在同一次推进里被判成"已超 2 天"
    直接 faded（测试里钉住时钟时必现）。同源是这里唯一的正确写法。
    """
    return datetime.now().strftime(TIME_FORMAT)

# 活跃主线（还在前面、没出结果）= 这三种状态
ACTIVE_STATUSES = ("upcoming", "near", "today")
# 已被解决/淡出，不再计入活跃数（resolved 超过 FADE_AFTER_DAYS 会被推成 faded）
CLOSED_STATUSES = ("resolved", "faded")

# 拍板决策 1：同时活跃 2~3 条。少了补，≥3 条本轮结果全丢（多了像连续剧，少了没感觉）
MIN_ACTIVE_ARCS = 2
MAX_ACTIVE_ARCS = 3

# 状态迁移阈值
NEAR_DAYS = 2          # 距今 <= 2 天进 near
FADE_AFTER_DAYS = 2    # resolved 超过 2 天转 faded
RESOLUTION_HOUR = 18   # 当天 18 点后才算"这件事有结果了"

# 生成约束
KEY_DATE_MIN_DAYS = 3
KEY_DATE_MAX_DAYS = 14
DEDUP_THRESHOLD = 0.5  # 字符 Jaccard ≥ 0.5 判重（与 memory.add_fact 同源实现，阈值另定）
DEDUP_LOOKBACK_DAYS = 30
# 两次生成尝试的最小间隔：主线不足时主动消息每 20~40 分钟醒一次，
# 不设节流的话一次生成失败就会连烧一晚上 flash 调用。
GENERATE_COOLDOWN_MINUTES = 60

# 事件通道
EVENT_WINDOW_HOURS = 24   # resolved_at 在 24h 内才够格触发事件消息
EVENT_DAILY_CAP = 1       # 每天最多 1 条事件消息


def _char_jaccard(a: str, b: str) -> float:
    """字符级 Jaccard 相似度（汉字集合，无汉字时退化为小写词/字符集合）。

    与 memory.add_fact / proactive._char_jaccard 的实现同源。
    按 FIXES11 负面清单，不跨模块合并这几份拷贝，仅在此保留一份局部实现。
    """
    a_chars = set(re.findall(r"[\u4e00-\u9fa5]", a))
    if not a_chars:
        a_chars = set(a.lower().split()) or set(a.lower())
    b_chars = set(re.findall(r"[\u4e00-\u9fa5]", b))
    if not b_chars:
        b_chars = set(b.lower().split()) or set(b.lower())
    union = a_chars | b_chars
    return len(a_chars & b_chars) / len(union) if union else 0.0


def _relative_day_label(key_date: str, today: Optional[datetime] = None) -> str:
    """把 key_date/时间点翻译成"今天/明天/后天/昨天"这种她嘴里会说的说法。"""
    ref = today or datetime.now()
    try:
        target = datetime.strptime(str(key_date)[:10], "%Y-%m-%d")
    except (TypeError, ValueError):
        return ""
    base = ref.replace(hour=0, minute=0, second=0, microsecond=0)
    delta = (target - base).days
    return {
        0: "今天",
        1: "明天",
        2: "后天",
        3: "大后天",
        -1: "昨天",
        -2: "前天",
    }.get(delta, f"{abs(delta)} 天后" if delta > 0 else f"{abs(delta)} 天前")


class LifeArcManager:
    """她的生活主线表：生成、推进、注入、事件。"""

    def __init__(self, db: Database, gateway: LLMGateway, persona: Persona):
        self.db = db
        self.gateway = gateway
        self.persona = persona

    # ==========================================
    # 读
    # ==========================================

    async def fetch_arcs(self, statuses: Optional[Tuple[str, ...]] = None) -> List[Dict[str, Any]]:
        """取主线行。statuses 为 None 时取全部。"""
        try:
            if statuses:
                placeholders = ",".join("?" for _ in statuses)
                rows = await self.db.fetchall(
                    f"SELECT * FROM life_arcs WHERE status IN ({placeholders}) ORDER BY key_date ASC, id ASC",
                    tuple(statuses),
                )
            else:
                rows = await self.db.fetchall("SELECT * FROM life_arcs ORDER BY key_date ASC, id ASC")
            return [dict(r) for r in rows]
        except Exception as e:
            logger.error(f"[Arcs] 读取生活主线失败: {e}")
            return []

    async def count_active(self) -> int:
        """活跃主线数（upcoming/near/today）"""
        rows = await self.fetch_arcs(ACTIVE_STATUSES)
        return len(rows)

    # ==========================================
    # 写
    # ==========================================

    async def insert_arc(
        self,
        title: str,
        detail: str,
        key_date: str,
        emotional_stake: str = "",
    ) -> bool:
        """写入一条主线；成功返回 True，参数不合法或写库失败返回 False。"""
        title = (title or "").strip()
        detail = (detail or "").strip()
        key_date = (key_date or "").strip()[:10]
        if not title or not detail or not key_date:
            return None
        try:
            await self.db.execute(
                """
                INSERT INTO life_arcs (title, detail, key_date, status, emotional_stake, created_at)
                VALUES (?, ?, ?, 'upcoming', ?, ?)
                """,
                (title, detail, key_date, (emotional_stake or "").strip(), _now_str()),
            )
            logger.info(f"[Arcs] 新增生活主线: 《{title}》 key_date={key_date}")
            return True  # 返回布尔语义即可，调用方不依赖具体 id
        except Exception as e:
            logger.error(f"[Arcs] 写入生活主线失败: {e}")
            return None

    # ==========================================
    # 任务2：生成器
    # ==========================================

    async def _recent_titles(self) -> List[str]:
        """近 30 天已有主线的 title+detail，供提示词防重复 + 入库前去重"""
        cutoff = (datetime.now() - timedelta(days=DEDUP_LOOKBACK_DAYS)).strftime(TIME_FORMAT)
        try:
            rows = await self.db.fetchall(
                "SELECT title, detail FROM life_arcs WHERE created_at >= ? ORDER BY id DESC",
                (cutoff,),
            )
            return [f"{r['title']}｜{r['detail']}" for r in rows]
        except Exception as e:
            logger.warning(f"[Arcs] 读取近 30 天主线失败，防重复降级: {e}")
            return []

    async def _generate_raw(self) -> List[Dict[str, str]]:
        """调 flash 生成 1~2 条主线（不做上限/去重校验，那是调用方的活）。"""
        now = datetime.now()
        recent = await self._recent_titles()
        anchor_note = calendar_anchor_note(now.strftime("%Y-%m-%d"), lookahead=1)
        if not anchor_note:
            # 查不到锚点不是致命的：留空让模型只靠"现在几月"判断，仍比没有强
            anchor_note = "（本条校历锚点缺失，请只按当前月份的一般节奏来定）"

        user_prompt = LIFE_ARC_GENERATE_PROMPT.format(
            character_core=self.persona.core_description or "（角色卡未提供）",
            current_date=now.strftime("%Y-%m-%d 星期") + "一二三四五六日"[now.weekday()],
            calendar_note=anchor_note,
            seed_pool=LIFE_ARC_SEED_POOL,
            recent_titles="\n".join(recent) if recent else "（无）",
            date_min=(now + timedelta(days=KEY_DATE_MIN_DAYS)).strftime("%Y-%m-%d"),
            date_max=(now + timedelta(days=KEY_DATE_MAX_DAYS)).strftime("%Y-%m-%d"),
        )

        try:
            resp = await self.gateway.chat(
                messages=[{"role": "user", "content": user_prompt}],
                model=self.gateway.config.observer_model,
                temperature=0.9,
                json_mode=True,
                purpose="life_arc",
            )
            data = json.loads(resp)
        except Exception as e:
            logger.warning(f"[Arcs] 生活主线生成失败: {e}")
            return []

        raw = data.get("arcs")
        if not isinstance(raw, list):
            logger.warning(f"[Arcs] 生成结果不是 arcs 列表，丢弃: {str(data)[:120]}")
            return []

        out: List[Dict[str, str]] = []
        for item in raw[:2]:  # 硬上限 2 条，超出部分直接丢
            if not isinstance(item, dict):
                continue
            out.append(
                {
                    "title": str(item.get("title", "")).strip(),
                    "detail": str(item.get("detail", "")).strip(),
                    "key_date": str(item.get("key_date", "")).strip()[:10],
                    "emotional_stake": str(item.get("emotional_stake", "")).strip(),
                }
            )
        return out

    def _validate_key_date(self, key_date: str, now: datetime) -> bool:
        """key_date 必须落在未来 3~14 天内，否则丢弃"""
        try:
            target = datetime.strptime(key_date, "%Y-%m-%d")
        except (TypeError, ValueError):
            return False
        delta = (target.date() - now.date()).days
        return KEY_DATE_MIN_DAYS <= delta <= KEY_DATE_MAX_DAYS

    async def ensure_arcs(self, min_active: int = MIN_ACTIVE_ARCS) -> int:
        """补主线到 min_active 条为止。返回本轮实际新增条数。

        三个硬闸门（顺序即优先级，宁可少不可滥）：
        1. 活跃已 ≥ MAX_ACTIVE_ARCS → 本轮生成结果**全部丢弃**（拍板决策 1）
        2. 距上次生成尝试不足 GENERATE_COOLDOWN_MINUTES → 直接跳过（防连烧 API）
        3. 单条 key_date 不在 3~14 天内 / 与近 30 天主线 Jaccard ≥ 0.5 → 丢弃
        """
        try:
            active = await self.count_active()
        except Exception as e:
            logger.warning(f"[Arcs] 统计活跃主线失败，跳过本轮补线: {e}")
            return 0

        if active >= MAX_ACTIVE_ARCS:
            logger.debug(f"[Arcs] 活跃主线已有 {active} 条（上限 {MAX_ACTIVE_ARCS}），不生成")
            return 0
        if active >= min_active:
            return 0

        # 节流
        if not await self._mark_generate_attempt():
            return 0

        candidates = await self._generate_raw()
        if not candidates:
            return 0

        if active >= MAX_ACTIVE_ARCS:
            # 生成期间别的路径可能又加了几条，复查一次
            logger.info("[Arcs] 生成期间活跃主线已达上限，本轮结果全部丢弃")
            return 0

        now = datetime.now()
        recent = await self._recent_titles()
        added = 0
        for cand in candidates:
            if (await self.count_active()) >= MAX_ACTIVE_ARCS:
                logger.info("[Arcs] 已达活跃上限，停止写入剩余候选")
                break
            title, detail = cand["title"], cand["detail"]
            if not title or not detail:
                continue
            if not self._validate_key_date(cand["key_date"], now):
                logger.info(
                    f"[Arcs] key_date={cand['key_date']!r} 不在未来 "
                    f"{KEY_DATE_MIN_DAYS}~{KEY_DATE_MAX_DAYS} 天内，丢弃《{title}》"
                )
                continue
            blob = f"{title}｜{detail}"
            dup_hit = next((r for r in recent if _char_jaccard(blob, r) >= DEDUP_THRESHOLD), None)
            if dup_hit is not None:
                logger.info(
                    f"[Arcs] 与近 30 天主线重复（相似度 "
                    f"{_char_jaccard(blob, dup_hit):.2f} ≥ {DEDUP_THRESHOLD}），丢弃《{title}》"
                )
                continue

            if await self.insert_arc(title, detail, cand["key_date"], cand["emotional_stake"]):
                added += 1
                recent.append(blob)

        logger.info(f"[Arcs] 本轮补充生活主线 {added} 条（生成候选 {len(candidates)} 条）")
        return added

    async def _mark_generate_attempt(self) -> bool:
        """节流闸：距上次尝试不足 GENERATE_COOLDOWN_MINUTES 则返回 False（不生成）。"""
        last = await self.db.get_state_json(STATE_KEY_ARC_GENERATE, {})
        last_dt = parse_dt(last.get("last_attempt")) if isinstance(last, dict) else None
        now = datetime.now()
        if last_dt and (now - last_dt).total_seconds() < GENERATE_COOLDOWN_MINUTES * 60:
            logger.debug("[Arcs] 距上次主线生成不足 1 小时，本轮跳过")
            return False
        await self.db.set_state_json(STATE_KEY_ARC_GENERATE, {"last_attempt": _now_str()})
        return True

    # ==========================================
    # 任务3：状态推进 + 结果生成
    # ==========================================

    async def _generate_resolution(self, arc: Dict[str, Any]) -> str:
        """生成 1~2 句结果；失败返回空串（空串=不推进状态，下轮再试）"""
        user_prompt = LIFE_ARC_RESOLUTION_PROMPT.format(
            title=arc.get("title", ""),
            detail=arc.get("detail", ""),
            emotional_stake=arc.get("emotional_stake", ""),
            key_date=arc.get("key_date", ""),
            current_date=datetime.now().strftime("%Y-%m-%d"),
        )
        try:
            resp = await self.gateway.chat(
                messages=[{"role": "user", "content": user_prompt}],
                model=self.gateway.config.observer_model,
                temperature=0.8,
                purpose="life_arc",
            )
            return (resp or "").strip()
        except Exception as e:
            logger.warning(f"[Arcs] 生成主线结果失败《{arc.get('title')}》: {e}")
            return ""

    async def advance_states(self) -> int:
        """推进状态机。返回本轮发生状态迁移的行数。

        每个主动消息周期都会调一次（很便宜：只在 today 且过 18:00 时才发 API）。
        """
        now = datetime.now()
        today = now.date()
        changed = 0

        # ① 前沿状态：upcoming → near → today
        for arc in await self.fetch_arcs(ACTIVE_STATUSES):
            try:
                target = datetime.strptime(str(arc["key_date"])[:10], "%Y-%m-%d").date()
            except (TypeError, ValueError):
                logger.warning(f"[Arcs] key_date 格式异常，跳过该行: {arc.get('key_date')!r}")
                continue
            delta = (target - today).days
            want = "today" if delta <= 0 else ("near" if delta <= NEAR_DAYS else "upcoming")
            if want != arc["status"]:
                await self.db.execute(
                    "UPDATE life_arcs SET status = ? WHERE id = ?", (want, arc["id"])
                )
                logger.info(f"[Arcs] 主线《{arc['title']}》状态 {arc['status']} → {want}")
                changed += 1

        # ② 当天 18:00 后出结果 → resolved
        if now.hour >= RESOLUTION_HOUR:
            for arc in await self.fetch_arcs(("today",)):
                if arc.get("resolution"):
                    continue
                resolution = await self._generate_resolution(arc)
                if not resolution:
                    continue  # 生成失败：保持 today，下个周期再试
                await self.db.execute(
                    "UPDATE life_arcs SET status = 'resolved', resolution = ?, resolved_at = ? WHERE id = ?",
                    (resolution, _now_str(), arc["id"]),
                )
                logger.info(f"[Arcs] 主线《{arc['title']}》出结果: {resolution}")
                changed += 1

        # ③ resolved 超过 2 天 → faded（行保留供去重）
        faded_cutoff = now - timedelta(days=FADE_AFTER_DAYS)
        for arc in await self.fetch_arcs(("resolved",)):
            r_dt = parse_dt(arc.get("resolved_at"))
            if r_dt and r_dt < faded_cutoff:
                await self.db.execute(
                    "UPDATE life_arcs SET status = 'faded' WHERE id = ?", (arc["id"],)
                )
                logger.info(f"[Arcs] 主线《{arc['title']}》已淡出")
                changed += 1

        return changed

    # ==========================================
    # 任务4：提示词注入
    # ==========================================

    async def build_prompt_block(self) -> str:
        """【她最近的生活】区块；无可注入主线时返回空串（整块省略，不留空标题）。"""
        try:
            now = datetime.now()
            lines: List[str] = []

            # 临近/今天：注入她在牵挂的那几件事
            for arc in await self.fetch_arcs(("near", "today")):
                label = _relative_day_label(arc.get("key_date", ""), now)
                stake = (arc.get("emotional_stake") or "").strip()
                suffix = f"（{label}）" if label else ""
                body = f"：{stake}" if stake else ""
                lines.append(f"- （临近）{arc['title']}{body}{suffix}")

            # 有结果：resolved ≤2 天，告诉她事情已经落地
            for arc in await self.fetch_arcs(("resolved",)):
                r_dt = parse_dt(arc.get("resolved_at"))
                if r_dt and (now - r_dt).total_seconds() > FADE_AFTER_DAYS * 24 * 3600:
                    continue
                label = _relative_day_label(arc.get("resolved_at", "")[:10], now)
                resolution = (arc.get("resolution") or "").strip()
                suffix = f"（{label}出的结果）" if label else ""
                body = f"：{resolution}" if resolution else ""
                lines.append(f"- （有结果）{arc['title']}{body}{suffix}")

            if not lines:
                return ""
            return (
                "\n【她最近的生活】（这是她当下真实的生活主线，她的话会自然带上这些事的进展）\n"
                + "\n".join(lines)
            )
        except Exception as e:
            logger.error(f"[Arcs] 生成生活主线区块异常: {e}")
            return ""

    # ==========================================
    # 任务5：事件通道
    # ==========================================

    async def _event_sent_today(self) -> bool:
        today = datetime.now().strftime("%Y-%m-%d")
        data = await self.db.get_state_json(STATE_KEY_ARC_EVENT_DAILY, {})
        if isinstance(data, dict) and data.get("date") == today:
            try:
                return int(data.get("count", 0)) >= EVENT_DAILY_CAP
            except (TypeError, ValueError):
                return False
        return False

    async def mark_event_sent(self) -> None:
        """事件消息发出后记账（每天最多 EVENT_DAILY_CAP 条）。"""
        today = datetime.now().strftime("%Y-%m-%d")
        data = await self.db.get_state_json(STATE_KEY_ARC_EVENT_DAILY, {})
        count = 0
        if isinstance(data, dict) and data.get("date") == today:
            try:
                count = int(data.get("count", 0))
            except (TypeError, ValueError):
                count = 0
        await self.db.set_state_json(STATE_KEY_ARC_EVENT_DAILY, {"date": today, "count": count + 1})

    async def claim_event(self) -> Optional[Dict[str, Any]]:
        """取一条够格发事件消息的主线，并**立刻**置 event_announced=1（防重发）。

        够格条件：status='resolved'、event_announced=0、resolved_at 在 24h 内、
        且今天还没发过事件消息。取不到返回 None。
        置位先于发送：宁可"发失败但不再重试"，也不冒同一件事连发两遍的风险。
        """
        try:
            if await self._event_sent_today():
                return None
            cutoff = (datetime.now() - timedelta(hours=EVENT_WINDOW_HOURS)).strftime(TIME_FORMAT)
            row = await self.db.fetchone(
                """
                SELECT * FROM life_arcs
                WHERE status = 'resolved' AND event_announced = 0
                  AND resolved_at != '' AND resolved_at >= ?
                ORDER BY resolved_at ASC LIMIT 1
                """,
                (cutoff,),
            )
            if not row:
                return None
            arc = dict(row)
            await self.db.execute(
                "UPDATE life_arcs SET event_announced = 1 WHERE id = ?", (arc["id"],)
            )
            logger.info(f"[Arcs] 事件通道选中主线《{arc['title']}》: {arc.get('resolution')}")
            return arc
        except Exception as e:
            logger.error(f"[Arcs] 事件通道取主线失败: {e}")
            return None

    async def release_event(self, arc: Dict[str, Any]) -> None:
        """事件消息生成或发送失败时回滚置位，让它下轮还能再试。"""
        try:
            await self.db.execute(
                "UPDATE life_arcs SET event_announced = 0 WHERE id = ?", (arc.get("id"),)
            )
        except Exception as e:
            logger.warning(f"[Arcs] 回滚 event_announced 失败: {e}")

    def build_event_material(self, arc: Dict[str, Any]) -> str:
        """事件消息的话题素材：把主线+结果拼成"刚发生的事"。

        语气要求（任务5）由这段文案承载：脱口而出，不是汇报。
        """
        title = (arc.get("title") or "").strip()
        resolution = (arc.get("resolution") or "").strip()
        detail = (arc.get("detail") or "").strip()
        parts = [f"这是刚刚发生在她身上的事：{title}"]
        if resolution:
            parts.append(f"结果：{resolution}")
        if detail:
            parts.append(f"背景（她自己知道，不用向他解释）：{detail}")
        parts.append(
            "生成要求：这是她脱口而出分享刚发生的事，语气是事情刚发生时的第一反应"
            "（报喜/吐槽/松口气都行），不是事后汇报。"
        )
        return " ".join(parts)
