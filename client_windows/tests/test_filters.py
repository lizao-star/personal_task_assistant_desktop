"""任务筛选逻辑单元测试。

覆盖 get_today_tasks / get_overdue_tasks / get_inbox_tasks 三者的互斥与覆盖关系：
- 三个视图互不重叠
- 合起来恰好是「全部未结束任务」
- 收集箱任务只出现在收集箱区
- 无截止时间的任务归入待办（不进逾期）
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta

from app.constants import STATUS_CANCELLED, STATUS_DONE, STATUS_INBOX, STATUS_TODO
from app.feishu.repository import (
    Task,
    get_inbox_tasks,
    get_overdue_tasks,
    get_today_tasks,
)


def _make(
    *,
    record_id: str,
    title: str = "t",
    status: str = STATUS_TODO,
    due_at: datetime | None = None,
) -> Task:
    """构造一个最小 Task 对象用于测试。"""
    return Task(record_id=record_id, title=title, status=status, due_at=due_at)


class TestFilters(unittest.TestCase):
    """三个筛选视图的互斥与覆盖关系。"""

    def setUp(self) -> None:
        """构造一个覆盖所有边界场景的任务集合。"""
        self.now = datetime(2026, 10, 7, 15, 0, 0)
        # 各类任务：今天截止、未来截止、过去截止、无截止、收集箱、已完成、已取消
        self.today_due = _make(record_id="today", due_at=self.now.replace(hour=18))
        self.future_due = _make(
            record_id="future", due_at=self.now + timedelta(days=3)
        )
        self.overdue = _make(record_id="overdue", due_at=self.now - timedelta(days=1))
        self.no_due = _make(record_id="no_due", due_at=None)
        self.inbox = _make(record_id="inbox", status=STATUS_INBOX)
        self.inbox_with_due = _make(
            record_id="inbox_due", status=STATUS_INBOX, due_at=self.now.replace(hour=18)
        )
        self.done = _make(record_id="done", status=STATUS_DONE, due_at=self.now)
        self.cancelled = _make(
            record_id="cancelled", status=STATUS_CANCELLED, due_at=self.now
        )
        self.tasks = [
            self.today_due,
            self.future_due,
            self.overdue,
            self.no_due,
            self.inbox,
            self.inbox_with_due,
            self.done,
            self.cancelled,
        ]

    def test_today_excludes_finished_and_inbox_and_overdue(self) -> None:
        """待办区：未结束 + 非收集箱 + 未逾期（含无截止时间）。"""
        today = get_today_tasks(self.tasks, now=self.now)
        ids = {t.record_id for t in today}
        # 今天截止、未来截止、无截止时间 都在待办区
        self.assertEqual(ids, {"today", "future", "no_due"})

    def test_overdue_excludes_finished_and_inbox(self) -> None:
        """逾期区：未结束 + 非收集箱 + 截止时间已过。"""
        overdue = get_overdue_tasks(self.tasks, now=self.now)
        ids = {t.record_id for t in overdue}
        self.assertEqual(ids, {"overdue"})

    def test_inbox_collects_only_inbox_status(self) -> None:
        """收集箱区：仅状态为「收集箱」的任务（无论是否有截止时间）。"""
        inbox = get_inbox_tasks(self.tasks)
        ids = {t.record_id for t in inbox}
        self.assertEqual(ids, {"inbox", "inbox_due"})

    def test_three_views_are_mutually_exclusive(self) -> None:
        """三个视图互不重叠：同一任务不会出现在两个视图里。"""
        today_ids = {t.record_id for t in get_today_tasks(self.tasks, now=self.now)}
        overdue_ids = {t.record_id for t in get_overdue_tasks(self.tasks, now=self.now)}
        inbox_ids = {t.record_id for t in get_inbox_tasks(self.tasks)}
        # 两两交集为空
        self.assertEqual(today_ids & overdue_ids, set())
        self.assertEqual(today_ids & inbox_ids, set())
        self.assertEqual(overdue_ids & inbox_ids, set())

    def test_three_views_cover_all_unfinished(self) -> None:
        """三个视图合起来恰好覆盖「全部未结束任务」。"""
        today = get_today_tasks(self.tasks, now=self.now)
        overdue = get_overdue_tasks(self.tasks, now=self.now)
        inbox = get_inbox_tasks(self.tasks)
        union = {t.record_id for t in today + overdue + inbox}
        # 应该是除了已完成/已取消以外的全部任务
        self.assertEqual(union, {"today", "future", "overdue", "no_due", "inbox", "inbox_due"})

    def test_no_due_task_goes_to_today_not_overdue(self) -> None:
        """无截止时间的任务归入待办区，不进逾期区。"""
        only_no_due = [_make(record_id="x", due_at=None)]
        self.assertEqual(len(get_today_tasks(only_no_due, now=self.now)), 1)
        self.assertEqual(len(get_overdue_tasks(only_no_due, now=self.now)), 0)


if __name__ == "__main__":
    unittest.main()
