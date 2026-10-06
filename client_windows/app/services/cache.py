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
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..feishu.repository import Task


@dataclass
class PendingOp:
    """离线队列中的一条待推送操作。"""

    op_id: int
    op_type: str           # create / complete / defer
    payload: dict[str, Any]
    force: bool = False    # 冲突确认后置 True，推送时跳过冲突检测
    created_at: str = ""


class LocalCache:
    """任务本地缓存（SQLite 连接 check_same_thread=False，内部加锁保证线程安全）。"""

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        # 写操作可能来自后台线程与 UI 线程，这里统一加锁
        self._lock = threading.Lock()
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
        # 离线操作队列（M2）：断网时的写操作按序补交
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_ops (
                op_id      INTEGER PRIMARY KEY AUTOINCREMENT,
                op_type    TEXT NOT NULL,
                payload    TEXT NOT NULL,
                force      INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
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
        with self._lock:
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

    def upsert_tasks(self, tasks: list[Task]) -> None:
        """增量合并：按 record_id 覆盖已有快照，不删除未出现的记录。"""
        if not tasks:
            return
        now_iso = datetime.now().isoformat(timespec="seconds")
        with self._lock:
            self._conn.executemany(
                "INSERT INTO task_cache(record_id, payload, cached_at) VALUES (?,?,?) "
                "ON CONFLICT(record_id) DO UPDATE SET "
                "payload=excluded.payload, cached_at=excluded.cached_at",
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

    def remove_tasks(self, record_ids: list[str]) -> None:
        """按 record_id 删除缓存记录（云端已删除时调用）。"""
        if not record_ids:
            return
        with self._lock:
            self._conn.executemany(
                "DELETE FROM task_cache WHERE record_id=?",
                [(rid,) for rid in record_ids],
            )
            self._conn.commit()

    def load_tasks(self) -> list[Task]:
        """读取本地缓存的全部任务。"""
        with self._lock:
            cur = self._conn.execute("SELECT payload FROM task_cache")
            rows = cur.fetchall()
        tasks: list[Task] = []
        for (payload,) in rows:
            data = json.loads(payload)
            # datetime 字段从 ISO 字符串还原
            for key in ("due_at", "start_at", "completed_at", "modified_at"):
                value = data.get(key)
                data[key] = datetime.fromisoformat(value) if value else None
            tasks.append(Task(**data))
        return tasks

    # ------------------------------------------------------------------
    # 离线操作队列（M2）
    # ------------------------------------------------------------------
    def enqueue_op(self, op_type: str, payload: dict[str, Any], force: bool = False) -> int:
        """写入一条待推送操作，返回 op_id。"""
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO pending_ops(op_type, payload, force, created_at) "
                "VALUES (?,?,?,?)",
                (
                    op_type,
                    json.dumps(payload, ensure_ascii=False),
                    1 if force else 0,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            self._conn.commit()
            return int(cur.lastrowid)

    def list_pending_ops(self) -> list[PendingOp]:
        """按入队顺序列出全部待推送操作。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT op_id, op_type, payload, force, created_at "
                "FROM pending_ops ORDER BY op_id"
            )
            rows = cur.fetchall()
        return [
            PendingOp(
                op_id=row[0],
                op_type=row[1],
                payload=json.loads(row[2]),
                force=bool(row[3]),
                created_at=row[4],
            )
            for row in rows
        ]

    def delete_op(self, op_id: int) -> None:
        """推送成功（或用户放弃）后删除该操作。"""
        with self._lock:
            self._conn.execute("DELETE FROM pending_ops WHERE op_id=?", (op_id,))
            self._conn.commit()

    def set_op_force(self, op_id: int) -> None:
        """用户确认覆盖云端后置 force=1，重新推送时跳过冲突检测。"""
        with self._lock:
            self._conn.execute(
                "UPDATE pending_ops SET force=1 WHERE op_id=?", (op_id,)
            )
            self._conn.commit()

    def pending_count(self) -> int:
        """待推送操作数（界面角标用）。"""
        with self._lock:
            cur = self._conn.execute("SELECT COUNT(*) FROM pending_ops")
            return int(cur.fetchone()[0])

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
