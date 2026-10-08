"""看板前端渲染与样式组件 (companion/admin_render.py)
从 admin.py 拆分出的手账纸主题 HTML 样式、SVG 图表与页面外壳渲染函数。
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Dict, Tuple

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
    grid-template-columns: repeat(auto-fit, minmax(min(300px, 100%), 1fr));
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

  /* 手机端适配：全部收在 768px 以下，桌面端（>768px）一个字节都不变 */
  @media (max-width: 768px) {
    .container { padding: 14px; }
    .nav-inner {
      height: auto;
      padding: 8px 14px;
    }
    .nav-links {
      overflow-x: auto;
      flex-wrap: nowrap;
      -webkit-overflow-scrolling: touch;
    }
    /* 触控目标：链接盒子高度 = 14px 字 × 1.6 行高 + 上下各 10px ≈ 42px（≥40px） */
    .nav-links a {
      padding: 10px 2px;
      white-space: nowrap;
    }
    .nav-right { display: none; }
    .card-hero { padding: 18px; }
    .card { padding: 16px; }
    .table { font-size: 12px; }
    .table th, .table td { padding: 6px 8px 6px 0; }
    .table-scroll { overflow-x: auto; }
    .log-window { max-height: 55vh; }
    h2 { font-size: 18px; }
    svg { max-width: 100%; height: auto; }

    /* 手指按不准：管理页三个操作按钮抬到 44px 高（iOS/安卓的推荐触控尺寸） */
    .btn-action { min-height: 44px; }

    /* 总览页手机端信息重排：手机要的"一眼答案"是阶段与复合分，不该先翻过整页。
       顺序 = 首屏一句话 → 阶段台阶 → 好感度（复合分+雷达）→ PAD → 关系档案。

       只有总览页的容器变 flex 列（page-overview 由 html_shell 的 container_class
       参数注入）：这条规则只为重排服务，影响面锁死在总览页——其余 6 页的手机端
       .container 仍是普通块流（可证未变）。实测总览页各区块只带 margin-bottom、
       对向 margin 为 0，块流与 flex 列的相邻间距都是 36px，重排不会顺带改间距。
       桌面完全没有这些规则，仍是普通块流。 */
    .container.page-overview {
      display: flex;
      flex-direction: column;
    }
    .container.page-overview > .sec-hero { order: 1; }
    .container.page-overview > .sec-vitals { order: 2; }
    .container.page-overview > .sec-pad { order: 3; }
    .container.page-overview > .sec-profile { order: 4; }
    /* .sec-vitals 在手机上塌成单列（见 .grid-2 规则），用 order 把阶段台阶顶到好感度前面 */
    .sec-vitals > .sec-stage { order: -1; }

    /* 关键数字放大。内联样式定死了桌面的字号/字重，media 块要盖过内联只能加 !important；
       这些规则的作用域只有 ≤768px，桌面计算值一个都没变。 */
    .composite-score { font-size: 30px !important; }
    .stage-name { font-size: 17px !important; }
    .stage-progress { font-weight: 600 !important; }
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


def render_nav(current_path: str, token_query: str = "") -> str:
    """导航条。token_query 是形如 "?token=xxx" 的查询串（token 为空时是空串）：

    页面内链接必须把 token 带上，否则在"token 非空"的模式下点一下导航就掉线
    （每个链接都会 403）。空串时输出与旧版逐字节一致。
    """
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
        items.append(f'<a href="{path}{token_query}" {cls}>{title}</a>')
    return f"""<div class="nav-bar">
      <div class="nav-inner">
        <div class="nav-links">{"".join(items)}</div>
        <div class="nav-right">
          <span class="nav-refresh font-num">30s 自动刷新</span>
        </div>
      </div>
    </div>"""


def html_shell(
    title: str,
    current_path: str,
    body_content: str,
    token_query: str = "",
    container_class: str = "",
) -> str:
    """页面外壳。token_query 只在"token 非空"模式下非空（页面内链接要带 token）；
    默认空串 = 输出与旧版逐字节一致。

    `<head>` 里除了 charset/自动刷新，还有两样：
    - viewport meta：手机端按设备宽度排版（缺了它 390px 屏会按 980px 缩放，整页缩小成一条）；
    - favicon / apple-touch-icon 两行 link：走 /favicon.png 与 /apple-touch-icon.png
      两个路由（admin.py），手机"添加到主屏幕"才有图标；token 非空时同样要带 token，
      否则图标请求会被鉴权拦掉（规则与 render_nav 一致）。

    container_class 是可选语义钩子（默认空串 = `<div class="container">`，与旧版逐字节一致）：
    目前只有总览页传 "page-overview"，供手机端把该页的 .container 变成 flex 列做信息重排。
    """
    container_attr = "container" + (f" {container_class}" if container_class else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta http-equiv="refresh" content="30">
  <link rel="icon" type="image/png" href="/favicon.png{token_query}">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png{token_query}">
  <title>{title} · QQ 伴侣机器人</title>
  {HTML_STYLE}
</head>
<body>
  {render_nav(current_path, token_query)}
  <div class="{container_attr}">
    {body_content}
  </div>
</body>
</html>"""

