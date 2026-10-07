"""AI 一句话建任务对话框（M3）。

流程：输入一句话 → 后台线程调 DeepSeek 解析 → 预览可编辑的字段草稿
→ 确认后交给主窗口走统一建任务链路（离线队列 + 推送 + 子任务写回）。

兜底与置信度：
- AI 不可用 / 解析失败 → 退化为「只取标题」草稿，字段由用户手补，程序不崩；
- confidence 低于阈值 → 提示横幅，任务将写入「收集箱」待人工确认，
  优先级置为保守的 P2（不自动定优先级）。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtWidgets import (
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..ai.parser import TaskDraft, parse_task
from ..ai.provider import LLMProvider
from ..constants import (
    ENERGY_HIGH,
    ENERGY_LOW,
    ENERGY_MEDIUM,
    PRIORITY_OPTIONS,
    STATUS_INBOX,
    STATUS_TODO,
    TASK_TYPE_OPTIONS,
)
from .ai_worker import AIInvoker


class AITaskDialog(QDialog):
    """一句话 → AI 解析 → 确认建任务。"""

    def __init__(
        self,
        provider: LLMProvider | None,
        threshold: float = 0.6,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("AI 一句话建任务")
        self.setModal(True)
        self.resize(480, 640)
        self._provider = provider
        self._threshold = threshold
        self._draft: TaskDraft | None = None
        self._invoker = AIInvoker(self)
        self._invoker.done.connect(self._on_parse_done)
        self._invoker.error.connect(self._on_parse_error)
        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ===== 输入区 =====
        layout.addWidget(QLabel("用一句话描述任务（例如：明天下午交高数作业，第三章习题1-10）"))
        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText("支持相对时间（明天/下周三）、截止时间、优先级暗示等")
        self.input_edit.setFixedHeight(64)
        layout.addWidget(self.input_edit)

        row = QHBoxLayout()
        self.parse_button = QPushButton("AI 解析")
        self.parse_button.clicked.connect(self._on_parse)
        row.addWidget(self.parse_button)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #555;")
        row.addWidget(self.status_label, stretch=1)
        layout.addLayout(row)

        if self._provider is None:
            self.parse_button.setEnabled(False)
            self.parse_button.setToolTip("未配置 DEEPSEEK_API_KEY（config/.env），AI 功能不可用")
            self.status_label.setText("AI 未配置，只能手动填写下方字段")

        # ===== 低置信度横幅（默认隐藏） =====
        self.review_label = QLabel()
        self.review_label.setWordWrap(True)
        self.review_label.setStyleSheet(
            "color: #b00020; font-weight: bold; background: #fff3f0; padding: 6px;"
        )
        self.review_label.setVisible(False)
        layout.addWidget(self.review_label)

        # ===== 解析结果（可编辑草稿） =====
        group = QGroupBox("解析结果（可直接修改）")
        form = QFormLayout(group)

        self.title_edit = QLineEdit()
        form.addRow("任务名*", self.title_edit)

        # 截止时间默认明天 18:00（表格要求必填；AI 没识别到时提醒用户确认）
        self.due_edit = QDateTimeEdit(datetime.now() + timedelta(days=1))
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setCalendarPopup(True)
        default_due = datetime.now().replace(
            hour=18, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self.due_edit.setDateTime(default_due)
        form.addRow("截止时间*", self.due_edit)

        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITY_OPTIONS)
        self.priority_combo.setCurrentText("P2")
        form.addRow("优先级", self.priority_combo)

        self.type_combo = QComboBox()
        self.type_combo.addItems(TASK_TYPE_OPTIONS)
        self.type_combo.setCurrentText("其他")
        form.addRow("任务类型", self.type_combo)

        self.energy_combo = QComboBox()
        self.energy_combo.addItems(["", ENERGY_HIGH, ENERGY_MEDIUM, ENERGY_LOW])
        form.addRow("精力需求", self.energy_combo)

        self.estimate_spin = QSpinBox()
        self.estimate_spin.setRange(0, 24 * 60)
        self.estimate_spin.setSingleStep(15)
        self.estimate_spin.setSuffix(" 分钟")
        form.addRow("预计耗时", self.estimate_spin)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("多个标签用英文逗号分隔，可留空")
        form.addRow("标签", self.tags_edit)

        layout.addWidget(group)

        # ===== 子任务 / 检查清单（每行一项） =====
        layout.addWidget(QLabel("子任务（每行一个，可留空）"))
        self.subtasks_edit = QPlainTextEdit()
        self.subtasks_edit.setFixedHeight(96)
        layout.addWidget(self.subtasks_edit)

        layout.addWidget(QLabel("检查清单（每行一项，可留空）"))
        self.checklist_edit = QPlainTextEdit()
        self.checklist_edit.setFixedHeight(72)
        layout.addWidget(self.checklist_edit)

        # ===== 按钮 =====
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("创建任务")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    # ------------------------------------------------------------------
    def _on_parse(self) -> None:
        """点击 AI 解析：后台线程调用，界面不卡。"""
        text = self.input_edit.toPlainText().strip()
        if not text:
            self.status_label.setText("请先输入任务描述")
            return
        if self._invoker.running:
            return
        self.parse_button.setEnabled(False)
        self.status_label.setText("AI 解析中…")
        assert self._provider is not None
        self._invoker.start(
            lambda: parse_task(self._provider, text, threshold=self._threshold)
        )

    def _on_parse_done(self, result: object) -> None:
        """解析完成：填充草稿表单。"""
        self.parse_button.setEnabled(True)
        draft, usage = result  # type: ignore[misc]
        self._draft = draft
        self.title_edit.setText(draft.title)
        if draft.due_at is not None:
            self.due_edit.setDateTime(draft.due_at)
        self.priority_combo.setCurrentText(draft.priority)
        self.type_combo.setCurrentText(draft.task_type)
        self.energy_combo.setCurrentText(draft.energy)
        self.estimate_spin.setValue(draft.estimate_min)
        self.tags_edit.setText("，".join(draft.tags))
        self.subtasks_edit.setPlainText("\n".join(draft.subtasks))
        self.checklist_edit.setPlainText("\n".join(draft.checklist))

        tips = []
        if draft.is_fallback:
            tips.append("AI 不可用，已按标题兜底，请手动补全字段")
        else:
            tips.append(f"解析完成，置信度 {draft.confidence:.2f}")
        if draft.due_at is None:
            tips.append("AI 未识别到截止时间，已置为明天 18:00，请确认")
        self.status_label.setText("；".join(tips))

        # 置信度策略：低置信 → 收集箱 + 待人工确认
        self.review_label.setVisible(draft.needs_review)
        if draft.needs_review:
            self.review_label.setText(
                f"置信度 {draft.confidence:.2f} 低于阈值 {self._threshold}，"
                "任务将写入「收集箱」待人工确认，优先级已置为 P2。"
            )
            self.priority_combo.setCurrentText("P2")

        usage_note = (
            f"本次用量 {usage.total_tokens} tokens" if usage is not None else ""
        )
        if usage_note:
            self.status_label.setText(f"{self.status_label.text()}　｜　{usage_note}")

    def _on_parse_error(self, message: str) -> None:
        """解析异常（正常路径已在 parse_task 内兜底，这里防御线程级异常）。"""
        self.parse_button.setEnabled(True)
        self.status_label.setText(f"AI 调用失败：{message}（可手动填写下方字段）")

    # ------------------------------------------------------------------
    def accept(self) -> None:  # type: ignore[override]
        """必填校验：任务名不能为空。"""
        if not self.title_edit.text().strip():
            self.title_edit.setFocus()
            self.title_edit.setPlaceholderText("任务名必填！")
            return
        super().accept()

    def result_dict(self) -> dict:
        """打包为建任务字段（键与 build_create_fields 关键字一致）。

        额外带 subtasks 列表与 low_confidence 标记，由 main.py 拆开处理：
        - subtasks → 推送阶段主任务落表后写入子任务表并关联；
        - low_confidence → 仅用于提示，写入状态由 status 字段承载。
        """
        draft = self._draft
        # 中英文逗号统一按分隔符处理
        raw_tags = self.tags_edit.text().replace("，", ",")
        tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
        subtasks = [
            s.strip()
            for s in self.subtasks_edit.toPlainText().splitlines()
            if s.strip()
        ]
        checklist = [
            s.strip()
            for s in self.checklist_edit.toPlainText().splitlines()
            if s.strip()
        ]
        low = bool(draft and draft.needs_review)
        return {
            "title": self.title_edit.text().strip(),
            "due_at": self.due_edit.dateTime().toPython(),
            "priority": self.priority_combo.currentText(),
            "task_type": self.type_combo.currentText(),
            "estimate_min": int(self.estimate_spin.value()),
            "energy": self.energy_combo.currentText(),
            "tags": tags,
            # AI拆解字段与子任务表用同一份（用户可编辑的）子任务列表，
            # 避免「主任务 AI拆解」与「子任务表」两处内容不一致
            "ai_breakdown": "\n".join(
                f"{i}. {s}" for i, s in enumerate(subtasks, 1)
            ),
            "checklist": "\n".join(checklist),
            # 低置信度写收集箱；正常写待办
            "status": STATUS_INBOX if low else STATUS_TODO,
            # 以下两项由 main.py 拆出，不进 build_create_fields
            "subtasks": subtasks,
            "low_confidence": low,
        }
