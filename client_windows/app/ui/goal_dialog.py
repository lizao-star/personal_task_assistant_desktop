# -*- coding: utf-8 -*-
"""目标编辑对话框（M4.5）。

双击主窗口目标计划区的目标行打开，与任务详情对话框同一套交互：
1. 查看：层级 / 状态 / 起止 / 进度 / 描述 / 关键结果；
2. 编辑：字段直接改，点「保存修改」只把真正改动的字段交给 main.py
   走离线队列 + 冲突检测写回飞书目标表；
3. 进度为只读展示：由客户端按「目标下已完成任务占比」自动计算并写回，
   手工修改会被下一轮同步覆盖。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QDate, QDateTime, QTime, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QScrollArea,
    QVBoxLayout,
    QWidget,
    QFrame,
)

from ..constants import GOAL_LEVEL_OPTIONS, GOAL_STATUS_OPTIONS
from ..feishu.repository import Goal


class GoalEditDialog(QDialog):
    """可编辑的目标详情页。"""

    # 点「保存修改」时发出：{record_id, changes, title}
    save_requested = Signal(dict)

    def __init__(self, goal: Goal, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("目标详情")
        self.setModal(True)
        self.setMinimumSize(480, 360)
        # 小屏幕上不超出可见区域：内容区可滚动，底部按钮始终可见
        width, height = 560, 620
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, available.width() - 80)
            height = min(height, available.height() - 80)
        self.resize(width, height)
        self._goal = goal
        self._build_ui()
        self._fill(goal)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ===== 可滚动内容区 =====
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        form = QFormLayout()
        content_layout.addLayout(form)

        self.title_edit = QLineEdit()
        form.addRow("目标名", self.title_edit)

        self.level_combo = QComboBox()
        self.level_combo.addItems(GOAL_LEVEL_OPTIONS)
        # 允许当前值为空时展示空白项
        self.level_combo.setCurrentIndex(-1)
        form.addRow("层级", self.level_combo)

        self.status_combo = QComboBox()
        self.status_combo.addItems(GOAL_STATUS_OPTIONS)
        self.status_combo.setCurrentIndex(-1)
        form.addRow("状态", self.status_combo)

        # 起止时间：可清空（勾选清空后置 None，写回时按「清空该列」处理）
        self.start_edit = self._make_datetime_edit()
        start_row = QHBoxLayout()
        start_row.addWidget(self.start_edit)
        self.start_clear = QCheckBox("清空")
        start_row.addWidget(self.start_clear)
        form.addRow("开始时间", start_row)

        self.end_edit = self._make_datetime_edit()
        end_row = QHBoxLayout()
        end_row.addWidget(self.end_edit)
        self.end_clear = QCheckBox("清空")
        end_row.addWidget(self.end_clear)
        form.addRow("结束时间", end_row)

        # 进度只读：由客户端按已完成任务占比自动计算写回
        self.progress_label = QLabel()
        self.progress_label.setStyleSheet("color: #666;")
        form.addRow("进度", self.progress_label)

        self.description_edit = QPlainTextEdit()
        self.description_edit.setPlaceholderText("目标描述（为什么做、衡量标准等）")
        self.description_edit.setFixedHeight(96)
        form.addRow("描述", self.description_edit)

        self.key_results_edit = QPlainTextEdit()
        self.key_results_edit.setPlaceholderText("关键结果（每行一条，衡量目标是否达成）")
        form.addRow("关键结果", self.key_results_edit)

        content_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        layout.addWidget(scroll, stretch=1)

        # ===== 按钮（固定在滚动区外） =====
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存修改")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _make_datetime_edit() -> QDateTimeEdit:
        """与任务详情一致的日期控件：可弹日历，精确到分钟。"""
        edit = QDateTimeEdit(QDateTime.currentDateTime())
        edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        edit.setCalendarPopup(True)
        return edit

    # ------------------------------------------------------------------
    # 数据填充与收集
    # ------------------------------------------------------------------
    def _fill(self, goal: Goal) -> None:
        self.title_edit.setText(goal.title)
        if goal.level:
            self.level_combo.setCurrentText(goal.level)
        if goal.status:
            self.status_combo.setCurrentText(goal.status)

        self._set_datetime(self.start_edit, self.start_clear, goal.start_at)
        self._set_datetime(self.end_edit, self.end_clear, goal.end_at)

        self.progress_label.setText(
            f"{goal.progress}%（由客户端按已完成任务占比自动更新，无需手动修改）"
        )
        self.description_edit.setPlainText(goal.description)
        self.key_results_edit.setPlainText(goal.key_results)

    @staticmethod
    def _set_datetime(
        edit: QDateTimeEdit, clear_box: QCheckBox, value: datetime | None
    ) -> None:
        if value is None:
            clear_box.setChecked(True)
        else:
            clear_box.setChecked(False)
            edit.setDateTime(
                QDateTime(
                    QDate(value.year, value.month, value.day),
                    QTime(value.hour, value.minute),
                )
            )

    def _collect_changes(self) -> dict:
        """对比当前输入与原始数据，只收集真正改动的字段（键与 Goal 属性同名）。"""
        goal = self._goal
        changes: dict = {}

        title = self.title_edit.text().strip()
        if title != goal.title:
            changes["title"] = title

        level = self.level_combo.currentText()
        if level != goal.level:
            changes["level"] = level

        status = self.status_combo.currentText()
        if status != goal.status:
            changes["status"] = status

        changes["start_at"] = self._read_datetime(
            self.start_edit, self.start_clear, goal.start_at
        )
        changes["end_at"] = self._read_datetime(
            self.end_edit, self.end_clear, goal.end_at
        )

        description = self.description_edit.toPlainText()
        if description != goal.description:
            changes["description"] = description

        key_results = self.key_results_edit.toPlainText()
        if key_results != goal.key_results:
            changes["key_results"] = key_results
        return changes

    @staticmethod
    def _read_datetime(
        edit: QDateTimeEdit, clear_box: QCheckBox, original: datetime | None
    ) -> datetime | None:
        """读取日期控件：勾选清空返回 None；值与原值相同则不收集。"""
        if clear_box.isChecked():
            return None if original is not None else _SKIP
        dt = edit.dateTime()
        value = datetime(
            dt.date().year(), dt.date().month(), dt.date().day(),
            dt.time().hour(), dt.time().minute(),
        )
        if original is not None and value == original.replace(second=0, microsecond=0):
            return _SKIP
        return value

    def _on_save(self) -> None:
        """保存：把 _SKIP 哨兵剔除后只留真正改动的字段。"""
        raw = self._collect_changes()
        changes = {k: v for k, v in raw.items() if v is not _SKIP}
        if not changes:
            self.reject()
            return
        self.save_requested.emit(
            {
                "record_id": self._goal.record_id,
                "changes": changes,
                "title": self.title_edit.text().strip() or self._goal.title,
            }
        )
        self.accept()


# 内部哨兵：日期控件值未变化时使用，最终会被剔除
_SKIP = object()
