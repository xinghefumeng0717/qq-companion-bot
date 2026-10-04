"""FIXES17 所有者审核文档生成器 (scripts/audit/build_sticker_review_docx.py)

**为什么要有这个**：纯文字审核表（`data/sticker_audit.md`）没法审——所有者看不到图，
判断不了"画面/含义"标得对不对，更没法发现旧标注本身是错的。所以把图片和三栏标注
排成一份 Word（`.docx`），图在左、字在右，一行一张，**点进单元格就能改**。

**为什么是 Word 不是 PDF**：所有者明确要"改一改"。Word 可编辑，PDF 不可编辑；
两份文件并存还会制造"我到底改的哪个"的歧义，所以只出 Word，PDF 不做。

**谁是唯一事实源**：这份 `.docx`。`data/sticker_audit.md` 降级成机器生成的只读快照
（供 grep/diff 用），**不要手改 md，改 docx**——第二段的 apply 脚本读的是 docx。

排版要点：
- A4 横向；列 = 图 | 名称 | 画面 | 含义 | 适用场景 | 旧标注（对照）
- 名称列是**匹配键**，脚本靠它对回 index.json，请勿改动名称文字
- 含义栏塌成同一族的行**整行标黄**，让"必须你拍板"的行一眼可见
- 图片压到长边 320px 的 PNG（保留透明），单文件不超几 MB

用法（【本地电脑执行】PowerShell）：
    ./venv/Scripts/python.exe scripts/audit/build_sticker_review_docx.py           # 生成
    ./venv/Scripts/python.exe scripts/audit/build_sticker_review_docx.py --verify  # 回读自检
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

WORKDIR = "data/sticker_audit_workdir/stickers"
INDEX_FILE = os.path.join(WORKDIR, "index.json")
CACHE_FILE = "data/sticker_audit_cache.json"
DOCX_FILE = "data/sticker_audit.docx"
MD_FILE = "data/sticker_audit.md"
THUMB_DIR = "data/sticker_audit_thumbs"

# 列顺序 = 审核表列顺序（apply 脚本按此读）
COLS = ("图", "名称", "画面", "含义", "适用场景", "旧标注（对照）")
IDX_NAME, IDX_画面, IDX_含义, IDX_场景 = 1, 2, 3, 4

IMG_MAX_EDGE = 320
IMG_CM = 2.8
COL_WIDTH_CM = (3.3, 2.8, 5.2, 6.0, 5.4, 3.0)  # 合计 25.7cm，A4 横向可用宽度

MEANING_FAMILY_HEAD = 6
MEANING_FAMILY_MIN = 2

INSTRUCTION = """审核方法（3 分钟看完这段就能上手）

1. 这份 Word 就是审核表，第二到第五列直接点进去改字，改完保存、关掉，跟我说一声就行。
2. 名称（第 2 列）是脚本认表的钥匙，别动它。图片和名称都不用改。
3. 三栏判据：画面 = 图里有什么；含义 = 她发这张图在表达什么情绪或态度（最重要）；
   适用场景 = 什么聊天语境下合适。
4. 想放弃某一张：把"含义"那一格删空就行，脚本会跳过、保持原样，不会瞎猜。
5. 黄色底纹的行：模型给这几张的含义开头撞了车、区分不开，请按你当初收藏时的印象改写。
6. 最后一列是旧的自动标注，只作对照，发现它标错了就在心里记一下，不用改它。
"""


# ==========================================
# 图片处理
# ==========================================

def make_thumb(name: str, rel_file: str) -> Optional[str]:
    """把表情包压成长边 320px 的 PNG 缩略图，返回路径。GIF 取第一帧。"""
    src = os.path.join(WORKDIR, rel_file)
    if not os.path.exists(src):
        return None
    os.makedirs(THUMB_DIR, exist_ok=True)
    safe = re.sub(r"[^\w一-鿿.-]", "_", name)
    dst = os.path.join(THUMB_DIR, f"{safe}.png")
    try:
        with Image.open(src) as im:
            im.seek(0)  # 动图取第一帧：和打标时模型看到的是同一帧，所见即所标
            im = im.convert("RGBA")
            w, h = im.size
            m = max(w, h)
            if m > IMG_MAX_EDGE:
                s = IMG_MAX_EDGE / float(m)
                im = im.resize((max(1, int(w * s)), max(1, int(h * s))), Image.Resampling.LANCZOS)
            im.save(dst, "PNG", optimize=True)
        return dst
    except Exception as e:
        print(f"  [!] 缩略图生成失败 {name}: {e}")
        return None


# ==========================================
# 含义撞车检测（和 relabel_stickers.py 同规则）
# ==========================================

def meaning_families(values: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """名字 -> 撞车族 key。同一族内含义开头一样，模型区分不了，必须所有者拍板。"""
    buckets: Dict[str, List[str]] = defaultdict(list)
    for name, v in values.items():
        m = (v.get("含义") or "").strip()
        if not m:
            continue
        buckets[re.split(r"[，,、/；;]", m)[0].strip()[:MEANING_FAMILY_HEAD]].append(name)
    out: Dict[str, str] = {}
    for key, names in buckets.items():
        if len(names) >= MEANING_FAMILY_MIN:
            for n in names:
                out[n] = key
    return out


# ==========================================
# 生成 docx
# ==========================================

def _shade(cell, fill: str) -> None:
    """给单元格加底纹（Word 的底纹要走 XML，python-docx 没封装）。"""
    from docx.oxml.ns import qn
    from docx.oxml import OxmlElement

    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    cell._tc.get_or_add_tcPr().append(shd)


def _set_cn_font(doc, size_pt: float = 9) -> None:
    """给整份文档设中文字体，否则 Word 会拿默认西文字体渲染汉字，字距很难看。"""
    from docx.oxml.ns import qn
    style = doc.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = None
    rpr = style.element.get_or_add_rPr()
    rf = rpr.get_or_add_rFonts()
    rf.set(qn("w:eastAsia"), "微软雅黑")
    rf.set(qn("w:ascii"), "微软雅黑")
    rf.set(qn("w:hAnsi"), "微软雅黑")
    style.font.size = doc.styles["Normal"].font.size
    st = doc.styles["Normal"].paragraph_format
    st.space_after = 0
    st.space_before = 0
    _ = size_pt  # 字号交给各 run 显式设置


def build() -> int:
    if not os.path.exists(CACHE_FILE) or not os.path.exists(INDEX_FILE):
        print("[X] 缺缓存或 workdir，先跑 scripts/audit/relabel_stickers.py")
        return 2

    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        cache = json.load(f)
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    ok = {k: v["value"] for k, v in cache.items() if v.get("ok")}
    bad = {k: v.get("error", "") for k, v in cache.items() if not v.get("ok")}
    names = [n for n in sorted(index.keys()) if n in ok]
    fams = meaning_families(ok)

    from docx import Document
    from docx.enum.section import WD_ORIENT
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.shared import Cm, Pt

    doc = Document()
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = Cm(29.7), Cm(21.0)
    sec.left_margin = sec.right_margin = Cm(1.8)
    sec.top_margin = sec.bottom_margin = Cm(1.5)
    _set_cn_font(doc)

    # 标题 + 说明
    h = doc.add_paragraph()
    r = h.add_run("表情包三栏标注 · 所有者审核表")
    r.bold = True
    r.font.size = Pt(16)

    p = doc.add_paragraph()
    r = p.add_run(
        f"共 {len(names)} 张（已标注 {len(ok)}，待人工 {len(bad)}）　|　"
        f"黄色行 {len(fams)} 张需你拍板　|　来源：服务器生产表情包库（只读拉回）"
    )
    r.font.size = Pt(9)
    r.font.color.rgb = None

    for line in INSTRUCTION.strip().splitlines():
        pp = doc.add_paragraph()
        rr = pp.add_run(line)
        rr.font.size = Pt(9.5)
        if line.strip().startswith(("1.", "5.")):
            rr.bold = True

    if fams:
        pp = doc.add_paragraph()
        rr = pp.add_run("含义撞车点名：")
        rr.bold = True
        rr.font.size = Pt(9.5)
        for key in sorted(set(fams.values())):
            same = [n for n in names if fams.get(n) == key]
            rr2 = pp.add_run(f"「{key}」{'、'.join(same)}　")
            rr2.font.size = Pt(9.5)
            rr2.bold = True

    doc.add_paragraph()

    # 主表
    table = doc.add_table(rows=1, cols=len(COLS))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False

    hdr = table.rows[0].cells
    for i, c in enumerate(COLS):
        run = hdr[i].paragraphs[0].add_run(c)
        run.bold = True
        run.font.size = Pt(9.5)
        _shade(hdr[i], "D9D9D9")
        hdr[i].width = Cm(COL_WIDTH_CM[i])

    missing_img: List[str] = []
    for name in names:
        row = table.add_row()
        cells = row.cells

        # 图（新单元格本来就是空的，不要先 text="" —— 那会塞一个空 run 进去，
        # 所有者在 Word 里点进去改字时容易选到空 run 上，保存后留下奇怪格式）
        thumb = make_thumb(name, index[name].get("file", ""))
        if thumb:
            try:
                run = cells[0].paragraphs[0].add_run()
                run.add_picture(thumb, height=Cm(IMG_CM))
            except Exception as e:
                print(f"  [!] 插图失败 {name}: {e}")
                cells[0].text = "（图挂了）"
        else:
            missing_img.append(name)
            cells[0].text = "（找不到图）"

        vals = (
            name,
            ok[name].get("画面", ""),
            ok[name].get("含义", ""),
            ok[name].get("适用场景", ""),
            index[name].get("desc", ""),
        )
        for i, v in enumerate(vals, start=1):
            run = cells[i].paragraphs[0].add_run(v or "　")
            run.font.size = Pt(9)
            if i == IDX_NAME:
                run.bold = True

        for i, c in enumerate(cells):
            c.width = Cm(COL_WIDTH_CM[i])

        if name in fams:
            for c in cells:
                _shade(c, "FFF2A8")

    doc.save(DOCX_FILE)
    size_kb = os.path.getsize(DOCX_FILE) / 1024
    print(f"已生成 {DOCX_FILE}（{size_kb:.0f} KB，{len(names)} 行）")
    if missing_img:
        print(f"[!] 缺图 {len(missing_img)} 张：{missing_img}")
    print(f"含义撞车标黄 {len(fams)} 张：{'、'.join(sorted(fams))}")
    return 0


# ==========================================
# 回读自检（第二段 apply 脚本也用这个函数，保证格式一定读得回来）
# ==========================================

def read_review_docx(path: str = DOCX_FILE) -> Tuple[List[Dict[str, str]], List[str]]:
    """把审核 docx 读回 {名称: 画面/含义/适用场景}。返回 (行列表, 警告列表)。

    绝不猜：读不出来的行只进警告，不产出标注。
    """
    from docx import Document

    doc = Document(path)
    if not doc.tables:
        return [], ["docx 里没有表格，可能被所有者整段删掉了"]

    rows: List[Dict[str, str]] = []
    warns: List[str] = []
    tbl = doc.tables[0]
    for i, row in enumerate(tbl.rows):
        cells = row.cells
        if len(cells) < 5:
            warns.append(f"第 {i + 1} 行只有 {len(cells)} 列，跳过")
            continue
        name = cells[IDX_NAME].text.strip()
        if not name or name == "名称":
            continue  # 表头/空行
        rec = {
            "名称": name,
            "画面": cells[IDX_画面].text.strip(),
            "含义": cells[IDX_含义].text.strip(),
            "适用场景": cells[IDX_场景].text.strip(),
        }
        if not rec["含义"]:
            rec["_blank"] = "1"  # 所有者清空 = 放弃这张，保持原样
        rows.append(rec)
    return rows, warns


def verify() -> int:
    rows, warns = read_review_docx()
    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index = json.load(f)

    print(f"docx 读回 {len(rows)} 行 / index.json {len(index)} 键")
    got = {r["名称"] for r in rows}
    missing = [n for n in index if n not in got]
    extra = [n for n in got if n not in index]
    blank = [r["名称"] for r in rows if r.get("_blank")]
    print(f"  名称对不上 index 的：{missing if missing else '无'}")
    print(f"  index 里没有的多余行：{extra if extra else '无'}")
    print(f"  含义被清空(将保持原样)：{blank if blank else '无'}")
    for w in warns:
        print(f"  [!] {w}")
    ok = not missing and not extra and not warns
    print("回读自检：" + ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="只回读自检，不重新生成")
    args = ap.parse_args()
    sys.exit(verify() if args.verify else build())
