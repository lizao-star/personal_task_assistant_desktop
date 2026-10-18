"""优先级计算引擎（纯函数，无任何外部 IO，可单元测试）。

公式（各因子 0~1 归一化后加权）：
    priority_score = 0.50 * urgency
                   + 0.30 * importance
                   + 0.10 * blocking
                   + 0.10 * energy_match
                   - penalty

设计原则：
- 完全不依赖 AI 与飞书公式字段，输入输出可复现；
- AI 只负责“建议”，排序以这里的计算结果为准；
- 紧迫度是主导因子（0.50）：时间更紧的独立任务不应被远期目标链上的
  前置任务仅凭「被依赖加成」超越；依赖链的先后顺序由 rank_tasks 的
  依赖修正硬保证（前置排在依赖者之前），blocking 因子只保留轻微的
  关键路径倾向（0.10），避免远期前置挤占紧迫任务的位置。
"""

from __future__ import annotations

from datetime import datetime

from ..constants import PRIORITY_TO_IMPORTANCE
from ..feishu.repository import Task

# 各因子权重：紧迫度为主导；依赖链顺序由 rank_tasks 硬保证，
# blocking 只保留轻微关键路径倾向，防止远期前置挤占紧迫独立任务
W_URGENCY = 0.50
W_IMPORTANCE = 0.30
W_BLOCKING = 0.10
W_ENERGY = 0.10
# 每延期一次扣减的分数
DELAY_PENALTY_PER_TIME = 0.02


# ----------------------------------------------------------------------
# 单因子计算
# ----------------------------------------------------------------------
def urgency(due_at: datetime | None, now: datetime) -> float:
    """截止紧迫度：越接近截止分数越高，无截止时间给中性偏低分。

    分档：
    - 已逾期 1.00 / 24h 内 0.95 / 72h 内 0.70 / 7 天内 0.50；
    - 7~30 天在 0.50~0.20 之间线性衰减——远期任务之间也分出先后，
      避免截止 8 天和截止 60 天同分，让时间维度失效（只剩 P 档在起作用）；
    - 30 天以上 0.20。
    """
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
    if delta_hours <= 720:
        # 7~30 天：0.50 线性衰减到 0.20（截止越早分越高）
        ratio = (delta_hours - 168) / (720 - 168)
        return 0.50 - ratio * 0.30
    return 0.20              # 30 天以上


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
    """计算单个任务的优先级分数（0~1，可能因延期惩罚略低于 0）。

    阻塞加成打折：紧迫度进入 7 天以上衰减段（<0.50）时，blocking 减半——
    「先做完它解锁下游」的催促意义随自身紧迫度下降而减弱，
    避免远期被依赖任务仅凭加成反超更早截止的独立任务。
    """
    u = urgency(task.due_at, now)
    b = blocking(blocked_count)
    if u < 0.50:
        b *= 0.5
    score = (
        W_URGENCY * u
        + W_IMPORTANCE * importance(task.priority)
        + W_BLOCKING * b
        + W_ENERGY * energy_match(task.energy, now)
        - task.delay_count * DELAY_PENALTY_PER_TIME
    )
    # 保留 4 位小数，避免展示抖动
    return round(max(score, 0.0), 4)


def _respect_dependencies(
    scored: list[tuple[Task, float]],
) -> list[tuple[Task, float]]:
    """对已按分数降序的列表做依赖顺序修正（稳定，不改变无依赖时的结果）。

    规则：若任务 A 依赖 B（B 还在列表中未就绪），A 即使分数更高也要排到 B 之后。
    实现：每轮从剩余任务里取「分数最高且前置均已就绪」的一项输出；
    所有任务互不依赖时，输出与原排序完全一致。
    循环依赖视为全部就绪，直接按原分数序输出，不会丢任务。
    """
    by_id = {task.record_id: (task, score) for task, score in scored}
    ordered: list[tuple[Task, float]] = []
    emitted: set[str] = set()
    remaining = list(scored)
    while remaining:
        for idx, (task, _score) in enumerate(remaining):
            deps = [
                dep
                for dep in task.depends_on
                if dep in by_id and dep not in emitted and dep != task.record_id
            ]
            if not deps:
                ordered.append(remaining.pop(idx))
                emitted.add(task.record_id)
                break
        else:
            # 剩余任务互相依赖（环）：按原分数序直接放行，避免死循环
            ordered.append(remaining.pop(0))
    return ordered


def rank_tasks(tasks: list[Task], now: datetime | None = None) -> list[tuple[Task, float]]:
    """对任务列表打分并排序，返回 (任务, 分数) 列表。

    主序为分数降序；在此基础上做依赖修正：被依赖的任务排在依赖者之前，
    保证目标拆解出的「先报名 → 再打印准考证」这类链条在列表中不倒挂。
    """
    now = now or datetime.now()
    blocked_counts = build_blocked_count(tasks)
    scored = [
        (task, calculate_score(task, now, blocked_counts.get(task.record_id, 0)))
        for task in tasks
    ]
    return _respect_dependencies(sorted(scored, key=lambda item: item[1], reverse=True))
