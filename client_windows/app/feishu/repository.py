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

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..constants import (
    FINISHED_STATUSES,
    STATUS_DONE,
    STATUS_TODO,
    SYNC_SOURCE_CLIENT,
    TaskFields,
)
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
    # M2 新增：写回与同步需要
    description: str = ""
    external_id: str = ""
    completed_at: datetime | None = None
    modified_at: datetime | None = None

    @property
    def is_finished(self) -> bool:
        """是否已处于结束状态。"""
        return self.status in FINISHED_STATUSES

    @property
    def modified_ms(self) -> int:
        """云端最后修改时间（毫秒），无则 0。冲突检测的基准值。"""
        if self.modified_at is None:
            return 0
        return int(self.modified_at.timestamp() * 1000)


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
        description=parse_text(fields.get(TaskFields.DESCRIPTION)),
        external_id=parse_text(fields.get(TaskFields.EXTERNAL_ID)),
        completed_at=parse_datetime(fields.get(TaskFields.COMPLETED_AT)),
        modified_at=parse_datetime(fields.get(TaskFields.UPDATED_AT)),
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


# ----------------------------------------------------------------------
# 写接口（M2）
# ----------------------------------------------------------------------
def generate_external_id() -> str:
    """生成客户端写入的幂等键，格式 win-<12位hex>。"""
    return f"win-{uuid.uuid4().hex[:12]}"


def _ms(dt: datetime) -> int:
    """本地 datetime -> 飞书毫秒时间戳。"""
    return int(dt.timestamp() * 1000)


def build_create_fields(
    *,
    title: str,
    due_at: datetime,
    priority: str,
    task_type: str,
    external_id: str,
    description: str = "",
    estimate_min: int = 0,
    reminder_rules: list[str] | None = None,
) -> dict[str, Any]:
    """把新建任务的本地输入组装为飞书 fields 字典。"""
    fields: dict[str, Any] = {
        TaskFields.TITLE: title,
        TaskFields.STATUS: STATUS_TODO,
        TaskFields.PRIORITY: priority,
        TaskFields.TYPE: task_type,
        TaskFields.DUE_AT: _ms(due_at),
        TaskFields.EXTERNAL_ID: external_id,
        TaskFields.SYNC_SOURCE: SYNC_SOURCE_CLIENT,
        TaskFields.DELAY_COUNT: 0,
    }
    if description:
        fields[TaskFields.DESCRIPTION] = description
    if estimate_min > 0:
        fields[TaskFields.ESTIMATE_MIN] = estimate_min
    if reminder_rules:
        fields[TaskFields.REMINDER_RULE] = reminder_rules
    return fields


def build_complete_fields(now: datetime | None = None) -> dict[str, Any]:
    """完成任务：状态=已完成 + 完成时间=现在。"""
    now = now or datetime.now()
    return {
        TaskFields.STATUS: STATUS_DONE,
        TaskFields.COMPLETED_AT: _ms(now),
    }


def build_defer_fields(new_due: datetime, delay_count: int) -> dict[str, Any]:
    """延期任务：新截止时间 + 延期次数+1 + 状态回到待办。"""
    return {
        TaskFields.DUE_AT: _ms(new_due),
        TaskFields.DELAY_COUNT: delay_count + 1,
        TaskFields.STATUS: STATUS_TODO,
    }


def find_by_external_id(
    client: FeishuClient, app_token: str, table_id: str, external_id: str
) -> Task | None:
    """按外部ID查重，命中则返回已存在的任务（幂等键）。"""
    conditions = [
        {"field_name": TaskFields.EXTERNAL_ID, "operator": "is", "value": [external_id]}
    ]
    for record in client.search_records(app_token, table_id, conditions):
        return to_task(record)
    return None


def create_task(
    client: FeishuClient, app_token: str, table_id: str, fields: dict[str, Any]
) -> Task:
    """新建任务（调用方需先按外部ID查重），返回创建后的 Task。"""
    record = client.create_record(app_token, table_id, fields)
    return to_task(record)


def update_task(
    client: FeishuClient,
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
) -> Task:
    """更新任务（完成/延期共用），返回更新后的 Task。"""
    record = client.update_record(app_token, table_id, record_id, fields)
    return to_task(record)


def get_remote_modified_ms(
    client: FeishuClient, app_token: str, table_id: str, record_id: str
) -> int:
    """读取云端单条记录的修改时间（毫秒），用于冲突检测。"""
    record = client.get_record(app_token, table_id, record_id)
    updated = parse_datetime(record.get("fields", {}).get(TaskFields.UPDATED_AT))
    return int(updated.timestamp() * 1000) if updated else 0
