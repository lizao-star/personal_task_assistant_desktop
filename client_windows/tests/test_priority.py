"""优先级引擎单元测试。

运行方式（在 client_windows 目录下）：
    python -m unittest discover -s tests -v
"""

import unittest
from datetime import datetime, timedelta

from app.feishu.repository import Task
from app.services.priority import (
    build_blocked_count,
    calculate_score,
    energy_match,
    importance,
    rank_tasks,
    urgency,
)


class UrgencyTest(unittest.TestCase):
    """截止紧迫度分档测试。"""

    def setUp(self):
        self.now = datetime(2026, 10, 6, 12, 0, 0)

    def test_overdue(self):
        self.assertEqual(urgency(self.now - timedelta(hours=1), self.now), 1.00)

    def test_within_24h(self):
        self.assertEqual(urgency(self.now + timedelta(hours=10), self.now), 0.95)

    def test_within_3_days(self):
        self.assertEqual(urgency(self.now + timedelta(days=2), self.now), 0.70)

    def test_within_7_days(self):
        self.assertEqual(urgency(self.now + timedelta(days=6), self.now), 0.50)

    def test_far_future(self):
        self.assertEqual(urgency(self.now + timedelta(days=30), self.now), 0.30)

    def test_none_due(self):
        self.assertEqual(urgency(None, self.now), 0.30)


class ImportanceTest(unittest.TestCase):
    """优先级映射测试。"""

    def test_mapping(self):
        self.assertEqual(importance("P0"), 1.0)
        self.assertEqual(importance("P1"), 0.8)
        self.assertEqual(importance("P2"), 0.6)
        self.assertEqual(importance("P3"), 0.4)

    def test_unknown_default(self):
        self.assertEqual(importance(""), 0.4)


class BlockingTest(unittest.TestCase):
    """依赖阻塞统计测试。"""

    def test_build_blocked_count(self):
        t1 = Task(record_id="r1", title="任务1")
        t2 = Task(record_id="r2", title="任务2", depends_on=["r1"])
        t3 = Task(record_id="r3", title="任务3", depends_on=["r1"])
        counts = build_blocked_count([t1, t2, t3])
        self.assertEqual(counts["r1"], 2)
        self.assertEqual(counts["r2"], 0)
        self.assertEqual(counts["r3"], 0)


class EnergyMatchTest(unittest.TestCase):
    """精力匹配测试。"""

    def test_morning_high_match(self):
        # 早上 9 点做高精力任务，完全匹配
        self.assertEqual(energy_match("高", datetime(2026, 10, 6, 9, 0)), 1.0)

    def test_unknown_energy(self):
        self.assertEqual(energy_match("", datetime(2026, 10, 6, 9, 0)), 0.5)


class RankTest(unittest.TestCase):
    """综合打分与排序测试。"""

    def setUp(self):
        self.now = datetime(2026, 10, 6, 10, 0, 0)

    def test_overdue_beats_far_future(self):
        overdue = Task(
            record_id="a", title="逾期任务", priority="P3",
            due_at=self.now - timedelta(hours=2),
        )
        future = Task(
            record_id="b", title="远期任务", priority="P0",
            due_at=self.now + timedelta(days=30),
        )
        ranked = rank_tasks([future, overdue], now=self.now)
        self.assertEqual(ranked[0][0].record_id, "a")
        self.assertGreater(ranked[0][1], ranked[1][1])

    def test_delay_penalty(self):
        """延期次数多的任务分数应被扣分。"""
        t1 = Task(record_id="a", title="任务1", priority="P1",
                  due_at=self.now + timedelta(hours=5))
        t2 = Task(record_id="b", title="任务2", priority="P1",
                  due_at=self.now + timedelta(hours=5), delay_count=3)
        s1 = calculate_score(t1, self.now)
        s2 = calculate_score(t2, self.now)
        self.assertAlmostEqual(s1 - s2, 0.06, places=3)

    def test_score_in_range(self):
        task = Task(record_id="a", title="任务", priority="P0",
                    due_at=self.now + timedelta(hours=1))
        score = calculate_score(task, self.now, blocked_count=3)
        self.assertGreaterEqual(score, 0.0)
        self.assertLessEqual(score, 1.0)


if __name__ == "__main__":
    unittest.main()
