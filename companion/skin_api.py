"""角色皮肤看板：数据接口 + 页面查找 (companion/skin_api.py)。

[admin].theme = "skin"（或没写 theme、而角色卡里有 theme/pages/ 目录）时启用：

  服务端只出数据：/api/v1/now、/memory、/debug、/costs、/stickers、/logs、/admin，全是 JSON；
  页面由角色卡自己画：characters/<卡>/theme/pages/ 里放 home.html、memory.html……，
  外加它们要的 css / js / 字体 / 图片；卡里没有的页面，用 companion/skins/default/ 的同名页兜底。
  桌面小窝（launcher/home.py）也是皮肤里的一页：launcher.html。

页面怎么拿数据：同源 fetch("/api/v1/now?token=…")，token 从自己地址栏的 ?token= 原样带上；
写操作仍走原来的 POST /admin/backup|restart|reset（带 X-Admin-Token 头、Accept: application/json）。

这里只取数、不排版；取数逻辑尽量复用 dashboard.collect_* 与 dashboard_pages.collect_*，
不另起一套查询。追踪文件（data/turn_traces/*.jsonl）只读最近两天，够调试页用。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Tuple

from companion import dashboard
from companion import dashboard_pages as pages
from companion.db import parse_dt

logger = logging.getLogger(__name__)

DEFAULT_SKIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skins", "default")

# 看板路径 → 皮肤页面名。小窝不在这里：它由 launcher/home.py 在本机提供。
PAGES: Dict[str, str] = {
    "/": "home",
    "/memory": "memory",
    "/debug": "debug",
    "/costs": "costs",
    "/stickers": "stickers",
    "/logs": "logs",
    "/admin": "admin",
}

# 皮肤目录里允许对外提供的文件类型（只读）。没有 .py、没有任意二进制。
ASSET_TYPES: Dict[str, str] = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
    ".txt": "text/plain; charset=utf-8",
}

TRACE_DAYS = 2          # 调试页读最近几天的追踪文件
TRACE_LIST_LIMIT = 30   # 调试页的轮次下拉最多列多少轮
LOG_LIMIT = 200

_SECTION_RE = re.compile(r"^【([^】\n]{1,40})】", re.M)
_LOG_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2}) (?P<time>\d{2}:\d{2}:\d{2})(?:,\d+)? "
    r"\[(?P<level>[A-Z]+)\] (?P<name>[^:]+): (?P<msg>.*)$"
)
_MODULE_TAG_RE = re.compile(r"^\[([^\]]{1,24})\]\s*")


# ---------------------------------------------------------------- 页面与静态文件


def card_pages_dir(char_dir: str) -> Optional[str]:
    """角色卡的皮肤页面目录 theme/pages/；没有就 None。"""
    if not char_dir:
        return None
    path = os.path.join(char_dir, "theme", "pages")
    return path if os.path.isdir(path) else None


def _safe_join(base: Optional[str], rel: str) -> Optional[str]:
    """base 目录下的 rel 文件的真实路径；越出 base、不存在、类型不在白名单 → None。"""
    if not base or not rel:
        return None
    ext = os.path.splitext(rel)[1].lower()
    if ext not in ASSET_TYPES:
        return None
    root = os.path.realpath(base)
    full = os.path.realpath(os.path.join(root, rel))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        return None
    return full


def find_file(card_pages: Optional[str], rel: str) -> Optional[str]:
    """先找角色卡的皮肤，再找默认皮肤。"""
    return _safe_join(card_pages, rel) or _safe_join(DEFAULT_SKIN_DIR, rel)


def content_type(path: str) -> str:
    return ASSET_TYPES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


# ---------------------------------------------------------------- JSON 化


def jsonable(obj: Any) -> Any:
    """把 collect_* 的结果变成 json 能直接吐的东西：时间转 ISO 字符串，元组转列表。"""
    if isinstance(obj, datetime):
        return obj.isoformat(timespec="seconds")
    if isinstance(obj, date):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return jsonable(dataclasses.asdict(obj))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, float):
        return round(obj, 4)
    return obj


# ---------------------------------------------------------------- 追踪文件


def _trace_files(trace_dir: str, days: int = TRACE_DAYS) -> List[str]:
    if not trace_dir or not os.path.isdir(trace_dir):
        return []
    names = sorted((n for n in os.listdir(trace_dir) if n.endswith(".jsonl")), reverse=True)
    return [os.path.join(trace_dir, n) for n in names[:days]]


def read_recent_traces(trace_dir: str, days: int = TRACE_DAYS) -> Dict[str, List[Dict[str, Any]]]:
    """最近几天的追踪事件，按 trace_id 分组；组内保持写入顺序，组按最新在前。"""
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for path in reversed(_trace_files(trace_dir, days)):  # 旧文件先读，保证组内时间顺序
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    tid = ev.get("trace_id")
                    if tid:
                        groups.setdefault(str(tid), []).append(ev)
        except OSError as e:
            logger.warning(f"[SkinAPI] 读追踪文件失败 {path}: {e}")
    return dict(reversed(list(groups.items())))


def _step(events: Iterable[Dict[str, Any]], step: str) -> Optional[Dict[str, Any]]:
    """该轮最后一次出现的某个步骤的 payload。"""
    found = None
    for ev in events:
        if ev.get("step") == step:
            found = ev.get("payload")
    return found if isinstance(found, dict) else None


def _turn_summary(tid: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    inp = _step(events, "turn_input") or {}
    sticker = _step(events, "expression_reaction")
    text = str(inp.get("text") or "")
    if not text and sticker:
        text = "（他发了个表情）"
    return {
        "id": tid,
        "at": str(events[0].get("timestamp") or ""),
        "source": str(inp.get("source") or ("sticker" if sticker else "")),
        "text": text,
    }


def _is_turn(events: List[Dict[str, Any]]) -> bool:
    steps = {e.get("step") for e in events}
    return bool(steps & {"turn_input", "expression_reaction", "delivery_result"})


def list_turns(traces: Dict[str, List[Dict[str, Any]]], limit: int = TRACE_LIST_LIMIT) -> List[Dict[str, Any]]:
    out = [_turn_summary(tid, evs) for tid, evs in traces.items() if _is_turn(evs)]
    return out[:limit]


def split_sections(prompt: str) -> List[Dict[str, Any]]:
    """系统提示词按【小节】切开；开头没有标题的部分叫「开场」。"""
    if not prompt:
        return []
    marks = list(_SECTION_RE.finditer(prompt))
    out: List[Dict[str, Any]] = []
    head = prompt[: marks[0].start()].strip() if marks else prompt.strip()
    if head:
        out.append({"title": "开场", "chars": len(head), "text": head})
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(prompt)
        body = prompt[m.start():end].strip()
        out.append({"title": m.group(1), "chars": len(body), "text": body})
    return out


def turn_detail(tid: str, events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """一轮的四个乐章：读到什么、写下什么、发出什么、记下什么。"""
    inp = _step(events, "turn_input") or {}
    runtime = _step(events, "runtime") or {}
    ctx = _step(events, "model_context") or {}
    raw = _step(events, "model_raw") or {}
    plan_ev = _step(events, "delivery_plan") or {}
    result = _step(events, "delivery_result") or {}
    validation = _step(events, "validation") or {}
    reaction = _step(events, "expression_reaction")
    error = _step(events, "model_error")

    messages = ctx.get("messages") or []
    system = ""
    extra: List[Dict[str, Any]] = []
    history_chars, history_n = 0, 0
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        role, content = str(msg.get("role") or ""), str(msg.get("content") or "")
        if i == 0 and role == "system":
            system = content
        elif role == "system":
            m = _SECTION_RE.match(content)
            extra.append({"title": m.group(1) if m else "系统旁注", "chars": len(content), "text": content})
        else:
            history_chars += len(content)
            history_n += 1
    sections = split_sections(system)
    if history_n:
        sections.append({
            "title": "最近对话", "chars": history_chars,
            "text": f"{history_n} 条（他和她的原话，按时间先后）",
        })
    sections.extend(extra)

    draft = plan_ev.get("draft") if isinstance(plan_ev.get("draft"), dict) else {}
    bubbles = [
        {"id": str(b.get("bubble_id") or b.get("id") or ""), "text": str(b.get("text") or "")}
        for b in (draft.get("bubbles") or []) if isinstance(b, dict)
    ]
    plan = plan_ev.get("plan") if isinstance(plan_ev.get("plan"), dict) else {}
    planned = [
        {"bubble": str(a.get("bubble_id") or ""), "channel": str(a.get("channel") or "text")}
        for a in (plan.get("actions") or []) if isinstance(a, dict)
    ]
    sent = [
        {
            "channel": str(m.get("channel") or "text"),
            "text": str(m.get("text") or ""),
            "message_id": m.get("message_id"),
            "fallback_from": m.get("fallback_from"),
            "attachment": bool(m.get("attachment")),
        }
        for m in (result.get("messages") or []) if isinstance(m, dict)
    ]
    if reaction and reaction.get("status") == "sent":
        sticker = reaction.get("sticker") or {}
        sent.append({
            "channel": "sticker",
            "text": str(sticker.get("desc") or sticker.get("name") or "") if isinstance(sticker, dict) else "",
            "message_id": reaction.get("message_id"), "fallback_from": None, "attachment": True,
        })

    return {
        "id": tid,
        "at": str(events[0].get("timestamp") or "") if events else "",
        "input": {"text": str(inp.get("text") or ""), "source": str(inp.get("source") or "")},
        "read": {"sections": sections, "chars": sum(s["chars"] for s in sections)},
        "write": {
            "text": str(raw.get("text") or ""),
            "bubbles": bubbles,
            "model": str(raw.get("served_model") or runtime.get("model") or ""),
            "error": error,
        },
        "send": {
            "status": str(result.get("status") or (reaction or {}).get("status") or ""),
            "planned": planned,
            "sent": sent,
            "errors": [str(e) for e in (result.get("errors") or [])],
        },
        "check": {
            "passed": validation.get("passed"),
            "errors": [str(e) for e in (validation.get("errors") or [])],
        },
    }


def latest_state_note(traces: Dict[str, List[Dict[str, Any]]]) -> Tuple[str, str]:
    """最近一轮里她被告知的「此刻的状态」原文和时间——看板「她心里在想」就用这句。"""
    for tid, events in traces.items():
        ctx = _step(events, "model_context") or {}
        for msg in ctx.get("messages") or []:
            content = str((msg or {}).get("content") or "") if isinstance(msg, dict) else ""
            if content.startswith("【你此刻的状态】"):
                return content.split("\n", 1)[-1].strip(), str(events[0].get("timestamp") or "")
    return "", ""


async def home_extra(srv: Any) -> Dict[str, Any]:
    """小窝皮肤要的几样：心情的原始数值（换算速度与调性）、关系是第几首、主模型最近一次好不好。"""
    mood = await srv.mood.get_state()
    aff = await srv.affection.get_state()
    return {
        "mood": {"v": float(mood.get("v", 0.0)), "a": float(mood.get("a", 0.0))},
        "stage_index": int(aff.get("stage", 0)),
        "model": model_health(read_recent_traces(_trace_dir(srv), days=1)),
    }


def model_health(traces: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """主模型最近一次是好是坏：最新的一次生成成功 → ok；最新的是报错 → 不 ok；还没生成过 → None。"""
    for events in traces.values():
        for ev in reversed(events):
            if ev.get("step") == "model_raw":
                return {"ok": True, "at": str(ev.get("timestamp") or ""), "error": ""}
            if ev.get("step") == "model_error":
                p = ev.get("payload") if isinstance(ev.get("payload"), dict) else {}
                return {"ok": False, "at": str(ev.get("timestamp") or ""), "error": str(p.get("message") or "")}
    return {"ok": None, "at": "", "error": ""}


def recent_stickers_sent(traces: Dict[str, List[Dict[str, Any]]], limit: int = 5) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for tid, events in traces.items():
        detail = turn_detail(tid, events)
        for m in detail["send"]["sent"]:
            if m["channel"] == "sticker" and m["text"]:
                out.append({"name": m["text"], "at": detail["at"]})
        if len(out) >= limit:
            break
    return out[:limit]


# ---------------------------------------------------------------- 作息


def today_routine(persona: Any, now: datetime, holiday_span: int = 0) -> List[Dict[str, Any]]:
    """今天 0–24 点逐小时问作息表，相邻同一件事合成一段。和聊天主链路同一个判断。"""
    blocks: List[Dict[str, Any]] = []
    for h in range(24):
        try:
            text, structured = persona.get_current_activity_detail(h, now.weekday(), holiday_span=holiday_span)
        except Exception:
            text, structured = persona.get_current_activity(h, now.weekday(), holiday_span=holiday_span), True
        if blocks and blocks[-1]["activity"] == text:
            blocks[-1]["end"] = h + 1
        else:
            blocks.append({"start": h, "end": h + 1, "activity": text, "structured": bool(structured)})
    for b in blocks:
        b["current"] = b["start"] <= now.hour < b["end"]
        b["past"] = now.hour >= b["end"]
    return blocks


# ---------------------------------------------------------------- 各页数据


async def api_now(srv: Any) -> Dict[str, Any]:
    """首页「此刻」：她在干什么、心情、关系走到哪、今天的作息、约定和惦记的事。"""
    d = await dashboard.collect_overview(srv)
    now: datetime = d["now"]
    mood = await srv.mood.get_state()
    persona = srv.persona
    stages = [{"name": s.name, "tone": s.tone} for s in getattr(persona, "stages", [])]

    last_user = await srv.db.fetchone("SELECT created_at FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1")
    last_dt = parse_dt(str(last_user["created_at"])) if last_user and last_user["created_at"] else None
    hours_quiet = round((now - last_dt).total_seconds() / 3600.0, 2) if last_dt else None

    span = 0
    try:
        from companion.persona import holiday_span  # 与 admin._current_activity 同一数据源

        span = holiday_span(now.strftime("%Y-%m-%d"), srv.assembler.get_holidays())
    except Exception:
        pass

    traces = read_recent_traces(_trace_dir(srv), days=1)
    note, note_at = latest_state_note(traces)
    arc = await srv.db.fetchone(
        "SELECT title, detail, key_date, emotional_stake FROM life_arcs "
        "WHERE status IN ('upcoming','near','today') ORDER BY key_date ASC LIMIT 1"
    )

    return jsonable({
        "now": now,
        "name": d["name"],
        "activity": d["activity"],
        "asleep": d["asleep"],
        "mood": {
            "label": d["mood_label"], "desc": d["mood_desc"],
            "v": float(mood.get("v", 0.0)), "a": float(mood.get("a", 0.0)),
        },
        "stage": {
            "index": d["stage"], "count": d["n_stages"], "name": d["stage_name"], "tone": d["tone"],
            "next": d["next_name"], "progress": d["progress"], "floor": d["floor_name"],
            "all": stages,
        },
        "dims": [{"name": k, "value": v} for k, v in d["dims"]],
        "state_note": note, "state_note_at": note_at,
        "hours_quiet": hours_quiet,
        "last_chat": d["last_chat"],
        "days_known": d["days_known"],
        "turns": d["turns"],
        "unanswered": d["unanswered"], "max_unanswered": d["max_unanswered"],
        "routine": today_routine(persona, now, span),
        "holiday_span": span,
        "arc": {
            "title": arc["title"], "detail": arc["detail"] or "", "date": arc["key_date"],
            "stake": arc["emotional_stake"] or "",
        } if arc else None,
        "model": model_health(traces),
        "meetups": d["meetups"],
        "followups": [{"topic": t, "when": w} for t, w in d["followups"]],
        "desires": d["desires"],
        "impressions": d["impressions"],
        "onebot": d["onebot"],
        "cost": {"today": d["cost_today"], "month": d["cost_month"]},
        "backup": d["backup"],
    })


async def api_memory(srv: Any) -> Dict[str, Any]:
    d = await dashboard.collect_memory(srv)
    rows = await srv.db.fetchall("SELECT content, confidence FROM impressions ORDER BY confidence DESC, id DESC LIMIT 8")
    d["impressions"] = [{"t": r["content"], "confidence": int(r["confidence"] or 1)} for r in rows]
    return jsonable(d)


async def api_costs(srv: Any) -> Dict[str, Any]:
    d = await pages.collect_costs(srv)
    now = datetime.now()
    month_start = now.strftime("%Y-%m-01 00:00")
    rows = await srv.db.fetchall(
        "SELECT substr(created_at, 1, 10) AS day, SUM(cost_estimate) AS c, COUNT(*) AS n "
        "FROM llm_calls WHERE created_at >= ? GROUP BY day ORDER BY day",
        (month_start,),
    )
    month_total = sum(float(r["c"] or 0.0) for r in rows)
    d.update({
        "month": now.strftime("%Y-%m"),
        "cost_month": month_total,
        "days": [{"day": r["day"], "cost": float(r["c"] or 0.0), "calls": int(r["n"] or 0)} for r in rows],
    })
    return jsonable(d)


async def api_debug(srv: Any, trace_id: str = "") -> Dict[str, Any]:
    base = await pages.collect_debug(srv)
    traces = read_recent_traces(_trace_dir(srv))
    turns = list_turns(traces)
    if trace_id not in traces:
        # 默认看最近一轮真正经过主模型的对话；纯表情轮次没有前三个乐章
        trace_id = next((t["id"] for t in turns if _step(traces[t["id"]], "turn_input")), turns[0]["id"] if turns else "")
    pick = trace_id
    return jsonable({
        "now": base["now"],
        "turns": turns,
        "turn": turn_detail(pick, traces[pick]) if pick else None,
        "observer": {
            "all": base["all"], "week": base["week"],
            "latest": _first_json(base["records"]),
        },
        "fallback_prompt_chars": base["prompt_chars"],
    })


def api_stickers(srv: Any) -> Dict[str, Any]:
    srv.stickers.load_index()
    items = []
    for name, data in srv.stickers._index.items():
        items.append({
            "name": name,
            "desc": str(data.get("desc") or ""),
            "meaning": str(data.get("meaning") or ""),
            "usage": str(data.get("usage") or ""),
            "url": srv._link(f"/stickers/img/{name}"),
        })
    traces = read_recent_traces(_trace_dir(srv))
    return jsonable({
        "now": datetime.now(), "items": items, "count": len(items),
        "recent": recent_stickers_sent(traces),
    })


def classify_log(line: str) -> Dict[str, str]:
    """一行日志拆成时间、级别、模块、正文；拆不开的整行当正文（例如 Traceback 的续行）。"""
    m = _LOG_RE.match(line)
    if not m:
        level = "error" if ("Traceback" in line or line.startswith(("  File ", "    "))) else "info"
        return {"date": "", "time": "", "level": level, "module": "", "msg": line}
    msg = m.group("msg")
    module = m.group("name").rsplit(".", 1)[-1]
    tag = _MODULE_TAG_RE.match(msg)
    if tag:
        module, msg = tag.group(1), msg[tag.end():]
    lv = m.group("level").lower()
    level = {"warning": "warn", "critical": "error"}.get(lv, lv)
    return {"date": m.group("date"), "time": m.group("time"), "level": level, "module": module, "msg": msg}


def api_logs(srv: Any, level: str = "") -> Dict[str, Any]:
    d = pages.collect_logs(srv)
    lines = [classify_log(ln) for ln in d["lines"] if ln.strip()]
    lines.reverse()  # 最新在上
    if level in ("info", "warn", "error", "debug"):
        lines = [ln for ln in lines if ln["level"] == level]
    return jsonable({
        "now": d["now"], "missing": d["missing"], "error": d["error"],
        "lines": lines[:LOG_LIMIT],
    })


async def api_admin(srv: Any) -> Dict[str, Any]:
    d = await pages.collect_admin(srv)
    backups = []
    try:
        names = sorted(
            (n for n in os.listdir(srv.backup_dir) if n.endswith(".db") and n != "latest.db"),
            reverse=True,
        )
        for n in names[:5]:
            p = os.path.join(srv.backup_dir, n)
            backups.append({
                "name": n, "bytes": os.path.getsize(p),
                "at": datetime.fromtimestamp(os.path.getmtime(p)),
            })
    except OSError:
        pass
    started = getattr(srv, "start_time", None)
    return jsonable({
        "now": d["now"],
        "last_backup": d["last_backup"],
        "backups": backups,
        "uptime_minutes": int((datetime.now() - started).total_seconds() / 60) if started else None,
        "token_on": d["token_on"],
    })


# ---------------------------------------------------------------- 小工具


def _trace_dir(srv: Any) -> str:
    return getattr(srv, "trace_dir", "") or "data/turn_traces"


def _first_json(records: List[str]) -> Any:
    for r in records or []:
        try:
            return json.loads(r)
        except ValueError:
            continue
    return None


__all__ = [
    "ASSET_TYPES", "DEFAULT_SKIN_DIR", "PAGES",
    "api_admin", "api_costs", "home_extra", "api_debug", "api_logs", "api_memory", "api_now", "api_stickers",
    "card_pages_dir", "classify_log", "content_type", "find_file", "jsonable",
    "list_turns", "model_health", "read_recent_traces", "split_sections", "today_routine", "turn_detail",
]
