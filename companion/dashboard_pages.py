"""新主题（plain / endfield）的五个页面：调试、费用、表情包、日志、管理。

这些页面原先由 admin.py 用 f-string 拼旧手账 HTML，再整体塞进新外壳——
结果是新旧两套 CSS 同时出现在一张页面里。现在拆成：

  collect_*  —— 只取数（查询语句与旧页面一致，功能不变）
  render_*   —— 只排版（全部用 dashboard.CSS 里的组件类，没有一处 style 属性）

总览与记忆在 dashboard.py；本模块与它共用外壳、皮肤、页头与页脚装饰。
journal 主题不走这里：admin.py 的 journal 分支仍是 admin_render 那套旧页面。
"""

from __future__ import annotations

import html
import json
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Sequence, Tuple

from companion.backup import get_last_backup_time
from companion.dashboard import _sh
from companion.db import TIME_FORMAT
from companion.observer import recent_observer_logs

LOG_FILE = "data/logs/bot.log"
LOG_LINES = 200
STICKER_LIMIT = 200


# ---------------------------------------------------------------- 排版零件


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _reg() -> str:
    return (
        '<div class="reg" aria-hidden="true"><i></i><i></i><i></i><i></i></div>'
    )


def _foot(meta: Sequence[str], link_href: str = "", link_label: str = "") -> str:
    items = "".join(f"<span>{m}</span>" for m in meta)
    link = (
        f'<a class="foot-link" href="{_e(link_href)}">{_e(link_label)}</a>'
        if link_href else ""
    )
    return f'<footer class="foot">{_reg()}{items}{link}</footer>'


def _head(
    idx: int,
    en: str,
    zh: str,
    sub: str = "",
    side: str = "",
    seal: str = "",
) -> str:
    seal_html = (
        f'<span class="seal seal-sm ink-only" aria-hidden="true">{_e(seal)}</span>'
        if seal else ""
    )
    sub_html = f'<p class="sub">{_e(sub)}</p>' if sub else ""
    return f"""
<header class="ph">
  <div class="ph-mark wide no-ink" aria-hidden="true">{_e(en)}</div>
  <div class="ph-mark brush ink-only" aria-hidden="true">{_e(seal or zh[:1])}</div>
  <div class="ph-main">
    <div class="kick"><span class="idx">{idx:02d}</span><span class="hatch" aria-hidden="true"></span>
      <span class="tickrule" aria-hidden="true"></span><span class="en">{_e(en)}</span></div>
    <div class="ph-title"><h1 class="lead">{_e(zh)}</h1>{seal_html}</div>
    {sub_html}
  </div>
  <div class="side">{side}</div>
</header>"""


def _empty(title: str, hint: str = "") -> str:
    hint_html = f'<p class="empty-p">{hint}</p>' if hint else ""
    return (
        f'<div class="empty-box"><span class="mk" aria-hidden="true"></span>'
        f'<div><p class="empty-t">{_e(title)}</p>{hint_html}</div></div>'
    )


def _stat(label: str, value: str, unit: str = "") -> str:
    unit_html = f' <small>{_e(unit)}</small>' if unit else ""
    return (
        f'<div class="stat"><span class="k">{_e(label)}</span>'
        f'<span class="v">{value}{unit_html}</span></div>'
    )


def _meter(
    label: str,
    value: float,
    max_value: float,
    display: str = "",
    warn: bool = False,
    note: str = "",
) -> str:
    value = max(0.0, min(float(max_value), float(value)))
    cls = "meter warn" if warn else "meter"
    note_html = f'<span class="mnote">{note}</span>' if note else ""
    return (
        f'<div class="{cls}"><div class="mrow"><span class="mk">{_e(label)}</span>'
        f'<span class="mv">{_e(display or f"{value:g}")}</span></div>'
        f'<progress max="{max_value:g}" value="{value:g}" aria-label="{_e(label)}"></progress>'
        f'{note_html}</div>'
    )


def _metric(label: str, value: float, max_value: float = 10.0) -> str:
    value = max(0.0, min(float(max_value), float(value)))
    return (
        f'<div class="metric"><span class="mk">{_e(label)}</span>'
        f'<span class="mv">{value:.2f}</span>'
        f'<progress max="{max_value:g}" value="{value:g}" aria-label="{_e(label)}"></progress></div>'
    )


def _stamp() -> str:
    return datetime.now().strftime("%m.%d %H:%M")


# ---------------------------------------------------------------- 费用


async def collect_costs(srv: Any) -> Dict[str, Any]:
    """与旧 /costs 同一批查询：全表汇总、近 24 小时、今日、缓存、分组、明细。"""
    total_row = await srv.db.fetchone(
        "SELECT COUNT(*) as calls, SUM(cost_estimate) as total_cost, "
        "SUM(prompt_tokens) as p_tokens, SUM(completion_tokens) as c_tokens FROM llm_calls"
    )
    past_24h = (datetime.now() - timedelta(hours=24)).strftime(TIME_FORMAT)
    row_24h = await srv.db.fetchone(
        "SELECT SUM(cost_estimate) as cost_24h FROM llm_calls WHERE created_at >= ?",
        (past_24h,),
    )
    cache_row = await srv.db.fetchone(
        "SELECT SUM(cache_hit_tokens) as hit, SUM(cache_miss_tokens) as miss FROM llm_calls"
    )
    cache_hit = float(cache_row["hit"] or 0.0) if cache_row else 0.0
    cache_miss = float(cache_row["miss"] or 0.0) if cache_row else 0.0
    cache_total = cache_hit + cache_miss

    group_rows = await srv.db.fetchall(
        """
        SELECT purpose, COUNT(*) as cnt, SUM(prompt_tokens) as p_tok,
               SUM(completion_tokens) as c_tok, SUM(cost_estimate) as cost
        FROM llm_calls GROUP BY purpose ORDER BY cost DESC
        """
    )
    detail_rows = await srv.db.fetchall(
        """
        SELECT id, purpose, model, prompt_tokens, completion_tokens, cost_estimate, created_at
        FROM llm_calls ORDER BY id DESC LIMIT 20
        """
    )

    return {
        "now": datetime.now(),
        "total_calls": int(total_row["calls"] or 0) if total_row else 0,
        "total_cost": float(total_row["total_cost"] or 0.0) if total_row else 0.0,
        "cost_24h": float(row_24h["cost_24h"] or 0.0) if row_24h else 0.0,
        "cost_today": await srv._today_cost(),
        "cache_hit": cache_hit,
        "cache_total": cache_total,
        "cache_rate": (cache_hit / cache_total * 100.0) if cache_total > 0 else 0.0,
        "groups": [
            {
                "purpose": r["purpose"] or "—",
                "calls": int(r["cnt"] or 0),
                "p_tok": int(r["p_tok"] or 0),
                "c_tok": int(r["c_tok"] or 0),
                "cost": float(r["cost"] or 0.0),
            }
            for r in group_rows
        ],
        "details": [
            {
                "id": r["id"],
                "purpose": r["purpose"] or "",
                "model": r["model"] or "",
                "p_tok": int(r["prompt_tokens"] or 0),
                "c_tok": int(r["completion_tokens"] or 0),
                "cost": float(r["cost_estimate"] or 0.0),
                "at": r["created_at"] or "",
            }
            for r in detail_rows
        ],
    }


def render_costs(d: Dict[str, Any]) -> str:
    stats = "".join(
        [
            _stat("今日费用", f"¥{d['cost_today']:.4f}"),
            _stat("近 24 小时", f"¥{d['cost_24h']:.4f}"),
            _stat("累计调用", f"{d['total_calls']:,}", "次"),
            _stat("累计估算", f"¥{d['total_cost']:.4f}"),
        ]
    )
    cache_note = f"命中 {int(d['cache_hit']):,} / 总计 {int(d['cache_total']):,} tokens"

    if d["groups"]:
        group_block = (
            '<div class="table-wrap"><table class="tbl"><thead><tr><th>Purpose</th>'
            '<th class="r">调用数</th><th class="r">Prompt</th><th class="r">Completion</th>'
            '<th class="r">总费用</th></tr></thead><tbody>'
            + "".join(
                f'<tr><td>{_e(g["purpose"])}</td><td class="num r">{g["calls"]:,}</td>'
                f'<td class="num r">{g["p_tok"]:,}</td><td class="num r">{g["c_tok"]:,}</td>'
                f'<td class="num r">¥{g["cost"]:.4f}</td></tr>'
                for g in d["groups"]
            )
            + "</tbody></table></div>"
        )
    else:
        group_block = _empty("还没有调用记录", "调用模型之后，这里会按用途拆开记账。")

    if d["details"]:
        detail_block = (
            '<div class="table-wrap"><table class="tbl"><thead><tr><th>ID</th><th>用途</th>'
            '<th>模型</th><th class="r">Prompt</th><th class="r">Completion</th>'
            '<th class="r">费用</th><th>时间</th></tr></thead><tbody>'
            + "".join(
                f'<tr><td class="num">{x["id"]}</td><td>{_e(x["purpose"])}</td>'
                f'<td class="num">{_e(x["model"])}</td><td class="num r">{x["p_tok"]:,}</td>'
                f'<td class="num r">{x["c_tok"]:,}</td><td class="num r">¥{x["cost"]:.6f}</td>'
                f'<td class="num">{_e(x["at"])}</td></tr>'
                for x in d["details"]
            )
            + "</tbody></table></div>"
        )
    else:
        detail_block = _empty("还没有调用明细", "最近 20 次调用会逐条列在这里。")

    return f"""
<div class="topbar">
  <span class="micro">/ COSTS / {d['now'].strftime('%m.%d %H:%M')}</span>
  <span class="micro">计费统计</span>
</div>
{_head(4, "COSTS", "计费统计",
       sub="每一次调用的账都记在这里：按用途拆开，也留最近二十次的明细。",
       side=f'<span class="micro">累计估算</span><span class="v num">¥{d["total_cost"]:.2f}</span>',
       seal="账")}
<section class="sec">
  <div class="col">
    {_sh(1, "Summary", "总费用汇总")}
    <div class="frame"><div class="grid-stats">{stats}</div></div>
    <div class="panel">
      {_meter("缓存命中率", d["cache_rate"], 100.0,
              display=f"{d['cache_rate']:.1f}%", note=cache_note)}
    </div>
  </div>
  <div class="col">
    {_sh(2, "By Purpose", "按业务用途分组统计")}
    {group_block}
  </div>
</section>
<section class="sec">
  <div class="col">
    {_sh(3, "Recent Calls", "最近 20 次调用明细")}
    {detail_block}
  </div>
</section>
{_foot([f'<span class="micro">统计口径 </span>llm_calls 全表',
        f'<span class="micro">此刻 </span><span class="num">{_stamp()}</span>'],
       "/debug", "看看调试信息")}"""


# ---------------------------------------------------------------- 调试


async def collect_debug(srv: Any) -> Dict[str, Any]:
    system_prompt = srv.assembler.last_assembled_prompt or "（尚未组装过提示词）"
    score_row = await srv.db.fetchone(
        """
        SELECT COUNT(*) as cnt, AVG(self_disclosure) as sd, AVG(responsiveness) as rs,
               AVG(warmth_score) as wa, AVG(resonance) as re FROM observer_scores
        """
    )
    past_7d = (datetime.now() - timedelta(days=7)).strftime(TIME_FORMAT)
    score_row_7d = await srv.db.fetchone(
        """
        SELECT COUNT(*) as cnt, AVG(self_disclosure) as sd, AVG(responsiveness) as rs,
               AVG(warmth_score) as wa, AVG(resonance) as re FROM observer_scores
        WHERE created_at >= ?
        """,
        (past_7d,),
    )

    def _score(row: Any) -> Dict[str, Any]:
        if not row or not row["cnt"]:
            return {}
        return {
            "cnt": int(row["cnt"]),
            "sd": float(row["sd"] or 0.0),
            "rs": float(row["rs"] or 0.0),
            "wa": float(row["wa"] or 0.0),
            "re": float(row["re"] or 0.0),
        }

    return {
        "now": datetime.now(),
        "system_prompt": system_prompt,
        "prompt_chars": len(system_prompt),
        "all": _score(score_row),
        "week": _score(score_row_7d),
        "records": [
            json.dumps(ob, ensure_ascii=False, indent=2)
            for ob in reversed(recent_observer_logs)
        ],
    }


def _score_panel(label: str, s: Dict[str, Any]) -> str:
    if not s:
        return (
            f'<div class="panel"><div class="panel-head"><h3>{_e(label)}</h3></div>'
            + _empty("还没有结算记录", "跑过几轮对话后，观察者会在这里留下打分。")
            + "</div>"
        )
    return f"""
<div class="panel">
  <div class="panel-head"><h3>{_e(label)}</h3><span class="pill">{s['cnt']} 次结算</span></div>
  <div class="grid-metrics">
    {_metric("自我表露", s["sd"])}
    {_metric("感知回应", s["rs"])}
    {_metric("情感温度", s["wa"])}
    {_metric("共鸣", s["re"])}
  </div>
</div>"""


def render_debug(d: Dict[str, Any]) -> str:
    if d["records"]:
        records = "".join(
            f'<details class="rec"><summary><span class="chev" aria-hidden="true"></span>'
            f'结算记录 #{i + 1}<span class="micro">原始 JSON</span></summary>'
            f'<pre class="code">{_e(blob)}</pre></details>'
            for i, blob in enumerate(d["records"])
        )
    else:
        records = _empty("还没有观察者结算记录", "她完成一次结算后，这里会留下原始打分与理由。")

    return f"""
<div class="topbar">
  <span class="micro">/ DEBUG / {d['now'].strftime('%m.%d %H:%M')}</span>
  <span class="micro">调试信息</span>
</div>
{_head(3, "DEBUG", "调试信息",
       sub="观察者的打分锚点、最近一次完整提示词，以及原始结算记录。",
       side=f'<span class="micro">结算记录</span><span class="v num">{len(d["records"])}</span>',
       seal="诊")}
<section class="sec">
  <div class="col">
    {_sh(1, "Observer", "观察者打分分布")}
    <p class="sub">均值长期 ≈6 为正常进度；若 ≈4 说明打分手紧，关系推进会偏慢。</p>
    <div class="duo">
      {_score_panel("全部时间", d["all"])}
      {_score_panel("近 7 天", d["week"])}
    </div>
  </div>
</section>
<section class="sec">
  <div class="col">
    {_sh(2, "System Prompt", "最近一次完整 System Prompt")}
    <div class="panel">
      <div class="panel-head"><h3>完整提示词</h3>
        <span class="micro num">{d["prompt_chars"]:,} 字符</span></div>
      <pre class="code">{_e(d["system_prompt"])}</pre>
    </div>
  </div>
</section>
<section class="sec">
  <div class="col">
    {_sh(3, "Observer Log", "最近 5 次观察者结算原始 JSON")}
    <div>{records}</div>
  </div>
</section>
{_foot([f'<span class="micro">提示词长度 </span><span class="num">{d["prompt_chars"]:,}</span>',
        f'<span class="micro">此刻 </span><span class="num">{_stamp()}</span>'],
       "/costs", "去看费用")}"""


# ---------------------------------------------------------------- 表情包


def collect_stickers(srv: Any) -> Dict[str, Any]:
    srv.stickers.load_index()
    items = [
        {
            "name": name,
            "desc": data.get("desc", ""),
            "url": srv._link(f"/stickers/img/{name}"),
        }
        for name, data in srv.stickers._index.items()
    ]
    return {"now": datetime.now(), "items": items}


def render_stickers(d: Dict[str, Any]) -> str:
    n = len(d["items"])
    if d["items"]:
        tiles = "".join(
            f'<figure class="tile"><div class="tile-img">'
            f'<img src="{_e(it["url"])}" alt="{_e(it["name"])}" loading="lazy"></div>'
            f'<figcaption><span class="nm">{_e(it["name"])}</span>'
            f'<span class="ds">{_e(it["desc"])}</span></figcaption></figure>'
            for it in d["items"]
        )
        gallery = f'<div class="gallery">{tiles}</div>'
    else:
        gallery = _empty(
            "还没有表情包",
            "把图片和 index.json 放进角色卡的 stickers 目录，她聊天时就能用了。",
        )

    return f"""
<div class="topbar">
  <span class="micro">/ STICKERS / {d['now'].strftime('%m.%d %H:%M')}</span>
  <span class="micro">表情包库</span>
</div>
{_head(5, "STICKERS", "表情包库",
       sub="她能从图库里挑图发给你；名字和描述决定她在什么场合用哪一张。",
       side=f'<span class="micro">已收录</span><span class="v num">{n} / {STICKER_LIMIT}</span>',
       seal="笑")}
<section class="sec">
  <div class="col">
    {_sh(1, "Gallery", "表情包图库")}
    {gallery}
  </div>
</section>
{_foot([f'<span class="micro">图库上限 </span><span class="num">{STICKER_LIMIT}</span>',
        f'<span class="micro">此刻 </span><span class="num">{_stamp()}</span>'],
       "/memory", "去看记忆")}"""


# ---------------------------------------------------------------- 日志


def collect_logs(srv: Any, path: str = LOG_FILE) -> Dict[str, Any]:
    missing = not os.path.exists(path)
    lines: List[str] = []
    error = ""
    if not missing:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = [ln.rstrip("\r\n") for ln in f.readlines()[-LOG_LINES:]]
        except Exception as e:  # 读不出来也要把页面给出去
            error = str(e)
    return {
        "now": datetime.now(),
        "path": path,
        "missing": missing,
        "error": error,
        "lines": lines,
    }


def _line_level(text: str) -> str:
    upper = text.upper()
    if "ERROR" in upper or "CRITICAL" in upper or "TRACEBACK" in upper:
        return "lvl-error"
    if "WARNING" in upper or " WARN" in upper:
        return "lvl-warn"
    if "DEBUG" in upper:
        return "lvl-debug"
    return ""


def render_logs(d: Dict[str, Any]) -> str:
    if d["missing"]:
        body = _empty(
            "还没有日志文件",
            f"{d['path']} 还没生成。服务跑起来、产生日志后，这里会显示最近 "
            f"{LOG_LINES} 行。",
        )
    elif d["error"]:
        body = _empty("日志读取失败", d["error"])
    elif not d["lines"]:
        body = _empty("日志文件是空的", "等机器人跑起来，这里就会开始滚动。")
    else:
        rows = "".join(
            f'<div class="logline {_line_level(ln)}">'
            f'<span class="ln" aria-hidden="true">{i + 1:03d}</span>'
            f'<span class="tx">{_e(ln)}</span></div>'
            for i, ln in enumerate(d["lines"])
        )
        body = f'<div class="logbox">{rows}</div>'

    count = len(d["lines"])
    return f"""
<div class="topbar">
  <span class="micro">/ LOGS / {d['now'].strftime('%m.%d %H:%M')}</span>
  <span class="micro">运行日志</span>
</div>
{_head(6, "LOGS", "运行日志",
       sub="服务最近写了什么。出错的行标红，警告加粗，调试行压暗。",
       side=f'<span class="micro">最近 {LOG_LINES} 行</span><span class="v num">{count}</span>',
       seal="历")}
<section class="sec">
  <div class="col">
    {_sh(1, "Tail", "运行日志")}
    <div class="panel">
      <div class="panel-head"><h3>{_e(d['path'])}</h3>
        <span class="micro num">{count} 行</span></div>
      {body}
    </div>
  </div>
</section>
{_foot([f'<span class="micro">文件 </span>{_e(d["path"])}',
        f'<span class="micro">此刻 </span><span class="num">{_stamp()}</span>'],
       "/admin", "去管理面板")}"""


# ---------------------------------------------------------------- 管理


async def collect_admin(srv: Any) -> Dict[str, Any]:
    last_backup = get_last_backup_time(srv.backup_dir) or "暂无备份"
    token_field = ""
    if srv.config.token:
        token_field = (
            f'<input type="hidden" name="token" value="{html.escape(srv.config.token)}">'
        )
    return {
        "now": datetime.now(),
        "last_backup": last_backup,
        "token_field": token_field,
        "db_path": srv.db_path,
        "token_on": bool(srv.config.token),
    }


def render_admin(d: Dict[str, Any]) -> str:
    return f"""
<div class="topbar">
  <span class="micro">/ ADMIN / {d['now'].strftime('%m.%d %H:%M')}</span>
  <span class="micro">管理控制台</span>
</div>
{_head(7, "ADMIN", "管理控制台",
       sub="日常运维、备份与数据重置都在这里。关键操作需要二次确认，并写入安全日志。",
       side=f'<span class="micro">上次备份</span><span class="num">{_e(d["last_backup"])}</span>',
       seal="管")}
<section class="sec">
  <div class="col">
    {_sh(1, "Operations", "运维操作")}
    <div class="frame"><div class="actions">
      <section class="act">
        <span class="ano">01 / BACKUP</span>
        <h3>立即备份数据库</h3>
        <p>立即对 <code>{_e(d["db_path"])}</code> 做在线备份，生成快照并覆盖
          <code>latest.db</code>（保留最新 14 份）。</p>
        <p class="meta"><span class="micro">上次备份</span>
          <span class="num">{_e(d["last_backup"])}</span></p>
        <form action="/admin/backup" method="post"
              onsubmit="return confirm('确定立即备份数据库吗？');">{d["token_field"]}
          <button type="submit" class="btn">立即备份</button>
        </form>
      </section>
      <section class="act">
        <span class="ano">02 / RESTART</span>
        <h3>重启伴侣机器人</h3>
        <p>用于代码更新或修改配置后重新加载。发出指令后进程在 5 秒后退出，
          由 systemd 自动拉起。</p>
        <p class="note">重启期间静默进行，不打扰机主。</p>
        <form action="/admin/restart" method="post"
              onsubmit="return confirm('确定重启机器人服务吗？服务将在 5 秒后退出并由 systemd 自动重新拉起。');">{d["token_field"]}
          <button type="submit" class="btn quiet">重启服务</button>
        </form>
      </section>
      <section class="act danger">
        <span class="ano">03 / RESET</span>
        <h3>重置关系数据（高危）</h3>
        <p>清空好感度、心情 PAD、对话历史、语义事实与日记。执行前会自动创建快照备份，
          表情包与计费明细完整保留。</p>
        <form action="/admin/reset" method="post"
              onsubmit="return confirm('警告：此操作将清空所有好感与记忆！确定要重置吗？');">{d["token_field"]}
          <div class="field">
            <label for="confirm-yes">输入大写 YES 确认执行</label>
            <input id="confirm-yes" type="text" name="confirm" placeholder="YES"
                   required autocomplete="off" spellcheck="false">
          </div>
          <button type="submit" class="btn danger">确认重置数据</button>
        </form>
      </section>
    </div></div>
  </div>
</section>
{_foot([f'<span class="micro">数据库 </span>{_e(d["db_path"])}',
        f'<span class="micro">鉴权 </span>{"已开启" if d["token_on"] else "仅本机"}'],
       "/", "返回总览")}"""


# ---------------------------------------------------------------- 结果 / 403


def render_notice(
    kind: str,
    title: str,
    kicker: str,
    paras: Sequence[str],
    links: Sequence[Tuple[str, str]] = (),
) -> str:
    """操作结果页（备份/重启/重置的成败）与 403 页共用的一套排版。"""
    kind = kind if kind in ("ok", "warn", "err") else "ok"
    # 纯文本段落自动包 <p>；调用方已经给出块级标签（列表等）时原样保留
    body = "".join(
        p if p.lstrip().startswith(("<p", "<ul", "<ol", "<div")) else f"<p>{p}</p>"
        for p in paras
    )
    nav = "".join(f'<a href="{_e(href)}">{_e(label)}</a>' for href, label in links)
    nav_html = f'<div class="nlinks">{nav}</div>' if nav else ""
    return f"""
<div class="topbar"><span class="micro">/ ADMIN / NOTICE</span><span class="micro">操作结果</span></div>
<section class="notice {kind}">
  <span class="nk">{_e(kicker)}</span>
  <h1 class="lead">{_e(title)}</h1>
  {body}
  {nav_html}
</section>"""


def render_forbidden() -> str:
    return render_notice(
        "err",
        "未授权",
        "ACCESS DENIED",
        [
            "读取仪表盘页面需要正确的 admin token。请在 URL 上带 token 参数，"
            "或在请求头携带 X-Admin-Token。",
        ],
    )


def render_unauthorized() -> str:
    return render_notice(
        "err",
        "未授权",
        "ACCESS DENIED",
        [
            "管理写操作需要正确的 admin token。请在请求头携带 X-Admin-Token，"
            "或在表单里附带 token 字段。",
        ],
        [("/admin", "返回管理面板")],
    )
