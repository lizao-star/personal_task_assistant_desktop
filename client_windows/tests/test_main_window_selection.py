"""主窗口任务表格选中逻辑的回归测试。

背景：三张任务表格的选中状态互不影响（Qt 的单选仅在本表内生效），
早期实现按「待办 → 逾期 → 收集箱」固定优先级取 record_id，导致
在待办表残留选中时操作逾期任务会误改待办任务。本测试锁定修复后的行为。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.feishu.repository import Task  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
