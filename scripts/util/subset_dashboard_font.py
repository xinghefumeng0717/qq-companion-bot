"""生成手机看板用的小体积楷体子集（scripts/util/subset_dashboard_font.py）

从完整 GB2312 字体里只挑看板真正会渲染的字：ASCII 可打印字符、relative_day 产出的
相对日期词、固定装饰字，以及指定角色卡的 name / stages[*].name。输出仍是 woff2，
原完整字体保留作缺字回退。用法:
  python scripts/util/subset_dashboard_font.py --card characters/example
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable, List, Set, Tuple

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]

DEFAULT_FONT = ROOT / "companion" / "assets" / "lxgw-wenkai-gb2312.woff2"
DEFAULT_OUTPUT = ROOT / "companion" / "assets" / "lxgw-wenkai-dashboard.woff2"
DEFAULT_CARD = ROOT / "characters" / "example"

MAX_BYTES = 250 * 1024

# relative_day（companion/datefmt.py）会拼出的全部汉字；年月日号用于兜底原样日期
RELATIVE_DAY_CHARS = "今昨前明后天大约周月个年月日号"

# 看板固定装饰字
DECORATIVE_CHARS = "印象忆"


def load_card_chars(card_dir: Path) -> Set[str]:
    """读取角色卡的 name 与 stages[*].name，其余字段一律不碰"""
    card_path = card_dir / "character.json"
    if not card_path.is_file():
        raise FileNotFoundError(f"找不到角色卡: {card_path}")

    with card_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    chars: Set[str] = set()

    name = data.get("name")
    if isinstance(name, str):
        chars.update(name)

    stages = data.get("stages")
    if isinstance(stages, list):
        for stage in stages:
            if isinstance(stage, dict) and isinstance(stage.get("name"), str):
                chars.update(stage["name"])

    return chars


def build_charset(card_dir: Path) -> Set[str]:
    chars = {chr(code) for code in range(0x20, 0x7F)}
    chars.update(RELATIVE_DAY_CHARS)
    chars.update(DECORATIVE_CHARS)
    chars.update(load_card_chars(card_dir))
    return chars


def subset_font(font_path: Path, output_path: Path, chars: Iterable[str]) -> int:
    text = "".join(sorted(chars))

    font = TTFont(str(font_path))
    font.recalcTimestamp = False
    try:
        options = subset.Options()
        options.flavor = "woff2"
        options.with_zopfli = False
        options.drop_tables += ["EBDT", "EBLC", "EBSC", "SVG "]
        options.name_IDs = ["*"]
        options.name_legacy = True

        subsetter = subset.Subsetter(options=options)
        subsetter.populate(text=text)
        subsetter.subset(font)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")
        try:
            font.save(str(tmp_path))
            os.replace(tmp_path, output_path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
    finally:
        font.close()

    return output_path.stat().st_size


def verify(font_path: Path, output_path: Path, chars: Set[str]) -> Tuple[List[str], List[str]]:
    """确认是有效 wOF2、所需字符全在 cmap 里；返回 (输出缺字, 源字体缺字)"""
    with output_path.open("rb") as f:
        if f.read(4) != b"wOF2":
            raise ValueError(f"输出不是 wOF2: {output_path}")

    source = TTFont(str(font_path))
    try:
        source_cmap = source.getBestCmap() or {}
    finally:
        source.close()

    out = TTFont(str(output_path))
    try:
        out_cmap = out.getBestCmap() or {}
        if out.flavor != "woff2":
            raise ValueError(f"输出 flavor 不是 woff2: {out.flavor!r}")
    finally:
        out.close()

    missing = [ch for ch in sorted(chars) if ord(ch) not in out_cmap]
    absent = [ch for ch in sorted(chars) if ord(ch) not in source_cmap]
    return missing, absent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成手机看板用的楷体子集 woff2")
    parser.add_argument("--card", type=Path, default=DEFAULT_CARD, help="角色卡目录（读 name / stages）")
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT, help="源字体 woff2")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="输出字体 woff2")
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    chars = build_charset(args.card)
    size = subset_font(args.font, args.output, chars)
    missing, absent_in_source = verify(args.font, args.output, chars)

    if absent_in_source:
        print(f"源字体缺少 {len(absent_in_source)} 个字符: {''.join(absent_in_source)}")

    print(f"字符数: {len(chars)}")
    print(f"输出: {args.output} ({size} 字节, {size / 1024:.1f} KB)")

    if missing:
        print(f"缺字 {len(missing)} 个: {''.join(missing)}")
        sys.exit(1)

    if size > MAX_BYTES:
        print(f"输出超过目标上限 {MAX_BYTES} 字节")
        sys.exit(1)

    print("校验通过: wOF2 有效，所需字符均在 cmap 中")


if __name__ == "__main__":
    main()
