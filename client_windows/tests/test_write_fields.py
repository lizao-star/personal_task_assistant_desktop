"""M2 字段构建相关单元测试。

覆盖 build_create_fields / build_complete_fields / build_defer_fields / generate_external_id。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from app.constants import (
    STATUS_DONE,
    STATUS_TODO,
    SYNC_SOURCE_CLIENT,
    MonitorLogFields,
    TaskFields,
)
from app.feishu.repository import (
    build_complete_fields,
    build_create_fields,
    build_defer_fields,
    build_monitor_log_fields,
    generate_external_id,
)


class TestGenerateExternalId(unittest.TestCase):
    """外部ID生成：格式与唯一性。"""

    def test_format(self):
        eid = generate_external_id()
        self.assertTrue(eid.startswith("win-"))
        # win- 后跟 12 位 hex
        self.assertEqual(len(eid), 4 + 12)

    def test_unique(self):
        ids = {generate_external_id() for _ in range(1000)}
        self.assertEqual(len(ids), 1000)


class TestBuildCreateFields(unittest.TestCase):
    """新建任务字段构建。"""

    def test_minimal(self):
        due = datetime(2026, 10, 8, 18, 0, 0)
        fields = build_create_fields(
            title="读论文",
            due_at=due,
            priority="P1",
            task_type="科研",
            external_id="win-abc123def456",
        )
        self.assertEqual(fields[TaskFields.TITLE], "读论文")
        self.assertEqual(fields[TaskFields.STATUS], STATUS_TODO)
        self.assertEqual(fields[TaskFields.PRIORITY], "P1")
        self.assertEqual(fields[TaskFields.TYPE], "科研")
        self.assertEqual(fields[TaskFields.DUE_AT], int(due.timestamp() * 1000))
        self.assertEqual(fields[TaskFields.EXTERNAL_ID], "win-abc123def456")
        self.assertEqual(fields[TaskFields.SYNC_SOURCE], SYNC_SOURCE_CLIENT)
        self.assertEqual(fields[TaskFields.DELAY_COUNT], 0)
        # 可选字段缺省时不写入
        self.assertNotIn(TaskFields.DESCRIPTION, fields)
        self.assertNotIn(TaskFields.ESTIMATE_MIN, fields)

    def test_full(self):
        due = datetime(2026, 10, 8, 18, 0, 0)
        fields = build_create_fields(
            title="写报告",
            due_at=due,
            priority="P0",
            task_type="作业",
            external_id="win-xxx",
            description="参考文献 ABC",
            estimate_min=120,
            reminder_rules=["提前30分钟"],
        )
        self.assertEqual(fields[TaskFields.DESCRIPTION], "参考文献 ABC")
        self.assertEqual(fields[TaskFields.ESTIMATE_MIN], 120)
        self.assertEqual(fields[TaskFields.REMINDER_RULE], ["提前30分钟"])


class TestBuildCompleteFields(unittest.TestCase):
    """完成任务字段构建。"""

    def test_sets_status_and_time(self):
        now = datetime(2026, 10, 7, 12, 0, 0)
        fields = build_complete_fields(now=now)
        self.assertEqual(fields[TaskFields.STATUS], STATUS_DONE)
        self.assertEqual(fields[TaskFields.COMPLETED_AT], int(now.timestamp() * 1000))


class TestBuildDeferFields(unittest.TestCase):
    """延期任务字段构建。"""

    def test_updates_due_and_count(self):
        new_due = datetime(2026, 10, 10, 23, 59, 0)
        fields = build_defer_fields(new_due, 2)
        self.assertEqual(fields[TaskFields.DUE_AT], int(new_due.timestamp() * 1000))
        self.assertEqual(fields[TaskFields.DELAY_COUNT], 3)
        self.assertEqual(fields[TaskFields.STATUS], STATUS_TODO)


class TestBuildMonitorLogFields(unittest.TestCase):
    """娱乐监控日志字段构建（M4）。

    重点回归：URL 列是飞书「超链接」字段，必须传 {"text","link"} 对象，
    否则会报 1254068 URLFieldConvFail。
    """

    def setUp(self):
        self.now = datetime(2026, 10, 7, 15, 30, 0)

    def test_url_is_hyperlink_object(self):
        fields = build_monitor_log_fields(
            occurred_at=self.now,
            duration_seconds=600,
            url="bilibili.com",
        )
        value = fields[MonitorLogFields.URL]
        self.assertIsInstance(value, dict)
        self.assertEqual(value["text"], "bilibili.com")
        self.assertEqual(value["link"], "https://bilibili.com")

    def test_url_keeps_existing_scheme(self):
        fields = build_monitor_log_fields(
            occurred_at=self.now,
            duration_seconds=60,
            url="http://example.com/page",
        )
        self.assertEqual(fields[MonitorLogFields.URL]["link"], "http://example.com/page")

    def test_url_omitted_when_empty(self):
        fields = build_monitor_log_fields(
            occurred_at=self.now, duration_seconds=60, url=""
        )
        self.assertNotIn(MonitorLogFields.URL, fields)

    def test_basic_fields(self):
        fields = build_monitor_log_fields(
            occurred_at=self.now,
            duration_seconds=2700,
            process_name="chrome.exe",
            window_title="哔哩哔哩",
            url="bilibili.com",
            notified=True,
            notify_count=3,
            closed=True,
            note="L4 触发",
        )
        self.assertEqual(fields[MonitorLogFields.OCCURRED_AT], int(self.now.timestamp() * 1000))
        self.assertEqual(fields[MonitorLogFields.DURATION], 2700)
        self.assertTrue(fields[MonitorLogFields.NOTIFIED])
        self.assertEqual(fields[MonitorLogFields.NOTIFY_COUNT], 3)
        self.assertTrue(fields[MonitorLogFields.CLOSED])
        self.assertEqual(fields[MonitorLogFields.NOTE], "L4 触发")


if __name__ == "__main__":
    unittest.main()
