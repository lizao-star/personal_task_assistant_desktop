"""M2 离线队列与缓存 upsert 相关单元测试。"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.feishu.repository import Task
from app.services.cache import LocalCache


def _mk_task(record_id: str, title: str = "T", status: str = "待办") -> Task:
    """构造测试用 Task。"""
    return Task(
        record_id=record_id,
        title=title,
        status=status,
        priority="P1",
        due_at=datetime(2026, 10, 8, 18, 0, 0),
        modified_at=datetime(2026, 10, 7, 12, 0, 0),
    )


class TestPendingOps(unittest.TestCase):
    """离线队列：入队/列表/删除/force。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = LocalCache(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_enqueue_and_list(self):
        op1 = self.cache.enqueue_op("create", {"fields": {"任务名": "a"}})
        op2 = self.cache.enqueue_op("complete", {"record_id": "r1"})
        self.assertLess(op1, op2)

        ops = self.cache.list_pending_ops()
        self.assertEqual(len(ops), 2)
        self.assertEqual(ops[0].op_type, "create")
        self.assertEqual(ops[1].op_type, "complete")
        self.assertFalse(ops[0].force)

    def test_delete_op(self):
        op1 = self.cache.enqueue_op("create", {})
        op2 = self.cache.enqueue_op("complete", {})
        self.cache.delete_op(op1)
        ops = self.cache.list_pending_ops()
        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].op_id, op2)

    def test_set_force(self):
        op = self.cache.enqueue_op("complete", {"record_id": "r1"})
        self.cache.set_op_force(op)
        ops = self.cache.list_pending_ops()
        self.assertTrue(ops[0].force)

    def test_pending_count(self):
        self.assertEqual(self.cache.pending_count(), 0)
        self.cache.enqueue_op("create", {})
        self.cache.enqueue_op("defer", {})
        self.assertEqual(self.cache.pending_count(), 2)


class TestTaskUpsert(unittest.TestCase):
    """upsert_tasks 增量合并。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = LocalCache(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_upsert_insert_and_update(self):
        # 初次插入两条
        self.cache.replace_tasks([_mk_task("r1", "任务一"), _mk_task("r2", "任务二")])
        tasks = self.cache.load_tasks()
        self.assertEqual(len(tasks), 2)

        # upsert 修改 r1 + 新增 r3；r2 应保持不变
        updated = _mk_task("r1", "任务一(改)", status="已完成")
        new = _mk_task("r3", "任务三")
        self.cache.upsert_tasks([updated, new])

        tasks = {t.record_id: t for t in self.cache.load_tasks()}
        self.assertEqual(len(tasks), 3)
        self.assertEqual(tasks["r1"].title, "任务一(改)")
        self.assertEqual(tasks["r1"].status, "已完成")
        self.assertEqual(tasks["r2"].title, "任务二")  # 未被 upsert 触及
        self.assertEqual(tasks["r3"].title, "任务三")

    def test_remove_tasks(self):
        self.cache.replace_tasks([_mk_task("r1"), _mk_task("r2"), _mk_task("r3")])
        self.cache.remove_tasks(["r1", "r3"])
        tasks = self.cache.load_tasks()
        self.assertEqual([t.record_id for t in tasks], ["r2"])

    def test_datetime_roundtrip(self):
        """datetime 字段（含新增 completed_at/modified_at）能正确还原。"""
        t = _mk_task("r1")
        t.completed_at = datetime(2026, 10, 7, 13, 0, 0)
        self.cache.replace_tasks([t])
        loaded = self.cache.load_tasks()[0]
        self.assertEqual(loaded.due_at, t.due_at)
        self.assertEqual(loaded.completed_at, t.completed_at)
        self.assertEqual(loaded.modified_at, t.modified_at)


if __name__ == "__main__":
    unittest.main()
