"""新建任务对话框（含可选 AI 解析与层级判定）。

两种用法：
1. 手动填写：直接填表单字段即可；
2. AI 辅助：顶部输入一句话 →「AI 解析」→ 自动填充表单（可再手工修改）。

M4.5 起支持三层任务：
- 目标（L1，1 个月以上）：必须向下拆解出 2~5 个 L2 任务，拆解结果在此可编辑；
- 任务（L2，1 天~1 个月）：按需拆分子任务，宁缺勿碎；
- AI 判定为子任务（L3）时按普通任务对待（子任务需挂在父任务下，不在此新建）。

AI 不可用 / 解析失败 / 低置信度时自动兜底：不崩、字段可手补；
低置信度任务写入「收集箱」待人工确认（与原 M3 约定一致）。

返回 dict：
- 任务级：键与 build_create_fields 关键字一致，另带 subtasks / low_confidence / level；
- 目标级：{"level": "goal", "goal": {...}, "tasks": [...], "low_confidence": bool}，
  由 main.py 拆成「建目标 → 建任务并关联目标 → 建子任务」。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.parser import GoalTaskDraft, TaskDraft, parse_task
from ..ai.provider import LLMProvider
from ..constants import (
    ENERGY_HIGH,
    ENERGY_LOW,
    ENERGY_MEDIUM,
    GOAL_LEVEL_OPTIONS,
    LEVEL_GOAL,
    LEVEL_SUBTASK,
    LEVEL_TASK,
    PRIORITY_OPTIONS,
    STATUS_INBOX,
    STATUS_TODO,
    TASK_TYPE_OPTIONS,
)
from .ai_worker import AIInvoker

# 目标级拆解任务表格的列（全部可编辑，空值回落默认）
# 「依赖」填前置任务在表格中的序号（从 0 开始，逗号分隔），空表示无前置
GOAL_TASK_COLUMNS = ["任务名", "截止时间(YYYY-MM-DD HH:MM)", "优先级", "预计分钟", "子任务(逗号分隔)", "依赖(序号,逗号分隔)"]


class NewTaskDialog(QDialog):
    """新建表单：手动填写，或一句话交给 AI 判定层级并拆解。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        provider: LLMProvider | None = None,
        threshold: float = 0.6,
    ):
        super().__init__(parent)
        self.setWindowTitle("新建任务")
        self.setModal(True)
        self.setMinimumSize(520, 360)
        # 小屏幕上不超出可见区域：内容区可滚动，底部按钮始终可见
        width, height = 640, 820
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            width = min(width, available.width() - 80)
            height = min(height, available.height() - 80)
        self.resize(width, height)
        self._provider = provider
        self._threshold = threshold
        self._draft: TaskDraft | None = None
        self._invoker = AIInvoker(self)
        self._invoker.done.connect(self._on_parse_done)
        self._invoker.error.connect(self._on_parse_error)
        self._build_ui()

    # ------------------------------------------------------------------
    # 界面
    # ------------------------------------------------------------------
    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # ===== 可滚动内容区：字段多、屏幕小也能看全 =====
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)

        # ===== 可选：一句话 AI 解析区 =====
        ai_group = QGroupBox("AI 辅助（可选：一句话自动判定层级并填充）")
        ai_layout = QVBoxLayout(ai_group)

        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText(
            "例如：这学期把毕业论文写完；或：明天下午交高数作业，第三章习题1-10。"
            "AI 会自动判断是「目标」还是「任务」"
        )
        self.input_edit.setFixedHeight(56)
        ai_layout.addWidget(self.input_edit)

        row = QHBoxLayout()
        self.parse_button = QPushButton("AI 解析填充")
        self.parse_button.clicked.connect(self._on_parse)
        row.addWidget(self.parse_button)
        self.status_label = QLabel()
        self.status_label.setStyleSheet("color: #555;")
        self.status_label.setWordWrap(True)
        row.addWidget(self.status_label, stretch=1)
        ai_layout.addLayout(row)

        if self._provider is None:
            self.parse_button.setEnabled(False)
            self.parse_button.setToolTip("未配置 DEEPSEEK_API_KEY（config/.env），AI 功能不可用")
            self.status_label.setText("AI 未配置，直接手动填写下方字段即可")

        # 低置信度横幅（默认隐藏）
        self.review_label = QLabel()
        self.review_label.setWordWrap(True)
        self.review_label.setStyleSheet(
            "color: #b00020; font-weight: bold; background: #fff3f0; padding: 6px;"
        )
        self.review_label.setVisible(False)
        ai_layout.addWidget(self.review_label)

        content_layout.addWidget(ai_group)

        # ===== 层级选择（AI 自动判定，也可手动改） =====
        level_row = QHBoxLayout()
        level_row.addWidget(QLabel("层级"))
        self.level_combo = QComboBox()
        self.level_combo.addItem("任务（L2，1 天~1 个月）", LEVEL_TASK)
        self.level_combo.addItem("目标（L1，1 个月以上，需拆解）", LEVEL_GOAL)
        self.level_combo.currentIndexChanged.connect(self._on_level_changed)
        level_row.addWidget(self.level_combo, stretch=1)
        content_layout.addLayout(level_row)

        # ===== 任务字段表单 =====
        form = QFormLayout()

        self.title_label = QLabel("任务名*")
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("例如：读完《XXX》第三章")
        form.addRow(self.title_label, self.title_edit)

        self.due_label = QLabel("截止时间*")
        self.due_edit = QDateTimeEdit(datetime.now() + timedelta(days=1))
        self.due_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.due_edit.setCalendarPopup(True)
        tomorrow_18 = datetime.now().replace(
            hour=18, minute=0, second=0, microsecond=0
        ) + timedelta(days=1)
        self.due_edit.setDateTime(tomorrow_18)
        form.addRow(self.due_label, self.due_edit)

        self.priority_combo = QComboBox()
        self.priority_combo.addItems(PRIORITY_OPTIONS)
        self.priority_combo.setCurrentText("P2")
        self.priority_row = self._wrap_row("优先级", self.priority_combo)
        form.addRow(self.priority_row)

        self.type_combo = QComboBox()
        self.type_combo.addItems(TASK_TYPE_OPTIONS)
        self.type_combo.setCurrentText("其他")
        self.type_row = self._wrap_row("任务类型", self.type_combo)
        form.addRow(self.type_row)

        self.energy_combo = QComboBox()
        self.energy_combo.addItems(["", ENERGY_HIGH, ENERGY_MEDIUM, ENERGY_LOW])
        self.energy_row = self._wrap_row("精力需求", self.energy_combo)
        form.addRow(self.energy_row)

        self.estimate_spin = QSpinBox()
        self.estimate_spin.setRange(0, 24 * 60)
        self.estimate_spin.setSingleStep(15)
        self.estimate_spin.setSuffix(" 分钟")
        self.estimate_row = self._wrap_row("预计耗时", self.estimate_spin)
        form.addRow(self.estimate_row)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("多个标签用英文逗号分隔，可留空")
        self.tags_row = self._wrap_row("标签", self.tags_edit)
        form.addRow(self.tags_row)

        self.desc_edit = QPlainTextEdit()
        self.desc_edit.setPlaceholderText("可选：补充说明、相关链接……")
        form.addRow("描述", self.desc_edit)

        self.advice_edit = QPlainTextEdit()
        self.advice_edit.setFixedHeight(64)
        self.advice_edit.setPlaceholderText(
            "AI 给出的策略建议：重点、方法与取舍（不是动作清单）"
        )
        form.addRow("AI 建议", self.advice_edit)

        content_layout.addLayout(form)

        # ===== 目标专属：目标属性 =====
        self.goal_group = QGroupBox("目标属性（L1）")
        goal_form = QFormLayout(self.goal_group)

        self.goal_level_combo = QComboBox()
        self.goal_level_combo.addItems(GOAL_LEVEL_OPTIONS)
        self.goal_level_combo.setCurrentText("月")
        goal_form.addRow("目标层级", self.goal_level_combo)

        self.goal_start_edit = QDateTimeEdit(datetime.now())
        self.goal_start_edit.setDisplayFormat("yyyy-MM-dd")
        self.goal_start_edit.setCalendarPopup(True)
        goal_form.addRow("开始时间", self.goal_start_edit)

        default_end = datetime.now() + timedelta(days=30)
        self.goal_end_edit = QDateTimeEdit(default_end)
        self.goal_end_edit.setDisplayFormat("yyyy-MM-dd")
        self.goal_end_edit.setCalendarPopup(True)
        goal_form.addRow("结束时间", self.goal_end_edit)

        self.key_results_edit = QPlainTextEdit()
        self.key_results_edit.setFixedHeight(64)
        self.key_results_edit.setPlaceholderText("每行一条可验收的关键结果，如：完成 3 万字初稿")
        goal_form.addRow("关键结果", self.key_results_edit)
        content_layout.addWidget(self.goal_group)

        # ===== 目标专属：L2 任务拆解草稿（可增删改） =====
        self.goal_tasks_group = QGroupBox("任务拆解（目标必须拆出 2~5 个任务）")
        gt_layout = QVBoxLayout(self.goal_tasks_group)
        self.goal_tasks_table = QTableWidget(0, len(GOAL_TASK_COLUMNS))
        self.goal_tasks_table.setHorizontalHeaderLabels(GOAL_TASK_COLUMNS)
        self.goal_tasks_table.verticalHeader().setVisible(False)
        self.goal_tasks_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.goal_tasks_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.goal_tasks_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.Stretch
        )
        self.goal_tasks_table.setMinimumHeight(150)
        gt_layout.addWidget(self.goal_tasks_table)

        gt_buttons = QHBoxLayout()
        add_btn = QPushButton("添加任务行")
        add_btn.clicked.connect(lambda: self._append_goal_task_row())
        gt_buttons.addWidget(add_btn)
        del_btn = QPushButton("删除选中行")
        del_btn.clicked.connect(self._remove_goal_task_row)
        gt_buttons.addWidget(del_btn)
        gt_buttons.addStretch()
        gt_layout.addLayout(gt_buttons)
        content_layout.addWidget(self.goal_tasks_group)

        # ===== 任务专属：子任务 / 检查清单（每行一项） =====
        self.subtasks_label = QLabel("子任务（每行一个，可留空；AI 按需拆解，不硬拆细碎动作）")
        content_layout.addWidget(self.subtasks_label)
        self.subtasks_edit = QPlainTextEdit()
        self.subtasks_edit.setFixedHeight(88)
        content_layout.addWidget(self.subtasks_edit)

        content_layout.addWidget(QLabel("检查清单（每行一项，可留空）"))
        self.checklist_edit = QPlainTextEdit()
        self.checklist_edit.setFixedHeight(64)
        content_layout.addWidget(self.checklist_edit)

        content_layout.addStretch(1)

        # 表单过长时可滚动查看（固定按钮区始终可见）
        scroll = QScrollArea()
        scroll.setWidget(content)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        layout.addWidget(scroll, stretch=1)

        # ===== 按钮 =====
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("创建")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 默认任务层级：隐藏目标专属区块
        self._on_level_changed()

    @staticmethod
    def _wrap_row(label: str, widget: QWidget) -> QWidget:
        """把「标签 + 控件」包成一个整体，便于按层级整体隐藏。"""
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(QLabel(label))
        box.addWidget(widget, stretch=1)
        return row

    # ------------------------------------------------------------------
    # 层级切换
    # ------------------------------------------------------------------
    def _current_level(self) -> str:
        return self.level_combo.currentData() or LEVEL_TASK

    def _on_level_changed(self, *_args) -> None:
        """目标层级显示拆解区，任务层级显示子任务区。"""
        is_goal = self._current_level() == LEVEL_GOAL
        self.goal_group.setVisible(is_goal)
        self.goal_tasks_group.setVisible(is_goal)
        self.subtasks_label.setVisible(not is_goal)
        self.subtasks_edit.setVisible(not is_goal)
        self.title_label.setText("目标名*" if is_goal else "任务名*")
        self.title_edit.setPlaceholderText(
            "例如：本学期完成毕业论文"
            if is_goal
            else "例如：读完《XXX》第三章"
        )
        # 目标的截止时间由「结束时间」承载，任务才需要截止时间
        self.due_label.setVisible(not is_goal)
        self.due_edit.setVisible(not is_goal)

    # ------------------------------------------------------------------
    # AI 解析（后台线程，界面不卡）
    # ------------------------------------------------------------------
    def _on_parse(self) -> None:
        """点击 AI 解析：后台线程调用 DeepSeek，完成后填充表单。"""
        text = self.input_edit.toPlainText().strip()
        if not text:
            self.status_label.setText("请先输入一句话任务描述")
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

        # 层级：AI 给 goal 才切到目标；subtask 按普通任务处理
        level = draft.level
        if level == LEVEL_GOAL:
            self.level_combo.setCurrentIndex(1)
        else:
            self.level_combo.setCurrentIndex(0)
        self._on_level_changed()

        self.title_edit.setText(draft.title)
        self.desc_edit.setPlainText(draft.description or "")
        self.advice_edit.setPlainText(draft.advice or "")
        if draft.due_at is not None:
            self.due_edit.setDateTime(draft.due_at)
            self.goal_end_edit.setDateTime(draft.due_at)
        self.priority_combo.setCurrentText(draft.priority)
        self.type_combo.setCurrentText(draft.task_type)
        self.energy_combo.setCurrentText(draft.energy)
        self.estimate_spin.setValue(draft.estimate_min)
        self.tags_edit.setText("，".join(draft.tags))
        self.subtasks_edit.setPlainText("\n".join(draft.subtasks))
        self.checklist_edit.setPlainText("\n".join(draft.checklist))

        if level == LEVEL_GOAL:
            self.key_results_edit.setPlainText("\n".join(draft.key_results))
            self._fill_goal_tasks(draft.tasks)

        tips = []
        if draft.is_fallback:
            tips.append("AI 不可用，已按标题兜底，请手动补全字段")
        else:
            label = {
                LEVEL_GOAL: "目标（已拆解为多个任务）",
                LEVEL_SUBTASK: "子任务（按普通任务创建）",
            }.get(level, "任务")
            tips.append(f"判定层级：{label}，置信度 {draft.confidence:.2f}")
        if draft.level_note:
            tips.append(draft.level_note)
        if draft.level_uncertain:
            tips.append("层级判定把握不足，请确认上方「层级」是否正确")
        if level != LEVEL_GOAL and draft.due_at is None:
            tips.append("AI 未识别到截止时间，已置为明天 18:00，请确认")
        if level == LEVEL_GOAL and len(draft.tasks) < 2:
            tips.append("AI 未给出足够的任务拆解，请手动补充 2 个以上任务")
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
    # 目标级拆解任务表格
    # ------------------------------------------------------------------
    def _append_goal_task_row(self, task: GoalTaskDraft | None = None) -> None:
        """在拆解表格末尾追加一行，可用草稿填充。"""
        row = self.goal_tasks_table.rowCount()
        self.goal_tasks_table.insertRow(row)
        default_due = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d 18:00")
        values = [
            task.title if task else "",
            task.due_at.strftime("%Y-%m-%d %H:%M") if task and task.due_at else default_due,
            task.priority if task else "P2",
            str(task.estimate_min) if task and task.estimate_min else "",
            "，".join(task.subtasks) if task else "",
            "，".join(str(i) for i in task.depends_on) if task else "",
        ]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            self.goal_tasks_table.setItem(row, col, item)

    def _remove_goal_task_row(self) -> None:
        row = self.goal_tasks_table.currentRow()
        if row >= 0:
            self.goal_tasks_table.removeRow(row)

    def _fill_goal_tasks(self, tasks: list[GoalTaskDraft]) -> None:
        """用 AI 拆解结果整体替换表格内容。"""
        self.goal_tasks_table.setRowCount(0)
        for task in tasks:
            self._append_goal_task_row(task)
        # 至少给两个空行，方便用户直接手填
        while self.goal_tasks_table.rowCount() < 2:
            self._append_goal_task_row()

    def _read_goal_tasks(self) -> list[dict]:
        """读取拆解表格为可写表的任务草稿列表，跳过无任务名的行。"""
        tasks: list[dict] = []
        for row in range(self.goal_tasks_table.rowCount()):
            def cell(col: int) -> str:
                item = self.goal_tasks_table.item(row, col)
                return item.text().strip() if item else ""

            title = cell(0)
            if not title:
                continue
            due = _parse_due_text(cell(1))
            if due is None:
                due = datetime.now().replace(hour=18, minute=0, second=0, microsecond=0) \
                    + timedelta(days=7)
            priority = cell(2).upper()
            if priority not in PRIORITY_OPTIONS:
                priority = "P2"
            try:
                estimate = int(float(cell(3) or 0))
            except ValueError:
                estimate = 0
            subtasks = [
                s.strip()
                for s in cell(4).replace("，", ",").split(",")
                if s.strip()
            ]
            # 依赖：先原样收集数字，收集完按有效任务数组统一清洗（序号基于数组而非行号）
            depends_on: list[int] = []
            for piece in cell(5).replace("，", ",").split(","):
                piece = piece.strip()
                if not piece:
                    continue
                try:
                    depends_on.append(int(piece))
                except ValueError:
                    continue
            tasks.append(
                {
                    "title": title,
                    "due_at": due,
                    "priority": priority,
                    "estimate_min": max(0, min(estimate, 24 * 60)),
                    "subtasks": subtasks[:6],
                    "depends_on": depends_on,
                }
            )
        # 统一清洗：去重、去自身、丢弃越界序号（以 tasks 数组索引为准）
        total = len(tasks)
        for i, task in enumerate(tasks):
            cleaned: list[int] = []
            for idx in task["depends_on"]:
                if 0 <= idx < total and idx != i and idx not in cleaned:
                    cleaned.append(idx)
            task["depends_on"] = cleaned
        return tasks

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------
    def accept(self) -> None:  # type: ignore[override]
        """必填校验：任务/目标名不能为空；目标必须至少拆解 2 个任务。"""
        if not self.title_edit.text().strip():
            self.title_edit.setFocus()
            self.title_edit.setPlaceholderText("名称必填！")
            return
        if self._current_level() == LEVEL_GOAL and len(self._read_goal_tasks()) < 2:
            QMessageBox.warning(
                self,
                "缺少任务拆解",
                "目标是 1 个月以上的长期事项，必须拆解出至少 2 个具体任务，"
                "请在「任务拆解」表格中补充。",
            )
            return
        super().accept()

    def result_dict(self) -> dict:
        """打包为建任务 / 建目标的字段 dict。

        - 任务级：键与 build_create_fields 关键字一致；
        - 目标级：goal 子字典 + tasks 列表，由 main.py 逐层落表。
        """
        level = self._current_level()
        draft = self._draft
        low = bool(draft and draft.needs_review)

        if level == LEVEL_GOAL:
            key_results = [
                s.strip()
                for s in self.key_results_edit.toPlainText().splitlines()
                if s.strip()
            ]
            # 目标表没有独立建议字段，把 AI 建议并入描述，避免信息丢失
            goal_desc = self.desc_edit.toPlainText().strip()
            advice = self.advice_edit.toPlainText().strip()
            if advice:
                goal_desc = f"{goal_desc}\n\nAI 建议：{advice}".strip()
            return {
                "level": LEVEL_GOAL,
                "goal": {
                    "title": self.title_edit.text().strip(),
                    "level": self.goal_level_combo.currentText(),
                    "start_at": self.goal_start_edit.dateTime().toPython(),
                    "end_at": self.goal_end_edit.dateTime().toPython(),
                    "description": goal_desc,
                    "key_results": key_results,
                },
                "tasks": self._read_goal_tasks(),
                "low_confidence": low,
            }

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
        return {
            "level": LEVEL_TASK,
            "title": self.title_edit.text().strip(),
            "due_at": self.due_edit.dateTime().toPython(),
            "priority": self.priority_combo.currentText(),
            "task_type": self.type_combo.currentText(),
            "energy": self.energy_combo.currentText(),
            "estimate_min": int(self.estimate_spin.value()),
            "tags": tags,
            "description": self.desc_edit.toPlainText().strip(),
            # AI拆解字段与子任务表用同一份（用户可编辑的）子任务列表，
            # 避免「主任务 AI拆解」与「子任务表」两处内容不一致
            "ai_breakdown": "\n".join(
                f"{i}. {s}" for i, s in enumerate(subtasks, 1)
            ),
            "ai_advice": self.advice_edit.toPlainText().strip(),
            "checklist": "\n".join(checklist),
            # 低置信度写收集箱；正常写待办
            "status": STATUS_INBOX if low else STATUS_TODO,
            # 以下两项由 main.py 拆出，不进 build_create_fields
            "subtasks": subtasks,
            "low_confidence": low,
        }


def _parse_due_text(text: str) -> datetime | None:
    """解析表格里的截止时间文本（YYYY-MM-DD HH:MM 或 YYYY-MM-DD）。"""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None
