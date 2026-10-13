"""编辑任务对话框（M2 写回扩展）。

构造时传入当前 Task 预填控件，用户确认后由 result_changes() 返回一份
「变更字段」dict（只含用户实际改动的键，键名与 Task 属性同名），
交由 main.py 走与完成/延期一致的离线队列 + 冲突检测写回链路。

日期字段提供「清空」入口：勾选清空会把该字段置为 None，
由 build_edit_fields 转成飞书可接受的 null。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..constants import (
    ENERGY_OPTIONS,
    PRIORITY_OPTIONS,
    REMINDER_RULE_OPTIONS,
    STATUS_OPTIONS,
    TASK_TYPE_OPTIONS,
)
from ..feishu.repository import Task


def _parse_tags(text: str) -> list[str]:
    """把「逗号/中文逗号」分隔的文本解析为标签数组（去空、去重保序）。"""
    raw = text.replace("，", ",").split(",")
    result: list[str] = []
    for item in raw:
        tag = item.strip()
        if tag and tag not in result:
            result.append(tag)
    return result


class EditTaskDialog(QDialog):
    """编辑单条任务的表单。"""

    def __init__(self, task: Task, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("编辑任务")
        self.setModal(True)
        self.resize(460, 680)
        self._task = task
        self._build_ui()
        self._fill(task)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        form = QFormLayout()

        # 任务名（必填）
        self.title_edit = QLineEdit()
        form.addRow("任务名*", self.title_edit)

        # 状态
        self.status_combo = QComboBox()
        self.status_combo.addItems(STATUS_OPTIONS)
        form.addRow("状态", self.status_combo)

        # 优先级
        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITY_OPTIONS)
        form.addRow("优先级", self.priority_combo)

        # 任务类型
        self.type_combo = QComboBox()
        self.type_combo.addItems(TASK_TYPE_OPTIONS)
        form.addRow("任务类型", self.type_combo)

        # 截止时间（可清空）
        self.due_edit = QDateTimeEdit()
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setCalendarPopup(True)
        self.due_clear = QCheckBox("清空")
        form.addRow("截止时间", self._with_clear(self.due_edit, self.due_clear))

        # 开始时间（可清空）
        self.start_edit = QDateTimeEdit()
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_edit.setCalendarPopup(True)
        self.start_clear = QCheckBox("清空")
        form.addRow("开始时间", self._with_clear(self.start_edit, self.start_clear))

        # 预计耗时
        self.estimate_spin = QSpinBox()
        self.estimate_spin.setRange(0, 24 * 60)
        self.estimate_spin.setSingleStep(15)
        self.estimate_spin.setSuffix(" 分钟")
        form.addRow("预计耗时", self.estimate_spin)

        # 精力需求（首项为空 = 未设置）
        self.energy_combo = QComboBox()
        for opt in ENERGY_OPTIONS:
            self.energy_combo.addItem(opt or "未设置", opt)
        form.addRow("精力需求", self.energy_combo)

        # 是否语音提醒
        self.voice_check = QCheckBox("到点用语音播报")
        form.addRow("语音提醒", self.voice_check)

        # 标签（逗号分隔）
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("多个标签用逗号分隔，例如：论文, 阅读")
        form.addRow("标签", self.tags_edit)

        # 提醒规则（多选复选）
        self.reminder_checks: dict[str, QCheckBox] = {}
        reminder_box = QVBoxLayout()
        reminder_box.setContentsMargins(0, 0, 0, 0)
        for opt in REMINDER_RULE_OPTIONS:
            cb = QCheckBox(opt)
            self.reminder_checks[opt] = cb
            reminder_box.addWidget(cb)
        form.addRow("提醒规则", self._wrap(reminder_box))

        # 描述
        self.desc_edit = QPlainTextEdit()
        form.addRow("描述", self.desc_edit)

        # AI 拆解（详情页展示的字段，一并支持编辑）
        self.breakdown_edit = QPlainTextEdit()
        self.breakdown_edit.setPlaceholderText("每行一个步骤，也可留空")
        form.addRow("AI 拆解", self.breakdown_edit)

        # 检查清单（详情页展示的字段，一并支持编辑）
        self.checklist_edit = QPlainTextEdit()
        self.checklist_edit.setPlaceholderText("每行一条检查项，也可留空")
        form.addRow("检查清单", self.checklist_edit)

        layout.addLayout(form)

        # 按钮
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _with_clear(self, widget: QWidget, clear_check: QCheckBox) -> QWidget:
        """把日期控件与「清空」勾选框排成一行。"""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget, stretch=1)
        row.addWidget(clear_check)
        # 勾选清空后禁用日期控件，直观提示该字段将被清除
        clear_check.toggled.connect(lambda checked, w=widget: w.setEnabled(not checked))
        return box

    def _wrap(self, inner) -> QWidget:
        """把布局包成一个控件，便于放进 QFormLayout。"""
        box = QWidget()
        box.setLayout(inner)
        return box

    # ------------------------------------------------------------------
    # 预填与取值
    # ------------------------------------------------------------------
    def _fill(self, task: Task) -> None:
        """用当前任务值预填各控件。"""
        self.title_edit.setText(task.title)

        idx = self.status_combo.findText(task.status)
        if idx >= 0:
            self.status_combo.setCurrentIndex(idx)

        idx = self.priority_combo.findText(task.priority)
        if idx >= 0:
            self.priority_combo.setCurrentIndex(idx)

        idx = self.type_combo.findText(task.task_type)
        if idx >= 0:
            self.type_combo.setCurrentIndex(idx)

        # 日期：无值则勾选“清空”
        if task.due_at is not None:
            self.due_edit.setDateTime(task.due_at)
            self.due_clear.setChecked(False)
        else:
            self.due_clear.setChecked(True)
        if task.start_at is not None:
            self.start_edit.setDateTime(task.start_at)
            self.start_clear.setChecked(False)
        else:
            self.start_clear.setChecked(True)

        self.estimate_spin.setValue(int(task.estimate_min or 0))

        # 精力需求（按下拉项的 data 值匹配）
        energy_idx = self.energy_combo.findData(task.energy or "")
        if energy_idx >= 0:
            self.energy_combo.setCurrentIndex(energy_idx)

        self.voice_check.setChecked(bool(task.voice))
        self.tags_edit.setText(", ".join(task.tags) if task.tags else "")

        selected = set(task.reminder_rules or [])
        for opt, cb in self.reminder_checks.items():
            cb.setChecked(opt in selected)

        self.desc_edit.setPlainText(task.description or "")
        self.breakdown_edit.setPlainText(task.ai_breakdown or "")
        self.checklist_edit.setPlainText(task.checklist or "")

    def result_changes(self) -> dict:
        """返回相对原任务的「变更字段」dict（只含真正改动的键）。"""
        t = self._task
        changes: dict = {}

        title = self.title_edit.text().strip()
        if title != t.title:
            changes["title"] = title

        status = self.status_combo.currentText()
        if status != t.status:
            changes["status"] = status

        priority = self.priority_combo.currentText()
        if priority != t.priority:
            changes["priority"] = priority

        task_type = self.type_combo.currentText()
        if task_type != t.task_type:
            changes["task_type"] = task_type

        # 截止时间：勾选清空 -> None
        if self.due_clear.isChecked():
            if t.due_at is not None:
                changes["due_at"] = None
        else:
            new_due = self.due_edit.dateTime().toPython()
            if t.due_at is None or new_due != t.due_at:
                changes["due_at"] = new_due

        # 开始时间
        if self.start_clear.isChecked():
            if t.start_at is not None:
                changes["start_at"] = None
        else:
            new_start = self.start_edit.dateTime().toPython()
            if t.start_at is None or new_start != t.start_at:
                changes["start_at"] = new_start

        estimate = int(self.estimate_spin.value())
        if estimate != int(t.estimate_min or 0):
            changes["estimate_min"] = estimate

        energy = self.energy_combo.currentData() or ""
        if energy != (t.energy or ""):
            changes["energy"] = energy

        voice = self.voice_check.isChecked()
        if voice != bool(t.voice):
            changes["voice"] = voice

        tags = _parse_tags(self.tags_edit.text())
        if tags != (t.tags or []):
            changes["tags"] = tags

        reminder_rules = [
            opt for opt, cb in self.reminder_checks.items() if cb.isChecked()
        ]
        if reminder_rules != (t.reminder_rules or []):
            changes["reminder_rules"] = reminder_rules

        description = self.desc_edit.toPlainText().strip()
        if description != (t.description or ""):
            changes["description"] = description

        ai_breakdown = self.breakdown_edit.toPlainText().strip()
        if ai_breakdown != (t.ai_breakdown or ""):
            changes["ai_breakdown"] = ai_breakdown

        checklist = self.checklist_edit.toPlainText().strip()
        if checklist != (t.checklist or ""):
            changes["checklist"] = checklist

        return changes

    # ------------------------------------------------------------------
    # 校验
    # ------------------------------------------------------------------
    def accept(self) -> None:  # type: ignore[override]
        """必填校验：任务名不能为空。"""
        if not self.title_edit.text().strip():
            self.title_edit.setFocus()
            self.title_edit.setPlaceholderText("任务名必填！")
            return
        super().accept()
