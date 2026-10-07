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
    STATUS_INBOX,
    STATUS_TODO,
    SYNC_SOURCE_CLIENT,
    SubtaskFields,
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
    """解析关联字段为 record_id 数组（兼容三种返回形态）。

    1. ["recXXXX"]
    2. {"link_record_ids": [...]} 或 {"record_ids": [...]}
    3. [{"record_ids": [...], "table_id": ..., "text": ...}]（较新 API 版本）
    """
    if not value:
        return []
    if isinstance(value, dict):
        ids = value.get("link_record_ids") or value.get("record_ids") or []
        return list(ids)
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                ids = item.get("link_record_ids") or item.get("record_ids") or []
                result.extend(ids)
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
    # M3 新增：AI 拆解与检查清单（详情对话框展示）
    ai_breakdown: str = ""
    checklist: str = ""

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
        ai_breakdown=parse_text(fields.get(TaskFields.AI_BREAKDOWN)),
        checklist=parse_text(fields.get(TaskFields.CHECKLIST)),
    )


# ----------------------------------------------------------------------
# 子任务领域对象
# ----------------------------------------------------------------------
@dataclass
class SubTask:
    """归一化后的子任务对象（属于某个主任务的一个步骤）。"""

    record_id: str
    parent_record_id: str = ""   # 所属任务的 record_id（取关联的第一个）
    title: str = ""
    status: str = ""
    order: int = 0
    estimate_min: int = 0
    due_at: datetime | None = None
    completed_at: datetime | None = None
    ai_hint: str = ""

    @property
    def is_finished(self) -> bool:
        """是否已完成。"""
        return self.status in FINISHED_STATUSES


def to_subtask(record: dict[str, Any]) -> SubTask:
    """把一条飞书「子任务表」原始记录转换为 SubTask 对象。"""
    fields = record.get("fields", {})
    parents = parse_links(fields.get(SubtaskFields.PARENT))
    return SubTask(
        record_id=record.get("record_id", ""),
        parent_record_id=parents[0] if parents else "",
        title=parse_text(fields.get(SubtaskFields.TITLE)) or "(未命名子任务)",
        status=parse_select(fields.get(SubtaskFields.STATUS)),
        order=parse_number(fields.get(SubtaskFields.ORDER)),
        estimate_min=parse_number(fields.get(SubtaskFields.ESTIMATE_MIN)),
        due_at=parse_datetime(fields.get(SubtaskFields.DUE_AT)),
        completed_at=parse_datetime(fields.get(SubtaskFields.COMPLETED_AT)),
        ai_hint=parse_text(fields.get(SubtaskFields.AI_HINT)),
    )


def list_subtasks(client: FeishuClient, app_token: str, table_id: str) -> list[SubTask]:
    """拉取子任务表全部记录并解析为 SubTask 列表。"""
    return [to_subtask(record) for record in client.iter_records(app_token, table_id)]


def group_subtasks(
    subtasks: list[SubTask], parent_record_id: str
) -> list[SubTask]:
    """取某个主任务下的子任务，按「顺序」升序排列。"""
    owned = [s for s in subtasks if s.parent_record_id == parent_record_id]
    return sorted(owned, key=lambda s: (s.order, s.title))


# ----------------------------------------------------------------------
# 仓储
# ----------------------------------------------------------------------
def list_tasks(client: FeishuClient, app_token: str, table_id: str) -> list[Task]:
    """拉取任务表全部记录并解析为 Task 列表。"""
    return [to_task(record) for record in client.iter_records(app_token, table_id)]


def get_today_tasks(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """待办任务：所有未结束且未逾期、不在收集箱的任务。

    与「逾期任务」和「收集箱」均互斥：
    - 已逾期的任务只出现在逾期区；
    - 收集箱中的任务单独出现在收集箱区；
    - 三者合起来恰好覆盖「全部未结束任务」，既不重叠也不遗漏。
    """
    now = now or datetime.now()
    return [
        task
        for task in tasks
        if not task.is_finished
        and task.status != STATUS_INBOX
        and (task.due_at is None or task.due_at >= now)
    ]


def get_overdue_tasks(tasks: list[Task], now: datetime | None = None) -> list[Task]:
    """逾期任务：截止时间已过，且未结束、不在收集箱。"""
    now = now or datetime.now()
    return [
        task
        for task in tasks
        if (
            not task.is_finished
            and task.status != STATUS_INBOX
            and task.due_at is not None
            and task.due_at < now
        )
    ]


def get_inbox_tasks(tasks: list[Task]) -> list[Task]:
    """收集箱：状态为「收集箱」的任务（待用户确认后再排期）。

    收集箱任务的优先级与截止时间都未确定，不参与排序、不进待办区、不进逾期区。
    用户一旦把状态改为「待办/进行中/等待」等，下次同步会自动从收集箱中移除。
    """
    return [task for task in tasks if task.status == STATUS_INBOX]


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
    status: str = STATUS_TODO,
    energy: str = "",
    tags: list[str] | None = None,
    ai_breakdown: str = "",
    checklist: str = "",
) -> dict[str, Any]:
    """把新建任务的本地输入组装为飞书 fields 字典。

    M3 起 AI 建任务会附带 energy/tags/ai_breakdown/checklist，
    以及低置信度时的 status=收集箱。
    """
    fields: dict[str, Any] = {
        TaskFields.TITLE: title,
        TaskFields.STATUS: status,
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
    if energy:
        fields[TaskFields.ENERGY] = energy
    if tags:
        fields[TaskFields.TAGS] = tags
    if ai_breakdown:
        fields[TaskFields.AI_BREAKDOWN] = ai_breakdown
    if checklist:
        fields[TaskFields.CHECKLIST] = checklist
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


def build_ai_breakdown_fields(
    ai_breakdown: str, checklist: str, advice: str = ""
) -> dict[str, Any]:
    """AI 拆解采纳：回填 AI拆解 / 检查清单 / AI建议 三个文本字段。"""
    fields: dict[str, Any] = {
        TaskFields.AI_BREAKDOWN: ai_breakdown,
        TaskFields.CHECKLIST: checklist,
    }
    if advice:
        fields[TaskFields.AI_ADVICE] = advice
    return fields


def build_subtask_fields(
    parent_record_id: str, titles: list[str]
) -> list[dict[str, Any]]:
    """把 AI 拆解出的子任务标题列表组装为子任务表 fields 列表。

    空标题会被过滤，顺序号按过滤后的结果连续编号 1..n，
    并通过「所属任务」关联主任务。
    """
    cleaned = [t.strip() for t in titles if t.strip()]
    return [
        {
            SubtaskFields.TITLE: title,
            SubtaskFields.PARENT: [parent_record_id],
            SubtaskFields.STATUS: STATUS_TODO,
            SubtaskFields.ORDER: i + 1,
        }
        for i, title in enumerate(cleaned)
    ]


def create_subtask(
    client: FeishuClient, app_token: str, table_id: str, fields: dict[str, Any]
) -> None:
    """新建一条子任务记录。"""
    client.create_record(app_token, table_id, fields)


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
