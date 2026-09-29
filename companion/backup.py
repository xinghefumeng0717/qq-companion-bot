"""应用内每日备份模块 (companion/backup.py)
不依赖系统 cron，在伴侣机器人进程内实现定时在线备份，保留最近 14 份并更新 latest.db。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import logging
import os
import re
import shutil
import sqlite3
from typing import List, Optional

from companion.db import TIME_FORMAT

logger = logging.getLogger(__name__)

BACKUP_PATTERN = re.compile(r"^companion-(\d{8}-\d{4})\.db$")


def run_daily_backup(
    db_path: str = "data/companion.db",
    backup_dir: str = "data/backup/daily",
    max_keep: int = 14,
) -> str:
    """执行在线数据库备份：
    1. 复制到 data/backup/daily/companion-YYYYMMDD-HHMM.db
    2. 同步覆盖一份到 data/backup/daily/latest.db
    3. 自动清理多余备份，保留最近 max_keep 份（默认 14 份）
    返回生成的备份绝对或相对路径。
    """
    if not os.path.exists(db_path):
        raise FileNotFoundError(f"源数据库不存在: {db_path}")

    os.makedirs(backup_dir, exist_ok=True)
    now = datetime.now()
    backup_name = f"companion-{now.strftime('%Y%m%d-%H%M')}.db"
    target_path = os.path.join(backup_dir, backup_name)
    latest_path = os.path.join(backup_dir, "latest.db")

    try:
        # 1. 优先使用 SQLite 在线备份 API (安全一致的快照)
        src_conn = sqlite3.connect(db_path)
        dst_conn = sqlite3.connect(target_path)
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
    except Exception as e:
        logger.warning(f"[Backup] SQLite 在线备份出错，回退到文件复制: {e}")
        shutil.copy2(db_path, target_path)

    # 2. 复制为 latest.db 供外部固定路径拉取
    shutil.copy2(target_path, latest_path)

    # 3. 轮转保留最近 max_keep 份（排除 latest.db）
    daily_files: List[str] = []
    for fname in os.listdir(backup_dir):
        if fname != "latest.db" and BACKUP_PATTERN.match(fname):
            daily_files.append(fname)

    # 按文件名升序排序（时间顺序）
    daily_files.sort()

    if len(daily_files) > max_keep:
        to_delete = daily_files[:-max_keep]
        for old_file in to_delete:
            old_path = os.path.join(backup_dir, old_file)
            try:
                os.remove(old_path)
                logger.info(f"[Backup] 已清理超期备份文件: {old_path}")
            except Exception as e:
                logger.warning(f"[Backup] 清理超期备份失败 {old_path}: {e}")

    logger.info(
        f"[Backup] 每日备份成功: {target_path} (已同步到 {latest_path}，当前保留 {min(len(daily_files), max_keep)} 份)"
    )
    return target_path


def get_last_backup_time(backup_dir: str = "data/backup/daily") -> Optional[str]:
    """获取最后一次备份的时间字符串，格式为 'YYYY-MM-DD HH:MM'"""
    if not os.path.exists(backup_dir):
        return None

    daily_files: List[str] = []
    for fname in os.listdir(backup_dir):
        if fname != "latest.db" and BACKUP_PATTERN.match(fname):
            daily_files.append(fname)

    if not daily_files:
        latest_file = os.path.join(backup_dir, "latest.db")
        if os.path.exists(latest_file):
            mtime = os.path.getmtime(latest_file)
            return datetime.fromtimestamp(mtime).strftime(TIME_FORMAT)
        return None

    daily_files.sort()
    latest_name = daily_files[-1]
    match = BACKUP_PATTERN.match(latest_name)
    if match:
        raw_ts = match.group(1)  # 20260930-0417
        try:
            dt = datetime.strptime(raw_ts, "%Y%m%d-%H%M")
            return dt.strftime(TIME_FORMAT)
        except ValueError:
            pass

    file_path = os.path.join(backup_dir, latest_name)
    mtime = os.path.getmtime(file_path)
    return datetime.fromtimestamp(mtime).strftime(TIME_FORMAT)


class DailyBackupScheduler:
    """应用内备份调度器：每天 04:17 自动备份，启动时若当天尚未备份则补一次"""

    def __init__(
        self,
        db_path: str = "data/companion.db",
        backup_dir: str = "data/backup/daily",
        target_hour: int = 4,
        target_minute: int = 17,
        max_keep: int = 14,
    ):
        self.db_path = db_path
        self.backup_dir = backup_dir
        self.target_hour = target_hour
        self.target_minute = target_minute
        self.max_keep = max_keep
        self._task: Optional[asyncio.Task] = None
        self._running = False

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            f"[Backup] 每日备份调度器已启动，目标时间: 每天 {self.target_hour:02d}:{self.target_minute:02d}"
        )

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        logger.info("[Backup] 每日备份调度器已停止")

    async def _run_loop(self) -> None:
        # 1. 启动检查：若当天尚未备份且数据库存在，先补一次
        await self._check_and_run_startup_backup()

        # 2. 定时主循环：每天 target_hour:target_minute 执行
        while self._running:
            try:
                now = datetime.now()
                target_dt = now.replace(
                    hour=self.target_hour,
                    minute=self.target_minute,
                    second=0,
                    microsecond=0,
                )
                if target_dt <= now:
                    target_dt += timedelta(days=1)

                seconds_to_wait = (target_dt - now).total_seconds()
                logger.info(
                    f"[Backup] 下次每日备份时间: {target_dt.strftime('%Y-%m-%d %H:%M:%S')} (等待 {seconds_to_wait:.0f} 秒)"
                )
                await asyncio.sleep(seconds_to_wait)

                if self._running and os.path.exists(self.db_path):
                    run_daily_backup(self.db_path, self.backup_dir, self.max_keep)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"[Backup] 每日定时备份异常: {e}")
                # 异常后稍作等待避免紧凑死循环
                await asyncio.sleep(60)

    async def _check_and_run_startup_backup(self) -> None:
        if not os.path.exists(self.db_path):
            return

        today_prefix = f"companion-{datetime.now().strftime('%Y%m%d')}-"
        has_today_backup = False
        if os.path.exists(self.backup_dir):
            for f in os.listdir(self.backup_dir):
                if f.startswith(today_prefix) and f.endswith(".db"):
                    has_today_backup = True
                    break

        if not has_today_backup:
            try:
                logger.info("[Backup] 启动检测：当天尚未执行备份，立即补做一次每日备份...")
                run_daily_backup(self.db_path, self.backup_dir, self.max_keep)
            except Exception as e:
                logger.warning(f"[Backup] 启动补做备份失败: {e}")
