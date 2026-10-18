"""主窗口任务表格选中逻辑的回归测试。

背景：三张任务表格的选中状态互不影响（Qt 的单选仅在本表内生效），
早期实现按「待办 → 逾期 → 收集箱」固定优先级取 record_id，导致
在待办表残留选中时操作逾期任务会误改待办任务。本测试锁定修复后的行为。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.feishu.repository import Goal, Task  # noqa: E402
from app.ui.main_window import MainWindow, TaskTable  # noqa: E402

_app = QApplication.instance() or QApplication([])


def make_task(record_id: str, title: str) -> Task:
    return Task(record_id=record_id, title=title)


class TaskTableSelectionTest(unittest.TestCase):
    """TaskTable 的行选中读取行为。"""

    def setUp(self):
        self.table = TaskTable()
        self.table.render([(make_task("r1", "任务一"), 1.0),
                           (make_task("r2", "任务二"), 0.5)])

    def test_selected_row_returns_record_id(self):
        self.table.selectRow(1)
        self.assertEqual(self.table.selected_record_id(), "r2")

    def test_cleared_selection_returns_empty(self):
        """清除选中后必须返回空串，不能返回 currentRow 残留的旧行。"""
        self.table.selectRow(0)
        self.table.clearSelection()
        self.assertEqual(self.table.selected_record_id(), "")

    def test_no_selection_returns_empty(self):
        self.assertEqual(self.table.selected_record_id(), "")

    def test_render_clears_previous_selection(self):
        """重新渲染后行内容已变，旧选中必须被清掉，避免指向别的任务。"""
        self.table.selectRow(0)
        self.table.render([(make_task("r9", "新任务"), 3.0)])
        self.assertEqual(self.table.selected_record_id(), "")

    def test_row_out_of_range_returns_empty(self):
        self.assertEqual(self.table.selected_record_id_from_row(99), "")


class CurrentSelectionTargetTest(unittest.TestCase):
    """主窗口「完成/延期/详情」目标定位。"""

    def setUp(self):
        self.window = MainWindow()
        self.today = make_task("today-1", "金融经济学作业")
        self.overdue = make_task("overdue-2", "逾期任务2")
        self.window.render([(self.today, 1.0)], [(self.overdue, 1.0)], [])

    def tearDown(self):
        self.window.close()

    def test_target_follows_last_clicked_table(self):
        """先选待办、再选逾期：目标应为逾期任务（历史 bug 会误判为待办）。"""
        self.window.today_table.selectRow(0)
        self.window.today_table.cellClicked.emit(0, 0)
        self.window.overdue_table.selectRow(0)
        self.window.overdue_table.cellClicked.emit(0, 0)
        self.assertEqual(self.window._current_selected_record_id(), "overdue-2")

    def test_clicking_table_clears_other_tables_selection(self):
        """点击逾期表后，待办表的选中应被清除，杜绝残留抢占。"""
        self.window.today_table.selectRow(0)
        self.window.overdue_table.selectRow(0)
        self.window.overdue_table.cellClicked.emit(0, 0)
        self.assertEqual(self.window.today_table.selected_record_id(), "")

    def test_no_selection_returns_empty(self):
        self.assertEqual(self.window._current_selected_record_id(), "")


class GoalTableTest(unittest.TestCase):
    """目标计划表格（M4.5）：渲染与按行取目标对象。"""

    def setUp(self):
        from app.ui.main_window import GoalTable

        self.table = GoalTable()
        self.goal = Goal(record_id="g1", title="毕业论文", level="月", progress=40,
                         task_record_ids=["t1", "t2"])
        self.table.render([self.goal])

    def test_render_shows_title_and_task_count(self):
        self.assertEqual(self.table.item(0, 0).text(), "毕业论文")
        self.assertEqual(self.table.item(0, 3).text(), "40%")
        self.assertEqual(self.table.item(0, 5).text(), "2")

    def test_goal_from_row(self):
        self.assertIs(self.table.goal_from_row(0), self.goal)
        self.assertIsNone(self.table.goal_from_row(9))


class DoubleClickDetailTest(unittest.TestCase):
    """双击任务行应打开可编辑详情页（M4.5：编辑与详情合并）。"""

    def setUp(self):
        self.window = MainWindow()
        self.overdue = make_task("overdue-2", "逾期任务2")
        self.window.render([], [(self.overdue, 1.0)], [])
        self.detail_ids: list[str] = []
        self.window.detail_requested.connect(self.detail_ids.append)

    def tearDown(self):
        self.window.close()

    def test_double_click_emits_detail(self):
        """双击行 → 发出 detail_requested(record_id)，由 main.py 组装数据后弹详情页。"""
        self.window.overdue_table.cellDoubleClicked.emit(0, 0)
        self.assertEqual(self.detail_ids, ["overdue-2"])

    def test_double_click_out_of_range_no_signal(self):
        """越界行不应发出信号。"""
        self.window.overdue_table.cellDoubleClicked.emit(99, 0)
        self.assertEqual(self.detail_ids, [])


class DetailSaveWiringTest(unittest.TestCase):
    """详情页内的「保存修改」应转成 edit_requested，走既有写回链路。"""

    def setUp(self):
        self.window = MainWindow()
        self.task = make_task("r1", "任务一")
        self.window.render([(self.task, 1.0)], [], [])
        self.edits: list[dict] = []
        self.window.edit_requested.connect(self.edits.append)

    def tearDown(self):
        self.window.close()

    def test_save_requested_forwards_to_edit(self):
        from unittest.mock import patch

        fake = _FakeDetailDialog()
        with patch("app.ui.main_window.TaskDetailDialog", return_value=fake):
            self.window.show_task_detail(self.task, [])
        fake.save_requested.emit(
            {"record_id": "r1", "changes": {"title": "改后的标题"}}
        )
        self.assertEqual(
            self.edits,
            [{"record_id": "r1", "changes": {"title": "改后的标题"}}],
        )


class _FakeDetailDialog:
    """替身详情对话框：只提供信号，绕过真实 QDialog。"""

    def __init__(self):
        from PySide6.QtCore import QObject, Signal

        class _Signals(QObject):
            apply_requested = Signal(dict)
            save_requested = Signal(dict)

        self._signals = _Signals()
        self.apply_requested = self._signals.apply_requested
        self.save_requested = self._signals.save_requested

    def exec(self):
        from PySide6.QtWidgets import QDialog

        return QDialog.Rejected


if __name__ == "__main__":
    unittest.main()
