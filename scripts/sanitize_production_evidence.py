"""把生产日志整理成可提交的脱敏证据包（只读原始文件，输出新文件）。"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REDACTIONS = [
    (re.compile(r"(?i)(token=)[^&\s]+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"), r"\1[REDACTED]"),
    (re.compile(r"sk-[A-Za-z0-9_-]+"), "[API_KEY]"),
    (re.compile(r"(?i)((?:api[_-]?key|access[_-]?token|admin[_-]?token)\s*[:=]\s*)\S+"), r"\1[REDACTED]"),
    (re.compile(r"\(\d{6,12}\)"), "([QQ_ID])"),
    (re.compile(r"(?i)(user_id|bot_qq|allowed_user_id)(\s*[:=]\s*)\d+"), r"\1\2[QQ_ID]"),
    # NapCat 的输入状态等 Notice 行会直接打印裸 QQ 号（2026-10-25 复查时实测漏过）
    (re.compile(r"(\[输入状态\]\s*)\d+"), r"\1[QQ_ID]"),
    (re.compile(r"(?i)(message_id\s*=\s*)\d+"), r"\1[MSG_ID]"),
    # 兜底：任何裸的 10~11 位数字（时间戳按短分组、不会被这条命中）
    (re.compile(r"(?<!\d)\d{10,11}(?!\d)"), "[QQ_ID]"),
]

SECRET_PATTERNS = [
    re.compile(r"(?i)token=(?!\[REDACTED\])"),
    re.compile(r"(?i)bearer\s+(?!\[REDACTED\])"),
    re.compile(r"sk-[A-Za-z0-9_-]+"),
    re.compile(r"\(\d{6,12}\)"),
    re.compile(r"(?<!\d)\d{10,11}(?!\d)"),
]

def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text

def write_redacted_file(source: Path, target: Path) -> None:
    raw = source.read_text(encoding="utf-8", errors="replace")
    # 行尾空格对证据没有意义，去掉后提交 diff 才干净
    text = "\n".join(line.rstrip() for line in redact(raw).splitlines())
    target.write_text(text + "\n", encoding="utf-8", newline="\n")

def export_turns(source: Path, target: Path) -> None:
    import sqlite3
    con = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        rows = [
            {"id": row[0], "role": row[1], "content": redact(row[2] or ""),
             "proactive": row[3], "has_image": row[4], "created_at": row[5]}
            for row in con.execute("SELECT id, role, content, proactive, has_image, created_at FROM turns ORDER BY id")
        ]
    finally:
        con.close()
    target.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="data/production/20261025_1115", type=Path)
    parser.add_argument("--output", default="docs/evidence/20261025_1115", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    write_redacted_file(args.input / "bot.log", args.output / "bot.redacted.log")
    write_redacted_file(args.input / "napcat.log", args.output / "napcat.redacted.log")
    export_turns(args.input / "companion.db", args.output / "turns.redacted.json")
    (args.output / "README.md").write_text(
        "# 生产证据包（脱敏）\n\n"        "来源：data/production/20261025_1115/。bot.log 与 napcat.log 做令牌、API Key、QQ 号脱敏；"        "turns.redacted.json 只导出 turns 表的对话字段，不包含原始 companion.db 或其他状态表。\n"        "原始文件仍只保留在本机，不上传。\n",
        encoding="utf-8", newline="\n")
    for path in args.output.glob("*.log"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                raise RuntimeError(f"脱敏检查失败：{path} 命中 {pattern.pattern}")
    print(f"脱敏证据包已写入：{args.output}")

if __name__ == "__main__":
    main()
