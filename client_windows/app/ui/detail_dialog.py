"""任务详情对话框：展示描述 / AI拆解 / 检查清单 / 子任务。

M3 起（本文件）支持「AI 拆解」：后台线程请求 DeepSeek 给出
子任务 + 检查清单 + 下一步建议的预览；用户点「采纳」才写表
（AI 只建议，写不写、改不改状态永远由用户决定）。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.parser import breakdown_task
from ..ai.provider import LLMProvider
from ..feishu.repository import SubTask, Task
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


class TaskDetailDialog(QDialog):
    """只读任务详情 + AI 拆解建议。"""

    # 点「采纳」时发出：{record_id, ai_breakdown, checklist, advice, subtasks}
    apply_requested = Signal(dict)

    def __init__(
        self,
        task: Task,
        subtasks: list[SubTask] | None = None,
        parent: QWidget | None = None,
        ai_provider: LLMProvider | None = None,
        ai_threshold: float = 0.6,
    ):
        super().__init__(parent)
        self.setWindowTitle("任务详情")
        self.setModal(True)
        self.resize(560, 600)
        self._task = task
        self._subtasks = subtasks or []
        self._ai_provider = ai_provider
        self._ai_threshold = ai_threshold
        self._suggestion = None  # AI 拆解建议（采纳前暂存）
        self._invoker = AIInvoker(self)
        self._invoker.done.connect(self._on_breakdown_done)
        self._invoker.error.connect(self._on_breakdown_error)
        self._build_ui()

    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        """搭建界面：顶部概要 + 可滚动的内容区。"""
        layout = QVBoxLayout(self)

        # ===== 任务名 =====
        title = QLabel(self._task.title)
        title.setWordWrap(True)
        f = title.font()
        f.setBold(True)
        f.setPointSize(f.pointSize() + 2)
        title.setFont(f)
        layout.addWidget(title)

        # ===== 概要信息 =====
        meta = QLabel(self._meta_text())
        meta.setWordWrap(True)
        meta.setStyleSheet("color: #555;")
        layout.addWidget(meta)

        # ===== 可滚动内容区 =====
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        self._add_text_section(content_layout, "描述", self._task.description)
        self._add_text_section(content_layout, "AI 拆解", self._task.ai_breakdown)
        self._add_text_section(content_layout, "检查清单", self._task.checklist)
        self._add_subtask_section(content_layout)
        self._add_ai_breakdown_section(content_layout)

        content_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(scroll, stretch=1)

        # ===== 底部按钮：AI 拆解 + 关闭 =====
        bottom = QHBoxLayout()
        if self._ai_provider is not None:
            self.ai_button = QPushButton("AI 拆解此任务")
            self.ai_button.clicked.connect(self._on_ai_breakdown)
            bottom.addWidget(self.ai_button)
        bottom.addStretch(1)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        # Close 按钮 role 为 RejectRole，点击即发出 rejected，直接关闭对话框
        buttons.rejected.connect(self.reject)
        bottom.addWidget(buttons)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------
    def _meta_text(self) -> str:
        """拼一行概要：状态/优先级/截止/类型/精力/延期。"""
        t = self._task
        parts = [
            f"状态：{t.status or '-'}",
            f"优先级：{t.priority or '-'}",
            "截止：" + (t.due_at.strftime("%Y-%m-%d %H:%M") if t.due_at else "-"),
            f"类型：{t.task_type or '-'}",
            f"精力：{t.energy or '-'}",
        ]
        if t.delay_count:
            parts.append(f"已延期 {t.delay_count} 次")
        if t.estimate_min:
            parts.append(f"预计 {t.estimate_min} 分钟")
        return "　｜　".join(parts)

    def _add_text_section(
        self, layout: QVBoxLayout, header: str, text: str
    ) -> None:
        """追加一个文本段；内容为空时跳过。"""
        if not text.strip():
            return
        label = QLabel(header)
        hf = label.font()
        hf.setBold(True)
        label.setFont(hf)
        layout.addWidget(label)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setTextFormat(Qt.PlainText)
        layout.addWidget(body)
        layout.addSpacing(8)

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
                # 鼠标悬停显示这一步的 AI 提示
                item.setToolTip(sub.ai_hint)
                if sub.is_finished:
                    item.setForeground(Qt.gray)
                table.setItem(row, col, item)
        layout.addWidget(table)
        layout.addSpacing(8)

    # ------------------------------------------------------------------
    # AI 拆解（M3）：预览建议 → 用户采纳才写表
    # ------------------------------------------------------------------
    def _add_ai_breakdown_section(self, layout: QVBoxLayout) -> None:
        """AI 建议预览区（默认隐藏，拿到结果后显示）。"""
        self.ai_group = QGroupBox("AI 拆解建议（预览）")
        ai_layout = QVBoxLayout(self.ai_group)

        self.ai_confidence_label = QLabel()
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
        if self._invoker.running:
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
        """拆解失败（网络/Key/重试后仍失败）：提示但不影响详情浏览。"""
        self.ai_button.setEnabled(True)
        self.ai_button.setText("AI 拆解此任务")
        self.ai_confidence_label.setText(f"AI 拆解失败：{message}")
        self.apply_button.setEnabled(False)
        self.ai_group.setVisible(True)

    def _on_apply(self) -> None:
        """采纳：发出写表请求并关闭对话框（写表走离线队列）。"""
        s = self._suggestion
        self.apply_requested.emit(
            {
                "record_id": self._task.record_id,
                "ai_breakdown": "\n".join(
                    f"{i}. {t}" for i, t in enumerate(s.subtasks, 1)
                ),
                "checklist": "\n".join(s.checklist),
                "advice": s.advice,
                "subtasks": s.subtasks,
            }
        )
        self.accept()
