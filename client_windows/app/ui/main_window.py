"""主窗口：展示逾期任务与今日待办。

数据来源是 (任务, 优先级分数) 列表；网络请求在后台线程完成，
本窗口只负责渲染与发出操作信号。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..config import APP_ICON_PATH
from ..feishu.repository import Task
from .tray import load_app_icon

# 表格列定义：列名 -> 取值函数
COLUMNS = [
    ("优先级", lambda t, s: t.priority or "-"),
    ("任务名", lambda t, s: t.title),
    ("状态", lambda t, s: t.status or "-"),
    ("截止时间", lambda t, s: t.due_at.strftime("%Y-%m-%d %H:%M") if t.due_at else "-"),
    ("类型", lambda t, s: t.task_type or "-"),
    ("分数", lambda t, s: f"{s:.2f}"),
]


class TaskTable(QTableWidget):
    """只读任务表格。"""

    def __init__(self):
        super().__init__(0, len(COLUMNS))
        self.setHorizontalHeaderLabels([name for name, _ in COLUMNS])
        self.verticalHeader().setVisible(False)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setAlternatingRowColors(True)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        # 任务名这一列自动拉伸占满剩余空间
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)

    def render(self, scored_tasks: list[tuple[Task, float]]) -> None:
        """用打分后的任务列表填充表格。"""
        self.setRowCount(len(scored_tasks))
        for row, (task, score) in enumerate(scored_tasks):
            for col, (_, getter) in enumerate(COLUMNS):
                item = QTableWidgetItem(getter(task, score))
                # 单元格数据不参与编辑，只用于展示
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                if col == 0 and task.priority == "P0":
                    # P0 任务加粗显示
                    font = item.font()
                    font.setBold(True)
                    item.setFont(font)
                self.setItem(row, col, item)


class MainWindow(QWidget):
    """主窗口。"""

    # 用户操作信号（由 main.py 接线）
    sync_requested = Signal()
    pause_toggled = Signal(bool)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("个人任务助理")
        self.setWindowIcon(load_app_icon())
        self.resize(720, 560)
        self._paused = False
        self._build_ui()

    def _build_ui(self) -> None:
        """搭建界面布局。"""
        layout = QVBoxLayout(self)

        # ===== 顶部操作栏 =====
        top = QHBoxLayout()
        self.status_label = QLabel("尚未同步")
        top.addWidget(self.status_label, stretch=1)

        self.pause_button = QPushButton("暂停提醒")
        self.pause_button.clicked.connect(self._on_pause_clicked)
        top.addWidget(self.pause_button)

        self.sync_button = QPushButton("立即同步")
        self.sync_button.clicked.connect(self.sync_requested.emit)
        top.addWidget(self.sync_button)
        layout.addLayout(top)

        # ===== 配置缺失警告（默认隐藏） =====
        self.warning_label = QLabel()
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet("color: #b00020; font-weight: bold;")
        self.warning_label.setVisible(False)
        layout.addWidget(self.warning_label)

        # ===== 逾期任务区 =====
        self.overdue_title = QLabel("⚠️ 逾期任务")
        f = self.overdue_title.font()
        f.setBold(True)
        self.overdue_title.setFont(f)
        layout.addWidget(self.overdue_title)
        self.overdue_table = TaskTable()
        layout.addWidget(self.overdue_table)

        # ===== 今日待办区 =====
        self.today_title = QLabel("📋 今日待办")
        self.today_title.setFont(f)
        layout.addWidget(self.today_title)
        self.today_table = TaskTable()
        layout.addWidget(self.today_table, stretch=1)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def render(
        self,
        today_scored: list[tuple[Task, float]],
        overdue_scored: list[tuple[Task, float]],
        synced_at: datetime | None = None,
    ) -> None:
        """渲染两个任务表格。"""
        self.today_table.render(today_scored)
        self.overdue_table.render(overdue_scored)
        self.today_title.setText(f"📋 今日待办（{len(today_scored)}）")
        self.overdue_title.setText(f"⚠️ 逾期任务（{len(overdue_scored)}）")
        if synced_at:
            self.status_label.setText(f"最近同步：{synced_at.strftime('%Y-%m-%d %H:%M')}")

    def show_warning(self, text: str) -> None:
        """显示配置或同步警告。"""
        self.warning_label.setText(text)
        self.warning_label.setVisible(bool(text))

    def set_syncing(self, syncing: bool) -> None:
        """切换同步中状态，防止重复点击。"""
        self.sync_button.setEnabled(not syncing)
        if syncing:
            self.status_label.setText("正在同步…")

    def set_paused(self, paused: bool) -> None:
        """设置提醒暂停状态。"""
        self._paused = paused
        self.pause_button.setText("恢复提醒" if paused else "暂停提醒")

    def is_paused(self) -> bool:
        return self._paused

    def _on_pause_clicked(self) -> None:
        """暂停/恢复按钮回调。"""
        self.set_paused(not self._paused)
        self.pause_toggled.emit(self._paused)

    def closeEvent(self, event) -> None:
        """点击关闭按钮时不退出程序，隐藏到托盘继续后台运行。"""
        event.ignore()
        self.hide()
