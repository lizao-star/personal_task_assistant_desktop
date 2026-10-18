"""任务详情对话框（M4.5：详情 + 编辑合并）。

双击任务行即打开本对话框，一个窗口同时承担三件事：
1. 查看：状态概要、描述、AI拆解、检查清单、子任务；
2. 编辑：14 个字段直接改，点「保存修改」只把真正改动的字段写回
   （交由 main.py 走离线队列 + 冲突检测，与完成/延期同一条链路）；
3. AI 拆解：后台线程请求 DeepSeek 给出子任务/检查清单/建议，
   用户点「采纳」才写入表格（AI 只建议，写不写由用户决定）。

日期字段提供「清空」入口：勾选清空会把该字段置为 None，
由 build_edit_fields 转成飞书可接受的 null。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.parser import breakdown_task
from ..ai.provider import LLMProvider
from ..constants import (
    ENERGY_OPTIONS,
    PRIORITY_OPTIONS,
    REMINDER_RULE_OPTIONS,
    STATUS_OPTIONS,
    TASK_TYPE_OPTIONS,
)
from ..feishu.repository import Goal, SubTask, Task
from .ai_worker import AIInvoker

# 子任务表列定义：列名 -> 取值函数
_SUB_COLUMNS = [
    ("顺序", lambda s: str(s.order) if s.order else "-"),
    ("子任务名", lambda s: s.title),
    ("状态", lambda s: s.status or "-"),
    (
        "截止时间",
        lambda s: s.due_at.strftime("%m-%d %H:%M") if s.due_at else "-",
    ),
]


def _parse_tags(text: str) -> list[str]:
    """把「逗号/中文逗号」分隔的文本解析为标签数组（去空、去重保序）。"""
    raw = text.replace("，", ",").split(",")
    result: list[str] = []
    for item in raw:
        tag = item.strip()
        if tag and tag not in result:
            result.append(tag)
    return result


class TaskDetailDialog(QDialog):
    """可编辑的任务详情页（M4.5 起唯一入口，替代原编辑对话框）。"""

    # 点「采纳」时发出：{record_id, ai_breakdown, checklist, advice, subtasks}
    apply_requested = Signal(dict)
    # 点「保存修改」时发出：{record_id, changes}（changes 只含实际改动的键）
    save_requested = Signal(dict)

    def __init__(
        self,
        task: Task,
        subtasks: list[SubTask] | None = None,
        parent: QWidget | None = None,
        ai_provider: LLMProvider | None = None,
        ai_threshold: float = 0.6,
        goals: list[Goal] | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("任务详情")
        self.setModal(True)
        self.resize(620, 760)
        self._task = task
        self._subtasks = subtasks or []
        self._ai_provider = ai_provider
        self._ai_threshold = ai_threshold
        self._goals = goals or []
        self._suggestion = None  # AI 拆解建议（采纳前暂存）
        self._invoker = AIInvoker(self)
        self._invoker.done.connect(self._on_breakdown_done)
        self._invoker.error.connect(self._on_breakdown_error)
        self._build_ui()
        self._fill(task)

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """搭建界面：顶部概要 + 可滚动表单/详情区 + 底部按钮。"""
        layout = QVBoxLayout(self)

        # ===== 可滚动内容区 =====
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        content_layout.addLayout(self._build_form())
        content_layout.addWidget(QLabel("描述"))
        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setFixedHeight(64)
        content_layout.addWidget(self.desc_edit)

        content_layout.addWidget(QLabel("AI 拆解"))
        self.breakdown_edit = QPlainTextEdit()
        self.breakdown_edit.setPlaceholderText("每行一个步骤，也可留空")
        self.breakdown_edit.setFixedHeight(80)
        content_layout.addWidget(self.breakdown_edit)

        content_layout.addWidget(QLabel("检查清单"))
        self.checklist_edit = QPlainTextEdit()
        self.checklist_edit.setPlaceholderText("每行一条检查项，也可留空")
        self.checklist_edit.setFixedHeight(64)
        content_layout.addWidget(self.checklist_edit)

        self._add_subtask_section(content_layout)
        self._add_ai_breakdown_section(content_layout)

        content_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(scroll, stretch=1)

        # ===== 底部按钮：AI 拆解 + 保存/取消 =====
        bottom = QHBoxLayout()
        if self._ai_provider is not None:
            self.ai_button = QPushButton("AI 拆解此任务")
            self.ai_button.clicked.connect(self._on_ai_breakdown)
            bottom.addWidget(self.ai_button)
        else:
            self.ai_button = None
        bottom.addStretch(1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Save).setText("保存修改")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

    def _build_form(self) -> QFormLayout:
        """构建可编辑字段表单（与原编辑对话框 14 个字段一致）。"""
        form = QFormLayout()

        self.title_edit = QLineEdit()
        form.addRow("任务名*", self.title_edit)

        self.status_combo = QComboBox()
        self.status_combo.addItems(STATUS_OPTIONS)
        form.addRow("状态", self.status_combo)

        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITY_OPTIONS)
        form.addRow("优先级", self.priority_combo)

        self.type_combo = QComboBox()
        self.type_combo.addItems(TASK_TYPE_OPTIONS)
        form.addRow("任务类型", self.type_combo)

        self.due_edit = QDateTimeEdit()
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setCalendarPopup(True)
        self.due_clear = QCheckBox("清空")
        form.addRow("截止时间", self._with_clear(self.due_edit, self.due_clear))

        self.start_edit = QDateTimeEdit()
        self.start_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.start_edit.setCalendarPopup(True)
        self.start_clear = QCheckBox("清空")
        form.addRow("开始时间", self._with_clear(self.start_edit, self.start_clear))

        self.estimate_spin = QSpinBox()
        self.estimate_spin.setRange(0, 24 * 60)
        self.estimate_spin.setSingleStep(15)
        self.estimate_spin.setSuffix(" 分钟")
        form.addRow("预计耗时", self.estimate_spin)

        self.energy_combo = QComboBox()
        for opt in ENERGY_OPTIONS:
            self.energy_combo.addItem(opt or "未设置", opt)
        form.addRow("精力需求", self.energy_combo)

        self.voice_check = QCheckBox("到点用语音播报")
        form.addRow("语音提醒", self.voice_check)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("多个标签用逗号分隔，例如：论文, 阅读")
        form.addRow("标签", self.tags_edit)

        self.reminder_checks: dict[str, QCheckBox] = {}
        reminder_box = QVBoxLayout()
        reminder_box.setContentsMargins(0, 0, 0, 0)
        for opt in REMINDER_RULE_OPTIONS:
            cb = QCheckBox(opt)
            self.reminder_checks[opt] = cb
            reminder_box.addWidget(cb)
        form.addRow("提醒规则", self._wrap(reminder_box))

        # M4.5：所属目标（有三层结构时可直接在详情页挂到目标下）
        self.goal_combo = QComboBox()
        self.goal_combo.addItem("（不关联）", "")
        for goal in self._goals:
            self.goal_combo.addItem(goal.title, goal.record_id)
        form.addRow("所属目标", self.goal_combo)

        return form

    def _with_clear(self, widget: QWidget, clear_check: QCheckBox) -> QWidget:
        """把日期控件与「清空」勾选框排成一行。"""
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget, stretch=1)
        row.addWidget(clear_check)
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

        energy_idx = self.energy_combo.findData(task.energy or "")
        if energy_idx >= 0:
            self.energy_combo.setCurrentIndex(energy_idx)

        self.voice_check.setChecked(bool(task.voice))
        self.tags_edit.setText(", ".join(task.tags) if task.tags else "")

        selected = set(task.reminder_rules or [])
        for opt, cb in self.reminder_checks.items():
            cb.setChecked(opt in selected)

        goal_idx = self.goal_combo.findData(task.goal_record_id or "")
        if goal_idx >= 0:
            self.goal_combo.setCurrentIndex(goal_idx)

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

        if self.due_clear.isChecked():
            if t.due_at is not None:
                changes["due_at"] = None
        else:
            new_due = self.due_edit.dateTime().toPython()
            if t.due_at is None or new_due != t.due_at:
                changes["due_at"] = new_due

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

        goal_record_id = self.goal_combo.currentData() or ""
        if goal_record_id != (t.goal_record_id or ""):
            changes["goal_record_id"] = goal_record_id

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
    # 子任务与 AI 拆解
    # ------------------------------------------------------------------
    def _add_subtask_section(self, layout: QVBoxLayout) -> None:
        """追加子任务清单表格；无子任务时跳过。"""
        if not self._subtasks:
            return
        done = sum(1 for s in self._subtasks if s.is_finished)
        label = QLabel(f"子任务（{done}/{len(self._subtasks)} 已完成）")
        hf = label.font()
        hf.setBold(True)
        label.setFont(hf)
        layout.addWidget(label)

        table = QTableWidget(len(self._subtasks), len(_SUB_COLUMNS))
        table.setHorizontalHeaderLabels([name for name, _ in _SUB_COLUMNS])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.setSelectionMode(QTableWidget.NoSelection)
        table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        table.setMinimumHeight(min(32 * (len(self._subtasks) + 1), 220))

        for row, sub in enumerate(self._subtasks):
            for col, (_, getter) in enumerate(_SUB_COLUMNS):
                item = QTableWidgetItem(getter(sub))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                item.setToolTip(sub.ai_hint)
                if sub.is_finished:
                    item.setForeground(Qt.gray)
                table.setItem(row, col, item)
        layout.addWidget(table)
        layout.addSpacing(8)

    # ------------------------------------------------------------------
    # AI 拆解：预览建议 → 用户采纳才写表
    # ------------------------------------------------------------------
    def _add_ai_breakdown_section(self, layout: QVBoxLayout) -> None:
        """AI 建议预览区（默认隐藏，拿到结果后显示）。"""
        self.ai_group = QGroupBox("AI 拆解建议（预览）")
        ai_layout = QVBoxLayout(self.ai_group)

        self.ai_confidence_label = QLabel()
        self.ai_confidence_label.setWordWrap(True)
        self.ai_confidence_label.setStyleSheet("color: #555;")
        ai_layout.addWidget(self.ai_confidence_label)

        self.ai_subtasks_label = QLabel()
        self.ai_subtasks_label.setWordWrap(True)
        self.ai_subtasks_label.setTextFormat(Qt.PlainText)
        ai_layout.addWidget(QLabel("建议子任务："))
        ai_layout.addWidget(self.ai_subtasks_label)

        self.ai_checklist_label = QLabel()
        self.ai_checklist_label.setWordWrap(True)
        self.ai_checklist_label.setTextFormat(Qt.PlainText)
        self.ai_checklist_label.setVisible(False)
        ai_layout.addWidget(QLabel("检查清单："))
        ai_layout.addWidget(self.ai_checklist_label)

        self.ai_advice_label = QLabel()
        self.ai_advice_label.setWordWrap(True)
        self.ai_advice_label.setTextFormat(Qt.PlainText)
        self.ai_advice_label.setVisible(False)
        ai_layout.addWidget(QLabel("下一步建议："))
        ai_layout.addWidget(self.ai_advice_label)

        actions = QHBoxLayout()
        self.apply_button = QPushButton("采纳（写入表格）")
        self.apply_button.clicked.connect(self._on_apply)
        actions.addWidget(self.apply_button)
        discard = QPushButton("忽略")
        discard.clicked.connect(self.ai_group.hide)
        actions.addWidget(discard)
        actions.addStretch(1)
        ai_layout.addLayout(actions)

        self.ai_group.setVisible(False)
        layout.addWidget(self.ai_group)

    def _on_ai_breakdown(self) -> None:
        """点击 AI 拆解：后台线程请求，按钮置忙。"""
        if self._invoker.running or self.ai_button is None:
            return
        assert self._ai_provider is not None
        self.ai_button.setEnabled(False)
        self.ai_button.setText("AI 拆解中…")
        self._invoker.start(
            lambda: breakdown_task(
                self._ai_provider, self._task.title, self._task.description
            )
        )

    def _on_breakdown_done(self, result: object) -> None:
        """拆解完成：展示建议预览。"""
        if self.ai_button is not None:
            self.ai_button.setEnabled(True)
            self.ai_button.setText("AI 拆解此任务")
        suggestion, usage = result  # type: ignore[misc]
        if not suggestion.subtasks and not suggestion.advice:
            self.ai_confidence_label.setText("AI 未给出有效建议，可稍后再试")
            self.ai_group.setVisible(True)
            self.apply_button.setEnabled(False)
            return
        self.apply_button.setEnabled(True)
        self._suggestion = suggestion
        self.ai_confidence_label.setText(
            f"置信度 {suggestion.confidence:.2f}"
            + (f"　｜　本次用量 {usage.total_tokens} tokens" if usage else "")
        )
        self.ai_subtasks_label.setText(
            "\n".join(f"{i}. {s}" for i, s in enumerate(suggestion.subtasks, 1))
            or "（无）"
        )
        has_checklist = bool(suggestion.checklist)
        self.ai_checklist_label.setVisible(has_checklist)
        if has_checklist:
            self.ai_checklist_label.setText("\n".join(suggestion.checklist))
        has_advice = bool(suggestion.advice)
        self.ai_advice_label.setVisible(has_advice)
        if has_advice:
            self.ai_advice_label.setText(suggestion.advice)
        self.ai_group.setVisible(True)

    def _on_breakdown_error(self, message: str) -> None:
        """拆解失败（网络/Key/重试后仍失败）：提示但不影响详情编辑。"""
        if self.ai_button is not None:
            self.ai_button.setEnabled(True)
            self.ai_button.setText("AI 拆解此任务")
        self.ai_confidence_label.setText(f"AI 拆解失败：{message}")
        self.apply_button.setEnabled(False)
        self.ai_group.setVisible(True)

    def _on_apply(self) -> None:
        """采纳：写表请求交给 main.py，同时回填表单（对话框不关闭，便于继续编辑）。"""
        s = self._suggestion
        if s is None:
            return
        breakdown = "\n".join(f"{i}. {t}" for i, t in enumerate(s.subtasks, 1))
        checklist = "\n".join(s.checklist)
        # 回填编辑区，并把基准值同步为回填值，避免「保存修改」重复提交同一内容
        self.breakdown_edit.setPlainText(breakdown)
        self.checklist_edit.setPlainText(checklist)
        self._task.ai_breakdown = breakdown.strip()
        self._task.checklist = checklist.strip()
        self.apply_requested.emit(
            {
                "record_id": self._task.record_id,
                "ai_breakdown": breakdown,
                "checklist": checklist,
                "advice": s.advice,
                "subtasks": s.subtasks,
            }
        )
        self.apply_button.setEnabled(False)
        self.ai_confidence_label.setText(
            f"已采纳并提交写入（置信度 {s.confidence:.2f}），子任务将在下次同步后出现"
        )

    # ------------------------------------------------------------------
    # 保存
    # ------------------------------------------------------------------
    def accept(self) -> None:  # type: ignore[override]
        """校验任务名非空；有变更则发出保存信号。"""
        if not self.title_edit.text().strip():
            self.title_edit.setFocus()
            self.title_edit.setPlaceholderText("任务名必填！")
            return
        changes = self.result_changes()
        if changes:
            self.save_requested.emit(
                {"record_id": self._task.record_id, "changes": changes}
            )
        super().accept()
