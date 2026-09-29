"""状态仪表盘 HTTP 服务 (admin.py)
基于 aiohttp 实现手账纸主题仪表盘 (绑定 127.0.0.1:8080)，30 秒自动刷新。
路由包含：总览 (/)、记忆 (/memory)、调试 (/debug)、计费 (/costs)、表情包 (/stickers)、日志 (/logs)、管理 (/admin)。
"""

from __future__ import annotations

import html
import json
import logging
import math
import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from aiohttp import web

from companion.affection import AffectionEngine, calc_composite_score, STAGE_THRESHOLDS
from companion.assembler import PromptAssembler
from companion.config import AdminConfig
from companion.db import Database, now_str
from companion.memory import POSITIVE_SENTIMENTS, NEGATIVE_SENTIMENTS, MemoryManager
from companion.mood import MoodEngine
from companion.observer import recent_observer_logs
from companion.persona import Persona
from companion.prompts import get_mood_description, get_mood_label, get_trust_description
from companion.proactive import ProactiveScheduler
from companion.backup import get_last_backup_time, run_daily_backup
from companion.reset import reset_database
from companion.stickers import StickerManager

logger = logging.getLogger(__name__)

HTML_STYLE = """
<style>
  :root {
    --bg:        #f6f1e7;   /* 宣纸底 */
    --card:      #fbf8f1;   /* 纸页卡片 */
    --border:    #e0d8c8;   /* 纸页裁切线 */
    --ink:       #2b2b26;   /* 正文墨色 */
    --ink-soft:  #6b675c;   /* 次级墨 */
    --ink-faint: #9a9484;   /* 弱化墨 */
    --celadon:   #4f8a6d;   /* 青瓷绿（数据、在线状态、强调） */
    --celadon-bg:#e3eee7;   /* 青瓷浅底（进度条、色笺） */
    --cinnabar:  #c04851;   /* 朱砂（警示、私密内容、离线状态） */
    --gold:      #b8862f;   /* 暖金（温暖类情感、里程碑） */
  }

  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 0;
    background-color: var(--bg);
    background-image: radial-gradient(circle, #e5dcc9 1px, transparent 1.2px);
    background-size: 26px 26px;
    color: var(--ink);
    font-family: "Microsoft YaHei UI", "PingFang SC", sans-serif;
    font-size: 14px;
    line-height: 1.6;
  }
  a {
    color: var(--celadon);
    text-decoration: none;
    transition: color 0.2s ease;
  }
  a:hover {
    text-decoration: underline;
  }
  .container {
    max-width: 1080px;
    margin: 0 auto;
    padding: 24px;
  }

  /* 仅允许三种容器：首屏卡、/admin 操作卡、pre 窗 */
  .card {
    background: transparent;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 20px 24px;
    margin-bottom: 20px;
    box-shadow: none;
  }

  /* 容器 1: 首屏卡 (入场动画 + 顶部和纸胶带) */
  @keyframes hero-enter {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
  .card-hero {
    position: relative;
    background: var(--card);
    border: 1px solid var(--border);
    border-left: 3px solid var(--celadon);
    border-radius: 4px;
    padding: 24px 28px;
    margin-bottom: 36px;
    animation: hero-enter 0.5s ease-out forwards;
  }
  .card-hero::before {
    content: "";
    position: absolute;
    width: 88px;
    height: 20px;
    top: -10px;
    left: 50%;
    transform: translateX(-50%) rotate(-2deg);
    background: rgba(79, 138, 109, 0.28);
    clip-path: polygon(0% 0%, 100% 0%, 97% 50%, 100% 100%, 0% 100%, 3% 50%);
    pointer-events: none;
    z-index: 2;
  }

  /* 区块标题与弹性横线（去盒子化核心） */
  .section-block {
    margin-bottom: 36px;
  }
  .section-title {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 2px;
    color: var(--ink-soft);
    margin: 0 0 12px 0;
  }
  .section-title-line {
    flex: 1;
    height: 1px;
    background: var(--border);
  }

  /* 状态灯呼吸动画 */
  @keyframes pulse-dot {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
  .pulse-dot {
    display: inline-block;
    animation: pulse-dot 2.6s infinite ease-in-out;
  }

  /* 雷达图动画 */
  @keyframes radar-draw {
    from { stroke-dashoffset: 100; }
    to { stroke-dashoffset: 0; }
  }
  @keyframes radar-fill {
    from { fill-opacity: 0; }
    to { fill-opacity: 0.25; }
  }
  .radar-polygon {
    stroke-dasharray: 100;
    stroke-dashoffset: 0;
    animation: radar-draw 0.8s ease-out forwards, radar-fill 0.6s ease-out 0.5s backwards;
  }

  /* 日记便签与胶带 */
  .diary-item {
    position: relative;
    margin-bottom: 24px;
    padding: 10px 14px;
  }
  .diary-tape {
    position: absolute;
    top: -6px;
    left: 16px;
    width: 48px;
    height: 12px;
    clip-path: polygon(0% 0%, 100% 0%, 96% 50%, 100% 100%, 0% 100%, 4% 50%);
    pointer-events: none;
    z-index: 1;
  }
  .diary-item:nth-child(odd) .diary-tape {
    transform: rotate(-1.5deg);
    background: rgba(79, 138, 109, 0.28);
  }
  .diary-item:nth-child(even) .diary-tape {
    transform: rotate(1.2deg);
    background: rgba(192, 72, 81, 0.25);
  }

  /* 欲言又止胶带 */
  .suppressed-tape {
    position: absolute;
    top: -6px;
    left: 16px;
    width: 48px;
    height: 12px;
    transform: rotate(-1.5deg);
    background: rgba(192, 72, 81, 0.28);
    clip-path: polygon(0% 0%, 100% 0%, 96% 50%, 100% 100%, 0% 100%, 4% 50%);
    pointer-events: none;
    z-index: 1;
  }

  /* 排版与栅格 */
  h2 {
    font-size: 20px;
    font-weight: 600;
    color: var(--ink);
    margin: 0 0 16px 0;
  }
  .grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
    gap: 24px;
  }
  .grid-2 {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 24px;
  }
  @media (max-width: 768px) {
    .grid-2 { grid-template-columns: 1fr; }
  }
  .font-sentiment {
    font-family: "STKaiti", "KaiTi", "SimSun", serif;
    font-style: italic;
  }
  .font-num {
    font-family: "Consolas", "Courier New", monospace;
  }
  /* 表格 = 横线记账本：只留横向行线，无竖线无外框无表头底色 */
  .table {
    width: 100%;
    border-collapse: collapse;
    margin-top: 10px;
    font-size: 13px;
  }
  .table th, .table td {
    border: none;
    border-bottom: 1px solid var(--border);
    padding: 8px 12px 8px 0;
    text-align: left;
  }
  .table th {
    color: var(--ink-soft);
    font-weight: 600;
    font-size: 12px;
    letter-spacing: 1px;
    border-bottom: 1px solid var(--ink-faint);
  }
  .table tr:last-child td {
    border-bottom: none;
  }

  /* 导航条 */
  .nav-bar {
    width: 100%;
    border-bottom: 1px solid var(--border);
    background: var(--card);
  }
  .nav-inner {
    max-width: 1080px;
    margin: 0 auto;
    padding: 0 24px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    height: 52px;
  }
  .nav-links {
    display: flex;
    align-items: center;
    gap: 20px;
  }
  .nav-links a {
    font-size: 14px;
    color: var(--ink-soft);
    text-decoration: none;
    padding: 14px 2px;
    border-bottom: 2px solid transparent;
    transition: color 0.2s ease, border-color 0.2s ease;
  }
  .nav-links a:hover {
    color: var(--ink);
  }
  .nav-links a.active {
    color: var(--ink);
    font-weight: 600;
    border-bottom: 2px solid var(--celadon);
  }
  .nav-right {
    display: flex;
    align-items: center;
  }
  .nav-refresh {
    color: var(--ink-faint);
    font-size: 12px;
  }

  /* 容器 3: pre 窗（纸面小票感：深一号纸色底 + 墨色字） */
  pre {
    background: #efe8d6;
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 16px;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    line-height: 1.7;
    color: var(--ink);
    white-space: pre-wrap;
    word-break: break-all;
  }
  .log-window {
    background: #efe8d6;
    border: 1px solid var(--border);
    border-radius: 4px;
    color: #57534a;
    font-family: "Consolas", "Courier New", monospace;
    font-size: 12px;
    line-height: 1.6;
    padding: 16px;
    max-height: 700px;
    overflow-y: auto;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .sticker-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(130px, 1fr));
    gap: 16px;
    margin-top: 10px;
  }
  .sticker-item {
    padding: 8px 4px;
    text-align: center;
  }
  .sticker-img {
    max-width: 100px;
    max-height: 100px;
    object-fit: cover;
    border-radius: 2px;
  }
  .btn-action {
    padding: 8px 18px;
    border: none;
    border-radius: 4px;
    cursor: pointer;
    font-weight: bold;
    font-size: 13px;
    font-family: inherit;
    transition: opacity 0.2s ease, background-color 0.2s ease;
  }
  .btn-action:hover {
    opacity: 0.9;
  }
</style>
"""


def format_chinese_date(dt: datetime) -> Tuple[str, str, str]:
    """生成中文日期、日、星期 (如 '九月', '廿九', '星期二')"""
    month_names = ["", "一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月"]
    day_names = [
        "",
        "初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
        "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
        "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十",
        "三十一"
    ]
    weekday_names = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    m = month_names[dt.month] if 1 <= dt.month <= 12 else f"{dt.month}月"
    d = day_names[dt.day] if 1 <= dt.day <= 31 else f"{dt.day}日"
    w = weekday_names[dt.weekday()]
    return m, d, w


def get_sentiment_color(sentiment: str) -> str:
    """手账情感色调色笺"""
    if sentiment in ["温暖", "幸福", "欢喜"]:
        return "var(--gold)"
    elif sentiment in ["思念", "释然"]:
        return "var(--celadon)"
    elif sentiment in ["不安", "伤感"]:
        return "var(--ink-soft)"
    else:
        return "var(--ink-faint)"


def render_section_header(title: str) -> str:
    """手账段落区块标题规范：13px / 600 / letter-spacing 2px + 弹性细横线"""
    return (
        f'<div class="section-title">'
        f'<span>{title}</span>'
        f'<div class="section-title-line"></div>'
        f'</div>'
    )


def render_radar_svg(dims: Dict[str, float]) -> str:
    """生成手账风六维雷达图 SVG，尺寸 300x260"""
    width, height = 300, 260
    cx, cy = 150, 130
    r = 75.0
    dim_keys = ["warmth", "trust", "intimacy", "intrigue", "patience", "tension"]
    labels = ["温暖", "信任", "亲密", "好奇", "包容", "紧张"]

    angles = [-math.pi / 2 + i * (2 * math.pi / 6) for i in range(6)]

    # 网格线多边形 (20%, 40%, 60%, 80%, 100%)
    grid_polygons = []
    for level in [0.2, 0.4, 0.6, 0.8, 1.0]:
        pts = [
            f"{cx + r * level * math.cos(a):.1f},{cy + r * level * math.sin(a):.1f}"
            for a in angles
        ]
        grid_polygons.append(
            f'<polygon points="{" ".join(pts)}" fill="none" stroke="var(--ink-faint)" stroke-width="0.8" opacity="0.6"/>'
        )

    # 轴线与端点标注
    axis_lines = []
    text_labels = []
    for i, a in enumerate(angles):
        x = cx + r * math.cos(a)
        y = cy + r * math.sin(a)
        axis_lines.append(
            f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" stroke="var(--ink-faint)" stroke-width="0.8" opacity="0.6"/>'
        )

        tx = cx + (r + 18) * math.cos(a)
        ty = cy + (r + 12) * math.sin(a) + 4
        anchor = "middle"
        if math.cos(a) > 0.3:
            anchor = "start"
        elif math.cos(a) < -0.3:
            anchor = "end"
        val = float(dims.get(dim_keys[i], 0.0))
        text_labels.append(
            f'<text x="{tx:.1f}" y="{ty:.1f}" text-anchor="{anchor}">'
            f'<tspan fill="var(--ink-soft)" font-size="12">{labels[i]}</tspan>'
            f'<tspan fill="var(--ink)" font-family="Consolas, monospace" font-size="13"> {val:.1f}</tspan>'
            f'</text>'
        )

    # 数据多边形与顶点圆点
    data_pts = []
    data_circles = []
    for i, a in enumerate(angles):
        val = float(dims.get(dim_keys[i], 0.0))
        ratio = min(1.0, max(0.0, val / 100.0))
        dx = cx + r * ratio * math.cos(a)
        dy = cy + r * ratio * math.sin(a)
        data_pts.append(f"{dx:.1f},{dy:.1f}")
        data_circles.append(f'<circle cx="{dx:.1f}" cy="{dy:.1f}" r="3" fill="var(--celadon)" />')

    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="display:block; margin: 6px auto;">
      {"".join(grid_polygons)}
      {"".join(axis_lines)}
      <polygon class="radar-polygon" points="{" ".join(data_pts)}" fill="var(--celadon)" fill-opacity="0.25" stroke="var(--celadon)" stroke-width="2" pathLength="100" />
      {"".join(data_circles)}
      {"".join(text_labels)}
    </svg>"""


def render_nav(current_path: str) -> str:
    links = [
        ("/", "总览"),
        ("/memory", "记忆"),
        ("/debug", "调试"),
        ("/costs", "计费"),
        ("/stickers", "表情包"),
        ("/logs", "日志"),
        ("/admin", "管理"),
    ]
    items = []
    for path, title in links:
        cls = 'class="active"' if current_path == path else ''
        items.append(f'<a href="{path}" {cls}>{title}</a>')
    return f"""<div class="nav-bar">
      <div class="nav-inner">
        <div class="nav-links">{"".join(items)}</div>
        <div class="nav-right">
          <span class="nav-refresh font-num">30s 自动刷新</span>
        </div>
      </div>
    </div>"""


def html_shell(title: str, current_path: str, body_content: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta http-equiv="refresh" content="30">
  <title>{title} · QQ 伴侣机器人</title>
  {HTML_STYLE}
</head>
<body>
  {render_nav(current_path)}
  <div class="container">
    {body_content}
  </div>
</body>
</html>"""


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

    async def start(self) -> None:
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
    # 1. / 总览 (Overview)
    # ==========================================
    async def handle_overview(self, request: web.Request) -> web.Response:
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
        today_str = datetime.now().strftime("%Y-%m-%d 00:00")
        row_today = await self.db.fetchone(
            "SELECT SUM(cost_estimate) as cost_today FROM llm_calls WHERE created_at >= ?",
            (today_str,),
        )
        cost_today = float(row_today["cost_today"] or 0.0) if row_today else 0.0

        # ------------------------------------------
        # 2.1 首屏"她此刻"
        # ------------------------------------------
        now_dt = datetime.now()
        month_cn, day_cn, weekday_cn = format_chinese_date(now_dt)
        date_str = f"{month_cn}{day_cn}，{weekday_cn}。"

        activity = self.persona.get_current_activity(now_dt.hour, now_dt.weekday())
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
            try:
                l_str = str(last_user_turn["created_at"]).strip()
                if len(l_str) >= 19:
                    l_dt = datetime.strptime(l_str[:19], "%Y-%m-%d %H:%M:%S")
                elif len(l_str) >= 16:
                    l_dt = datetime.strptime(l_str[:16], "%Y-%m-%d %H:%M")
                else:
                    l_dt = None
                if l_dt:
                    hours_diff = (now_dt - l_dt).total_seconds() / 3600.0
                    if hours_diff >= 1.0:
                        last_hours_phrase = f" 他已有 {int(hours_diff)} 小时没说话了。"
            except Exception:
                pass

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

        turns_cnt_row = await self.db.fetchone("SELECT value FROM counters WHERE key = 'total_turns'")
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
        return web.Response(text=html_shell("总览", "/", content), content_type="text/html")

    # ==========================================
    # 2. /memory 记忆 (手账时间轴)
    # ==========================================
    async def handle_memory(self, request: web.Request) -> web.Response:
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
            try:
                l_dt = datetime.strptime(l_str, "%Y-%m-%d %H:%M")
                days = max(0.0, (now_dt - l_dt).total_seconds() / 86400.0)
            except Exception:
                days = 0.0

            tau_base = max(20.0, imp * 20.0)
            tau_eff = tau_base * (1.0 + 0.15 * rc)
            if st in POSITIVE_SENTIMENTS:
                tau_eff *= 2.0
            elif st in NEGATIVE_SENTIMENTS:
                tau_eff *= 1.5

            strength = imp * (1.0 + 0.3 * math.log2(rc + 1)) * math.exp(-days / tau_eff)
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
        return web.Response(text=html_shell("记忆系统", "/memory", content), content_type="text/html")

    # ==========================================
    # 3. /debug 调试
    # ==========================================
    async def handle_debug(self, request: web.Request) -> web.Response:
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
        past_7d = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M")
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
        return web.Response(text=html_shell("调试信息", "/debug", content), content_type="text/html")

    # ==========================================
    # 4. /costs 计费
    # ==========================================
    async def handle_costs(self, request: web.Request) -> web.Response:
        total_row = await self.db.fetchone(
            "SELECT COUNT(*) as calls, SUM(cost_estimate) as total_cost, SUM(prompt_tokens) as p_tokens, SUM(completion_tokens) as c_tokens FROM llm_calls"
        )
        total_calls = total_row["calls"] if total_row else 0
        total_cost = float(total_row["total_cost"] or 0.0) if total_row else 0.0

        past_24h_str = (datetime.now() - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M")
        row_24h = await self.db.fetchone(
            "SELECT SUM(cost_estimate) as cost_24h FROM llm_calls WHERE created_at >= ?",
            (past_24h_str,),
        )
        cost_24h = float(row_24h["cost_24h"] or 0.0) if row_24h else 0.0

        today_str = datetime.now().strftime("%Y-%m-%d 00:00")
        row_today = await self.db.fetchone(
            "SELECT SUM(cost_estimate) as cost_today FROM llm_calls WHERE created_at >= ?",
            (today_str,),
        )
        cost_today = float(row_today["cost_today"] or 0.0) if row_today else 0.0

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
        return web.Response(text=html_shell("计费统计", "/costs", content), content_type="text/html")

    # ==========================================
    # 5. /stickers 表情包
    # ==========================================
    async def handle_stickers(self, request: web.Request) -> web.Response:
        self.stickers.load_index()
        items = []
        for name, data in self.stickers._index.items():
            desc = data.get("desc", "")
            items.append(
                f"""<div class="sticker-item">
                  <img src="/stickers/img/{html.escape(name)}" class="sticker-img" alt="{html.escape(name)}">
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
        return web.Response(text=html_shell("表情包库", "/stickers", content), content_type="text/html")

    async def handle_sticker_image(self, request: web.Request) -> web.Response:
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
        return web.Response(text=html_shell("运行日志", "/logs", content), content_type="text/html")

    # ==========================================
    # 7. /api/status 状态 API (JSON)
    # ==========================================
    async def handle_api_status(self, request: web.Request) -> web.Response:
        aff_state = await self.affection.get_state()
        composite = float(aff_state.get("composite", 30.0))
        stage_idx = int(aff_state.get("stage", 0))
        stage_obj = self.persona.get_stage(stage_idx)

        today_str = datetime.now().strftime("%Y-%m-%d 00:00")
        row_today = await self.db.fetchone(
            "SELECT SUM(cost_estimate) as cost_today FROM llm_calls WHERE created_at >= ?",
            (today_str,),
        )
        cost_today = float(row_today["cost_today"] or 0.0) if row_today else 0.0

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
        last_backup = get_last_backup_time(self.backup_dir) or "暂无备份"
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
            <form action="/admin/backup" method="post" onsubmit="return confirm('确定立即备份数据库吗？');">
              <button type="submit" class="btn-action" style="background: var(--celadon); color: #fff;">立即备份</button>
            </form>
          </div>

          <div class="card">
            <h3>2. 重启伴侣机器人</h3>
            <p style="font-size:13px; color:var(--ink-soft);">用于代码更新或修改配置后重新加载。发出重启指令后进程将在 5 秒后退出，由 systemd 自动拉起。</p>
            <p style="font-size:13px; color:var(--ink-faint); margin: 12px 0;">重启期间静默重启，不打扰机主。</p>
            <form action="/admin/restart" method="post" onsubmit="return confirm('确定重启机器人服务吗？服务将在 5 秒后退出并由 systemd 自动重新拉起。');">
              <button type="submit" class="btn-action" style="background: var(--gold); color: #fff;">重启服务</button>
            </form>
          </div>

          <div class="card" style="border: 1px solid var(--cinnabar);">
            <h3 style="color:var(--cinnabar);">3. 重置关系数据 (高危)</h3>
            <p style="font-size:13px; color:var(--ink-soft);">清空好感度、心情 PAD、对话历史、语义事实与日记。执行前会自动创建快照备份，表情包与计费明细完整保留。</p>
            <form action="/admin/reset" method="post" onsubmit="return confirm('警告：此操作将清空所有好感与记忆！确定要重置吗？');">
              <div style="margin: 12px 0;">
                <label style="font-size:13px; color:var(--cinnabar); display:block; margin-bottom:6px;">输入大写 <code>YES</code> 确认执行：</label>
                <input type="text" name="confirm" placeholder="YES" required style="padding: 6px 10px; background: var(--bg); border: 1px solid var(--cinnabar); color: var(--ink); border-radius: 4px; width: 120px; font-family: monospace;">
              </div>
              <button type="submit" class="btn-action" style="background: var(--cinnabar); color: #fff;">确认重置数据</button>
            </form>
          </div>
        </div>
        """
        return web.Response(text=html_shell("管理控制台", "/admin", content), content_type="text/html")

    async def handle_admin_backup(self, request: web.Request) -> web.Response:
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
              <p><a href="/admin">返回管理面板</a> | <a href="/">返回总览</a></p>
            </div>
            """
            return web.Response(text=html_shell("备份结果", "/admin", content), content_type="text/html")
        except Exception as e:
            logger.warning(f"[AdminAction] 立即备份失败: {e}")
            if "application/json" in request.headers.get("Accept", ""):
                return web.json_response({"status": "error", "error": str(e)}, status=500)
            content = f"""
            <div class="card">
              <h3 style="color:var(--cinnabar);">备份失败</h3>
              <p>异常信息：{html.escape(str(e))}</p>
              <p><a href="/admin">返回管理面板</a></p>
            </div>
            """
            return web.Response(text=html_shell("备份失败", "/admin", content), content_type="text/html", status=500)

    async def handle_admin_restart(self, request: web.Request) -> web.Response:
        logger.warning("[AdminAction] 收到重启服务请求，5 秒后退出进程由 systemd 自动拉起")

        async def _delayed_restart() -> None:
            await asyncio.sleep(5)
            os._exit(0)

        asyncio.create_task(_delayed_restart())

        if "application/json" in request.headers.get("Accept", ""):
            return web.json_response({"status": "ok", "message": "已收到，5 秒后重启"})

        content = """
        <div class="card">
          <h3 style="color:var(--gold);">已收到重启请求</h3>
          <p>机器人进程将在 <strong>5 秒后</strong> 退出，并由 systemd 守护进程 (Restart=always) 自动拉起。</p>
          <p>请等待约 10 秒后刷新总览页面：<a href="/">返回总览</a></p>
        </div>
        """
        return web.Response(text=html_shell("重启中", "/admin", content), content_type="text/html")

    async def handle_admin_reset(self, request: web.Request) -> web.Response:
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
            content = """
            <div class="card">
              <h3 style="color:var(--cinnabar);">重置已拒绝</h3>
              <p>必须准确输入大写 <code>YES</code> 方可重置数据。数据库未被更改。</p>
              <p><a href="/admin">返回管理面板</a></p>
            </div>
            """
            return web.Response(text=html_shell("重置被拒绝", "/admin", content), content_type="text/html", status=400)

        try:
            res = reset_database(db_path=self.db_path, purge_all=False)
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
              <p><a href="/">查看总览</a> | <a href="/admin">返回管理面板</a></p>
            </div>
            """
            return web.Response(text=html_shell("重置完成", "/admin", content), content_type="text/html")
        except Exception as e:
            logger.warning(f"[AdminAction] 数据重置执行异常: {e}")
            if "application/json" in request.headers.get("Accept", "") or request.content_type == "application/json":
                return web.json_response({"status": "error", "error": str(e)}, status=500)
            content = f"""
            <div class="card">
              <h3 style="color:var(--cinnabar);">重置执行失败</h3>
              <p>异常信息：{html.escape(str(e))}</p>
              <p><a href="/admin">返回管理面板</a></p>
            </div>
            """
            return web.Response(text=html_shell("重置失败", "/admin", content), content_type="text/html", status=500)
