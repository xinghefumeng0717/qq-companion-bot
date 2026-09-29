"""SQLite 异步数据库连接与建表 (db.py)
使用 aiosqlite 管理单文件数据库 data/companion.db。
"""

from __future__ import annotations

from contextlib import asynccontextmanager
import json
import logging
import os
from datetime import datetime
from typing import Any, List, Optional, Tuple
import aiosqlite

logger = logging.getLogger(__name__)

TIME_FORMAT = "%Y-%m-%d %H:%M"

STATE_KEY_AFFECTION = "affection"
STATE_KEY_MOOD = "mood"
STATE_KEY_UNANSWERED_PROACTIVE = "unanswered_proactive"

COUNTER_KEY_TOTAL_TURNS = "total_turns"
COUNTER_KEY_ARCHIVED_TURNS = "archived_turns"


def now_str() -> str:
    """获取当前时间字符串 (%Y-%m-%d %H:%M)"""
    return datetime.now().strftime(TIME_FORMAT)


class Database:
    def __init__(self, db_path: str = "data/companion.db"):
        self.db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        self._in_transaction: bool = False

    async def connect(self) -> aiosqlite.Connection:
        if self._conn is None:
            dir_name = os.path.dirname(self.db_path)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row
        return self._conn

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @asynccontextmanager
    async def transaction(self):
        """异步事务上下文管理器，异常时自动回滚，正常退出时自动提交"""
        conn = await self.connect()
        if self._in_transaction:
            # 嵌套事务中直接复用
            yield conn
            return

        self._in_transaction = True
        try:
            yield conn
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise
        finally:
            self._in_transaction = False

    async def execute(self, sql: str, parameters: Tuple[Any, ...] | List[Any] = ()) -> aiosqlite.Cursor:
        conn = await self.connect()
        cursor = await conn.execute(sql, parameters)
        if not self._in_transaction:
            await conn.commit()
        return cursor

    async def executemany(self, sql: str, seq_of_parameters: List[Tuple[Any, ...]]) -> aiosqlite.Cursor:
        conn = await self.connect()
        cursor = await conn.executemany(sql, seq_of_parameters)
        if not self._in_transaction:
            await conn.commit()
        return cursor

    async def fetchone(self, sql: str, parameters: Tuple[Any, ...] | List[Any] = ()) -> Optional[aiosqlite.Row]:
        conn = await self.connect()
        async with conn.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def fetchall(self, sql: str, parameters: Tuple[Any, ...] | List[Any] = ()) -> List[aiosqlite.Row]:
        conn = await self.connect()
        async with conn.execute(sql, parameters) as cursor:
            return await cursor.fetchall()

    async def get_state_json(self, key: str, default: Optional[Any] = None) -> Optional[Any]:
        """读取 state 表中指定 key 的 JSON 字符串并反序列化，读取失败或异常时返回 default"""
        row = await self.fetchone("SELECT value FROM state WHERE key = ?", (key,))
        if not row or not row["value"]:
            return default
        try:
            return json.loads(row["value"])
        except Exception as e:
            logger.error(f"[Database] 解析 state[{key}] JSON 异常: {e}")
            return default

    async def set_state_json(self, key: str, value: Any) -> None:
        """将 value 序列化为 JSON 字符串写入 state 表中指定 key"""
        val_str = json.dumps(value, ensure_ascii=False)
        await self.execute(
            "INSERT OR REPLACE INTO state (key, value) VALUES (?, ?)",
            (key, val_str),
        )

    async def init_tables(self) -> None:
        """初始化全部数据表与默认计数器"""
        conn = await self.connect()
        await conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS turns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT,
                content TEXT,
                proactive INTEGER DEFAULT 0,
                has_image INTEGER DEFAULT 0,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS counters (
                key TEXT PRIMARY KEY,
                value INTEGER
            );

            CREATE TABLE IF NOT EXISTS diary (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                importance INTEGER,
                sentiment TEXT,
                recall_count INTEGER DEFAULT 0,
                created_at TEXT,
                last_recall_at TEXT
            );

            CREATE TABLE IF NOT EXISTS diary_archive (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                importance INTEGER,
                sentiment TEXT,
                recall_count INTEGER DEFAULT 0,
                created_at TEXT,
                last_recall_at TEXT
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT UNIQUE,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS followups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic TEXT,
                remind_after TEXT,
                done INTEGER DEFAULT 0,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS suppressed_desires (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS state (
                key TEXT PRIMARY KEY,
                value TEXT
            );

            CREATE TABLE IF NOT EXISTS milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stage INTEGER,
                reached_at TEXT
            );

            CREATE TABLE IF NOT EXISTS stickers (
                name TEXT PRIMARY KEY,
                file TEXT,
                desc TEXT,
                md5 TEXT UNIQUE,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS llm_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purpose TEXT,
                model TEXT,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cache_hit_tokens INTEGER DEFAULT 0,
                cache_miss_tokens INTEGER DEFAULT 0,
                cost_estimate REAL,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS observer_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                self_disclosure REAL,
                responsiveness REAL,
                warmth_score REAL,
                resonance REAL,
                created_at TEXT
            );
            """
        )
        await conn.commit()

        # 旧库迁移：llm_calls 补充缓存命中两列
        async with conn.execute("PRAGMA table_info(llm_calls)") as cur:
            existing_cols = {row[1] for row in await cur.fetchall()}
        for col in ("cache_hit_tokens", "cache_miss_tokens"):
            if col not in existing_cols:
                await conn.execute(f"ALTER TABLE llm_calls ADD COLUMN {col} INTEGER DEFAULT 0")
        await conn.commit()

        # 初始化计数器
        async with conn.execute("SELECT value FROM counters WHERE key = ?", (COUNTER_KEY_TOTAL_TURNS,)) as cur:
            row = await cur.fetchone()
            if row is None:
                await conn.execute("INSERT INTO counters (key, value) VALUES (?, 0)", (COUNTER_KEY_TOTAL_TURNS,))

        async with conn.execute("SELECT value FROM counters WHERE key = ?", (COUNTER_KEY_ARCHIVED_TURNS,)) as cur:
            row = await cur.fetchone()
            if row is None:
                await conn.execute("INSERT INTO counters (key, value) VALUES (?, 0)", (COUNTER_KEY_ARCHIVED_TURNS,))

        await conn.commit()
