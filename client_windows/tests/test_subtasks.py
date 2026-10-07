"""M3 子任务与 AI 字段相关单元测试。

覆盖 to_task 的 AI拆解/检查清单解析、to_subtask 解析、
group_subtasks 分组排序、LocalCache 子任务缓存读写。
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from app.constants import SubtaskFields, TaskFields
from app.feishu.repository import SubTask, group_subtasks, to_subtask, to_task
from app.services.cache import LocalCache


def _mk_task_record(fields: dict) -> dict:
    """构造飞书任务表原始记录形态。"""
    return {"record_id": "recTask1", "fields": fields}


def _mk_sub_record(record_id: str, fields: dict) -> dict:
    """构造飞书子任务表原始记录形态。"""
    return {"record_id": record_id, "fields": fields}


class TestTaskAIFields(unittest.TestCase):
    """to_task 对 AI拆解 / 检查清单 的解析。"""

    def test_parse_ai_fields(self):
        record = _mk_task_record(
            {
                TaskFields.TITLE: [{"type": "text", "text": "写论文"}],
                TaskFields.AI_BREAKDOWN: [
                    {"type": "text", "text": "1. 找文献\n2. 写提纲"}
                ],
                TaskFields.CHECKLIST: [
                    {"type": "text", "text": "查重\n格式检查"}
                ],
            }
        )
        task = to_task(record)
        self.assertEqual(task.ai_breakdown, "1. 找文献\n2. 写提纲")
        self.assertEqual(task.checklist, "查重\n格式检查")

    def test_missing_ai_fields_default_empty(self):
        record = _mk_task_record({TaskFields.TITLE: "读论文"})
        task = to_task(record)
        self.assertEqual(task.ai_breakdown, "")
        self.assertEqual(task.checklist, "")


class TestToSubtask(unittest.TestCase):
    """to_subtask 字段解析（含关联字段两种返回形态）。"""

    def test_parse_full(self):
        record = _mk_sub_record(
            "recSub1",
            {
                SubtaskFields.TITLE: [{"type": "text", "text": "找 5 篇文献"}],
                SubtaskFields.PARENT: ["recTask1"],
                SubtaskFields.STATUS: "进行中",
                SubtaskFields.ORDER: 2,
                SubtaskFields.ESTIMATE_MIN: 60,
                SubtaskFields.DUE_AT: 1696492800000,
                SubtaskFields.AI_HINT: [{"type": "text", "text": "优先中文核心"}],
            },
        )
        sub = to_subtask(record)
        self.assertEqual(sub.record_id, "recSub1")
        self.assertEqual(sub.parent_record_id, "recTask1")
        self.assertEqual(sub.title, "找 5 篇文献")
        self.assertEqual(sub.status, "进行中")
        self.assertEqual(sub.order, 2)
        self.assertEqual(sub.estimate_min, 60)
        self.assertIsNotNone(sub.due_at)
        self.assertEqual(sub.ai_hint, "优先中文核心")
        self.assertFalse(sub.is_finished)

    def test_parse_link_dict_shape(self):
        """关联字段为 {"link_record_ids":[...]} 形态时也能解析。"""
        record = _mk_sub_record(
            "recSub2",
            {
                SubtaskFields.TITLE: "写提纲",
                SubtaskFields.PARENT: {"link_record_ids": ["recTask9"]},
            },
        )
        sub = to_subtask(record)
        self.assertEqual(sub.parent_record_id, "recTask9")

    def test_parse_link_record_ids_shape(self):
        """关联字段为 [{"record_ids":[...]}] 形态（较新 API 版本）时也能解析。"""
        record = _mk_sub_record(
            "recSub2b",
            {
                SubtaskFields.TITLE: "写提纲",
                SubtaskFields.PARENT: [
                    {
                        "record_ids": ["recTask8"],
                        "table_id": "tblTask",
                        "text": "主任务",
                        "type": "text",
                    }
                ],
            },
        )
        sub = to_subtask(record)
        self.assertEqual(sub.parent_record_id, "recTask8")

    def test_no_parent(self):
        sub = to_subtask(_mk_sub_record("recSub3", {SubtaskFields.TITLE: "孤儿步骤"}))
        self.assertEqual(sub.parent_record_id, "")
        self.assertEqual(sub.title, "孤儿步骤")

    def test_finished_status(self):
        sub = to_subtask(
            _mk_sub_record(
                "recSub4",
                {SubtaskFields.TITLE: "已完成步骤", SubtaskFields.STATUS: "已完成"},
            )
        )
        self.assertTrue(sub.is_finished)


class TestGroupSubtasks(unittest.TestCase):
    """group_subtasks 过滤与排序。"""

    def setUp(self):
        self.subtasks = [
            SubTask(record_id="a", parent_record_id="t1", title="步骤B", order=2),
            SubTask(record_id="b", parent_record_id="t1", title="步骤A", order=1),
            SubTask(record_id="c", parent_record_id="t2", title="别家步骤", order=1),
            SubTask(record_id="d", parent_record_id="t1", title="无顺序", order=0),
        ]

    def test_filter_and_sort(self):
        grouped = group_subtasks(self.subtasks, "t1")
        self.assertEqual([s.record_id for s in grouped], ["d", "b", "a"])

    def test_empty(self):
        self.assertEqual(group_subtasks(self.subtasks, "nope"), [])


class TestSubtaskCache(unittest.TestCase):
    """LocalCache 子任务快照的替换与读取。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = LocalCache(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        self.cache.close()
        self.tmp.cleanup()

    def test_roundtrip(self):
        sub = SubTask(
            record_id="recSub1",
            parent_record_id="recTask1",
            title="找文献",
            status="待办",
            order=1,
            due_at=datetime(2026, 10, 8, 12, 0, 0),
            ai_hint="先中文后英文",
        )
        self.cache.replace_subtasks([sub])
        loaded = self.cache.load_subtasks()
        self.assertEqual(len(loaded), 1)
        got = loaded[0]
        self.assertEqual(got.record_id, "recSub1")
        self.assertEqual(got.parent_record_id, "recTask1")
        self.assertEqual(got.title, "找文献")
        self.assertEqual(got.due_at, datetime(2026, 10, 8, 12, 0, 0))
        self.assertEqual(got.ai_hint, "先中文后英文")

    def test_replace_clears_old(self):
        self.cache.replace_subtasks(
            [SubTask(record_id="old", parent_record_id="t", title="旧")]
        )
        self.cache.replace_subtasks(
            [SubTask(record_id="new", parent_record_id="t", title="新")]
        )
        loaded = self.cache.load_subtasks()
        self.assertEqual([s.record_id for s in loaded], ["new"])

    def test_empty_then_load(self):
        self.cache.replace_subtasks([])
        self.assertEqual(self.cache.load_subtasks(), [])


if __name__ == "__main__":
    unittest.main()
