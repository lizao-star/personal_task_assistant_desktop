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

    def test_far_future_decays(self):
        """7~30 天线性衰减：截止越早分越高；30 天以上收敛到 0.20。"""
        near = urgency(self.now + timedelta(days=10), self.now)
        far = urgency(self.now + timedelta(days=25), self.now)
        beyond = urgency(self.now + timedelta(days=30), self.now)
        self.assertEqual(beyond, 0.20)
        self.assertGreater(near, far)
        self.assertGreater(far, beyond)
        # 衰减段中点抽查：15 天 ≈ 0.396（0.50 - (360-168)/552*0.30）
        self.assertAlmostEqual(
            urgency(self.now + timedelta(days=15), self.now), 0.3957, places=3
        )

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


class DependencyRankTest(unittest.TestCase):
    """依赖顺序修正：被依赖的任务排在依赖者之前。"""

    def setUp(self):
        self.now = datetime(2026, 10, 6, 10, 0, 0)

    def _task(self, rid: str, due_offset_days: float, **kw) -> Task:
        return Task(
            record_id=rid,
            title=kw.pop("title", rid),
            priority=kw.pop("priority", "P1"),
            due_at=self.now + timedelta(days=due_offset_days),
            **kw,
        )

    def test_no_dependency_keeps_score_order(self):
        """无依赖时输出与纯分数排序完全一致（回归保护）。"""
        far = self._task("far", 20)
        near = self._task("near", 1)
        ranked = rank_tasks([far, near], now=self.now)
        self.assertEqual([t.record_id for t, _ in ranked], ["near", "far"])

    def test_dependent_moved_after_prerequisite(self):
        """第一步截止远（分数低），第二步截止近（分数高）：仍应第一步在前。"""
        step1 = self._task("step1", 10, title="收集资料")
        step2 = self._task("step2", 2, title="写报告", depends_on=["step1"])
        ranked = rank_tasks([step2, step1], now=self.now)
        self.assertEqual([t.record_id for t, _ in ranked], ["step1", "step2"])

    def test_dependency_outside_list_ignored(self):
        """前置任务不在列表内（已完成等）时不影响排序。"""
        step2 = self._task("step2", 1, depends_on=["ghost"])
        step3 = self._task("step3", 10)
        ranked = rank_tasks([step3, step2], now=self.now)
        self.assertEqual([t.record_id for t, _ in ranked], ["step2", "step3"])

    def test_circular_dependency_no_crash_no_loss(self):
        """循环依赖不崩溃、不丢任务，按原分数序输出。"""
        a = self._task("a", 1, depends_on=["b"])
        b = self._task("b", 10, depends_on=["a"])
        ranked = rank_tasks([b, a], now=self.now)
        self.assertEqual({t.record_id for t, _ in ranked}, {"a", "b"})

    def test_chain_order(self):
        """三步链条：无论输入顺序如何，输出保持链条顺序。"""
        s1 = self._task("s1", 15, title="报名")
        s2 = self._task("s2", 10, title="打印准考证", depends_on=["s1"])
        s3 = self._task("s3", 5, title="参加考试", depends_on=["s2"])
        ranked = rank_tasks([s3, s1, s2], now=self.now)
        self.assertEqual(
            [t.record_id for t, _ in ranked], ["s1", "s2", "s3"]
        )

    def test_urgent_independent_beats_distant_prerequisite(self):
        """紧迫独立任务不被远期被依赖前置超越。

        前置 due 20 天后但 P0（有 blocking 加成），独立任务 due 明天 P2：
        紧迫度主导（0.50）下独立任务分数必须更高；
        依赖修正只保证 s1 在 s2 之前，不许插到 urgent 前面。
        """
        # 链：s1（远期前置，被 s2 依赖）→ s2
        s1 = self._task("s1", 20, priority="P0", title="远期前置")
        s2 = self._task("s2", 18, priority="P0", title="链上后继", depends_on=["s1"])
        urgent = self._task("urgent", 1, priority="P2", title="明天截止的独立任务")
        ranked = rank_tasks([s1, s2, urgent], now=self.now)
        ids = [t.record_id for t, _ in ranked]
        self.assertEqual(ids.index("urgent"), 0, f"排序结果 {ids}")
        self.assertLess(ids.index("s1"), ids.index("s2"))

    def test_blocking_discount_for_distant_tasks(self):
        """远期（紧迫度 <0.50）被依赖任务的阻塞加成减半。

        同任务对比：due 12 天（紧迫度约 0.435）时 blocking 贡献应为
        完整加成的一半（0.10×1/3×0.5≈0.017）；due 6 天（紧迫度 0.50）时
        加成完整生效（0.10×1/3≈0.033）。
        """
        distant = self._task("dep", 12, priority="P1", title="远期被依赖")
        s_block = calculate_score(distant, self.now, blocked_count=1)
        s_none = calculate_score(distant, self.now, blocked_count=0)
        self.assertAlmostEqual(s_block - s_none, 0.0167, places=3)

        near = self._task("udep", 6, priority="P1", title="近期被依赖")
        s_block2 = calculate_score(near, self.now, blocked_count=1)
        s_none2 = calculate_score(near, self.now, blocked_count=0)
        self.assertAlmostEqual(s_block2 - s_none2, 0.0333, places=3)


if __name__ == "__main__":
    unittest.main()
