"""M2 字段构建相关单元测试。

覆盖 build_create_fields / build_complete_fields / build_defer_fields / generate_external_id。
"""

from __future__ import annotations

import unittest
from datetime import datetime

from app.constants import (
    GOAL_STATUS_DOING,
    GOAL_STATUS_NOT_STARTED,
    STATUS_DONE,
    STATUS_TODO,
    SYNC_SOURCE_CLIENT,
    GoalFields,
    MonitorLogFields,
    TaskFields,
)
from app.feishu.repository import (
    build_complete_fields,
    build_create_fields,
    build_defer_fields,
    build_edit_fields,
    build_goal_fields,
    build_goal_edit_fields,
    build_monitor_log_fields,
    compute_goal_progresses,
    generate_external_id,
    Goal,
    Task,
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


class TestBuildGoalFields(unittest.TestCase):
    """M4.5：目标/项目表字段构建。"""

    def test_minimal(self):
        fields = build_goal_fields(title="完成毕业论文", external_id="win-goal01")
        self.assertEqual(fields[GoalFields.TITLE], "完成毕业论文")
        self.assertEqual(fields[GoalFields.LEVEL], "月")
        self.assertEqual(fields[GoalFields.STATUS], GOAL_STATUS_NOT_STARTED)
        self.assertEqual(fields[GoalFields.SYNC_SOURCE], SYNC_SOURCE_CLIENT)
        self.assertEqual(fields[GoalFields.EXTERNAL_ID], "win-goal01")
        # 未给出的可选字段不写入，避免误清空
        self.assertNotIn(GoalFields.START_AT, fields)
        self.assertNotIn(GoalFields.END_AT, fields)
        self.assertNotIn(GoalFields.DESCRIPTION, fields)
        self.assertNotIn(GoalFields.KEY_RESULTS, fields)

    def test_full(self):
        start = datetime(2026, 10, 1, 0, 0, 0)
        end = datetime(2026, 12, 31, 23, 59, 0)
        fields = build_goal_fields(
            title="学期科研计划",
            start_at=start,
            end_at=end,
            level="季",
            status=GOAL_STATUS_DOING,
            description="按月推进",
            key_results=["投出一篇论文", "  ", "完成实验"],
            external_id="win-goal02",
        )
        self.assertEqual(fields[GoalFields.START_AT], int(start.timestamp() * 1000))
        self.assertEqual(fields[GoalFields.END_AT], int(end.timestamp() * 1000))
        self.assertEqual(fields[GoalFields.LEVEL], "季")
        self.assertEqual(fields[GoalFields.STATUS], GOAL_STATUS_DOING)
        # 关键结果拼成多行文本，空项被过滤
        self.assertEqual(fields[GoalFields.KEY_RESULTS], "- 投出一篇论文\n- 完成实验")

    def test_key_results_accepts_plain_text(self):
        fields = build_goal_fields(title="x", key_results="已完成开题")
        self.assertEqual(fields[GoalFields.KEY_RESULTS], "已完成开题")


class TestBuildGoalEditFields(unittest.TestCase):
    """M4.5：目标编辑字段构建（只写改动键，日期可清空）。"""

    def test_partial_changes_only(self):
        fields = build_goal_edit_fields({"title": "新目标名"})
        self.assertEqual(fields, {GoalFields.TITLE: "新目标名"})

    def test_full_edit(self):
        start = datetime(2026, 10, 1, 8, 0, 0)
        end = datetime(2026, 12, 31, 23, 0, 0)
        fields = build_goal_edit_fields(
            {
                "title": "学期科研计划",
                "level": "季",
                "status": GOAL_STATUS_DOING,
                "start_at": start,
                "end_at": end,
                "description": "按月推进",
                "key_results": "- 投稿\n- 实验",
            }
        )
        self.assertEqual(fields[GoalFields.TITLE], "学期科研计划")
        self.assertEqual(fields[GoalFields.LEVEL], "季")
        self.assertEqual(fields[GoalFields.STATUS], GOAL_STATUS_DOING)
        self.assertEqual(fields[GoalFields.START_AT], int(start.timestamp() * 1000))
        self.assertEqual(fields[GoalFields.END_AT], int(end.timestamp() * 1000))
        self.assertEqual(fields[GoalFields.DESCRIPTION], "按月推进")
        self.assertEqual(fields[GoalFields.KEY_RESULTS], "- 投稿\n- 实验")

    def test_clear_dates_writes_null(self):
        """None 表示清空该日期列，应写 null。"""
        fields = build_goal_edit_fields({"start_at": None, "end_at": None})
        self.assertIsNone(fields[GoalFields.START_AT])
        self.assertIsNone(fields[GoalFields.END_AT])

    def test_progress_written(self):
        fields = build_goal_edit_fields({"progress": 50})
        self.assertEqual(fields, {GoalFields.PROGRESS: 50})


class TestComputeGoalProgresses(unittest.TestCase):
    """M4.5：目标进度自动回写——按目标下已结束任务占比计算。"""

    @staticmethod
    def _task(record_id: str, status: str, goal_id: str = "recG1") -> Task:
        return Task(
            record_id=record_id,
            title=record_id,
            status=status,
            goal_record_id=goal_id,
        )

    def test_progress_by_finished_ratio(self):
        tasks = [
            self._task("t1", STATUS_DONE),
            self._task("t2", STATUS_DONE),
            self._task("t3", STATUS_TODO),
            self._task("t4", "进行中"),
        ]
        result = compute_goal_progresses(tasks)
        self.assertEqual(result, {"recG1": 50})

    def test_task_without_goal_ignored(self):
        tasks = [self._task("t1", STATUS_DONE, goal_id="")]
        self.assertEqual(compute_goal_progresses(tasks), {})

    def test_goal_without_tasks_missing(self):
        """目标下没有任务时不产生键（调用方据此跳过该目标）。"""
        tasks = [self._task("t1", STATUS_DONE, goal_id="recOther")]
        self.assertEqual(compute_goal_progresses(tasks), {"recOther": 100})

    def test_all_done_is_100(self):
        tasks = [self._task("t1", STATUS_DONE), self._task("t2", STATUS_DONE)]
        self.assertEqual(compute_goal_progresses(tasks), {"recG1": 100})


class TestGoalRelationFields(unittest.TestCase):
    """M4.5：任务与目标、子任务与任务的关联字段。"""

    def test_create_with_goal_record_id(self):
        fields = build_create_fields(
            title="写文献综述",
            due_at=datetime(2026, 10, 20, 18, 0, 0),
            priority="P1",
            task_type="科研",
            external_id="win-task01",
            goal_record_id="recGoal01",
        )
        self.assertEqual(fields[TaskFields.GOAL], ["recGoal01"])

    def test_create_without_goal_omits_field(self):
        fields = build_create_fields(
            title="x",
            due_at=datetime(2026, 10, 20, 18, 0, 0),
            priority="P2",
            task_type="其他",
            external_id="win-task02",
        )
        self.assertNotIn(TaskFields.GOAL, fields)

    def test_edit_goal_record_id(self):
        fields = build_edit_fields({"goal_record_id": "recGoal02"})
        self.assertEqual(fields[TaskFields.GOAL], ["recGoal02"])

    def test_edit_clear_goal(self):
        """传空串表示解除关联，应写空数组而非省略。"""
        fields = build_edit_fields({"goal_record_id": "  "})
        self.assertEqual(fields[TaskFields.GOAL], [])


class TestBuildEditFields(unittest.TestCase):
    """编辑任务字段构建：仅写入调用方显式传入的字段。"""

    def test_only_provided_fields(self):
        fields = build_edit_fields({"title": "改后的标题"})
        self.assertEqual(fields[TaskFields.TITLE], "改后的标题")
        # 未传入的字段不应被写入，避免误清空远端数据
        self.assertNotIn(TaskFields.DUE_AT, fields)
        self.assertNotIn(TaskFields.PRIORITY, fields)
        self.assertNotIn(TaskFields.DESCRIPTION, fields)

    def test_multiple_fields(self):
        due = datetime(2026, 11, 1, 9, 0, 0)
        fields = build_edit_fields({
            "title": "新标题",
            "due_at": due,
            "priority": "P2",
            "description": "补充说明",
            "estimate_min": 45,
            "reminder_rules": ["提前10分钟"],
        })
        self.assertEqual(fields[TaskFields.TITLE], "新标题")
        self.assertEqual(fields[TaskFields.DUE_AT], int(due.timestamp() * 1000))
        self.assertEqual(fields[TaskFields.PRIORITY], "P2")
        self.assertEqual(fields[TaskFields.DESCRIPTION], "补充说明")
        self.assertEqual(fields[TaskFields.ESTIMATE_MIN], 45)
        self.assertEqual(fields[TaskFields.REMINDER_RULE], ["提前10分钟"])

    def test_clear_due_at(self):
        """日期传 None 表示清空，应为 null 而非省略。"""
        fields = build_edit_fields({"due_at": None})
        self.assertIn(TaskFields.DUE_AT, fields)
        self.assertIsNone(fields[TaskFields.DUE_AT])

    def test_clear_multi_select(self):
        """多选传空列表表示清空全部选项。"""
        fields = build_edit_fields({"tags": [], "reminder_rules": []})
        self.assertEqual(fields[TaskFields.TAGS], [])
        self.assertEqual(fields[TaskFields.REMINDER_RULE], [])

    def test_status_done_fills_completed_at(self):
        """状态改为已完成时自动补完成时间，避免脏数据。"""
        fields = build_edit_fields({"status": STATUS_DONE})
        self.assertEqual(fields[TaskFields.STATUS], STATUS_DONE)
        self.assertIn(TaskFields.COMPLETED_AT, fields)
        self.assertIsInstance(fields[TaskFields.COMPLETED_AT], int)

    def test_status_done_respects_explicit_completed_at(self):
        """显式给出完成时间时以传入值为准。"""
        done_at = datetime(2026, 10, 9, 20, 0, 0)
        fields = build_edit_fields({"status": STATUS_DONE, "completed_at": done_at})
        self.assertEqual(fields[TaskFields.COMPLETED_AT], int(done_at.timestamp() * 1000))

    def test_status_not_done_no_completed_at(self):
        """改为非完成状态时不写完成时间。"""
        fields = build_edit_fields({"status": STATUS_TODO})
        self.assertEqual(fields[TaskFields.STATUS], STATUS_TODO)
        self.assertNotIn(TaskFields.COMPLETED_AT, fields)

    def test_ai_breakdown_and_checklist(self):
        """AI拆解 / 检查清单 支持写入与清空。"""
        fields = build_edit_fields({
            "ai_breakdown": "1. 读文献\n2. 写提纲",
            "checklist": "开题报告已交",
        })
        self.assertEqual(fields[TaskFields.AI_BREAKDOWN], "1. 读文献\n2. 写提纲")
        self.assertEqual(fields[TaskFields.CHECKLIST], "开题报告已交")
        empty = build_edit_fields({"ai_breakdown": "", "checklist": ""})
        self.assertEqual(empty[TaskFields.AI_BREAKDOWN], "")
        self.assertEqual(empty[TaskFields.CHECKLIST], "")


if __name__ == "__main__":
    unittest.main()
