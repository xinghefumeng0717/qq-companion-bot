"""状态仪表盘 HTTP 服务 (admin.py)
基于 aiohttp 实现手账纸主题仪表盘 (绑定 127.0.0.1:8080)，30 秒自动刷新。
路由包含：总览 (/)、记忆 (/memory)、调试 (/debug)、计费 (/costs)、表情包 (/stickers)、日志 (/logs)、管理 (/admin)。
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote
from aiohttp import web

from companion.affection import AffectionEngine, STAGE_THRESHOLDS
from companion.assembler import PromptAssembler
from companion.config import AdminConfig
from companion.db import Database, COUNTER_KEY_TOTAL_TURNS, TIME_FORMAT, parse_dt, now_str
from companion.memory import POSITIVE_SENTIMENTS, NEGATIVE_SENTIMENTS, MemoryManager, calc_diary_strength
from companion.mood import MoodEngine
from companion.observer import recent_observer_logs
from companion.persona import Persona, holiday_span
from companion.prompts import get_mood_description, get_mood_label, get_trust_description
from companion.proactive import ProactiveScheduler
from companion.backup import get_last_backup_time, run_daily_backup
from companion.reset import reset_database
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

from companion.admin_render import (
    HTML_STYLE,
    format_chinese_date,
    get_sentiment_color,
    render_section_header,
    render_radar_svg,
    render_nav,
    html_shell,
)

# 管理页鉴权（外部评审）：token 为空时保持 localhost 信任模式，行为与旧版逐字节一致；
# token 非空时，**写操作**必须携带匹配的 token（请求头 X-Admin-Token 或表单/JSON 字段），
# **读页面**（9 条 GET 路由）同样要求 token（URL query ?token= 或同一个请求头）——
# 读侧泄漏的是记忆、日志、计费与关系状态，把 host 绑到非回环时不能只挡写不挡读。
ADMIN_TOKEN_HEADER = "X-Admin-Token"
ADMIN_TOKEN_QUERY = "token"
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def warn_if_admin_exposed_without_token(config: AdminConfig) -> None:
    """host 绑到非回环地址却没设 token 时，打一条醒目的"裸奔"警告。

    token 为空 = 完全信任同网段：写操作（/admin/reset、/admin/restart、/admin/backup）
    一个 POST 就能打掉服务，读页面（/、/memory、/debug、/costs、/logs 等）会把记忆、
    日志与计费摊开给人看——confirm=YES 这类前端校验完全挡不住直接请求。
    """
    if config.token:
        return
    if config.host not in _LOOPBACK_HOSTS:
        logger.warning(
            "[Admin] ⚠ 安全警告：管理页 host=%s 已绑定到非回环地址，但 [admin].token 为空——"
            "/admin/reset、/admin/restart、/admin/backup 等写操作与 /memory、/logs、/costs "
            "等读页面对同网段完全裸奔。请先在 config.toml 的 [admin] 段设好 token 再对外开放。",
            config.host,
        )


__all__ = [
    "AdminServer",
    "HTML_STYLE",
    "format_chinese_date",
    "get_sentiment_color",
    "render_section_header",
    "render_radar_svg",
    "render_nav",
    "html_shell",
]


class AdminServer:
    def __init__(
        self,
        config: AdminConfig,
        persona: Persona,
        affection: AffectionEngine,
        mood: MoodEngine,
        memory: MemoryManager,
        stickers: StickerManager,
        proactive: Optional[ProactiveScheduler],
        assembler: PromptAssembler,
        db: Database,
        onebot: Optional[Any] = None,
        db_path: str = "data/companion.db",
        backup_dir: str = "data/backup/daily",
    ):
        self.config = config
        self.persona = persona
        self.affection = affection
        self.mood = mood
        self.memory = memory
        self.stickers = stickers
        self.proactive = proactive
        self.assembler = assembler
        self.db = db
        self.onebot = onebot
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.start_time = datetime.now()
        self._runner: Optional[web.AppRunner] = None

    def _current_activity(self, now_dt: datetime) -> str:
        """看板"她此刻"的活动文案：节假日段长与聊天主链路同一数据源
        （assembler.get_holidays）。长假期间她不在学校，看板不许显示
        "在学校上课"（生产实测 2026-10-05 国庆穿帮：聊天主链路已按长假口径，
        看板却在显示在校作息）。"""
        span = holiday_span(now_dt.strftime("%Y-%m-%d"), self.assembler.get_holidays())
        return self.persona.get_current_activity(
            now_dt.hour, now_dt.weekday(), holiday_span=span
        )

    async def _today_cost(self) -> float:
        """获取今日累计 LLM 费用"""
        today_str = datetime.now().strftime("%Y-%m-%d 00:00")
        row_today = await self.db.fetchone(
            "SELECT SUM(cost_estimate) as cost_today FROM llm_calls WHERE created_at >= ?",
            (today_str,),
        )
        return float(row_today["cost_today"] or 0.0) if row_today else 0.0

    async def start(self) -> None:
        warn_if_admin_exposed_without_token(self.config)
        app = web.Application()
        app.router.add_get("/", self.handle_overview)
        app.router.add_get("/api/status", self.handle_api_status)
        app.router.add_get("/memory", self.handle_memory)
        app.router.add_get("/debug", self.handle_debug)
        app.router.add_get("/costs", self.handle_costs)
        app.router.add_get("/stickers", self.handle_stickers)
        app.router.add_get("/stickers/img/{name}", self.handle_sticker_image)
        app.router.add_get("/logs", self.handle_logs)
        app.router.add_get("/admin", self.handle_admin)
        app.router.add_post("/admin/backup", self.handle_admin_backup)
        app.router.add_post("/admin/restart", self.handle_admin_restart)
        app.router.add_post("/admin/reset", self.handle_admin_reset)

        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.config.host, self.config.port)
        await site.start()
        logger.info(f"[Admin] 仪表盘已启动: http://{self.config.host}:{self.config.port}")

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    # ==========================================
    # 写操作鉴权
    # ==========================================
    async def _write_authorized(self, request: web.Request) -> bool:
        """写操作鉴权 + CSRF 防护。

        CSRF（外部审计发现）：只认 POST + confirm 字段挡不住跨站表单——
        恶意网页能跨站构造 POST 打向 127.0.0.1:8080。这里拦一道：
        浏览器跨站提交必带 Origin 头，Origin 的 host 与本服务不一致即拒绝；
        不带 Origin 的非浏览器客户端（curl/脚本）不受影响。

        token 为空 = 仅 localhost 信任模式，直接放行（与改动前行为逐字节一致）；
        token 非空时，要求请求头 X-Admin-Token 或表单/JSON 字段 token 与之相等。
        """
        origin = request.headers.get("Origin") or request.headers.get("Referer")
        if origin:
            from urllib.parse import urlparse

            def _norm(host: str) -> str:
                # localhost 与 127.0.0.1 视为同源（浏览器怎么开的都有）
                h = host.split("@")[-1].lower()
                return h.replace("localhost", "127.0.0.1").replace("[::1]", "127.0.0.1")

            origin_host = _norm(urlparse(origin).netloc)
            if origin_host and origin_host != _norm(request.host):
                logger.warning(
                    f"[AdminAction] 拒绝跨站写操作：Origin={origin_host} != {request.host}"
                )
                return False

        expected = self.config.token
        if not expected:
            return True
        provided = request.headers.get(ADMIN_TOKEN_HEADER, "")
        if not provided:
            provided = await self._token_from_body(request)
        return provided == expected

    async def _token_from_body(self, request: web.Request) -> str:
        """从 JSON 或表单体里取 token 字段；读不出来一律当空串。"""
        try:
            if request.content_type == "application/json":
                body = await request.json()
                if isinstance(body, dict):
                    return str(body.get("token", ""))
                return ""
        except Exception:
            return ""
        try:
            data = await request.post()
            return str(data.get("token", ""))
        except Exception:
            return ""

    # ==========================================
    # 读页面鉴权（GET）
    # ==========================================
    def _read_authorized(self, request: web.Request) -> bool:
        """读页面鉴权。

        token 为空 = 仅 localhost 信任模式，直接放行（与改动前逐字节一致）；
        token 非空时，要求 URL query ?token= 或请求头 X-Admin-Token 与之相等。
        query 优先于请求头：浏览器地址栏点链接只能带 query（这也是页面内链接
        必须把 token 带上的原因，见 _token_query / _link）。
        """
        expected = self.config.token
        if not expected:
            return True
        provided = request.query.get(ADMIN_TOKEN_QUERY, "") or request.headers.get(
            ADMIN_TOKEN_HEADER, ""
        )
        return provided == expected

    def _token_query(self) -> str:
        """"?token=xxx"（token 为空时是空串，此时页面输出与旧版逐字节一致）。"""
        if not self.config.token:
            return ""
        return f"?{ADMIN_TOKEN_QUERY}={quote(self.config.token, safe='')}"

    def _link(self, path: str) -> str:
        """页面内链接：带上 token，保证点一下不会掉线。token 为空时原样返回。"""
        return f"{path}{self._token_query()}"

    def _read_forbidden_response(self, request: web.Request) -> web.Response:
        """读页面鉴权失败：不回显 token，不泄漏任何页面内容。"""
        logger.warning("[AdminPage] 鉴权失败：缺少或错误的 admin token，已拒绝读取仪表盘页面")
        if "application/json" in request.headers.get("Accept", ""):
            return web.json_response(
                {"status": "error", "error": "未授权：缺少或错误的 admin token"},
                status=403,
            )
        content = """
        <div class="card">
          <h3 style="color:var(--cinnabar);">未授权</h3>
          <p>读取仪表盘页面需要正确的 admin token。请在 URL 上带 <code>?token=</code>，
          或在请求头携带 <code>X-Admin-Token</code>。</p>
        </div>
        """
        return web.Response(
            text=html_shell("未授权", "", content),
            content_type="text/html",
            status=403,
        )

    def _unauthorized_response(self, request: web.Request) -> web.Response:
        logger.warning("[AdminAction] 鉴权失败：缺少或错误的 admin token，已拒绝写操作")
        if (
            "application/json" in request.headers.get("Accept", "")
            or request.content_type == "application/json"
        ):
            return web.json_response(
                {"status": "error", "error": "未授权：缺少或错误的 admin token"},
                status=403,
            )
        content = """
        <div class="card">
          <h3 style="color:var(--cinnabar);">未授权</h3>
          <p>管理写操作需要正确的 admin token。请在请求头携带 <code>X-Admin-Token</code>，
          或在表单里附带 <code>token</code> 字段。</p>
          <p><a href="/admin">返回管理面板</a></p>
        </div>
        """
        return web.Response(
            text=html_shell("未授权", "/admin", content),
            content_type="text/html",
            status=403,
        )

    # ==========================================
    # 1. / 总览 (Overview)
    # ==========================================
    async def handle_overview(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        aff_state = await self.affection.get_state()
        dims = aff_state.get("dims", {})
        composite = float(aff_state.get("composite", 30.0))
        stage_idx = int(aff_state.get("stage", 0))
        stage_obj = self.persona.get_stage(stage_idx)

        mood_state = await self.mood.get_state()
        v = float(mood_state.get("v", 2.0))
        a = float(mood_state.get("a", 1.0))
        t = float(mood_state.get("t", 7.0))
        frustration = float(mood_state.get("frustration", 0.0))
        mood_lbl = get_mood_label(v, a)
        mood_dsc = get_mood_description(v, a)
        trust_dsc = get_trust_description(t)

        max_unanswered = (
            self.proactive.config.max_unanswered
            if (self.proactive and self.proactive.config)
            else 2
        )
        unanswered = await self.proactive.get_unanswered_count() if self.proactive else 0

        # 今日费用
        cost_today = await self._today_cost()

        # ------------------------------------------
        # 2.1 首屏"她此刻"
        # ------------------------------------------
        now_dt = datetime.now()
        month_cn, day_cn, weekday_cn = format_chinese_date(now_dt)
        date_str = f"{month_cn}{day_cn}，{weekday_cn}。"

        # 节假日段长与聊天主链路同一数据源（assembler.get_holidays）——
        # 长假期间她不在学校，看板不许显示"在学校上课"（生产实测 10-05 国庆穿帮）
        activity = self._current_activity(now_dt)
        if activity.startswith("正在"):
            act_phrase = activity
        elif activity.startswith("在"):
            act_phrase = f"正{activity}"
        else:
            act_phrase = f"正在{activity}"

        # 距上次机主说话小时数
        last_user_turn = await self.db.fetchone("SELECT created_at FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1")
        if not last_user_turn or not last_user_turn["created_at"]:
            last_user_turn = await self.db.fetchone("SELECT created_at FROM turns ORDER BY id DESC LIMIT 1")

        last_hours_phrase = ""
        if last_user_turn and last_user_turn["created_at"]:
            l_dt = parse_dt(str(last_user_turn["created_at"]))
            if l_dt:
                hours_diff = (now_dt - l_dt).total_seconds() / 3600.0
                if hours_diff >= 1.0:
                    last_hours_phrase = f" 他已有 {int(hours_diff)} 小时没说话了。"

        moment_sentence = f"{date_str}{mood_dsc}，{act_phrase}。{last_hours_phrase}".strip()

        # ------------------------------------------
        # 2.2 好感度雷达图 + 阶段台阶
        # ------------------------------------------
        radar_svg = render_radar_svg(dims)

        # 10 级横向台阶
        step_items = []
        for idx in range(10):
            if idx < stage_idx:
                bg = "var(--celadon)"
            elif idx == stage_idx:
                bg = "var(--gold)"
            else:
                bg = "var(--border)"
            step_items.append(f'<div style="flex:1; height:8px; border-radius:2px; background:{bg};" title="阶段 {idx}"></div>')
        steps_bar_html = f'<div style="display:flex; gap:4px; margin: 16px 0 12px 0;">{"".join(step_items)}</div>'

        thresholds = STAGE_THRESHOLDS
        curr_thresh = thresholds[stage_idx] if stage_idx < len(thresholds) else thresholds[-1]
        if stage_idx < len(thresholds) - 1:
            next_stage_obj = self.persona.get_stage(stage_idx + 1)
            next_thresh = thresholds[stage_idx + 1]
            rem = max(0.0, next_thresh - composite)
            step_subtext = f"复合分 <span class=\"font-num\">{composite:.1f}</span> · 距「{html.escape(next_stage_obj.name)}」还需 <span class=\"font-num\">{rem:.1f}</span>"
            stg_info = f"距阶段 {stage_idx + 1} 还差 {rem:.1f} 分（目标: {next_thresh} 分）"
        else:
            step_subtext = f"复合分 <span class=\"font-num\">{composite:.1f}</span> · 已达最高好感度阶段"
            stg_info = "已达最高好感度阶段"

        # ------------------------------------------
        # 2.3 关系档案 + 连接状态
        # ------------------------------------------
        earliest_turn = await self.db.fetchone("SELECT created_at FROM turns ORDER BY id ASC LIMIT 1")
        if earliest_turn and earliest_turn["created_at"]:
            try:
                e_str = str(earliest_turn["created_at"]).strip()
                if len(e_str) >= 10:
                    e_date = datetime.strptime(e_str[:10], "%Y-%m-%d").date()
                    now_date = datetime.now().date()
                    days_known = max(1, (now_date - e_date).days + 1)
                else:
                    days_known = 1
            except Exception:
                days_known = 1
        else:
            days_known = 0

        turns_cnt_row = await self.db.fetchone("SELECT value FROM counters WHERE key = ?", (COUNTER_KEY_TOTAL_TURNS,))
        total_turns = int(turns_cnt_row["value"]) if (turns_cnt_row and turns_cnt_row["value"] is not None) else 0

        milestone_rows = await self.db.fetchall("SELECT stage, reached_at FROM milestones ORDER BY id DESC")
        milestone_items = []
        for mr in milestone_rows:
            stg_num = int(mr["stage"])
            stg_o = self.persona.get_stage(stg_num)
            milestone_items.append(
                f'<div style="display:flex; align-items:center; gap:8px; margin: 6px 0;">'
                f'<span style="width:6px; height:6px; border-radius:50%; background:var(--gold); display:inline-block; flex-shrink:0;"></span>'
                f'<span style="font-size:13px; color:var(--ink);">阶段 {stg_num} · {html.escape(stg_o.name)}</span>'
                f'<span class="font-num" style="font-size:12px; color:var(--ink-faint);">({mr["reached_at"]})</span>'
                f'</div>'
            )
        milestone_html = "".join(milestone_items) if milestone_items else '<p style="color:var(--ink-faint); font-size:13px; margin:4px 0;">暂无里程碑记录（当前处于阶段 0）</p>'

        onebot_conn = bool(self.onebot and self.onebot.is_connected)
        if onebot_conn:
            onebot_badge = '<span style="color:var(--celadon); font-weight:600; font-size:14px;"><span class="pulse-dot">●</span> OneBot 在线</span>'
        else:
            onebot_badge = '<span style="color:var(--cinnabar); font-weight:600; font-size:14px;"><span class="pulse-dot">●</span> OneBot 离线</span>'

        last_backup_display = get_last_backup_time(self.backup_dir) or "暂无备份"

        # ------------------------------------------
        # 2.4 PAD 情绪卡
        # ------------------------------------------
        # 愉悦度 V (-5~+5) 中心偏离式
        v_clamped = max(-5.0, min(5.0, v))
        v_pct = (abs(v_clamped) / 5.0) * 50.0
        if v >= 0:
            v_bar = f'<div style="position:absolute; left:50%; top:0; height:100%; width:{v_pct:.1f}%; background:var(--celadon); border-radius:3px;"></div>'
        else:
            v_bar = f'<div style="position:absolute; left:{50.0 - v_pct:.1f}%; top:0; height:100%; width:{v_pct:.1f}%; background:var(--cinnabar); border-radius:3px;"></div>'

        # 唤醒度 A (-5~+5) 中心偏离式
        a_clamped = max(-5.0, min(5.0, a))
        a_pct = (abs(a_clamped) / 5.0) * 50.0
        if a >= 0:
            a_bar = f'<div style="position:absolute; left:50%; top:0; height:100%; width:{a_pct:.1f}%; background:var(--celadon); border-radius:3px;"></div>'
        else:
            a_bar = f'<div style="position:absolute; left:{50.0 - a_pct:.1f}%; top:0; height:100%; width:{a_pct:.1f}%; background:var(--cinnabar); border-radius:3px;"></div>'

        # 安心度 T (0~10) 从左至右
        t_pct = min(100.0, max(0.0, (t / 10.0) * 100.0))

        content = f"""
        <!-- 首屏卡：容器 1 (带顶部和纸胶带与入场动画) -->
        <div class="card-hero">
          <div class="font-sentiment" style="font-size: 17px; line-height: 1.9; color: var(--ink);">
            {html.escape(moment_sentence)}
          </div>
        </div>

        <!-- 2.2 好感度雷达图 + 阶段台阶 -->
        <div class="grid-2 section-block">
          <div>
            {render_section_header("好感度状态")}
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:8px;">
              <span style="font-size:13px; color:var(--ink-soft);">复合好感分</span>
              <span class="font-num" style="font-size:20px; font-weight:bold; color:var(--ink);">{composite:.1f}</span>
            </div>
            {radar_svg}
          </div>

          <div>
            {render_section_header("阶段台阶（进阶进度）")}
            {steps_bar_html}
            <div style="display:flex; justify-content:space-between; align-items:center; margin-top:14px; margin-bottom:12px;">
              <span class="font-sentiment" style="font-size:15px; color:var(--gold); font-weight:600;">阶段 {stage_idx} · {html.escape(stage_obj.name)}</span>
              <span style="font-size:12px; color:var(--ink-soft);">{stg_info}</span>
            </div>
            <div style="font-size:13px; color:var(--ink-soft); margin-top:8px;">
              {step_subtext}
            </div>
            <div class="font-sentiment" style="font-size:13px; color:var(--ink-soft); margin-top:18px; line-height:1.7;">
              当前相处倾向：{html.escape(stage_obj.tone)}
            </div>
          </div>
        </div>

        <!-- 2.3 关系档案 + 连接状态 -->
        <div class="grid-2 section-block">
          <div>
            {render_section_header("关系档案 (Relationship Profile)")}
            <div style="display:flex; gap:36px; margin-bottom:18px;">
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">认识天数:</div>
                <div style="margin-top:4px;"><span class="font-num" style="font-size:28px; font-weight:bold; color:var(--ink);">{days_known}</span> <span style="font-size:13px; color:var(--ink-soft);">天</span></div>
              </div>
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">累计对话:</div>
                <div style="margin-top:4px;"><span class="font-num" style="font-size:28px; font-weight:bold; color:var(--ink);">{total_turns}</span> <span style="font-size:13px; color:var(--ink-soft);">轮</span></div>
              </div>
            </div>
            <div style="margin-top:12px;">
              <div style="font-size:13px; font-weight:600; color:var(--ink); margin-bottom:8px;">里程碑时间线:</div>
              {milestone_html}
            </div>
          </div>

          <div>
            {render_section_header("连接与运行状态")}
            <div style="display:flex; flex-direction:column; gap:12px; margin-top:6px;">
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">OneBot 状态</div>
                <div style="margin-top:4px;">{onebot_badge}</div>
              </div>
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">今日估算费用</div>
                <div style="margin-top:4px;"><span class="font-num" style="font-size:18px; font-weight:bold; color:var(--ink);">¥{cost_today:.4f}</span></div>
              </div>
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">最近备份时间</div>
                <div class="font-num" style="font-size:13px; color:var(--ink); margin-top:4px;">{html.escape(str(last_backup_display))}</div>
              </div>
            </div>
          </div>
        </div>

        <!-- 2.4 PAD 情绪卡 -->
        <div class="section-block">
          {render_section_header("情绪与心理 (PAD)")}
          <div style="display:flex; flex-direction:column; gap:14px; margin-top:10px;">
            <div>
              <div style="display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px;">
                <span>愉悦度 (Valence)</span>
                <span class="font-num">{v:.1f}</span>
              </div>
              <div style="position:relative; width:100%; height:6px; background:var(--celadon-bg); border-radius:3px; overflow:hidden;">
                {v_bar}
                <div style="position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--ink-faint); opacity:0.5;"></div>
              </div>
            </div>

            <div>
              <div style="display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px;">
                <span>唤醒度 (Arousal)</span>
                <span class="font-num">{a:.1f}</span>
              </div>
              <div style="position:relative; width:100%; height:6px; background:var(--celadon-bg); border-radius:3px; overflow:hidden;">
                {a_bar}
                <div style="position:absolute; left:50%; top:0; bottom:0; width:1px; background:var(--ink-faint); opacity:0.5;"></div>
              </div>
            </div>

            <div>
              <div style="display:flex; justify-content:space-between; font-size:13px; margin-bottom:4px;">
                <span>安心度 (Trust)</span>
                <span class="font-num">{t:.2f}</span>
              </div>
              <div style="position:relative; width:100%; height:6px; background:var(--celadon-bg); border-radius:3px; overflow:hidden;">
                <div style="position:absolute; left:0; top:0; height:100%; width:{t_pct:.1f}%; background:var(--celadon); border-radius:3px;"></div>
              </div>
            </div>
          </div>

          <p class="font-sentiment" style="font-size: 14px; color: var(--ink-soft); margin-top: 18px; line-height: 1.8;">
            {mood_lbl} · {mood_dsc}。{trust_dsc}。
          </p>
          <div style="display:flex; justify-content:space-between; font-size:12px; color:var(--ink-faint); margin-top:10px; border-top:1px solid var(--border); padding-top:8px;">
            <span>主动消息未回计数：<span class="font-num">{unanswered} / {max_unanswered}</span></span>
            <span>冷落驱力：<span class="font-num">{frustration:.2f}</span></span>
          </div>
        </div>
        """
        return web.Response(
            text=html_shell("总览", "/", content, self._token_query()),
            content_type="text/html",
        )

    # ==========================================
    # 2. /memory 记忆 (手账时间轴)
    # ==========================================
    async def handle_memory(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        rows = await self.db.fetchall(
            """
            SELECT id, content, importance, sentiment, recall_count, created_at, last_recall_at
            FROM diary ORDER BY id DESC
            """
        )
        now_dt = datetime.now()
        timeline_nodes = []
        for r in rows:
            imp = float(r["importance"] or 5)
            rc = int(r["recall_count"] or 0)
            st = str(r["sentiment"] or "平静")
            l_str = r["last_recall_at"] or r["created_at"] or now_str()
            l_dt = parse_dt(l_str)
            days = max(0.0, (now_dt - l_dt).total_seconds() / 86400.0) if l_dt else 0.0

            strength, tau_eff = calc_diary_strength(imp, rc, st, days)
            percent = min(100.0, (strength / 10.0) * 100.0)
            color = get_sentiment_color(st)

            timeline_nodes.append(
                f"""<div class="diary-item">
                  <div class="diary-tape"></div>
                  <div style="position: absolute; left: -29px; top: 12px; width: 8px; height: 8px; border-radius: 50%; background: {color};"></div>
                  <div style="display: flex; align-items: center; gap: 8px; margin-bottom: 6px;">
                    <span class="font-num" style="font-size: 12px; color: var(--ink-faint);">{r['created_at']}</span>
                    <span style="display: inline-block; width: 24px; height: 4px; border-radius: 2px; background: {color};" title="{html.escape(st)}"></span>
                    <span style="font-size: 12px; color: var(--ink-soft);">{html.escape(st)}</span>
                  </div>
                  <div class="font-sentiment" style="font-size: 15px; color: var(--ink); line-height: 1.7; margin-bottom: 8px;">
                    {html.escape(r['content'])}
                  </div>
                  <div style="display: flex; align-items: center; gap: 10px;">
                    <div style="width: 120px; height: 3px; background: rgba(224, 216, 200, 0.4); border-radius: 2px; overflow: hidden;">
                      <div style="width: {percent:.1f}%; height: 100%; background: var(--celadon); border-radius: 2px;"></div>
                    </div>
                    <span class="font-num" style="font-size: 11px; color: var(--ink-faint);">强度 {strength:.2f} · 加固 {rc} 次</span>
                  </div>
                </div>"""
            )

        diary_content = "".join(timeline_nodes) if timeline_nodes else '<p style="color:var(--ink-faint);">暂无日记记录</p>'

        # 语义事实
        fact_rows = await self.db.fetchall("SELECT content FROM facts ORDER BY id ASC")
        fact_items = "".join([f"<li style='margin:6px 0;'>{html.escape(f['content'])}</li>" for f in fact_rows]) or "<li>暂无事实</li>"

        # 欲言又止池 (红笔批注感)
        sup_rows = await self.db.fetchall("SELECT content, created_at FROM suppressed_desires ORDER BY id DESC LIMIT 5")
        sup_items = "".join([
            f"<li style='margin:8px 0;'>"
            f"<div class='font-sentiment' style='font-size:14px; line-height:1.6;'>{html.escape(s['content'])}</div>"
            f"<div class='font-num' style='color:var(--ink-faint); font-size:11px; margin-top:2px;'>({s['created_at']})</div>"
            f"</li>"
            for s in sup_rows
        ]) or "<li>暂无欲言又止记录</li>"

        # 待跟进事项
        fu_rows = await self.db.fetchall("SELECT topic, remind_after, done FROM followups ORDER BY id DESC LIMIT 20")
        fu_items = "".join([
            f"<tr><td>{html.escape(f['topic'])}</td><td class='font-num'>{f['remind_after']}</td><td>{'已完成' if f['done'] else '待处理'}</td></tr>"
            for f in fu_rows
        ]) or "<tr><td colspan='3'>暂无事项</td></tr>"

        content = f"""
        <div class="section-block">
          {render_section_header(f"情景记忆日记 (共 {len(rows)} 篇 · 手账时间轴)")}
          <div style="position: relative; border-left: 2px solid var(--border); margin: 24px 0 16px 14px; padding-left: 24px;">
            {diary_content}
          </div>
        </div>

        <div class="grid-2 section-block">
          <div>
            {render_section_header("关于机主的语义记忆 (Facts)")}
            <ul style="padding-left: 20px; margin: 8px 0;">{fact_items}</ul>
          </div>
          <div style="position: relative; border-left: 3px solid var(--cinnabar); padding-left: 18px;">
            <div class="suppressed-tape"></div>
            {render_section_header("欲言又止池 (Suppressed Desires)")}
            <ul style="padding-left: 18px; margin: 8px 0;">{sup_items}</ul>
          </div>
        </div>

        <div class="section-block">
          {render_section_header("待跟进事项 (Followups)")}
          <table class="table">
            <thead><tr><th>事项</th><th>提醒时间</th><th>状态</th></tr></thead>
            <tbody>{fu_items}</tbody>
          </table>
        </div>
        """
        return web.Response(
            text=html_shell("记忆系统", "/memory", content, self._token_query()),
            content_type="text/html",
        )

    # ==========================================
    # 3. /debug 调试
    # ==========================================
    async def handle_debug(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        system_prompt = self.assembler.last_assembled_prompt or "（尚未组装过提示词）"

        observer_blocks = []
        for i, ob in enumerate(reversed(recent_observer_logs)):
            json_str = json.dumps(ob, ensure_ascii=False, indent=2)
            observer_blocks.append(f"<h4 style='color:var(--ink-soft); margin:12px 0 6px 0; font-size:13px;'>结算记录 #{i+1}</h4><pre>{html.escape(json_str)}</pre>")

        if not observer_blocks:
            observer_blocks.append("<p style='color:var(--ink-faint);'>暂无观察者结算记录</p>")

        # 观察者打分分布
        score_row = await self.db.fetchone(
            """
            SELECT COUNT(*) as cnt, AVG(self_disclosure) as sd, AVG(responsiveness) as rs,
                   AVG(warmth_score) as wa, AVG(resonance) as re FROM observer_scores
            """
        )
        past_7d = (datetime.now() - timedelta(days=7)).strftime(TIME_FORMAT)
        score_row_7d = await self.db.fetchone(
            """
            SELECT COUNT(*) as cnt, AVG(self_disclosure) as sd, AVG(responsiveness) as rs,
                   AVG(warmth_score) as wa, AVG(resonance) as re FROM observer_scores
            WHERE created_at >= ?
            """,
            (past_7d,),
        )

        def _fmt_score_row(label: str, row) -> str:
            if not row or not row["cnt"]:
                return f"<p style='margin:6px 0;'><strong>{label}:</strong> 暂无数据</p>"
            return (
                f"<p style='margin:6px 0;'><strong>{label}（{row['cnt']} 次）:</strong> "
                f"自我表露 <strong class='font-num'>{row['sd']:.2f}</strong> / "
                f"感知回应 <strong class='font-num'>{row['rs']:.2f}</strong> / "
                f"情感温度 <strong class='font-num'>{row['wa']:.2f}</strong> / "
                f"共鸣 <strong class='font-num'>{row['re']:.2f}</strong></p>"
            )

        content = f"""
        <div class="section-block">
          {render_section_header("观察者打分分布（校准打分锚点用）")}
          <p style="font-size:12px; color:var(--ink-soft); margin-bottom:12px;">均值长期 ≈6 为正常进度；若 ≈4 说明打分手紧，关系推进会偏慢。</p>
          {_fmt_score_row("全部时间", score_row)}
          {_fmt_score_row("近 7 天", score_row_7d)}
        </div>

        <div class="section-block">
          {render_section_header("最近一次完整 System Prompt")}
          <pre>{html.escape(system_prompt)}</pre>
        </div>

        <div class="section-block">
          {render_section_header("最近 5 次观察者结算原始 JSON")}
          {"".join(observer_blocks)}
        </div>
        """
        return web.Response(
            text=html_shell("调试信息", "/debug", content, self._token_query()),
            content_type="text/html",
        )

    # ==========================================
    # 4. /costs 计费
    # ==========================================
    async def handle_costs(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        total_row = await self.db.fetchone(
            "SELECT COUNT(*) as calls, SUM(cost_estimate) as total_cost, SUM(prompt_tokens) as p_tokens, SUM(completion_tokens) as c_tokens FROM llm_calls"
        )
        total_calls = total_row["calls"] if total_row else 0
        total_cost = float(total_row["total_cost"] or 0.0) if total_row else 0.0

        past_24h_str = (datetime.now() - timedelta(hours=24)).strftime(TIME_FORMAT)
        row_24h = await self.db.fetchone(
            "SELECT SUM(cost_estimate) as cost_24h FROM llm_calls WHERE created_at >= ?",
            (past_24h_str,),
        )
        cost_24h = float(row_24h["cost_24h"] or 0.0) if row_24h else 0.0

        cost_today = await self._today_cost()

        cache_row = await self.db.fetchone(
            "SELECT SUM(cache_hit_tokens) as hit, SUM(cache_miss_tokens) as miss FROM llm_calls"
        )
        cache_hit = float(cache_row["hit"] or 0.0) if cache_row else 0.0
        cache_miss = float(cache_row["miss"] or 0.0) if cache_row else 0.0
        cache_total = cache_hit + cache_miss
        cache_rate = (cache_hit / cache_total * 100.0) if cache_total > 0 else 0.0

        group_rows = await self.db.fetchall(
            """
            SELECT purpose, COUNT(*) as cnt, SUM(prompt_tokens) as p_tok, SUM(completion_tokens) as c_tok, SUM(cost_estimate) as cost
            FROM llm_calls GROUP BY purpose ORDER BY cost DESC
            """
        )
        group_trs = []
        for r in group_rows:
            group_trs.append(
                f"""<tr>
                  <td>{r['purpose']}</td>
                  <td class="font-num">{r['cnt']}</td>
                  <td class="font-num">{r['p_tok']}</td>
                  <td class="font-num">{r['c_tok']}</td>
                  <td class="font-num">¥{float(r['cost'] or 0.0):.4f}</td>
                </tr>"""
            )

        detail_rows = await self.db.fetchall(
            """
            SELECT id, purpose, model, prompt_tokens, completion_tokens, cost_estimate, created_at
            FROM llm_calls ORDER BY id DESC LIMIT 20
            """
        )
        detail_trs = []
        for r in detail_rows:
            detail_trs.append(
                f"""<tr>
                  <td class="font-num">{r['id']}</td>
                  <td>{r['purpose']}</td>
                  <td class="font-num">{r['model']}</td>
                  <td class="font-num">{r['prompt_tokens']}</td>
                  <td class="font-num">{r['completion_tokens']}</td>
                  <td class="font-num">¥{float(r['cost_estimate'] or 0.0):.6f}</td>
                  <td class="font-num" style="font-size:12px; color:var(--ink-faint);">{r['created_at']}</td>
                </tr>"""
            )

        content = f"""
        <div class="grid-2 section-block">
          <div>
            {render_section_header("总费用汇总")}
            <div style="display:grid; grid-template-columns: 1fr 1fr; gap:16px; margin: 12px 0;">
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">今日费用</div>
                <div class="font-num" style="font-size:24px; font-weight:bold; color:var(--ink); margin-top:4px;">¥{cost_today:.4f}</div>
              </div>
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">近 24 小时费用</div>
                <div class="font-num" style="font-size:24px; font-weight:bold; color:var(--ink); margin-top:4px;">¥{cost_24h:.4f}</div>
              </div>
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">累计调用次数</div>
                <div class="font-num" style="font-size:24px; font-weight:bold; color:var(--ink); margin-top:4px;">{total_calls} <span style="font-size:13px; font-weight:normal; color:var(--ink-soft);">次</span></div>
              </div>
              <div>
                <div style="color:var(--ink-soft); font-size:12px;">累计估算费用</div>
                <div class="font-num" style="font-size:24px; font-weight:bold; color:var(--ink); margin-top:4px;">¥{total_cost:.4f}</div>
              </div>
            </div>
            <div style="border-top:1px solid var(--border); padding-top:12px; margin-top:8px;">
              <div style="color:var(--ink-soft); font-size:12px; margin-bottom:4px;">缓存命中率</div>
              <div style="display:flex; align-items:baseline; gap:8px;">
                <span class="font-num" style="font-size:24px; font-weight:bold; color:var(--celadon);">{cache_rate:.1f}%</span>
                <span class="font-num" style="font-size:12px; color:var(--ink-soft);">(命中 {int(cache_hit)} / 总计 {int(cache_total)} tokens)</span>
              </div>
            </div>
          </div>

          <div>
            {render_section_header("按业务用途分组统计")}
            <table class="table">
              <thead><tr><th>Purpose</th><th>调用数</th><th>Prompt</th><th>Completion</th><th>总费用</th></tr></thead>
              <tbody>{"".join(group_trs)}</tbody>
            </table>
          </div>
        </div>

        <div class="section-block">
          {render_section_header("最近 20 次调用明细")}
          <table class="table">
            <thead><tr><th>ID</th><th>用途</th><th>模型</th><th>Prompt</th><th>Completion</th><th>费用</th><th>时间</th></tr></thead>
            <tbody>{"".join(detail_trs)}</tbody>
          </table>
        </div>
        """
        return web.Response(
            text=html_shell("计费统计", "/costs", content, self._token_query()),
            content_type="text/html",
        )

    # ==========================================
    # 5. /stickers 表情包
    # ==========================================
    async def handle_stickers(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        self.stickers.load_index()
        items = []
        for name, data in self.stickers._index.items():
            desc = data.get("desc", "")
            items.append(
                f"""<div class="sticker-item">
                  <img src="{self._link(f'/stickers/img/{html.escape(name)}')}" class="sticker-img" alt="{html.escape(name)}">
                  <div style="font-weight:600; font-size:13px; margin-top:6px; color:var(--ink);">{html.escape(name)}</div>
                  <div style="color:var(--ink-soft); font-size:12px; margin-top:2px;">{html.escape(desc)}</div>
                </div>"""
            )

        content = f"""
        <div class="section-block">
          {render_section_header(f"表情包图库 (共 {len(items)} / 200 张)")}
          <div class="sticker-grid">{"".join(items) if items else "<p style='color:var(--ink-faint);'>暂无表情包</p>"}</div>
        </div>
        """
        return web.Response(
            text=html_shell("表情包库", "/stickers", content, self._token_query()),
            content_type="text/html",
        )

    async def handle_sticker_image(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        name = request.match_info.get("name", "")
        if name in self.stickers._index:
            rel_file = self.stickers._index[name].get("file", "")
            full_path = os.path.join(self.stickers.stickers_dir, rel_file)
            if os.path.exists(full_path):
                return web.FileResponse(full_path)
        return web.Response(status=404, text="Not Found")

    # ==========================================
    # 6. /logs 日志 (纸面小票风日志窗)
    # ==========================================
    async def handle_logs(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        log_path = "data/logs/bot.log"
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                log_content = "".join(lines[-200:]) if lines else "（日志文件为空）"
            except Exception as e:
                log_content = f"读取日志出错: {e}"
        else:
            log_content = "（日志文件 data/logs/bot.log 尚不存在，启动服务并产生日志后可在此查看）"

        content = f"""
        <div class="section-block">
          {render_section_header("运行日志 (data/logs/bot.log 最近 200 行)")}
          <pre class="log-window">{html.escape(log_content)}</pre>
        </div>
        """
        return web.Response(
            text=html_shell("运行日志", "/logs", content, self._token_query()),
            content_type="text/html",
        )

    # ==========================================
    # 7. /api/status 状态 API (JSON)
    # ==========================================
    async def handle_api_status(self, request: web.Request) -> web.Response:
        # 读页面鉴权一并覆盖 /api/status：它不只是"活着没"的健康检查，
        # 还带着 stage_name / composite / today_cost——关系状态与开销。
        # 统一规则更好审计，也让"token 非空 = 什么都读不到"这句话成立；
        # 监控脚本改用 ?token= 或 X-Admin-Token 即可（token 为空时行为不变）。
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        aff_state = await self.affection.get_state()
        composite = float(aff_state.get("composite", 30.0))
        stage_idx = int(aff_state.get("stage", 0))
        stage_obj = self.persona.get_stage(stage_idx)

        cost_today = await self._today_cost()

        onebot_conn = bool(self.onebot and self.onebot.is_connected)
        last_backup = get_last_backup_time(self.backup_dir)
        uptime = int((datetime.now() - self.start_time).total_seconds() / 60)

        data = {
            "bot_alive": True,
            "onebot_connected": onebot_conn,
            "stage_name": stage_obj.name,
            "composite": round(composite, 1),
            "today_cost": round(cost_today, 4),
            "last_backup_time": last_backup,
            "uptime_minutes": uptime,
        }
        return web.json_response(data)

    # ==========================================
    # 8. /admin 管理控制台
    # ==========================================
    async def handle_admin(self, request: web.Request) -> web.Response:
        if not self._read_authorized(request):
            return self._read_forbidden_response(request)
        last_backup = get_last_backup_time(self.backup_dir) or "暂无备份"
        # token 为空时 token_field 也是空串，页面 HTML 与改动前逐字节一致。
        # token 非空时回填配置里的 token（而不是请求里带的那个）：表单是 POST，
        # 靠这个隐藏域过 _write_authorized；用请求 token 会让"走请求头进来的人"点表单就 403。
        token_field = ""
        if self.config.token:
            token_field = (
                f'<input type="hidden" name="token" value="{html.escape(self.config.token)}">'
            )
        content = f"""
        <div class="card">
          <h2>管理操作控制台</h2>
          <p style="color:var(--ink-soft);">在此可以执行日常运维、备份与数据重置操作。所有关键操作均需二次确认，并记录安全警告日志。</p>
        </div>

        <div class="grid">
          <div class="card">
            <h3>1. 立即备份数据库</h3>
            <p style="font-size:13px; color:var(--ink-soft);">立即对 <code>{html.escape(self.db_path)}</code> 进行在线备份，生成快照并覆盖 <code>latest.db</code>（保留最新 14 份）。</p>
            <p style="font-size:13px; margin: 12px 0;"><strong>上次备份时间：</strong> <span class="font-num" style="color:var(--celadon);">{html.escape(str(last_backup))}</span></p>
            <form action="/admin/backup" method="post" onsubmit="return confirm('确定立即备份数据库吗？');">{token_field}
              <button type="submit" class="btn-action" style="background: var(--celadon); color: #fff;">立即备份</button>
            </form>
          </div>

          <div class="card">
            <h3>2. 重启伴侣机器人</h3>
            <p style="font-size:13px; color:var(--ink-soft);">用于代码更新或修改配置后重新加载。发出重启指令后进程将在 5 秒后退出，由 systemd 自动拉起。</p>
            <p style="font-size:13px; color:var(--ink-faint); margin: 12px 0;">重启期间静默重启，不打扰机主。</p>
            <form action="/admin/restart" method="post" onsubmit="return confirm('确定重启机器人服务吗？服务将在 5 秒后退出并由 systemd 自动重新拉起。');">{token_field}
              <button type="submit" class="btn-action" style="background: var(--gold); color: #fff;">重启服务</button>
            </form>
          </div>

          <div class="card" style="border: 1px solid var(--cinnabar);">
            <h3 style="color:var(--cinnabar);">3. 重置关系数据 (高危)</h3>
            <p style="font-size:13px; color:var(--ink-soft);">清空好感度、心情 PAD、对话历史、语义事实与日记。执行前会自动创建快照备份，表情包与计费明细完整保留。</p>
            <form action="/admin/reset" method="post" onsubmit="return confirm('警告：此操作将清空所有好感与记忆！确定要重置吗？');">{token_field}
              <div style="margin: 12px 0;">
                <label style="font-size:13px; color:var(--cinnabar); display:block; margin-bottom:6px;">输入大写 <code>YES</code> 确认执行：</label>
                <input type="text" name="confirm" placeholder="YES" required style="padding: 6px 10px; background: var(--bg); border: 1px solid var(--cinnabar); color: var(--ink); border-radius: 4px; width: 120px; font-family: monospace;">
              </div>
              <button type="submit" class="btn-action" style="background: var(--cinnabar); color: #fff;">确认重置数据</button>
            </form>
          </div>
        </div>
        """
        return web.Response(
            text=html_shell("管理控制台", "/admin", content, self._token_query()),
            content_type="text/html",
        )

    async def handle_admin_backup(self, request: web.Request) -> web.Response:
        if not await self._write_authorized(request):
            return self._unauthorized_response(request)
        try:
            backup_path = run_daily_backup(self.db_path, self.backup_dir)
            logger.warning(f"[AdminAction] 立即备份成功: {backup_path}")
            if "application/json" in request.headers.get("Accept", ""):
                return web.json_response({"status": "ok", "backup_path": backup_path})
            content = f"""
            <div class="card">
              <h3 style="color:var(--celadon);">备份成功</h3>
              <p>备份文件已生成：<code>{html.escape(backup_path)}</code></p>
              <p>已同步更新最新备份：<code>{html.escape(os.path.join(self.backup_dir, 'latest.db'))}</code></p>
              <p><a href="{self._link('/admin')}">返回管理面板</a> | <a href="{self._link('/')}">返回总览</a></p>
            </div>
            """
            return web.Response(
                text=html_shell("备份结果", "/admin", content, self._token_query()),
                content_type="text/html",
            )
        except Exception as e:
            logger.warning(f"[AdminAction] 立即备份失败: {e}")
            if "application/json" in request.headers.get("Accept", ""):
                return web.json_response({"status": "error", "error": str(e)}, status=500)
            content = f"""
            <div class="card">
              <h3 style="color:var(--cinnabar);">备份失败</h3>
              <p>异常信息：{html.escape(str(e))}</p>
              <p><a href="{self._link('/admin')}">返回管理面板</a></p>
            </div>
            """
            return web.Response(
                text=html_shell("备份失败", "/admin", content, self._token_query()),
                content_type="text/html",
                status=500,
            )

    async def handle_admin_restart(self, request: web.Request) -> web.Response:
        if not await self._write_authorized(request):
            return self._unauthorized_response(request)
        logger.warning("[AdminAction] 收到重启服务请求，5 秒后退出进程由 systemd 自动拉起")

        async def _delayed_restart() -> None:
            await asyncio.sleep(5)
            logger.warning("[AdminAction] 硬退出，跳过优雅停机")
            os._exit(0)

        asyncio.create_task(_delayed_restart())

        if "application/json" in request.headers.get("Accept", ""):
            return web.json_response({"status": "ok", "message": "已收到，5 秒后重启"})

        content = f"""
        <div class="card">
          <h3 style="color:var(--gold);">已收到重启请求</h3>
          <p>机器人进程将在 <strong>5 秒后</strong> 退出，并由 systemd 守护进程 (Restart=always) 自动拉起。</p>
          <p>请等待约 10 秒后刷新总览页面：<a href="{self._link('/')}">返回总览</a></p>
        </div>
        """
        return web.Response(
            text=html_shell("重启中", "/admin", content, self._token_query()),
            content_type="text/html",
        )

    async def handle_admin_reset(self, request: web.Request) -> web.Response:
        if not await self._write_authorized(request):
            return self._unauthorized_response(request)
        confirm_val = ""
        if request.content_type == "application/json":
            try:
                body = await request.json()
                confirm_val = str(body.get("confirm", "")).strip()
            except Exception:
                pass
        else:
            try:
                data = await request.post()
                confirm_val = str(data.get("confirm", "")).strip()
            except Exception:
                pass

        if confirm_val != "YES":
            logger.warning(f"[AdminAction] 重置数据请求拒绝: 缺少 confirm=YES (实际为 '{confirm_val}')")
            if "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json":
                return web.json_response({"status": "error", "error": "缺少 confirm=YES，拒绝重置"}, status=400)
            content = f"""
            <div class="card">
              <h3 style="color:var(--cinnabar);">重置已拒绝</h3>
              <p>必须准确输入大写 <code>YES</code> 方可重置数据。数据库未被更改。</p>
              <p><a href="{self._link('/admin')}">返回管理面板</a></p>
            </div>
            """
            return web.Response(
                text=html_shell("重置被拒绝", "/admin", content, self._token_query()),
                content_type="text/html",
                status=400,
            )

        try:
            res = reset_database(
                db_path=self.db_path,
                backup_dir=self.backup_dir,
                purge_all=False,
            )
            logger.warning(f"[AdminAction] 数据重置成功: 备份至 {res.get('backup_path')}, 清理统计: {res.get('cleared_counts')}")
            if "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json":
                return web.json_response({"status": "ok", "result": res})
            counts_html = "".join([f"<li>{k}: 清空 {v} 行</li>" for k, v in res.get("cleared_counts", {}).items()])
            content = f"""
            <div class="card">
              <h3 style="color:var(--celadon);">重置完成</h3>
              <p>已先在 <code>{html.escape(str(res.get('backup_path')))}</code> 完成全量备份。</p>
              <p>各表清除详情：</p>
              <ul>{counts_html}</ul>
              <p>表情包资产 (stickers) 与计费明细 (llm_calls) 已按策略完整保留。</p>
              <p><a href="{self._link('/')}">查看总览</a> | <a href="{self._link('/admin')}">返回管理面板</a></p>
            </div>
            """
            return web.Response(
                text=html_shell("重置完成", "/admin", content, self._token_query()),
                content_type="text/html",
            )
        except Exception as e:
            logger.warning(f"[AdminAction] 数据重置执行异常: {e}")
            if "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json":
                return web.json_response({"status": "error", "error": str(e)}, status=500)
            content = f"""
            <div class="card">
              <h3 style="color:var(--cinnabar);">重置执行失败</h3>
              <p>异常信息：{html.escape(str(e))}</p>
              <p><a href="{self._link('/admin')}">返回管理面板</a></p>
            </div>
            """
            return web.Response(
                text=html_shell("重置失败", "/admin", content, self._token_query()),
                content_type="text/html",
                status=500,
            )
