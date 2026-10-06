"""本地 SQLite 缓存。

用途（M1）：
1. 缓存最近一次同步的任务快照，断网时界面仍可展示；
2. 记录 last_sync 时间戳（M2 增量同步会用到）；
3. 记录已触发的提醒，防止重启后重复提醒。

注意：本地缓存不是事实源，飞书多维表格才是。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from ..feishu.repository import Task


class LocalCache:
    """任务本地缓存（线程安全由调用方保证；SQLite 连接 check_same_thread=False）。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._init_tables()

    def _init_tables(self) -> None:
        """初始化缓存表结构。"""
        cur = self._conn.cursor()
        # 任务快照
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS task_cache (
                record_id   TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                cached_at   TEXT NOT NULL
            )
            """
        )
        # 简单键值元数据（如 last_sync）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS sync_meta (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # 已触发提醒记录（幂等）
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS reminder_log (
                reminder_key TEXT PRIMARY KEY,
                fired_at     TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # 任务缓存
    # ------------------------------------------------------------------
    def replace_tasks(self, tasks: list[Task]) -> None:
        """用最新任务列表整体替换本地快照。"""
        now_iso = datetime.now().isoformat(timespec="seconds")
        cur = self._conn.cursor()
        cur.execute("DELETE FROM task_cache")
        cur.executemany(
            "INSERT INTO task_cache(record_id, payload, cached_at) VALUES (?,?,?)",
            [
                (
                    task.record_id,
                    json.dumps(asdict(task), ensure_ascii=False, default=_json_default),
                    now_iso,
                )
                for task in tasks
            ],
        )
        self._conn.commit()

    def load_tasks(self) -> list[Task]:
        """读取本地缓存的全部任务。"""
        cur = self._conn.execute("SELECT payload FROM task_cache")
        tasks: list[Task] = []
        for (payload,) in cur.fetchall():
            data = json.loads(payload)
            # datetime 字段从 ISO 字符串还原
            for key in ("due_at", "start_at"):
                value = data.get(key)
                data[key] = datetime.fromisoformat(value) if value else None
            tasks.append(Task(**data))
        return tasks

    # ------------------------------------------------------------------
    # 元数据
    # ------------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        """写入元数据。"""
        self._conn.execute(
            "INSERT INTO sync_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self._conn.commit()

    def get_meta(self, key: str, default: str = "") -> str:
        """读取元数据。"""
        cur = self._conn.execute("SELECT value FROM sync_meta WHERE key=?", (key,))
        row = cur.fetchone()
        return row[0] if row else default

    # ------------------------------------------------------------------
    # 提醒去重
    # ------------------------------------------------------------------
    def is_reminder_fired(self, reminder_key: str) -> bool:
        """该提醒是否已经触发过。"""
        cur = self._conn.execute(
            "SELECT 1 FROM reminder_log WHERE reminder_key=?", (reminder_key,)
        )
        return cur.fetchone() is not None

    def mark_reminder_fired(self, reminder_key: str) -> None:
        """记录已触发的提醒。"""
        self._conn.execute(
            "INSERT OR IGNORE INTO reminder_log(reminder_key, fired_at) VALUES(?,?)",
            (reminder_key, datetime.now().isoformat(timespec="seconds")),
        )
        self._conn.commit()

    def close(self) -> None:
        """关闭数据库连接。"""
        self._conn.close()


def _json_default(obj):
    """json 序列化兜底：datetime 转为 ISO 字符串。"""
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"不可序列化的类型: {type(obj)}")
