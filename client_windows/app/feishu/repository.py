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
    GOAL_FINISHED_STATUSES,
    GOAL_STATUS_DOING,
    GOAL_STATUS_NOT_STARTED,
    GoalFields,
    MonitorLogFields,
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
    # AI 策略建议：怎么做才能做好（不同于拆解动作清单）
    ai_advice: str = ""
    # M4.5 新增：所属目标（目标项目表 record_id，最多 1 个）
    goal_record_id: str = ""

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
    goals = parse_links(fields.get(TaskFields.GOAL))
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
        ai_advice=parse_text(fields.get(TaskFields.AI_ADVICE)),
        goal_record_id=goals[0] if goals else "",
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


# ----------------------------------------------------------------------
# 目标/项目领域对象（M4.5）
# ----------------------------------------------------------------------
@dataclass
class Goal:
    """归一化后的目标/项目（L1）。"""

    record_id: str
    title: str
    level: str = ""
    status: str = ""
    start_at: datetime | None = None
    end_at: datetime | None = None
    description: str = ""
    key_results: str = ""
    progress: int = 0
    external_id: str = ""
    modified_at: datetime | None = None
    # 该目标下的任务 record_id（由任务表「所属目标」反向聚合，不依赖关联列）
    task_record_ids: list[str] = field(default_factory=list)

    @property
    def is_finished(self) -> bool:
        """目标是否已结束（完成/取消）。"""
        return self.status in GOAL_FINISHED_STATUSES

    @property
    def modified_ms(self) -> int:
        """云端最后修改时间（毫秒），无则 0。"""
        if self.modified_at is None:
            return 0
        return int(self.modified_at.timestamp() * 1000)


def to_goal(record: dict[str, Any]) -> Goal:
    """把一条飞书「目标/项目表」原始记录转换为 Goal 对象。"""
    fields = record.get("fields", {})
    return Goal(
        record_id=record.get("record_id", ""),
        title=parse_text(fields.get(GoalFields.TITLE)) or "(未命名目标)",
        level=parse_select(fields.get(GoalFields.LEVEL)),
        status=parse_select(fields.get(GoalFields.STATUS)),
        start_at=parse_datetime(fields.get(GoalFields.START_AT)),
        end_at=parse_datetime(fields.get(GoalFields.END_AT)),
        description=parse_text(fields.get(GoalFields.DESCRIPTION)),
        key_results=parse_text(fields.get(GoalFields.KEY_RESULTS)),
        progress=parse_number(fields.get(GoalFields.PROGRESS)),
        external_id=parse_text(fields.get(GoalFields.EXTERNAL_ID)),
        modified_at=parse_datetime(fields.get(GoalFields.UPDATED_AT)),
    )


def list_goals(client: FeishuClient, app_token: str, table_id: str) -> list[Goal]:
    """拉取目标/项目表全部记录并解析为 Goal 列表。"""
    return [to_goal(record) for record in client.iter_records(app_token, table_id)]


def attach_goal_tasks(goals: list[Goal], tasks: list[Task]) -> list[Goal]:
    """按任务的「所属目标」把任务 record_id 聚合到对应 Goal 上。

    目标表里的「关联任务」是反向关联列，写入时机与任务表不同步，
    因此界面展示以任务表的正向关联为准，保证刚建的目标也能立刻看到任务。
    """
    by_goal: dict[str, list[str]] = {}
    for task in tasks:
        if task.goal_record_id:
            by_goal.setdefault(task.goal_record_id, []).append(task.record_id)
    for goal in goals:
        goal.task_record_ids = by_goal.get(goal.record_id, [])
    return goals


def get_active_goals(goals: list[Goal]) -> list[Goal]:
    """活跃目标：未完成、未取消（主窗口目标计划区展示用）。"""
    return [g for g in goals if not g.is_finished]


def _progress_percent(goals_tasks: list[Task]) -> int:
    """按已完成任务数计算进度百分比（0~100）。"""
    if not goals_tasks:
        return 0
    done = sum(1 for t in goals_tasks if t.is_finished)
    return int(round(done * 100 / len(goals_tasks)))


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


def _hyperlink(value: str) -> dict[str, str]:
    """把 URL 字符串组装为飞书「超链接」字段值对象。

    超链接字段（field type 15）写入格式必须为 {"text": ..., "link": ...}，
    link 需带协议头；缺协议时按 https 补齐。
    """
    text = (value or "").strip()
    link = text if text.startswith(("http://", "https://")) else f"https://{text}"
    return {"text": text, "link": link}


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
    ai_advice: str = "",
    goal_record_id: str = "",
) -> dict[str, Any]:
    """把新建任务的本地输入组装为飞书 fields 字典。

    M3 起新建任务的 AI 解析会附带 energy/tags/ai_breakdown/checklist，
    以及低置信度时的 status=收集箱。
    M4.5 起支持 goal_record_id：写入任务的「所属目标」关联字段。
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
    if ai_advice:
        fields[TaskFields.AI_ADVICE] = ai_advice
    if goal_record_id:
        fields[TaskFields.GOAL] = [goal_record_id]
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


def build_edit_fields(changes: dict[str, Any]) -> dict[str, Any]:
    """把编辑对话框的字段变更组装为飞书 fields 字典（M2 写回扩展：编辑任务）。

    只写入 changes 中出现的键，未改动的字段不写，避免误清空其他列。
    类型/清空语义：
    - 日期：datetime -> 毫秒时间戳；None -> 传 null（飞书按“清空该字段”处理）；
    - 多选：list[str]，传空列表即清空全部选项；
    - 单选/文本：传空串即清空；
    - 复选框：bool。
    状态改为「已完成」且调用方未显式给出完成时间时，自动补当前时间，
    避免出现“已完成但无完成时间”的脏数据。

    支持的键（与 Task 属性同名）：
        title / status / priority / task_type / due_at / start_at /
        estimate_min / energy / voice / tags / description / reminder_rules /
        ai_breakdown / checklist / ai_advice
    """
    fields: dict[str, Any] = {}

    if "title" in changes:
        fields[TaskFields.TITLE] = (changes.get("title") or "").strip()
    if "priority" in changes:
        fields[TaskFields.PRIORITY] = changes.get("priority") or ""
    if "task_type" in changes:
        fields[TaskFields.TYPE] = changes.get("task_type") or ""
    if "due_at" in changes:
        due = changes.get("due_at")
        fields[TaskFields.DUE_AT] = _ms(due) if isinstance(due, datetime) else None
    if "start_at" in changes:
        start = changes.get("start_at")
        fields[TaskFields.START_AT] = _ms(start) if isinstance(start, datetime) else None
    if "estimate_min" in changes:
        fields[TaskFields.ESTIMATE_MIN] = int(changes.get("estimate_min") or 0)
    if "energy" in changes:
        fields[TaskFields.ENERGY] = changes.get("energy") or ""
    if "voice" in changes:
        fields[TaskFields.VOICE] = bool(changes.get("voice"))
    if "tags" in changes:
        fields[TaskFields.TAGS] = list(changes.get("tags") or [])
    if "description" in changes:
        fields[TaskFields.DESCRIPTION] = changes.get("description") or ""
    if "reminder_rules" in changes:
        fields[TaskFields.REMINDER_RULE] = list(changes.get("reminder_rules") or [])
    if "ai_breakdown" in changes:
        fields[TaskFields.AI_BREAKDOWN] = changes.get("ai_breakdown") or ""
    if "checklist" in changes:
        fields[TaskFields.CHECKLIST] = changes.get("checklist") or ""
    if "ai_advice" in changes:
        fields[TaskFields.AI_ADVICE] = changes.get("ai_advice") or ""
    if "goal_record_id" in changes:
        goal_id = (changes.get("goal_record_id") or "").strip()
        fields[TaskFields.GOAL] = [goal_id] if goal_id else []
    if "status" in changes:
        status = changes.get("status") or ""
        fields[TaskFields.STATUS] = status
        if status == STATUS_DONE:
            completed = changes.get("completed_at")
            fields[TaskFields.COMPLETED_AT] = (
                _ms(completed) if isinstance(completed, datetime) else _ms(datetime.now())
            )

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


# ----------------------------------------------------------------------
# 写接口（M4.5：目标/项目表）
# ----------------------------------------------------------------------
def build_goal_fields(
    *,
    title: str,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    level: str = "月",
    status: str = GOAL_STATUS_NOT_STARTED,
    description: str = "",
    key_results: list[str] | str | None = None,
    external_id: str = "",
) -> dict[str, Any]:
    """把目标级输入组装为飞书「目标/项目表」fields 字典。

    - 日期：datetime -> 毫秒时间戳，None 时不写（避免误清空）；
    - 关键结果：list[str] 按行拼接为文本，便于在飞书里直接阅读；
    - 外部ID / 同步来源：与任务表一致，用于幂等重试。
    """
    fields: dict[str, Any] = {
        GoalFields.TITLE: title,
        GoalFields.LEVEL: level,
        GoalFields.STATUS: status,
        GoalFields.SYNC_SOURCE: SYNC_SOURCE_CLIENT,
    }
    if start_at is not None:
        fields[GoalFields.START_AT] = _ms(start_at)
    if end_at is not None:
        fields[GoalFields.END_AT] = _ms(end_at)
    if description:
        fields[GoalFields.DESCRIPTION] = description
    if key_results:
        if isinstance(key_results, str):
            text = key_results.strip()
        else:
            text = "\n".join(f"- {kr.strip()}" for kr in key_results if kr.strip())
        if text:
            fields[GoalFields.KEY_RESULTS] = text
    if external_id:
        fields[GoalFields.EXTERNAL_ID] = external_id
    return fields


def find_goal_by_external_id(
    client: FeishuClient, app_token: str, table_id: str, external_id: str
) -> Goal | None:
    """按外部ID查重目标，命中则返回已存在的目标（幂等键）。"""
    conditions = [
        {"field_name": GoalFields.EXTERNAL_ID, "operator": "is", "value": [external_id]}
    ]
    for record in client.search_records(app_token, table_id, conditions):
        return to_goal(record)
    return None


def create_goal(
    client: FeishuClient, app_token: str, table_id: str, fields: dict[str, Any]
) -> Goal:
    """新建目标/项目记录，返回创建后的 Goal（含 record_id）。"""
    record = client.create_record(app_token, table_id, fields)
    return to_goal(record)


# ----------------------------------------------------------------------
# 娱乐监控日志表写入（M4）
# ----------------------------------------------------------------------
def build_monitor_log_fields(
    *,
    occurred_at: datetime,
    duration_seconds: int,
    process_name: str = "",
    window_title: str = "",
    url: str = "",
    notified: bool = False,
    notify_count: int = 0,
    closed: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """把一次娱乐会话的聚合结果组装为飞书日志表 fields 字典。

    一次会话只产出一条聚合记录，不逐秒上报（避免触发飞书限流）。
    字段定义见 constants.MonitorLogFields。
    """
    fields: dict[str, Any] = {
        MonitorLogFields.OCCURRED_AT: _ms(occurred_at),
        MonitorLogFields.DURATION: duration_seconds,
        MonitorLogFields.NOTIFIED: notified,
        MonitorLogFields.NOTIFY_COUNT: notify_count,
        MonitorLogFields.CLOSED: closed,
    }
    if process_name:
        fields[MonitorLogFields.PROCESS_NAME] = process_name
    if window_title:
        fields[MonitorLogFields.WINDOW_TITLE] = window_title
    if url:
        # 「URL」列是飞书「超链接」字段（非纯文本），必须传 {"text","link"} 对象，
        # 传字符串会报 1254068 URLFieldConvFail。
        fields[MonitorLogFields.URL] = _hyperlink(url)
    if note:
        fields[MonitorLogFields.NOTE] = note
    return fields


def create_monitor_log(
    client: FeishuClient, app_token: str, table_id: str, fields: dict[str, Any]
) -> None:
    """新建一条娱乐监控日志记录（无冲突检测，建记录即删本地队列行）。

    兜底归一化：离线队列中可能残留历史入队的旧格式 payload（URL 存的是
    纯字符串），这里统一转为超链接对象，避免旧行永远推送失败、无限重试刷屏。
    """
    url = fields.get(MonitorLogFields.URL)
    if url and not isinstance(url, dict):
        fields = {**fields, MonitorLogFields.URL: _hyperlink(url)}
    client.create_record(app_token, table_id, fields)
