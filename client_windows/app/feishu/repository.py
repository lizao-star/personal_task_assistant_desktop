"""多维表格任务仓储：把飞书原始记录解析为本地 Task 对象，并提供筛选。

多维表格字段在 OpenAPI 中的返回形态（要点）：
- 文本：[{"type":"text","text":"..."}]
- 单选："P1"（字符串）
- 多选：["论文", "阅读"]（字符串数组）
- 数字：90
- 复选框：true/false
- 日期：1696492800000（毫秒时间戳）
- 关联：["recXXXX"]（record_id 数组，不同版本可能为 {"link_record_ids": [...]}）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..constants import FINISHED_STATUSES, TaskFields
from .client import FeishuClient


# ----------------------------------------------------------------------
# 字段解析工具
# ----------------------------------------------------------------------
def parse_text(value: Any) -> str:
    """解析文本字段为纯字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for seg in value:
            if isinstance(seg, dict):
                parts.append(seg.get("text", ""))
            else:
                parts.append(str(seg))
        return "".join(parts)
    if isinstance(value, dict):
        return value.get("text", "")
    return str(value)


def parse_number(value: Any) -> int:
    """解析数字字段为整数，无效时返回 0。"""
    if value is None or value == "":
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def parse_datetime(value: Any) -> datetime | None:
    """解析日期字段（毫秒时间戳）为本地 datetime。"""
    if value is None or value == "":
        return None
    try:
        # 飞书日期为毫秒时间戳
        return datetime.fromtimestamp(int(value) / 1000)
    except (TypeError, ValueError, OSError):
        return None


def parse_select(value: Any) -> str:
    """解析单选字段为字符串。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("text", "")
    return str(value)


def parse_multi_select(value: Any) -> list[str]:
    """解析多选字段为字符串数组。"""
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    return [v if isinstance(v, str) else v.get("text", "") for v in value]


def parse_links(value: Any) -> list[str]:
    """解析关联字段为 record_id 数组（兼容两种返回形态）。"""
    if not value:
        return []
    if isinstance(value, dict):
        return list(value.get("link_record_ids", []) or [])
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                result.extend(item.get("link_record_ids", []) or [])
        return result
    return []


# ----------------------------------------------------------------------
# 任务领域对象
# ----------------------------------------------------------------------
@dataclass
class Task:
    """归一化后的任务对象。"""

    record_id: str
    title: str
    status: str = ""
    priority: str = ""
    task_type: str = ""
    due_at: datetime | None = None
    start_at: datetime | None = None
    estimate_min: int = 0
    voice: bool = False
    energy: str = ""
    delay_count: int = 0
    tags: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)
    reminder_rules: list[str] = field(default_factory=list)

    @property
    def is_finished(self) -> bool:
        """是否已处于结束状态。"""
        return self.status in FINISHED_STATUSES


def to_task(record: dict[str, Any]) -> Task:
    """把一条飞书原始记录转换为 Task 对象。"""
    fields = record.get("fields", {})
    return Task(
        record_id=record.get("record_id", ""),
        title=parse_text(fields.get(TaskFields.TITLE)) or "(未命名任务)",
        status=parse_select(fields.get(TaskFields.STATUS)),
        priority=parse_select(fields.get(TaskFields.PRIORITY)),
        task_type=parse_select(fields.get(TaskFields.TYPE)),
        due_at=parse_datetime(fields.get(TaskFields.DUE_AT)),
        start_at=parse_datetime(fields.get(TaskFields.START_AT)),
        estimate_min=parse_number(fields.get(TaskFields.ESTIMATE_MIN)),
        voice=bool(fields.get(TaskFields.VOICE)),
        energy=parse_select(fields.get(TaskFields.ENERGY)),
        delay_count=parse_number(fields.get(TaskFields.DELAY_COUNT)),
        tags=parse_multi_select(fields.get(TaskFields.TAGS)),
        depends_on=parse_links(fields.get(TaskFields.DEPENDS_ON)),
        reminder_rules=parse_multi_select(fields.get(TaskFields.REMINDER_RULE)),
    )


# ----------------------------------------------------------------------
# 仓储
# ----------------------------------------------------------------------
def list_tasks(client: FeishuClient, app_token: str, table_id: str) -> list[Task]:
    """拉取任务表全部记录并解析为 Task 列表。"""
    return [to_task(record) for record in client.iter_records(app_token, table_id)]


def get_today_tasks(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """今日待办：截止时间为今天，且未结束。"""
    now = now or datetime.now()
    result = []
    for task in tasks:
        if task.is_finished or task.due_at is None:
            continue
        if task.due_at.date() == now.date():
            result.append(task)
    return result


def get_overdue_tasks(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """逾期任务：截止时间已过，且未结束。"""
    now = now or datetime.now()
    return [
        task
        for task in tasks
        if not task.is_finished and task.due_at is not None and task.due_at < now
    ]
