"""FIXES17 表情包标注应用入库 (scripts/audit/apply_sticker_audit.py)

把所有者审核过的 `data/sticker_audit.docx`（带图，唯一事实源）写回
`data/sticker_audit_workdir/stickers/index.json`。

**只改本地 workdir 里那份 index.json**（= 将来随第二次 reset 一起同步的新种子库）。
服务器一个字都不碰（任务书负面清单第 1 条）；生产 DB 的清理由部署时另做，
原因见交付摘要"部署时必须做的一件事"。

**绝不猜**：docx 里读不出来的行、名称对不上的行、含义被清空的行，
一律保持原样并进报告，绝不自己补。

**可回退**：写回前自动备份成 `index.backup-<时间戳>.json`；表情包图片文件一张不动。
所以"删掉某张"= 只从索引里移除，随时能加回来。

用法（【本地电脑执行】PowerShell）：
    ./venv/Scripts/python.exe scripts/audit/apply_sticker_audit.py --dry-run   # 先看会改什么
    ./venv/Scripts/python.exe scripts/audit/apply_sticker_audit.py             # 真写
    ./venv/Scripts/python.exe scripts/audit/apply_sticker_audit.py --sync-card  # 同步到角色卡目录

`--sync-card`：把 workdir 的 index.json + 图片同步到 `characters/qingzi/stickers/`
（部署手册 0.2 节：角色卡目录里的 stickers/ 才是长期归宿，部署时同步到服务器）。
workdir 里的 58 张图会一并带过去：其中 15 张已从索引移除但**图片保留**，
这样"删掉某张"随时能加回来。同步前自动备份原 index.json。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from scripts.audit.build_sticker_review_docx import read_review_docx  # noqa: E402

INDEX_FILE = "data/sticker_audit_workdir/stickers/index.json"
DOCX_FILE = "data/sticker_audit.docx"
REPORT_FILE = "data/apply_sticker_audit_report.json"

# ==========================================
# 所有者 2026-10-04 18:08 拍板的三件事（原话记在这里，进 git 可追溯）
# ==========================================

# 决定一：「把这 15 张从表情包库里彻底删掉」
# ⚠ 这与任务书负面清单「不删任何现有表情包」冲突，执行模型在选项里已明示，
#   所有者仍选此项，按所有者口径执行。仅从 index.json 移除，磁盘图片不删。
DELETE_STICKERS = [
    "你不要过来呀", "吃东西", "吃惊", "听我说谢谢你", "干饭", "气鼓鼓",
    "没生气哦", "浙大菲比", "绫华优雅", "绫华微笑", "菲比乖巧",
    "蓝发厨娘", "让我康康", "赞", "馋了",
]

# 决定二：「剥掉括号里的讨论，只把干净可用那句话入库，我另出一份问题清单回复你」
# 所有者那 5 段话里，括号/逗号后面的是**跟执行模型的讨论**（疑问、推测、"应该不是"），
# 不是标注定稿，不能进生产库当语义依据。
# 安全性：已核验全库 116 个含义/适用场景单元格里**零括号**，此规则不会误伤别的行。
STRIP_TRAILING_NOTE = True

# 决定三：「挪到含义栏，适用场景我另外想」+ 猫猫探头原话没写完
# 这两张没法用通用规则处理（一个是栏位写错，一个是内容不完整），单独显式指定。
OWNER_OVERRIDES: Dict[str, Dict[str, str]] = {
    # 她把含义写在了「适用场景」栏。按决定三挪到含义栏，适用场景留空等她补。
    "冰雕小猫": {
        "meaning": "保持一种淡定的，佛系的态度",
        "usage": "",
        "_note": "原话写在适用场景栏，按决定三挪到含义栏；适用场景留空待所有者补",
    },
    # 她写的「这个代表冒泡的意思其实是」没写完，usage 留空，含义保留模型原标注。
    "猫猫探头": {
        "usage": "",
        "_note": "所有者原话未写完，usage 留空待补；含义沿用模型标注",
    },
}

# 括号讨论的剥离：匹配结尾的（全角/半角）括号块，以及「用，（…」这种逗号后接括号的情形
_NOTE_TAIL = re.compile(r"[（(][^（()）]*[）)]\s*$")
_TRAILING_COMMA = re.compile(r"[，,、;；]\s*$")


def strip_note(text: str) -> Tuple[str, bool]:
    """剥掉结尾的括号讨论块。返回 (清洗后文本, 是否发生了清洗)。"""
    if not STRIP_TRAILING_NOTE:
        return text, False
    s = text.strip()
    changed = False
    while True:
        new = _NOTE_TAIL.sub("", s)
        if new == s:
            break
        s, changed = new.strip(), True
    s2 = _TRAILING_COMMA.sub("", s).strip()
    if s2 != s:
        s, changed = s2, True
    return s, changed


def sync_to_card(workdir_dir: str, stamp: str) -> int:
    """把 workdir 的 index.json + 图片同步到 characters/qingzi/stickers/。

    部署手册 0.2 节：角色卡目录里的 stickers/（含 index.json 和图片）才是长期归宿，
    部署时整体同步到服务器。workdir 里的 58 张图一并带过去（15 张已从索引移除但
    图片保留 → 删错了能加回来）。原 index.json 先备份。
    """
    import shutil

    card_dir = "characters/qingzi/stickers"
    os.makedirs(card_dir, exist_ok=True)

    src_index = os.path.join(workdir_dir, "index.json")
    dst_index = os.path.join(card_dir, "index.json")
    if os.path.exists(dst_index):
        bak = os.path.join(card_dir, f"index.backup-{stamp}.json")
        shutil.copy2(dst_index, bak)
        print(f"已备份原角色卡 index → {bak}")

    copied = 0
    for fn in os.listdir(workdir_dir):
        if fn == "index.json" or fn.startswith("index.backup-"):
            continue
        src = os.path.join(workdir_dir, fn)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(card_dir, fn)
        if not os.path.exists(dst) or os.path.getsize(src) != os.path.getsize(dst):
            shutil.copy2(src, dst)
            copied += 1
    shutil.copy2(src_index, dst_index)

    with open(dst_index, "r", encoding="utf-8") as f:
        new_keys = json.load(f)
    files = {v.get("file", "") for v in new_keys.values()}
    missing = [k for k, v in new_keys.items() if not os.path.exists(os.path.join(card_dir, v.get("file", "")))]
    print(f"已同步到 {card_dir}：{copied} 张图更新/新增，index.json 共 {len(new_keys)} 键")
    if missing:
        print(f"[X] 索引指向但卡目录里没有图：{missing}")
        return 1
    print(f"✓ {len(files)} 个被引用文件全部就位")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告不写回")
    ap.add_argument("--sync-card", action="store_true", help="同步到 characters/qingzi/stickers/")
    args = ap.parse_args()

    if not os.path.exists(DOCX_FILE):
        print(f"[X] 找不到 {DOCX_FILE}（所有者还没审，或已被移动）")
        return 2
    if not os.path.exists(INDEX_FILE):
        print(f"[X] 找不到 {INDEX_FILE}，先做任务 0：拉回生产表情包库")
        return 2

    with open(INDEX_FILE, "r", encoding="utf-8") as f:
        index: Dict[str, Dict[str, str]] = json.load(f)
    before_keys = set(index.keys())

    rows, warns = read_review_docx(DOCX_FILE)
    docx_map = {r["名称"]: r for r in rows}

    new_index: Dict[str, Dict[str, str]] = {}
    added: List[str] = []
    deleted: List[str] = []
    skipped_blank: List[str] = []
    not_in_docx: List[str] = []
    stripped: List[Dict[str, str]] = []
    unknown_names = [n for n in docx_map if n not in index]

    for name, entry in index.items():  # 保持原顺序
        # 决定一：删除
        if name in DELETE_STICKERS:
            deleted.append(name)
            continue

        rec = docx_map.get(name)
        if rec is None:
            # docx 里没这一行（所有者删了行）→ 不猜，保持原样
            not_in_docx.append(name)
            new_index[name] = dict(entry)
            continue

        meaning = (rec.get("含义") or "").strip()
        usage = (rec.get("适用场景") or "").strip()

        ov = OWNER_OVERRIDES.get(name)
        if ov:
            if "meaning" in ov:
                meaning = ov["meaning"].strip()
            if "usage" in ov:
                usage = ov["usage"].strip()

        meaning, m_ch = strip_note(meaning)
        usage, u_ch = strip_note(usage)
        if m_ch or u_ch:
            stripped.append({"名称": name, "含义": meaning, "适用场景": usage})

        # 含义被清空 = 所有者放弃这张的新标注 → 保持原样，不加 meaning
        if not meaning:
            skipped_blank.append(name)
            new_index[name] = dict(entry)
            continue

        # 任务书：desc 保留旧画面描述（兼容），新增 meaning/usage
        new_entry: Dict[str, str] = {"file": entry.get("file", "")}
        new_entry["desc"] = entry.get("desc", name)
        new_entry["meaning"] = meaning
        new_entry["usage"] = usage
        new_index[name] = new_entry
        added.append(name)

    # ---------------- 报告 ----------------
    print("=" * 68)
    print(f"删除 {len(deleted)} 张 | 写入新标注 {len(added)} 张 | "
          f"跳过(含义为空) {len(skipped_blank)} 张 | 跳过(docx 无此行) {len(not_in_docx)} 张")
    print(f"索引键数：{len(before_keys)} -> {len(new_index)}")
    print("=" * 68)

    if deleted:
        print("\n【删除】")
        for n in deleted:
            print(f"  - {n}")
    if skipped_blank:
        print("\n【跳过·含义被清空，保持原标注原样】")
        print("  " + "、".join(skipped_blank))
    if not_in_docx:
        print("\n【跳过·docx 里没有这一行】")
        print("  " + "、".join(not_in_docx))
    if stripped:
        print("\n【已剥离括号里的讨论（不入库）】")
        for s in stripped:
            print(f"  {s['名称']}: 含义=「{s['含义']}」 场景=「{s['适用场景']}」")
    for name, ov in OWNER_OVERRIDES.items():
        if name in before_keys:
            print(f"\n【所有者例外处理】{name}: {ov['_note']}")
    if unknown_names:
        print("\n【docx 里有、索引里没有的名称——未处理】")
        print("  " + "、".join(unknown_names))
    if warns:
        print("\n【解析警告】")
        for w in warns:
            print(f"  [!] {w}")

    if args.dry_run:
        print("\n>>> DRY-RUN：没有写任何文件。去掉 --dry-run 才真写。")
        return 0

    # ---------------- 备份 + 写回 ----------------
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(os.path.dirname(INDEX_FILE), f"index.backup-{stamp}.json")
    with open(backup, "w", encoding="utf-8") as f:
        json.dump(index, f, ensure_ascii=False, indent=2)

    with open(INDEX_FILE, "w", encoding="utf-8") as f:
        json.dump(new_index, f, ensure_ascii=False, indent=2)

    report = {
        "applied_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "backup": backup,
        "index": INDEX_FILE,
        "keys_before": len(before_keys),
        "keys_after": len(new_index),
        "deleted": deleted,
        "written": added,
        "skipped_blank_meaning": skipped_blank,
        "skipped_not_in_docx": not_in_docx,
        "stripped_notes": stripped,
        "owner_overrides_applied": {k: v["_note"] for k, v in OWNER_OVERRIDES.items() if k in before_keys},
        "parse_warnings": warns,
        "unknown_names_in_docx": unknown_names,
    }
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n已写回 {INDEX_FILE}")
    print(f"备份   {backup}")
    print(f"报告   {REPORT_FILE}")

    if args.sync_card:
        print("\n--- 同步到角色卡目录 ---")
        rc = sync_to_card(os.path.dirname(INDEX_FILE), stamp)
        report["synced_to_card_dir"] = "characters/qingzi/stickers"
        with open(REPORT_FILE, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        if rc != 0:
            return rc
    return 0


if __name__ == "__main__":
    sys.exit(main())
