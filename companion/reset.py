"""数据重置工具 (companion/reset.py)
一键清空关系数据，自动先备份到 data/backup/，从头开始相处。
用法:
  python -m companion.reset [--purge-all] [--yes]
"""

from __future__ import annotations

import argparse
from datetime import datetime
import os
import shutil
import sqlite3
import sys
from typing import Any, Dict, List, Optional

RELATIONSHIP_TABLES = [
    "turns",
    "diary",
    "diary_archive",
    "facts",
    "followups",
    "suppressed_desires",
    "observer_scores",
    "milestones",
    "state",
]


def reset_database(
    db_path: str = "data/companion.db",
    backup_dir: str = "data/backup",
    purge_all: bool = False,
) -> Dict[str, Any]:
    """执行数据库备份与重置，返回执行结果摘要"""
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"未找到数据库文件: {db_path}")

    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_filename = f"companion-{timestamp}.db"
    backup_path = os.path.join(backup_dir, backup_filename)

    # 1. 先完整备份数据库文件
    shutil.copy2(db_path, backup_path)

    # 2. 连接并清空各表
    conn = sqlite3.connect(db_path)
    c = conn.cursor()
    cleared_counts: Dict[str, int] = {}

    try:
        c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        existing_tables = {row[0] for row in c.fetchall()}

        for table in RELATIONSHIP_TABLES:
            if table in existing_tables:
                c.execute(f"SELECT COUNT(*) FROM {table}")
                cnt = c.fetchone()[0]
                c.execute(f"DELETE FROM {table}")
                cleared_counts[table] = cnt

        # counters 归零
        if "counters" in existing_tables:
            c.execute(
                "UPDATE counters SET value = 0 WHERE key IN ('total_turns', 'archived_turns')"
            )
            c.execute(
                "INSERT OR IGNORE INTO counters (key, value) VALUES ('total_turns', 0)"
            )
            c.execute(
                "INSERT OR IGNORE INTO counters (key, value) VALUES ('archived_turns', 0)"
            )

        if purge_all:
            for table in ["stickers", "llm_calls"]:
                if table in existing_tables:
                    c.execute(f"SELECT COUNT(*) FROM {table}")
                    cnt = c.fetchone()[0]
                    c.execute(f"DELETE FROM {table}")
                    cleared_counts[table] = cnt

        conn.commit()
    finally:
        conn.close()

    return {
        "backup_path": backup_path,
        "cleared_counts": cleared_counts,
        "counters_reset": ["total_turns", "archived_turns"],
        "purge_all": purge_all,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="QQ伴侣机器人 数据重置工具")
    parser.add_argument(
        "--purge-all",
        action="store_true",
        help="连同表情包资产 (stickers) 与计费历史 (llm_calls) 一并清空",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="跳过交互式二次确认",
    )
    parser.add_argument(
        "--db-path",
        default="data/companion.db",
        help="目标数据库文件路径（默认 data/companion.db）",
    )
    parser.add_argument(
        "--backup-dir",
        default="data/backup",
        help="备份保存目录（默认 data/backup）",
    )
    args = parser.parse_args()

    if not os.path.exists(args.db_path):
        print(f"❌ 数据库文件 {args.db_path} 不存在，无需重置。")
        return

    if not args.yes:
        print("\n" + "!" * 60)
        print("【警告】此操作将清空所有关系数据（聊天记录、好感度、心境、日记、记忆等）！")
        print("系统会在重置前将当前数据库完整备份到 data/backup/ 目录。")
        if args.purge_all:
            print("注意：已指定 --purge-all，表情包与计费历史也将被一并清除！")
        print("!" * 60)
        confirm = input("\n请输入大写 'YES' 确认执行重置: ").strip()
        if confirm != "YES":
            print("操作已取消。")
            return

    try:
        res = reset_database(
            db_path=args.db_path,
            backup_dir=args.backup_dir,
            purge_all=args.purge_all,
        )
        print("\n" + "=" * 55)
        print("✨ 数据重置成功！✨")
        print(f"📦 备份文件: {res['backup_path']}")
        print("🧹 已清空表及数据行数:")
        for tbl, cnt in res["cleared_counts"].items():
            print(f"  • {tbl:<20}: {cnt} 行已清除")
        print("🔄 计数器归零: total_turns=0, archived_turns=0")
        print("💡 好感度与情绪状态已清空，下次交互时将从角色卡默认初始值重建。")
        print("=" * 55 + "\n")
    except Exception as e:
        print(f"\n❌ 重置失败: {e}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
