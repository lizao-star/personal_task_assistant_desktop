"""优先级计算引擎（纯函数，无任何外部 IO，可单元测试）。

公式（各因子 0~1 归一化后加权）：
    priority_score = 0.40 * urgency
                   + 0.30 * importance
                   + 0.20 * blocking
                   + 0.10 * energy_match
                   - penalty

设计原则：
- 完全不依赖 AI 与飞书公式字段，输入输出可复现；
- AI 只负责“建议”，排序以这里的计算结果为准。
"""

from __future__ import annotations

from datetime import datetime

from ..constants import PRIORITY_TO_IMPORTANCE
from ..feishu.repository import Task

# 各因子权重
W_URGENCY = 0.40
W_IMPORTANCE = 0.30
W_BLOCKING = 0.20
W_ENERGY = 0.10
# 每延期一次扣减的分数
DELAY_PENALTY_PER_TIME = 0.02


# ----------------------------------------------------------------------
# 单因子计算
# ----------------------------------------------------------------------
def urgency(due_at: datetime | None, now: datetime) -> float:
    """截止紧迫度：越接近截止分数越高，无截止时间给中性偏低分。"""
    if due_at is None:
        return 0.30
    delta_hours = (due_at - now).total_seconds() / 3600

    if delta_hours < 0:
        return 1.00          # 已逾期
    if delta_hours <= 24:
        return 0.95          # 24 小时内
    if delta_hours <= 72:
        return 0.70          # 3 天内
    if delta_hours <= 168:
        return 0.50          # 7 天内
    return 0.30              # 更远


def importance(priority: str) -> float:
    """重要性：P0~P3 映射为 0~1。"""
    return PRIORITY_TO_IMPORTANCE.get(priority, 2) / 5.0


def build_blocked_count(tasks: list[Task]) -> dict[str, int]:
    """统计每个任务被多少其他任务依赖（阻塞下游越多越优先）。"""
    counts = {task.record_id: 0 for task in tasks}
    for task in tasks:
        for dep_id in task.depends_on:
            if dep_id in counts:
                counts[dep_id] += 1
    return counts


def blocking(blocked_count: int) -> float:
    """阻塞度：阻塞 0 个为 0，阻塞 3 个及以上为 1。"""
    return min(blocked_count / 3.0, 1.0)


def energy_match(energy: str, now: datetime) -> float:
    """精力匹配度：任务精力需求与当前时段匹配程度。

    时段划分（可按个人作息调整）：
    - 08:00-12:00 高精力时段
    - 14:00-18:00 中精力时段
    - 19:00-23:00 中低精力时段
    - 其余为低精力时段
    """
    hour = now.hour
    if 8 <= hour < 12:
        slot = "高"
    elif 14 <= hour < 18:
        slot = "中"
    elif 19 <= hour < 23:
        slot = "中"
    else:
        slot = "低"

    if not energy:
        return 0.5
    # 完全匹配为 1，相邻档为 0.6，跨两档为 0.2
    levels = {"低": 0, "中": 1, "高": 2}
    if energy not in levels or slot not in levels:
        return 0.5
    gap = abs(levels[energy] - levels[slot])
    return {0: 1.0, 1: 0.6, 2: 0.2}[gap]


# ----------------------------------------------------------------------
# 综合计算
# ----------------------------------------------------------------------
def calculate_score(
    task: Task,
    now: datetime,
    blocked_count: int = 0,
) -> float:
    """计算单个任务的优先级分数（0~1，可能因延期惩罚略低于 0）。"""
    score = (
        W_URGENCY * urgency(task.due_at, now)
        + W_IMPORTANCE * importance(task.priority)
        + W_BLOCKING * blocking(blocked_count)
        + W_ENERGY * energy_match(task.energy, now)
        - task.delay_count * DELAY_PENALTY_PER_TIME
    )
    # 保留 4 位小数，避免展示抖动
    return round(max(score, 0.0), 4)


def rank_tasks(tasks: list[Task], now: datetime | None = None) -> list[tuple[Task, float]]:
    """对任务列表打分并按分数降序排列，返回 (任务, 分数) 列表。"""
    now = now or datetime.now()
    blocked_counts = build_blocked_count(tasks)
    scored = [
        (task, calculate_score(task, now, blocked_counts.get(task.record_id, 0)))
        for task in tasks
    ]
    return sorted(scored, key=lambda item: item[1], reverse=True)
