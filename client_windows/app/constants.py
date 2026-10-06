"""飞书多维表格字段名与常量定义。

注意：这里的字符串必须与飞书多维表格「任务表」中的列名完全一致，
否则通过 OpenAPI 读写时会取不到值。
"""


class TaskFields:
    """任务表字段名（按已完成.md 中的定义）。"""

    TITLE = "任务名"
    DESCRIPTION = "描述"
    STATUS = "状态"
    PRIORITY = "优先级"
    TYPE = "任务类型"
    SOURCE = "来源"
    GOAL = "所属目标"
    TAGS = "标签"
    DUE_AT = "截止时间"
    START_AT = "开始时间"
    ESTIMATE_MIN = "预计耗时"
    ACTUAL_MIN = "实际耗时"
    REMINDER_RULE = "提醒规则"
    NEXT_REMIND_AT = "下次提醒时间"
    REMINDER_STATUS = "提醒状态"
    REPEAT_RULE = "重复规则"
    DEPENDS_ON = "依赖任务"
    SUBTASKS = "子任务"
    CHECKLIST = "检查清单"
    AI_BREAKDOWN = "AI拆解"
    AI_ADVICE = "AI建议"
    PRIORITY_SCORE = "优先级分数"
    ENERGY = "精力需求"
    SCENE = "场景"
    VOICE = "是否语音提醒"
    MONITORED = "是否监督"
    COMPLETED_AT = "完成时间"
    DELAY_COUNT = "延期次数"
    REVIEW = "复盘"
    ATTACHMENT = "附件"
    URL = "链接"
    # M2 新增字段
    EXTERNAL_ID = "外部ID"
    SYNC_SOURCE = "同步来源"
    UPDATED_AT = "修改时间"


# ===== 状态选项 =====
STATUS_INBOX = "收集箱"
STATUS_TODO = "待办"
STATUS_DOING = "进行中"
STATUS_WAITING = "等待"
STATUS_DONE = "已完成"
STATUS_CANCELLED = "已取消"
STATUS_DEFERRED = "延期"
# 视为“已结束、不再提醒”的状态集合
FINISHED_STATUSES = {STATUS_DONE, STATUS_CANCELLED}

# ===== 优先级选项（建任务表单用） =====
PRIORITY_OPTIONS = ["P0", "P1", "P2", "P3"]

# ===== 任务类型选项（建任务表单用） =====
TASK_TYPE_OPTIONS = ["课程", "科研", "作业", "考试", "组会", "生活", "其他"]

# ===== 同步来源选项 =====
SYNC_SOURCE_CLIENT = "客户端"

# ===== 优先级 -> 重要性分值（1~5）的映射 =====
# 飞书表中是 P0~P3，优先级算法需要 1~5 的重要性分值
PRIORITY_TO_IMPORTANCE = {
    "P0": 5,
    "P1": 4,
    "P2": 3,
    "P3": 2,
}

# ===== 精力需求选项 =====
ENERGY_HIGH = "高"
ENERGY_MEDIUM = "中"
ENERGY_LOW = "低"

# ===== 提醒规则文案 -> 提前时长（分钟） =====
REMINDER_OFFSET_MINUTES = {
    "提前1天": 24 * 60,
    "提前2小时": 2 * 60,
    "提前30分钟": 30,
    "提前10分钟": 10,
}
