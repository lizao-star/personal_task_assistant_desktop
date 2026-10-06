"""新建任务对话框。

返回一个 dict，字段与 build_create_fields 的关键字参数一一对应：
    title / due_at / priority / task_type / estimate_min / description
外部ID 与同步来源由调用方生成，不在本对话框内处理。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..constants import PRIORITY_OPTIONS, TASK_TYPE_OPTIONS


class NewTaskDialog(QDialog):
    """新建任务表单。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("新建任务")
        self.setModal(True)
        self.resize(420, 360)
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # 任务名（必填）
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("例如：读完《XXX》第三章")
        form.addRow("任务名*", self.title_edit)

        # 截止时间（默认明天 18:00）
        self.due_edit = QDateTimeEdit(datetime.now() + timedelta(days=1))
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setCalendarPopup(True)
        # 把时间默认调到 18:00，避免当前分钟造成"已经过了"的错觉
        tomorrow_18 = datetime.now().replace(hour=18, minute=0, second=0) + timedelta(days=1)
        self.due_edit.setDateTime(tomorrow_18)
        form.addRow("截止时间*", self.due_edit)

        # 优先级
        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITY_OPTIONS)
        self.priority_combo.setCurrentText("P2")
        form.addRow("优先级", self.priority_combo)

        # 任务类型
        self.type_combo = QComboBox()
        self.type_combo.addItems(TASK_TYPE_OPTIONS)
        self.type_combo.setCurrentText("其他")
        form.addRow("任务类型", self.type_combo)

        # 预计耗时（分钟）
        self.estimate_spin = QSpinBox()
        self.estimate_spin.setRange(0, 24 * 60)
        self.estimate_spin.setSingleStep(15)
        self.estimate_spin.setSuffix(" 分钟")
        form.addRow("预计耗时", self.estimate_spin)

        # 描述
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("可选：补充说明、检查清单草稿、相关链接……")
        form.addRow("描述", self.desc_edit)

        layout.addLayout(form)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def accept(self) -> None:  # type: ignore[override]
        """必填校验：任务名不能为空。"""
        if not self.title_edit.text().strip():
            self.title_edit.setFocus()
            self.title_edit.setPlaceholderText("任务名必填！")
            return
        super().accept()

    def result_dict(self) -> dict:
        """把用户输入打包为 dict（键名与 build_create_fields 关键字一致）。"""
        return {
            "title": self.title_edit.text().strip(),
            "due_at": self.due_edit.dateTime().toPython(),
            "priority": self.priority_combo.currentText(),
            "task_type": self.type_combo.currentText(),
            "estimate_min": int(self.estimate_spin.value()),
            "description": self.desc_edit.toPlainText().strip(),
        }
