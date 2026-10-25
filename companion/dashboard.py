"""新版看板 (companion/dashboard.py)：可选主题 + 角色卡皮肤。

[admin].theme：
  journal  —— 原来的手账纸看板（默认，admin_render.py，一个字没动）
  plain    —— 通用：白底黑字、左侧导航栏，适合任何角色
  endfield —— 终末地工业风：plain 的骨架 + 信号黄、编号、斜纹、刻度、镂空大字

皮肤（可选）：角色卡目录下 theme/skin.json，叠在 plain / endfield 上。
角色可以使用自己的配色、头像和鼠标光标；皮肤里的图片只从 theme/ 目录读，
素材留在卡里，不进公开仓库；没有皮肤时一切照常。

数据：总览与记忆两页在这里取数并渲染；调试/费用/表情包/日志/管理五页在
dashboard_pages.py 里取数并渲染，共用这里的 CSS 与外壳。journal 主题完全不经过本模块。
"""

from __future__ import annotations

import html
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from companion.datefmt import relative_day
from companion.db import COUNTER_KEY_TOTAL_TURNS, parse_dt
from companion.memory import calc_diary_strength
from companion.prompts import get_mood_description, get_mood_label

logger = logging.getLogger(__name__)

THEMES = ("journal", "plain", "endfield")
DIM_ORDER = [
    ("warmth", "温暖"), ("trust", "信任"), ("intimacy", "亲密"),
    ("intrigue", "好奇"), ("patience", "包容"), ("tension", "紧张"),
]
SKIN_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
DASHBOARD_KAI_FONT_FILE = "lxgw-wenkai-dashboard.woff2"
# 装饰字体子集（OFL），与完整霞鹜文楷一样免鉴权
DISPLAY_FONTS = {
    DASHBOARD_KAI_FONT_FILE: "LXGW WenKai Dashboard",
    "archivo-wide.woff2": "Archivo Wide",
    "outfit.woff2": "Outfit",
    "space-grotesk.woff2": "Space Grotesk",
}

NAV = [
    ("/", "总览", '<rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/>'),
    ("/memory", "记忆", '<path d="M5 3h11l3 3v15H5z"/><path d="M9 9h6M9 13h6M9 17h3"/>'),
    ("/debug", "调试", '<path d="M8 6l-6 6 6 6M16 6l6 6-6 6"/>'),
    ("/costs", "费用", '<path d="M4 19V9M10 19V5M16 19v-7M22 19H2"/>'),
    ("/stickers", "表情包", '<circle cx="12" cy="12" r="8"/><path d="M9 10h.01M15 10h.01M9 15c1.5 1.5 4.5 1.5 6 0"/>'),
    ("/logs", "日志", '<path d="M5 4h14M5 9h14M5 14h10M5 19h7"/>'),
    ("/admin", "管理", '<circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M5 5l2 2M17 17l2 2M5 19l2-2M17 7l2-2"/>'),
]

BUTTERFLY = (
    '<path d="M20 15C14 2 2 2 4 10c1 6 10 6 16 5zM20 15c6-13 18-13 16-5-1 6-10 6-16 5z'
    'M20 15c-5 3-12 11-8 12s7-7 8-12zM20 15c5 3 12 11 8 12s-7-7-8-12z"/>'
)


# ---------------------------------------------------------------- 皮肤


@dataclass
class Skin:
    """角色卡 theme/skin.json。所有图片路径相对 theme/ 目录。"""

    base_dir: str
    ink: bool = False
    subtitle: str = ""
    char_color: str = "#CDEBDC"
    avatar: Dict[str, str] = field(default_factory=dict)   # 心情标签 / default / asleep → 图片
    cursor: Dict[str, str] = field(default_factory=dict)   # normal / link / text → 图片
    art: Dict[str, str] = field(default_factory=dict)      # hero / memory / unsaid / online / busy / offline
    terms: Dict[str, str] = field(default_factory=dict)    # online / last_chat 等换个说法

    def has(self, rel: str) -> bool:
        return bool(rel) and skin_file(self, rel) is not None

    def avatar_for(self, mood_label: str, asleep: bool = False) -> str:
        for key in (["asleep"] if asleep else []) + [mood_label, "default"]:
            if self.has(self.avatar.get(key, "")):
                return self.avatar[key]
        return ""

    def term(self, key: str, default: str) -> str:
        val = self.terms.get(key)
        return val if isinstance(val, str) and val.strip() else default


def _str_map(obj: Any) -> Dict[str, str]:
    if not isinstance(obj, dict):
        return {}
    return {str(k): v for k, v in obj.items() if isinstance(v, str)}


def load_skin(char_dir: str) -> Optional[Skin]:
    """读卡目录下 theme/skin.json；没有或坏了就返回 None（看板照常，只是没皮肤）。"""
    base = os.path.join(char_dir, "theme")
    path = os.path.join(base, "skin.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.warning(f"[Dashboard] 皮肤 {path} 读取失败，按无皮肤处理: {e}")
        return None
    if not isinstance(data, dict):
        return None
    color = data.get("char_color")
    return Skin(
        base_dir=base,
        ink=data.get("ink") is True,
        subtitle=str(data.get("subtitle") or ""),
        char_color=color if isinstance(color, str) and color.startswith("#") else "#CDEBDC",
        avatar=_str_map(data.get("avatar")),
        cursor=_str_map(data.get("cursor")),
        art=_str_map(data.get("art")),
        terms=_str_map(data.get("terms")),
    )


def skin_file(skin: Optional[Skin], rel: str) -> Optional[str]:
    """皮肤图片的绝对路径；不在 theme/ 目录里、不是图片、不存在 → None。"""
    if skin is None or not rel or not rel.lower().endswith(SKIN_IMAGE_EXTS):
        return None
    base = os.path.realpath(skin.base_dir)
    full = os.path.realpath(os.path.join(base, rel))
    if not full.startswith(base + os.sep) or not os.path.isfile(full):
        return None
    return full


# ---------------------------------------------------------------- 外壳


def _e(text: Any) -> str:
    return html.escape(str(text if text is not None else ""))


def _skin_url(rel: str, tq: str) -> str:
    from urllib.parse import quote

    return f"/skin/{quote(rel)}{tq}"


CSS = """
@font-face{font-family:"QZKai";src:url("/assets/lxgw-wenkai-dashboard.woff2") format("woff2");font-display:swap}
@font-face{font-family:"QZKai Full";src:url("/assets/lxgw-wenkai-gb2312.woff2") format("woff2");font-display:swap}
@font-face{font-family:"Archivo Wide";src:url("/assets/archivo-wide.woff2") format("woff2");font-display:swap}
@font-face{font-family:"Outfit";src:url("/assets/outfit.woff2") format("woff2");font-weight:300 600;font-display:swap}
@font-face{font-family:"Space Grotesk";src:url("/assets/space-grotesk.woff2") format("woff2");font-weight:500 600;font-display:swap}
:root{
  --bg:#FFFFFF;--surface:#F2F2F2;--surface-2:#FAFAFA;--ink:#191919;--ink-2:#626262;
  --line:#D9D9D9;--line-soft:#E6E6E6;--mark:#191919;--warn:#FC6C00;--money:#25759F;--ok:#007F43;
  --paper:#F4EFE4;--paper-2:#FBF8F1;--paper-ink:#1E1E1E;--paper-soft:#5C5548;--seal:#B8332A;--char:#CDEBDC;
  /* 原手账看板的变量名：旧页面不再用它们，保留仅为兼容外部自定义样式 */
  --card:#FAFAFA;--border:#D9D9D9;--ink-soft:#626262;--ink-faint:#626262;
  --celadon:#191919;--celadon-bg:#E6E6E6;--cinnabar:#C0392B;--gold:#191919;
}
.theme-endfield{--mark:#FFFA00}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Microsoft YaHei UI","PingFang SC","Noto Sans SC","Source Han Sans SC",system-ui,sans-serif;font-size:15px;line-height:1.6}
a{color:var(--ink)}
.en{font-family:"Outfit",system-ui,sans-serif}
.num{font-family:"Space Grotesk","Outfit",system-ui,sans-serif;font-feature-settings:"tnum" 1;
  font-variant-numeric:tabular-nums}
.wide{font-family:"Archivo Wide","Outfit",sans-serif;font-weight:800}
.serif{font-family:"Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif}
.brush{font-family:"QZKai","QZKai Full","STKaiti","KaiTi",serif}
.micro{font-family:"Outfit",system-ui,sans-serif;font-size:12px;letter-spacing:.04em;color:var(--ink-2)}
.app{display:flex;min-height:100vh;background:linear-gradient(90deg,#191919 88px,transparent 88px)}
.rail{flex:0 0 88px;background:#191919;display:flex;flex-direction:column;padding:16px 0;position:sticky;top:0;height:100vh}
.rail .logo{width:52px;height:52px;margin:0 auto 18px;border-radius:50%;background:var(--char);overflow:hidden;display:flex;align-items:flex-end;justify-content:center;color:#191919;font-weight:900;font-size:22px}
.rail .logo.txt{align-items:center;border-radius:0;background:var(--mark)}
.theme-plain .rail .logo.txt{background:#FAFAFA}
.rail .logo img{width:62px;margin-bottom:-6px;display:block}
.rail a{display:flex;flex-direction:column;align-items:center;gap:6px;padding:12px 0;color:#B3B3B3;
  text-decoration:none;font-size:12px;position:relative;transition:color .22s ease,transform .22s ease}
.rail a:hover,.rail a.on{color:#FFFFFF}
.rail a.on::before{content:"";position:absolute;left:0;top:8px;bottom:8px;width:4px;background:var(--mark)}
.theme-plain .rail a.on::before{background:#FAFAFA}
.rail svg{width:22px;height:22px;fill:none;stroke:currentColor;stroke-width:1.6}
.main{flex:1;min-width:0;display:flex;flex-direction:column}
.topbar{display:flex;flex-wrap:wrap;justify-content:space-between;align-items:center;gap:8px 24px;padding:14px 40px;border-bottom:1px solid var(--line)}
.pill{display:inline-flex;align-items:center;gap:6px;min-height:28px;padding:2px 12px;border-radius:999px;font-size:13px;font-weight:500;background:var(--surface)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block}
.sec{padding:48px 40px;display:flex;flex-wrap:wrap;gap:40px 64px;border-top:1px solid var(--line)}
.col{flex:1 1 380px;min-width:0;display:flex;flex-direction:column;gap:16px}
.sh{display:flex;flex-direction:column;gap:2px}
.sh .en-row{display:flex;align-items:center;gap:10px}
.sh .idx,.sh .hatch{display:none}
.theme-endfield .sh .idx{display:inline;font-family:"Space Grotesk",sans-serif;font-size:12px;font-weight:600}
.theme-endfield .sh .hatch{display:inline-block;width:22px;height:8px;background:repeating-linear-gradient(135deg,#8A8A8A 0 1.5px,transparent 1.5px 5px)}
.sh .en{font-weight:300;font-size:13px;letter-spacing:.05em;color:var(--ink-2);text-transform:uppercase}
.sh h2{margin:0;font-size:22px;font-weight:700}
.row{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--line-soft);font-size:15px;line-height:1.6}
.row .micro{white-space:nowrap}
.bar{height:4px;background:var(--line-soft)}.bar>i{display:block;height:100%;background:var(--ink)}
.empty{color:var(--ink-2);font-size:14px}
.hero{display:flex;flex-wrap:wrap;min-height:560px;position:relative;overflow:hidden}
.hero-art{flex:1 1 520px;position:relative;min-height:440px;background:var(--surface);overflow:hidden}
.theme-endfield .hero-art{background-image:radial-gradient(#D9D9D9 1px,transparent 1px);background-size:14px 14px}
.ghost{position:absolute;left:-10px;top:10px;font-size:280px;line-height:1;letter-spacing:-.04em;color:transparent;-webkit-text-stroke:2px #D9D9D9;display:none}
.theme-endfield .ghost{display:block}
.hero-name{position:absolute;left:40px;bottom:32px;display:flex;flex-direction:column;gap:6px}
.hero-name .big{font-size:64px;font-weight:900;line-height:1}
.hero-side{flex:0 1 460px;min-width:0;padding:40px;display:flex;flex-direction:column;gap:36px}
.vf{position:relative}
.theme-endfield .vf{padding:18px;margin:-18px}
.vf>i{display:none}
.theme-endfield .vf>i{display:block;position:absolute;width:12px;height:12px;border-color:var(--ink);border-style:solid}
.av{position:relative;width:96px;height:96px;flex:0 0 auto}
.av .ring{position:absolute;inset:0;display:none}
.theme-endfield .av .ring{display:block}
.av .face{position:absolute;left:12px;top:12px;width:72px;height:72px;border-radius:50%;background:var(--char);overflow:hidden;display:flex;align-items:flex-end;justify-content:center;font-size:30px;font-weight:900}
.av .face.txt{align-items:center;background:var(--surface)}
.av .face img{width:84px;margin-bottom:-8px;display:block}
.ladder{display:grid;grid-template-columns:repeat(10,minmax(0,1fr));gap:3px}
.ladder div{display:flex;flex-direction:column;gap:4px}
.ladder .seg{height:6px;background:var(--line-soft)}.ladder .seg.on{background:var(--ink)}
.ladder .tri{width:10px;height:7px;visibility:hidden}.ladder .tri.on{visibility:visible}
.ladder .n{display:none;font-size:10px;color:var(--ink-2)}
.theme-endfield .ladder .n{display:block}
.inverse{background:#191919;color:#FAFAFA;border-top:0}
.inverse .sh .en,.inverse .micro{color:#B3B3B3}
.inverse .row{border-color:#383838}
.foot{display:flex;flex-wrap:wrap;align-items:center;gap:12px 40px;padding:20px 40px;font-size:14px;border-top:1px solid var(--line)}
.reg{display:none;gap:3px}.reg i{width:14px;height:6px;display:block}
.theme-endfield .reg{display:flex}
.chips{display:flex;flex-wrap:wrap;gap:8px}
.chip{min-height:32px;padding:0 16px;border-radius:999px;border:0;font:inherit;font-size:13px;background:var(--surface);color:var(--ink);cursor:pointer}
.chip.on{background:#191919;color:#FFFFFF}
.search{display:flex;align-items:center;gap:10px;border-bottom:2px solid var(--ink);height:44px;min-width:260px}
.search input{border:0;background:transparent;font:inherit;font-size:15px;outline:none;flex:1;min-width:0}
.diary{display:grid;grid-template-columns:96px minmax(0,1fr);gap:16px;padding:16px 0;border-top:1px solid var(--line)}
.hero-chibi{position:absolute;right:9%;bottom:34px;width:210px}
.seal{display:inline-flex;align-items:center;justify-content:center;background:var(--seal);color:var(--paper);font-family:"QZKai","QZKai Full","STKaiti","KaiTi",serif;border-radius:3px;transform:rotate(-4deg);line-height:1;flex-direction:column}
.ink-only{display:none}
.skin-ink .ink-only{display:block}
.skin-ink .no-ink{display:none}
.skin-ink .hero-art{background:var(--paper)}
.skin-ink .paper{background:var(--paper);color:var(--paper-ink)}
.skin-ink .paper .micro,.skin-ink .paper .row .micro{color:var(--paper-soft)}
.skin-ink .paper .row{border-color:#E2D9C6}
.skin-ink .paper-card{background:var(--paper);color:var(--paper-ink);padding:28px 30px;position:relative}
.skin-ink .lead{font-family:"Noto Serif SC","Source Han Serif SC","Songti SC","SimSun",serif}
pre{white-space:pre-wrap;word-break:break-all}

/* ---- 页面组件层：调试 / 费用 / 表情包 / 日志 / 管理共用 ---- */
:where(a,button,input,summary):focus-visible{outline:2px solid var(--ink);outline-offset:3px}
.theme-endfield :where(a,button,input,summary):focus-visible{outline-color:var(--ink);box-shadow:0 0 0 4px rgba(255,250,0,.6)}
a{transition:color .22s ease,opacity .22s ease}
.ph{display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;gap:18px 40px;
  padding:40px 40px 26px;position:relative;overflow:hidden}
.ph-main{display:flex;flex-direction:column;gap:6px;position:relative;z-index:1;min-width:0}
.ph .kick{display:flex;align-items:center;gap:10px}
.ph .idx{display:none;font-family:"Space Grotesk",sans-serif;font-size:12px;font-weight:600;letter-spacing:.04em}
.theme-endfield .ph .idx{display:inline}
.ph .hatch{display:none}
.theme-endfield .ph .hatch{display:inline-block;width:22px;height:8px;
  background:repeating-linear-gradient(135deg,#8A8A8A 0 1.5px,transparent 1.5px 5px)}
.ph .en{font-family:"Outfit",system-ui,sans-serif;font-weight:300;font-size:12px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--ink-2)}
.ph h1{margin:0;font-size:34px;font-weight:800;letter-spacing:-.01em;line-height:1.2}
.ph .sub{margin:0;font-size:14px;line-height:1.75;color:var(--ink-2);max-width:54ch}
.ph .side{position:relative;z-index:1;display:flex;flex-direction:column;gap:4px;align-items:flex-end;text-align:right}
.ph .side .v{font-family:"Space Grotesk","Outfit",sans-serif;font-size:30px;font-weight:600;
  line-height:1;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.ph-title{display:flex;align-items:flex-start;gap:16px}
.ph-title .seal{margin-top:8px}
.tickrule{display:none;width:84px;height:9px;opacity:.4;
  background:repeating-linear-gradient(90deg,var(--ink) 0 1px,transparent 1px 8px)}
.theme-endfield .tickrule{display:inline-block}
.frame{position:relative}
.frame::before,.frame::after{content:"";position:absolute;width:12px;height:12px;
  border:0 solid var(--ink);display:none;pointer-events:none}
.theme-endfield .frame::before{display:block;left:-1px;top:-1px;border-width:2px 0 0 2px}
.theme-endfield .frame::after{display:block;right:-1px;bottom:-1px;border-width:0 2px 2px 0}
.duo{display:grid;grid-template-columns:repeat(auto-fit,minmax(298px,1fr));gap:20px}
.duo .grid-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}
.foot .foot-link{display:inline-block;margin-left:auto;text-decoration:underline;text-underline-offset:3px;
  transition:color .22s ease,transform .22s ease}
.foot .foot-link:hover{color:var(--ink-2)}
.foot .foot-link:active{transform:scale(.98)}
.rail a:active{transform:scale(.98)}
.notice .nlinks a{display:inline-block}
.notice .nlinks a:active{transform:scale(.98)}
.reg i:nth-child(1){background:#EC008C}
.reg i:nth-child(2){background:#00AEEF}
.reg i:nth-child(3){background:#FFF200}
.reg i:nth-child(4){background:#191919}
.ph-mark{position:absolute;right:-12px;top:-34px;font-size:150px;line-height:1;letter-spacing:-.04em;
  color:transparent;-webkit-text-stroke:2px var(--line-soft);pointer-events:none;user-select:none}
.ph-mark.ink-only{-webkit-text-stroke:0;color:var(--paper-ink);opacity:.07}
.skin-ink .ph-mark.no-ink{display:none}
.skin-ink .ph-mark.ink-only{display:block}
.ph .seal{position:relative;z-index:1;flex:0 0 auto}
.seal.seal-sm{width:34px;height:34px;font-size:20px;gap:1px}
.skin-ink .seal.ink-only{display:inline-flex}
.grid-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(168px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line)}
.stat{background:var(--bg);padding:20px 22px;display:flex;flex-direction:column;gap:8px;min-width:0}
.stat .k{font-family:"Outfit",system-ui,sans-serif;font-size:12px;letter-spacing:.04em;color:var(--ink-2)}
.stat .v{font-family:"Space Grotesk","Outfit",sans-serif;font-size:32px;font-weight:600;line-height:1;
  letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat .v small{font-family:inherit;font-size:13px;font-weight:400;color:var(--ink-2);letter-spacing:0}
.panel{border:1px solid var(--line);background:var(--bg);padding:24px 26px;
  display:flex;flex-direction:column;gap:18px;min-width:0}
.panel .panel-head{display:flex;flex-wrap:wrap;align-items:baseline;justify-content:space-between;gap:8px 16px}
.panel h3{margin:0;font-size:17px;font-weight:700;line-height:1.3}
.meter{display:flex;flex-direction:column;gap:8px}
.meter .mrow{display:flex;align-items:baseline;justify-content:space-between;gap:12px}
.meter .mk{font-size:13px;color:var(--ink-2)}
.meter .mv{font-family:"Space Grotesk","Outfit",sans-serif;font-size:15px;font-weight:600;
  font-variant-numeric:tabular-nums}
.mnote{font-size:12px;color:var(--ink-2);font-variant-numeric:tabular-nums}
progress{appearance:none;-webkit-appearance:none;display:block;width:100%;height:6px;border:0;
  background:var(--line-soft);color:var(--ink)}
progress::-webkit-progress-bar{background:var(--line-soft)}
progress::-webkit-progress-value{background:var(--ink)}
progress::-moz-progress-bar{background:var(--ink)}
.meter.warn progress::-webkit-progress-value{background:var(--seal)}
.meter.warn progress::-moz-progress-bar{background:var(--seal)}
.grid-metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(132px,1fr));gap:20px 24px}
.metric{display:flex;flex-direction:column;gap:6px;min-width:0}
.metric .mk{font-size:12px;color:var(--ink-2)}
.metric .mv{font-family:"Space Grotesk","Outfit",sans-serif;font-size:26px;font-weight:600;line-height:1;
  letter-spacing:-.01em;font-variant-numeric:tabular-nums}
.table-wrap{overflow-x:auto;-webkit-overflow-scrolling:touch}
.tbl{width:100%;border-collapse:collapse;font-size:14px}
.tbl th{padding:10px 12px;text-align:left;font-family:"Outfit",system-ui,sans-serif;font-weight:500;
  font-size:12px;letter-spacing:.04em;color:var(--ink-2);border-bottom:1px solid var(--ink);white-space:nowrap}
.tbl td{padding:12px;border-bottom:1px solid var(--line-soft);vertical-align:top}
.tbl .num{font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl .r{text-align:right}
.tbl tbody tr{transition:background .2s ease}
.tbl tbody tr:hover{background:var(--surface-2)}
.code{margin:0;padding:18px 20px;background:var(--surface-2);border-left:3px solid var(--ink);
  font-family:"Cascadia Mono",Consolas,"SFMono-Regular",ui-monospace,monospace;
  font-size:12.5px;line-height:1.75;max-height:62vh;overflow:auto;
  white-space:pre-wrap;word-break:break-word;color:var(--ink)}
.rec{border-bottom:1px solid var(--line-soft)}
.rec summary{list-style:none;cursor:pointer;padding:12px 0;display:flex;align-items:center;gap:10px;
  font-size:14px;font-weight:600;transition:color .2s ease}
.rec summary::-webkit-details-marker{display:none}
.rec summary:hover{color:var(--ink-2)}
.rec summary .chev{width:7px;height:7px;border-right:1.5px solid currentColor;
  border-bottom:1.5px solid currentColor;transform:rotate(-45deg);transition:transform .2s ease}
.rec[open] summary .chev{transform:rotate(45deg)}
.rec .code{margin:2px 0 16px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:16px}
.tile{margin:0;border:1px solid var(--line);background:var(--bg);display:flex;flex-direction:column;
  min-width:0;transition:transform .24s ease,border-color .24s ease}
.tile:hover{transform:translateY(-2px);border-color:var(--ink)}
.tile-img{aspect-ratio:1;display:flex;align-items:center;justify-content:center;overflow:hidden;
  background-color:var(--surface-2);
  background-image:repeating-linear-gradient(135deg,var(--line-soft) 0 1px,transparent 1px 9px)}
.tile-img img{max-width:100%;max-height:100%;object-fit:contain;display:block}
.tile figcaption{padding:10px 12px;border-top:1px solid var(--line-soft);
  display:flex;flex-direction:column;gap:2px;min-width:0}
.tile .nm{font-size:13px;font-weight:600}
.tile .ds{font-size:12px;line-height:1.55;color:var(--ink-2)}
.logbox{border:1px solid var(--line);background:var(--surface-2);padding:12px 0;
  max-height:66vh;overflow:auto;font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;
  font-size:12.5px;line-height:1.7}
.logline{display:grid;grid-template-columns:56px minmax(0,1fr);gap:14px;padding:1px 18px}
.logline:hover{background:var(--bg)}
.logline .ln{color:var(--ink-2);opacity:.55;text-align:right;font-variant-numeric:tabular-nums;
  user-select:none}
.logline .tx{white-space:pre-wrap;word-break:break-word}
.lvl-error .tx{color:var(--seal)}
.lvl-warn .tx{color:var(--ink);font-weight:600}
.lvl-debug .tx{color:var(--ink-2)}
.actions{display:grid;grid-template-columns:repeat(auto-fit,minmax(296px,1fr));gap:1px;
  background:var(--line);border:1px solid var(--line)}
.act{background:var(--bg);padding:26px 26px 24px;display:flex;flex-direction:column;gap:12px;min-width:0}
.act .ano{font-family:"Space Grotesk",sans-serif;font-size:12px;font-weight:600;
  letter-spacing:.04em;color:var(--ink-2)}
.act h3{margin:0;font-size:19px;font-weight:700;line-height:1.3}
.act.danger h3{color:var(--seal)}
.act p{margin:0;font-size:14px;line-height:1.75;color:var(--ink-2)}
.act p.note{color:var(--ink-2);font-size:13px}
.act .meta{display:flex;align-items:baseline;gap:8px;font-size:13px}
.act form{margin-top:auto;padding-top:8px;display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:12px;color:var(--seal)}
.field input{font:inherit;font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;font-size:14px;
  letter-spacing:.14em;padding:9px 12px;width:120px;border:1px solid var(--seal);
  background:var(--bg);color:var(--ink);transition:box-shadow .2s ease,border-color .2s ease}
.field input:focus{outline:none;box-shadow:0 0 0 3px rgba(184,51,42,.18)}
.field input:focus-visible{outline:2px solid var(--seal);outline-offset:2px}
.btn{display:inline-flex;align-items:center;justify-content:center;min-height:44px;padding:0 22px;
  border:1px solid var(--ink);background:var(--ink);color:#FAFAFA;font:inherit;font-size:14px;
  font-weight:600;cursor:pointer;text-decoration:none;
  transition:transform .2s ease,box-shadow .2s ease,background .2s ease,color .2s ease}
.btn:hover{transform:translateY(-1px);box-shadow:3px 3px 0 var(--line)}
.theme-endfield .btn:hover{box-shadow:3px 3px 0 var(--mark)}
.btn:active{transform:translateY(0) scale(.98);box-shadow:none}
.btn.quiet{background:transparent;color:var(--ink)}
.btn.quiet:hover{background:var(--surface-2)}
.btn.danger{background:var(--seal);border-color:var(--seal);color:var(--paper)}
.btn.danger:hover{box-shadow:3px 3px 0 var(--line)}
.act code,.notice code,.panel code{font-family:"Cascadia Mono",Consolas,ui-monospace,monospace;
  font-size:13px;background:var(--surface-2);border:1px solid var(--line-soft);padding:1px 6px}
.empty-box{display:flex;align-items:flex-start;gap:16px;padding:26px 24px;
  border:1px dashed var(--line);background:var(--surface-2)}
.empty-box .mk{flex:0 0 auto;width:8px;height:8px;background:var(--line);margin-top:9px}
.empty-t{margin:0 0 4px;font-size:16px;font-weight:600;line-height:1.4}
.empty-p{margin:0;font-size:14px;line-height:1.7;color:var(--ink-2);max-width:48ch}
.notice{max-width:760px;margin:44px 40px 64px;padding:32px 34px;border:1px solid var(--line);
  display:flex;flex-direction:column;gap:14px;position:relative}
.notice .nk{font-family:"Outfit",system-ui,sans-serif;font-size:12px;letter-spacing:.14em;
  text-transform:uppercase;color:var(--ink-2)}
.notice h1{margin:0;font-size:30px;font-weight:800;line-height:1.25}
.notice p{margin:0;font-size:15px;line-height:1.8;color:var(--ink-2)}
.notice ul{margin:0;padding-left:20px;font-size:14px;line-height:1.9;color:var(--ink-2)}
.notice.err{border-left:3px solid var(--seal)}
.notice.err h1{color:var(--seal)}
.notice.warn{border-left:3px solid var(--mark)}
.notice.ok{border-left:3px solid var(--ink)}
.notice .nlinks{display:flex;flex-wrap:wrap;gap:18px;margin-top:4px}
.notice .nlinks a{font-size:14px;text-decoration:underline;text-underline-offset:3px;
  transition:color .22s ease}
.notice .nlinks a:hover{color:var(--ink-2)}
.skin-ink .panel,.skin-ink .stat,.skin-ink .tile,.skin-ink .act,.skin-ink .notice{background:var(--paper-2)}
.skin-ink .act,.skin-ink .tile,.skin-ink .panel,.skin-ink .notice{border-color:#E2D9C6}
.skin-ink .grid-stats,.skin-ink .actions{background:#E2D9C6;border-color:#E2D9C6}
.skin-ink .code,.skin-ink .logbox{background:var(--paper);border-left-color:var(--seal)}
.skin-ink .empty-box{background:var(--paper-2);border-color:#D8CDB6}
@media (max-width:760px){
  .app{flex-direction:column;background:none}
  .rail{flex:0 0 auto;height:auto;position:static;flex-direction:row;align-items:center;overflow-x:auto;padding:0 8px}
  .rail .logo{width:36px;height:36px;margin:8px 8px 8px 4px;font-size:16px}
  .rail .logo img{width:42px}
  .rail a{flex-direction:row;padding:14px 10px;font-size:13px;white-space:nowrap}
  .rail a svg{display:none}
  .rail a.on::before{left:8px;right:8px;top:auto;bottom:0;width:auto;height:3px}
  .topbar,.sec,.foot{padding-left:16px;padding-right:16px}
  .hero-art{min-height:320px;flex-basis:100%}
  .hero-side{padding:28px 16px}
  .ghost{font-size:170px}
  .hero-name{left:16px}
  .search{min-width:0;width:100%}
  .theme-endfield .vf{margin:0;padding:14px}
  .hero-chibi{width:130px;right:4%;bottom:40px}
  .ph{padding:26px 16px 20px;gap:14px 24px}
  .ph h1{font-size:26px}
  .ph .side{align-items:flex-start;text-align:left}
  .ph .side .v{font-size:24px}
  .ph-mark{font-size:96px;top:-16px;right:-8px}
  .grid-stats{grid-template-columns:repeat(2,minmax(0,1fr))}
  .stat{padding:16px}
  .stat .v{font-size:26px}
  .panel{padding:18px 16px}
  .grid-metrics{grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
  .metric .mv{font-size:22px}
  .tbl th,.tbl td{padding:10px 8px}
  .code{font-size:11.5px;max-height:52vh;padding:14px}
  .gallery{grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:10px}
  .tile figcaption{padding:8px 10px}
  .logbox{font-size:11px;max-height:56vh}
  .logline{grid-template-columns:34px minmax(0,1fr);gap:8px;padding:1px 10px}
  .actions{grid-template-columns:1fr}
  .act{padding:20px 16px}
  .act h3{font-size:17px}
  .notice{margin:24px 16px 40px;padding:22px 18px}
  .notice h1{font-size:24px}
  .btn{width:100%}
  .act form{flex-wrap:wrap}
}
"""


OVERVIEW_REFRESH_JS = """<script>
(function () {
  function refresh() {
    if (document.hidden) return;
    fetch(window.location.href, {cache: "no-store"})
      .then(function (response) {
        if (!response.ok) throw new Error("refresh failed");
        return response.text();
      })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        var current = document.querySelector(".main");
        var next = doc.querySelector(".main");
        if (!current || !next) return;
        var scrollY = window.scrollY;
        current.replaceWith(next);
        window.scrollTo(0, scrollY);
      })
      .catch(function () {});
  }
  window.setInterval(refresh, 30000);
})();
</script>"""


def render_shell(
    title: str,
    current_path: str,
    content: str,
    theme: str,
    skin: Optional[Skin],
    token_query: str = "",
    char_name: str = "",
    refresh: bool = True,
) -> str:
    """新主题的页面外壳：左侧导航栏 + 内容。

    plain / endfield 的七个页面共用这一份 CSS（dashboard.CSS），没有任何页面再往
    `<head>` 里塞第二套样式表；journal 主题走 admin_render.html_shell，与它无关。
    总览在 refresh=True 时后台局部更新，其他页面不自动刷新。
    """
    body_cls = [f"theme-{theme}"]
    if skin and skin.ink:
        body_cls.append("skin-ink")
    cursor_css = ""
    if skin:
        rules = []
        for key, sel, fallback, hot in (
            ("normal", "body", "auto", "3 3"),
            ("link", "a,button,label,.chip", "pointer", "6 3"),
            ("text", "input,textarea", "text", "8 8"),
        ):
            rel = skin.cursor.get(key, "")
            if skin.has(rel):
                rules.append(f'{sel}{{cursor:url("{_skin_url(rel, token_query)}") {hot},{fallback}}}')
        cursor_css = "\n".join(rules)
    char_color = f":root{{--char:{skin.char_color}}}" if skin else ""

    logo_rel = skin.avatar_for("default") if skin else ""
    if logo_rel:
        logo = f'<div class="logo"><img src="{_skin_url(logo_rel, token_query)}" alt="{_e(char_name)}"></div>'
    else:
        logo = f'<div class="logo txt" aria-hidden="true">{_e(char_name[:1] or "她")}</div>'
    links = []
    for path, label, icon in NAV:
        cls = ' class="on" aria-current="page"' if path == current_path else ""
        links.append(
            f'<a href="{path}{token_query}"{cls}><svg viewBox="0 0 24 24" aria-hidden="true">{icon}</svg>{label}</a>'
        )
    refresh_script = OVERVIEW_REFRESH_JS if refresh and current_path == "/" else ""
    app_title = _e((char_name or "伴侣") + "的小窝")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="apple-mobile-web-app-title" content="{app_title}">
  <link rel="icon" type="image/png" href="/favicon.png{token_query}">
  <link rel="apple-touch-icon" href="/apple-touch-icon.png{token_query}">
  <title>{_e(title)} · {_e(char_name or "伴侣")}</title>
  <style>{CSS}
{char_color}
{cursor_css}</style>
</head>
<body class="{' '.join(body_cls)}">
<div class="app">
  <nav class="rail" aria-label="主导航">{logo}{''.join(links)}</nav>
  <div class="main">{content}</div>
</div>
{refresh_script}
</body>
</html>"""


# ---------------------------------------------------------------- 小部件


def _sh(idx: int, en: str, zh: str, tag: str = "h2") -> str:
    return (
        f'<div class="sh"><div class="en-row"><span class="idx">{idx:02d}</span>'
        f'<span class="hatch"></span><span class="en">{_e(en)}</span></div>'
        f"<{tag}>{_e(zh)}</{tag}></div>"
    )


def _tick_ring() -> str:
    ticks = "".join(
        f'<line x1="48" y1="2" x2="48" y2="{7 if i % 5 == 0 else 5}" transform="rotate({i * 6} 48 48)"/>'
        for i in range(60)
    )
    return (
        '<svg class="ring" viewBox="0 0 96 96" aria-hidden="true" stroke="#8A8A8A" stroke-width="1">'
        f'{ticks}<path d="M48 2 A46 46 0 0 1 94 48" fill="none" stroke="#191919" stroke-width="2"/></svg>'
    )


def _avatar(d: Dict[str, Any], skin: Optional[Skin], tq: str) -> str:
    rel = skin.avatar_for(d["mood_label"], d["asleep"]) if skin else ""
    if rel:
        face = (
            f'<div class="face"><img src="{_skin_url(rel, tq)}" '
            f'alt="她现在的样子：{_e(d["mood_label"])}"></div>'
        )
    else:
        face = f'<div class="face txt" aria-hidden="true">{_e(d["name"][:1])}</div>'
    return f'<div class="av">{_tick_ring()}{face}</div>'


def _mountains(fid: str, dark: bool = False) -> str:
    fill = "#F4EFE4" if dark else "#1E1E1E"
    ops = (".07", ".11", ".15") if dark else (".10", ".16", ".22")
    return f"""<svg class="ink-only" aria-hidden="true" viewBox="0 0 800 400" preserveAspectRatio="none"
 style="position:absolute;left:0;right:0;bottom:0;width:100%;height:62%">
<defs><filter id="{fid}" x="-10%" y="-10%" width="120%" height="120%"><feGaussianBlur stdDeviation="6"/></filter></defs>
<path d="M0 260 C60 200 110 150 170 190 C220 120 260 90 320 160 C360 130 400 150 430 200 C480 150 540 110 600 180 C650 140 720 170 800 210 L800 400 L0 400 Z" fill="{fill}" opacity="{ops[0]}" filter="url(#{fid})"/>
<path d="M0 320 C80 270 140 240 210 280 C260 230 320 210 380 260 C440 230 500 250 560 290 C620 250 700 260 800 300 L800 400 L0 400 Z" fill="{fill}" opacity="{ops[1]}" filter="url(#{fid})"/>
<path d="M0 370 C120 340 220 330 330 355 C440 335 560 340 800 365 L800 400 L0 400 Z" fill="{fill}" opacity="{ops[2]}" filter="url(#{fid})"/>
<circle cx="640" cy="90" r="34" fill="#B8332A" opacity=".7" filter="url(#{fid})"/>
</svg>"""


def _butterfly(style: str, color: str = "#1E1E1E", size: int = 46) -> str:
    return (
        f'<svg class="ink-only" aria-hidden="true" width="{size}" height="{int(size * .75)}" viewBox="0 0 40 30" '
        f'style="position:absolute;{style}" fill="{color}">{BUTTERFLY}</svg>'
    )


def _seal_v(text: str, w: int = 40, fs: int = 22) -> str:
    chars = "".join(f"<span>{_e(c)}</span>" for c in text[:2])
    h = w if len(text) < 2 else int(w * 1.65)
    return f'<span class="seal" style="width:{w}px;height:{h}px;font-size:{fs}px;gap:2px">{chars}</span>'


def _rel(dt: Optional[datetime], now: datetime) -> str:
    if not dt:
        return ""
    return relative_day(dt.date(), now.date())


def _ago(dt: Optional[datetime], now: datetime) -> str:
    if not dt:
        return "还没聊过"
    secs = (now - dt).total_seconds()
    if secs < 3600:
        return f"{max(1, int(secs // 60))} 分钟前"
    if secs < 86400:
        return f"{int(secs // 3600)} 小时前"
    return _rel(dt, now)


# ---------------------------------------------------------------- 总览


async def collect_overview(srv: Any) -> Dict[str, Any]:
    """从 AdminServer 身上取总览要的数据（只读）。"""
    now = datetime.now()
    persona = srv.persona
    aff = await srv.affection.get_state()
    dims = aff.get("dims", {}) or {}
    composite = float(aff.get("composite", 0.0))
    stage = int(aff.get("stage", 0))
    max_stage = int(aff.get("max_stage", stage))
    th = getattr(srv.affection, "thresholds", None)
    if not (isinstance(th, list) and th):
        from companion.affection import STAGE_THRESHOLDS as th
    n_stages = len(th)
    stage_obj = persona.get_stage(stage)
    if stage < n_stages - 1:
        lo, hi = th[stage], th[stage + 1]
        progress = max(0.0, min(1.0, (composite - lo) / (hi - lo))) if hi > lo else 0.0
        next_name = persona.get_stage(stage + 1).name
    else:
        progress, next_name = 1.0, ""
    drop = getattr(srv.affection, "stage_max_drop", None)
    floor_name = ""
    if isinstance(drop, int):
        floor_name = persona.get_stage(max(0, max_stage - drop)).name

    mood = await srv.mood.get_state()
    v, a = float(mood.get("v", 2.0)), float(mood.get("a", 1.0))
    activity = srv._current_activity(now)
    asleep = "睡" in activity

    arc = await srv.db.fetchone(
        "SELECT title, emotional_stake FROM life_arcs WHERE status IN ('upcoming','near','today') "
        "ORDER BY key_date ASC LIMIT 1"
    )
    last_user = await srv.db.fetchone("SELECT created_at FROM turns WHERE role = 'user' ORDER BY id DESC LIMIT 1")
    first = await srv.db.fetchone("SELECT created_at FROM turns ORDER BY id ASC LIMIT 1")
    first_dt = parse_dt(str(first["created_at"])) if first and first["created_at"] else None
    turns_row = await srv.db.fetchone("SELECT value FROM counters WHERE key = ?", (COUNTER_KEY_TOTAL_TURNS,))

    imps = await srv.db.fetchall("SELECT content FROM impressions ORDER BY confidence DESC, id DESC LIMIT 5")
    meet = await srv.db.fetchall(
        "SELECT when_at, place, plan FROM meetups WHERE status = 0 ORDER BY when_at LIMIT 3"
    )
    fus = await srv.db.fetchall(
        "SELECT topic, remind_after FROM followups WHERE done = 0 ORDER BY remind_after LIMIT 4"
    )
    desires = []
    try:
        desires = await srv.memory.get_live_desires(limit=2)
    except Exception:
        pass

    month = now.strftime("%Y-%m-01 00:00")
    row_m = await srv.db.fetchone("SELECT SUM(cost_estimate) AS c FROM llm_calls WHERE created_at >= ?", (month,))
    unanswered = await srv.proactive.get_unanswered_count() if srv.proactive else 0
    max_un = srv.proactive.config.max_unanswered if (srv.proactive and srv.proactive.config) else 2

    from companion.backup import get_last_backup_time

    return {
        "now": now,
        "name": persona.name,
        "stage": stage, "n_stages": n_stages, "stage_name": stage_obj.name, "tone": stage_obj.tone,
        "next_name": next_name, "progress": progress, "floor_name": floor_name,
        "dims": [(zh, float(dims.get(k, 0.0))) for k, zh in DIM_ORDER],
        "mood_label": get_mood_label(v, a), "mood_desc": get_mood_description(v, a),
        "activity": activity, "asleep": asleep,
        "arc": (arc["title"], arc["emotional_stake"] or "") if arc else None,
        "last_chat": _ago(parse_dt(str(last_user["created_at"])) if last_user else None, now),
        "days_known": max(1, (now.date() - first_dt.date()).days + 1) if first_dt else 0,
        "turns": int(turns_row["value"]) if turns_row and turns_row["value"] is not None else 0,
        "impressions": [r["content"] for r in imps],
        "meetups": [
            {"when": _rel(parse_dt(r["when_at"]), now), "time": (parse_dt(r["when_at"]) or now).strftime("%H:%M"),
             "place": r["place"] or "", "plan": r["plan"] or ""}
            for r in meet
        ],
        "followups": [(r["topic"], _rel(parse_dt(r["remind_after"]), now)) for r in fus],
        "desires": [r["content"] for r in desires],
        "unanswered": unanswered, "max_unanswered": max_un,
        "onebot": bool(srv.onebot and srv.onebot.is_connected),
        "cost_today": await srv._today_cost(),
        "cost_month": float(row_m["c"] or 0.0) if row_m else 0.0,
        "backup": get_last_backup_time(srv.backup_dir) or "暂无",
    }


def _dims_note(dims: List[Any]) -> str:
    named = {k: v for k, v in dims}
    top = sorted((p for p in dims if p[0] != "紧张"), key=lambda p: -p[1])[:2]
    tension = named.get("紧张", 0.0)
    t = "很低" if tension < 15 else ("有一点" if tension < 40 else "偏高")
    return f"{top[0][0]}和{top[1][0]}最高，紧张{t}。" if len(top) == 2 else ""


def render_overview(d: Dict[str, Any], skin: Optional[Skin], tq: str) -> str:
    ink = bool(skin and skin.ink)
    online_term = skin.term("online", "在线") if skin else "在线"
    last_term = skin.term("last_chat", "上次聊天") if skin else "上次聊天"
    status_line = d["activity"]
    if d["arc"]:
        title, stake = d["arc"]
        status_line = f"{d['activity']}。心里挂着「{title}」" + (f"：{stake}" if stake else "")
    stage_txt = f"{d['stage']:02d}"

    ladder = "".join(
        f'<div><svg class="tri{" on" if i == d["stage"] else ""}" viewBox="0 0 10 7" aria-hidden="true">'
        f'<path d="M0 0h10L5 7z" fill="#191919"/></svg>'
        f'<span class="seg{" on" if i <= d["stage"] else ""}"></span><span class="n num">{i:02d}</span></div>'
        for i in range(d["n_stages"])
    )
    floor = f"{_e(d['floor_name'])} · 最多退到这" if d["floor_name"] else ""
    nxt = f"{_e(d['next_name'])} →" if d["next_name"] else "已到最高阶段"
    pct = int(round(d["progress"] * 100))

    # 首屏左侧：有水墨皮肤 = 一幅她的画；否则 = 大字名字 + 镂空阶段号
    hero_rel = skin.art.get("hero", "") if skin else ""
    hero_img = (
        f'<img class="ink-only hero-chibi" src="{_skin_url(hero_rel, tq)}" alt="" aria-hidden="true">'
        if skin and skin.has(hero_rel) else ""
    )
    subtitle = _e(skin.subtitle) if skin and skin.subtitle else ""
    hero = f"""
<div class="hero-art">
  <div class="ghost wide no-ink" aria-hidden="true">{stage_txt}</div>
  {_mountains("m1")}
  {_butterfly("left:62%;top:22%;transform:rotate(-12deg);opacity:.85")}
  {_butterfly("left:72%;top:34%;transform:rotate(18deg);opacity:.5", size=26)}
  <div class="ink-only" style="position:absolute;left:44px;top:44px">
    <div style="display:flex;align-items:flex-start;gap:18px">
      <h1 class="brush" style="margin:0;font-weight:400;font-size:220px;line-height:.9;color:#1E1E1E">{_e(d['name'][:1])}</h1>
      <div style="display:flex;flex-direction:column;align-items:center;gap:14px;padding-top:22px">
        {_seal_v(d['stage_name'])}
        <span class="serif" style="writing-mode:vertical-rl;font-size:16px;letter-spacing:.3em;color:#5C5548">{_e(d['tone'])}</span>
      </div>
    </div>
  </div>
  {hero_img}
  <div class="hero-name no-ink">
    <span class="micro">{subtitle or _e(d['tone'])}</span>
    <span class="big">{_e(d['name'])}</span>
  </div>
  <div class="ink-only micro" style="position:absolute;left:40px;bottom:22px;color:#5C5548">{subtitle}</div>
</div>"""

    side = f"""
<div class="hero-side">
  <div class="vf" style="display:flex;flex-direction:column;gap:14px">
    <i style="left:0;top:0;border-width:2px 0 0 2px"></i><i style="right:0;top:0;border-width:2px 2px 0 0"></i>
    <i style="left:0;bottom:0;border-width:0 0 2px 2px"></i><i style="right:0;bottom:0;border-width:0 2px 2px 0"></i>
    <div style="display:flex;align-items:center;gap:16px">{_avatar(d, skin, tq)}{_sh(1, "Status", "她现在")}</div>
    <p class="lead" style="margin:0;font-size:17px;line-height:1.8">{_e(status_line)}</p>
    <div class="chips"><span class="pill">心情 · {_e(d['mood_label'])}</span><span class="pill">{_e(d['mood_desc'])}</span></div>
    <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px;border-top:1px solid var(--line);padding-top:14px">
      <div><div class="micro">{_e(last_term)}</div><div style="font-weight:500">{_e(d['last_chat'])}</div></div>
      <div><div class="micro">主动消息没回</div><div class="num" style="font-weight:500">{d['unanswered']} / {d['max_unanswered']}</div></div>
    </div>
  </div>
  <div style="display:flex;flex-direction:column;gap:14px">
    {_sh(2, "Relationship", "关系阶段")}
    <div style="display:flex;align-items:baseline;gap:14px;flex-wrap:wrap">
      <span class="num" style="font-size:52px;font-weight:600;line-height:1">{stage_txt}</span>
      <span class="num" style="font-size:18px;color:var(--ink-2)">/ {d['n_stages'] - 1:02d}</span>
      <span class="lead" style="font-size:28px;font-weight:900">{_e(d['stage_name'])}</span>
    </div>
    <div style="font-size:14px;color:var(--ink-2)">{_e(d['tone'])} · 认识 {d['days_known']} 天 · 聊过 {d['turns']} 轮</div>
    <div class="ladder" aria-label="阶段 {d['stage']}，共 0 到 {d['n_stages'] - 1}">{ladder}</div>
    <div style="display:flex;justify-content:space-between;gap:12px;font-size:13px;color:var(--ink-2)"><span>{floor}</span><span>{nxt}</span></div>
    <div style="display:flex;justify-content:space-between;font-size:14px"><span>离下一阶</span><span class="num">{pct}%</span></div>
    <div class="bar"><i style="width:{pct}%"></i></div>
  </div>
</div>"""

    dims_html = "".join(
        f'<div style="display:grid;grid-template-columns:52px minmax(0,1fr) 40px;align-items:center;gap:14px">'
        f'<span style="font-weight:500">{zh}</span><div class="bar"><i style="width:{max(0, min(100, val)):.0f}%;'
        f'background:{"var(--warn)" if zh == "紧张" else "var(--ink)"}"></i></div>'
        f'<span class="num" style="text-align:right">{val:.0f}</span></div>'
        for zh, val in d["dims"]
    )
    imps = "".join(
        f'<div class="row lead" style="justify-content:flex-start;font-size:16px"><span>· {_e(t)}</span></div>'
        for t in d["impressions"]
    ) or '<p class="empty">还没攒够对你的看法（每聊一阵她会自己总结几条）。</p>'
    impressions_box = f"""
<div class="col paper-card">
  <div class="ink-only" style="position:absolute;right:22px;top:20px">{_seal_v("印象", 34, 20)}</div>
  {_sh(4, "Impression", "她怎么看你")}
  <div class="micro">她自己记下的，不会说出口</div>
  {imps}
</div>"""

    if d["meetups"]:
        m = d["meetups"][0]
        meet = f"""
<div class="paper-card" style="display:flex;flex-direction:column;gap:10px">
  <div class="micro">{_e(skin.term("meetup", "约好的见面") if skin else "约好的见面")}</div>
  <div class="brush ink-only" style="font-size:52px;line-height:1">{_e(m['when'])}</div>
  <div class="wide no-ink" style="font-size:40px;line-height:1">{_e(m['when'])} <span class="num" style="font-size:18px">{_e(m['time'])}</span></div>
  <div class="lead" style="font-size:20px;font-weight:700">{_e(m['place'])}{(' · ' + _e(m['plan'])) if m['plan'] else ''}</div>
  <div class="micro">见完 3 小时后，系统会替她补一段见面纪要</div>
  <div class="ink-only" style="position:absolute;right:24px;bottom:22px">{_seal_v(d['name'][:1], 44, 28)}</div>
</div>"""
    else:
        meet = '<p class="empty" style="color:#B3B3B3">最近没有约好的见面。</p>'
    todo = "".join(f'<div class="row"><span>{_e(t)}</span><span class="micro">{_e(w)}</span></div>' for t, w in d["followups"])
    todo += "".join(f'<div class="row"><span>{_e(t)}</span><span class="micro">憋着没说</span></div>' for t in d["desires"])
    todo = todo or '<p class="empty" style="color:#B3B3B3">没有挂着的事。</p>'

    return f"""
<div class="topbar">
  <span class="micro">/ OVERVIEW / {d['now'].strftime('%m.%d %H:%M')}</span>
  <span class="pill"><span class="dot" style="background:{'#007F43' if d['onebot'] else '#C0392B'}"></span>
  {_e(online_term if d['onebot'] else 'QQ 没连上')}</span>
</div>
<section class="hero">{hero}{side}</section>
<section class="sec">
  <div class="col">{_sh(3, "Affinity", "她对你")}{dims_html}
    <p class="micro" style="font-size:14px">{_e(_dims_note(d['dims']))}</p></div>
  {impressions_box}
</section>
<section class="sec inverse">
  <div class="col">{_sh(5, "Appointment", "约定")}{meet}</div>
  <div class="col">{_sh(6, "Follow-up", "她惦记着的事")}{todo}</div>
</section>
<footer class="foot">
  <div class="reg" aria-hidden="true"><i style="background:#EC008C"></i><i style="background:#00AEEF"></i><i style="background:#FFF200"></i><i style="background:#191919"></i></div>
  <span><span class="micro">今日 </span><span class="num" style="color:var(--money)">¥{d['cost_today']:.2f}</span></span>
  <span><span class="micro">本月 </span><span class="num" style="color:var(--money)">¥{d['cost_month']:.2f}</span></span>
  <span><span class="micro">上次备份 </span>{_e(d['backup'])}</span>
  <a href="/debug{tq}" style="margin-left:auto">调试信息</a>
</footer>"""


# ---------------------------------------------------------------- 记忆


async def collect_memory(srv: Any) -> Dict[str, Any]:
    now = datetime.now()

    async def _layer(table: str) -> Dict[str, List[Dict[str, Any]]]:
        cols = "content, core, created_at" + (", prev" if table == "self_facts" else "")
        try:
            rows = await srv.db.fetchall(f"SELECT {cols} FROM {table} ORDER BY id DESC")
        except Exception:
            rows = []
        out: Dict[str, List[Dict[str, Any]]] = {"core": [], "recent": []}
        for r in rows:
            item = {
                "t": r["content"],
                "d": _rel(parse_dt(r["created_at"]), now),
                "prev": (r["prev"] if table == "self_facts" else "") or "",
            }
            out["core" if r["core"] else "recent"].append(item)
        return out

    diaries = []
    for r in await srv.db.fetchall(
        "SELECT content, importance, sentiment, recall_count, created_at, last_recall_at FROM diary ORDER BY id DESC LIMIT 60"
    ):
        last = parse_dt(r["last_recall_at"] or r["created_at"])
        days = max(0.0, (now - last).total_seconds() / 86400.0) if last else 0.0
        strength, _ = calc_diary_strength(float(r["importance"] or 5), int(r["recall_count"] or 0), str(r["sentiment"] or "平静"), days)
        diaries.append({
            "t": r["content"], "mood": r["sentiment"] or "平静",
            "d": _rel(parse_dt(r["created_at"]), now),
            # 墨迹随记忆强度变淡，但最淡也要看得清
            "op": round(0.62 + 0.38 * max(0.0, min(1.0, strength / 10.0)), 2),
        })

    meetups = [
        {"place": r["place"] or "", "plan": r["plan"] or "", "recap": r["recap"] or "",
         "when": _rel(parse_dt(r["when_at"]), now), "pending": int(r["status"]) == 0}
        for r in await srv.db.fetchall(
            "SELECT when_at, place, plan, status, recap FROM meetups WHERE status IN (0, 1) ORDER BY when_at DESC LIMIT 8"
        )
    ]
    live = []
    try:
        live = [r["content"] for r in await srv.memory.get_live_desires(limit=5)]
    except Exception:
        pass
    old = [
        r["content"] for r in await srv.db.fetchall("SELECT content FROM suppressed_desires ORDER BY id DESC LIMIT 8")
        if r["content"] not in live
    ][:3]
    return {
        "about": await _layer("facts"), "self": await _layer("self_facts"),
        "diaries": diaries, "meetups": meetups, "unsaid": live, "unsaid_old": old,
    }


def _fact_rows(items: List[Dict[str, Any]], kind: str) -> str:
    out = []
    for it in items:
        prev = (
            f' <span style="color:var(--ink-2);text-decoration:line-through;font-size:13px">{_e(it["prev"])}</span>'
            if it.get("prev") else ""
        )
        out.append(
            f'<div class="row" data-k="{kind}" data-q="{_e(it["t"])}"><span>{_e(it["t"])}{prev}</span>'
            f'<span class="micro">{_e(it["d"])}{" · 改口" if prev else ""}</span></div>'
        )
    return "".join(out)


def _layer_block(layer: Dict[str, List[Dict[str, Any]]], kind: str) -> str:
    if not layer["core"] and not layer["recent"]:
        return '<p class="empty">还没有。</p>'
    html_parts = []
    if layer["core"]:
        html_parts.append(f'<div class="micro" style="margin-top:8px">核心 · 一直记着（{len(layer["core"])}）</div>')
        html_parts.append(_fact_rows(layer["core"], kind))
    if layer["recent"]:
        html_parts.append(f'<div class="micro" style="margin-top:16px">其他（{len(layer["recent"])}）</div>')
        html_parts.append(_fact_rows(layer["recent"][:40], kind))
    return "".join(html_parts)


SEARCH_JS = """<script>
(function(){var q=document.getElementById('mq'),cur='all';
function run(){var s=(q.value||'').trim();document.querySelectorAll('[data-k]').forEach(function(el){
 var okK=cur==='all'||el.getAttribute('data-k')===cur;var okQ=!s||(el.getAttribute('data-q')||'').indexOf(s)>=0;
 el.style.display=(okK&&okQ)?'':'none';});}
q&&q.addEventListener('input',run);
document.querySelectorAll('.chip[data-f]').forEach(function(b){b.addEventListener('click',function(){
 cur=b.getAttribute('data-f');document.querySelectorAll('.chip[data-f]').forEach(function(x){x.classList.toggle('on',x===b);x.setAttribute('aria-pressed',x===b);});run();});});
})();
</script>"""


def render_memory(d: Dict[str, Any], skin: Optional[Skin], tq: str) -> str:
    art = skin.art if skin else {}
    mem_art = (
        f'<img src="{_skin_url(art["memory"], tq)}" alt="" aria-hidden="true" style="width:110px;display:block">'
        if skin and skin.has(art.get("memory", "")) else ""
    )
    unsaid_art = (
        f'<img src="{_skin_url(art["unsaid"], tq)}" alt="" aria-hidden="true" style="width:56px;display:block">'
        if skin and skin.has(art.get("unsaid", "")) else ""
    )
    diaries = "".join(
        f'<div class="diary" data-k="diary" data-q="{_e(x["t"])}" style="opacity:{x["op"]}">'
        f'<div style="display:flex;flex-direction:column;gap:8px;align-items:flex-start">'
        f'<span class="brush" style="font-size:22px;line-height:1">{_e(x["d"])}</span>'
        f'<span class="pill" style="min-height:22px;font-size:12px">{_e(x["mood"])}</span></div>'
        f'<div class="lead" style="font-size:16px;line-height:1.85">{_e(x["t"])}</div></div>'
        for x in d["diaries"]
    ) or '<p class="empty">还没有日记（每天聊完她会写一篇）。</p>'
    meets = []
    for m in d["meetups"]:
        head = f'{_e(m["place"])}{(" · " + _e(m["plan"])) if m["plan"] else ""}'
        if m["pending"]:
            meets.append(
                f'<div data-k="meet" data-q="{head}" style="background:#191919;color:#FAFAFA;padding:16px 20px;'
                f'display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center">'
                f'<span style="font-weight:700">{head}</span><span class="pill" style="background:transparent;'
                f'border:1px solid #FAFAFA;color:#FAFAFA">{_e(m["when"])} · 待赴约</span></div>'
            )
        else:
            meets.append(
                f'<div class="paper-card" data-k="meet" data-q="{head} {_e(m["recap"])}" '
                f'style="display:flex;flex-direction:column;gap:8px;border:1px solid var(--line-soft);padding:18px 20px">'
                f'<div style="display:flex;justify-content:space-between;gap:12px"><span class="lead" style="font-weight:900;font-size:17px">{head}</span>'
                f'<span class="micro">{_e(m["when"])}</span></div>'
                f'<div class="lead" style="line-height:1.85;padding-right:44px">{_e(m["recap"])}</div></div>'
            )
    meets_html = "".join(meets) or '<p class="empty">还没有线下见面。</p>'
    unsaid = "".join(
        f'<div class="row lead" data-k="unsaid" data-q="{_e(t)}"><span>{_e(t)}</span><span class="micro">还想说</span></div>'
        for t in d["unsaid"]
    ) + "".join(
        f'<div class="row lead" data-k="unsaid" data-q="{_e(t)}" style="color:var(--ink-2)">'
        f'<span style="text-decoration:line-through">{_e(t)}</span><span class="micro">已作废</span></div>'
        for t in d["unsaid_old"]
    ) or '<p class="empty">没有憋着的话。</p>'

    return f"""
<div class="topbar"><span class="micro">/ MEMORY ARCHIVE</span><span class="micro">记忆档案</span></div>
<header style="position:relative;overflow:hidden;padding:44px 40px 28px;display:flex;flex-wrap:wrap;justify-content:space-between;align-items:flex-end;gap:24px">
  <div class="wide no-ink" aria-hidden="true" style="position:absolute;right:-20px;top:-20px;font-size:180px;line-height:1;color:transparent;-webkit-text-stroke:2px var(--line-soft)">ARCHIVE</div>
  <div class="brush ink-only" aria-hidden="true" style="position:absolute;right:30px;top:-50px;font-size:240px;line-height:1;opacity:.06">忆</div>
  <div style="position:relative;display:flex;align-items:flex-end;gap:20px">{mem_art}
    <div><div class="micro" style="text-transform:uppercase">Memory Archive</div>
    <h1 class="lead" style="margin:4px 0 0;font-size:36px;font-weight:900">她记得什么</h1></div></div>
  <label class="search" style="position:relative"><span style="position:absolute;width:1px;height:1px;overflow:hidden">搜索记忆</span>
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
    <input id="mq" type="text" placeholder="搜一搜"></label>
</header>
<div class="chips" style="padding:0 40px 28px">
  <button class="chip on" data-f="all" aria-pressed="true">全部</button>
  <button class="chip" data-f="about" aria-pressed="false">关于你</button>
  <button class="chip" data-f="self" aria-pressed="false">她自己</button>
  <button class="chip" data-f="diary" aria-pressed="false">日记</button>
  <button class="chip" data-f="meet" aria-pressed="false">见面</button>
</div>
<section class="sec">
  <div class="col">{_sh(1, "About You", "她记得的你")}{_layer_block(d['about'], 'about')}</div>
  <div class="col">{_sh(2, "About Herself", "她说过的自己")}{_layer_block(d['self'], 'self')}</div>
</section>
<section class="sec paper" style="position:relative;overflow:hidden">
  {_mountains("m2").replace('height:62%', 'height:160px')}
  <div class="col" style="position:relative">{_sh(3, "Diary", "她的日记")}
    <div class="micro">越旧、越平淡的越淡；被想起一次就深一点</div>{diaries}</div>
  <div class="col" style="position:relative;gap:28px">
    <div style="display:flex;flex-direction:column;gap:12px">{_sh(4, "Meetups", "见面纪要")}{meets_html}</div>
    <div style="display:flex;flex-direction:column;gap:6px">{_sh(5, "Unsaid", "憋着没说的")}{unsaid}
      <div style="display:flex;align-items:center;gap:14px;margin-top:10px">{unsaid_art}
      <span class="micro" style="font-size:13px">作废的念头划掉留底，她不会再拿出来说。</span></div></div>
  </div>
</section>
{SEARCH_JS}"""


# ---------------------------------------------------------------- 小窝用的状态


async def collect_home(srv: Any, skin: Optional[Skin], tq: str) -> Dict[str, Any]:
    """给桌面小窝的 /api/home：她是谁、现在怎样、头像和状态图。"""
    d = await collect_overview(srv)
    out = {
        "name": d["name"], "stage": d["stage"], "stage_name": d["stage_name"], "tone": d["tone"],
        "mood_label": d["mood_label"], "activity": d["activity"],
        "arc": d["arc"][0] if d["arc"] else "", "last_chat": d["last_chat"],
        "onebot_connected": d["onebot"], "today_cost": round(d["cost_today"], 4),
        "subtitle": skin.subtitle if skin else "", "ink": bool(skin and skin.ink),
        "char_color": skin.char_color if skin else "",
        "avatar": "", "art": {}, "terms": dict(skin.terms) if skin else {},
    }
    if skin:
        rel = skin.avatar_for(d["mood_label"], d["asleep"])
        out["avatar"] = _skin_url(rel, tq) if rel else ""
        out["art"] = {k: _skin_url(v, tq) for k, v in skin.art.items() if skin.has(v)}
    return out
