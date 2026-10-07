"""主窗口：展示逾期任务与待办任务，提供新建/完成/延期入口。

数据来源是 (任务, 优先级分数) 列表；网络请求在后台线程完成，
本窗口只负责渲染与发出操作信号。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QDateTimeEdit,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..ai.provider import LLMProvider
from ..feishu.repository import SubTask, Task
from .ai_task_dialog import AITaskDialog
from .detail_dialog import TaskDetailDialog
from .task_dialog import NewTaskDialog
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
    """只读任务表格；内部记录每行的 record_id 供选中取用。"""

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
        # 每行对应一条任务的 record_id，选中行后从这里取
        self._row_record_ids: list[str] = []

    def render(self, scored_tasks: list[tuple[Task, float]]) -> None:
        """用打分后的任务列表填充表格。"""
        self.setRowCount(len(scored_tasks))
        self._row_record_ids = [task.record_id for task, _ in scored_tasks]
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

    def selected_record_id(self) -> str:
        """当前选中行的 record_id；未选中返回空串。"""
        return self.selected_record_id_from_row(self.currentRow())

    def selected_record_id_from_row(self, row: int) -> str:
        """指定行的 record_id；越界返回空串。"""
        if 0 <= row < len(self._row_record_ids):
            return self._row_record_ids[row]
        return ""


class DeferDialog(QDialog):
    """延期对话框：快捷预设 + 自定义时间。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("延期到")
        self.setModal(True)
        self._new_due: datetime | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        # 自定义时间
        row = QHBoxLayout()
        row.addWidget(QLabel("自定义："))
        self.dt_edit = QDateTimeEdit(datetime.now() + timedelta(days=1))
        self.dt_edit.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.dt_edit.setCalendarPopup(True)
        row.addWidget(self.dt_edit, stretch=1)
        layout.addLayout(row)

        # 按钮组
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def new_due(self) -> datetime:
        return self.dt_edit.dateTime().toPython()


class MainWindow(QWidget):
    """主窗口。"""

    # 用户操作信号（由 main.py 接线）
    sync_requested = Signal()
    pause_toggled = Signal(bool)
    create_requested = Signal(dict)                       # 新建任务的字段 dict
    complete_requested = Signal(str)                      # record_id
    defer_requested = Signal(str, datetime)               # record_id, 新截止时间
    detail_requested = Signal(str)                        # record_id（查看详情）
    breakdown_apply_requested = Signal(dict)              # M3：采纳 AI 拆解建议

    def __init__(self):
        super().__init__()
        self.setWindowTitle("个人任务助理")
        self.setWindowIcon(load_app_icon())
        self.resize(860, 620)
        self._paused = False
        # M3：AI 供应商（未配置 Key 时为 None，AI 功能置灰）
        self._ai_provider: LLMProvider | None = None
        self._ai_threshold = 0.6
        # 最近一次渲染的任务（record_id -> Task），供详情查看
        self._rendered_tasks: dict[str, Task] = {}
        self._build_ui()

    def _build_ui(self) -> None:
        """搭建界面布局。"""
        layout = QVBoxLayout(self)

        # ===== 顶部操作栏 =====
        top = QHBoxLayout()
        self.status_label = QLabel("尚未同步")
        top.addWidget(self.status_label, stretch=1)

        self.new_button = QPushButton("新建任务")
        self.new_button.clicked.connect(self._on_new_task)
        top.addWidget(self.new_button)

        # M3：AI 一句话建任务（未配置 Key 时置灰）
        self.ai_button = QPushButton("AI 建任务")
        self.ai_button.clicked.connect(self._on_ai_create)
        self.ai_button.setEnabled(False)
        self.ai_button.setToolTip("未配置 DEEPSEEK_API_KEY（config/.env）")
        top.addWidget(self.ai_button)

        self.complete_button = QPushButton("完成")
        self.complete_button.clicked.connect(self._on_complete)
        top.addWidget(self.complete_button)

        self.detail_button = QPushButton("详情")
        self.detail_button.clicked.connect(self._on_detail)
        top.addWidget(self.detail_button)

        self.defer_button = QPushButton("延期")
        self.defer_button.clicked.connect(self._on_defer)
        top.addWidget(self.defer_button)

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

        # ===== 逾期任务区（占可用高度约 25%） =====
        self.overdue_title = QLabel("⚠️ 逾期任务")
        f = self.overdue_title.font()
        f.setBold(True)
        self.overdue_title.setFont(f)
        layout.addWidget(self.overdue_title)
        self.overdue_table = TaskTable()
        layout.addWidget(self.overdue_table, stretch=1)

        # ===== 待办任务区（未逾期，按优先级分数排序，默认折叠，占可用高度约 50%） =====
        # 折叠状态下只显示前 N 个最重要的任务，其余可点「展开全部」查看
        self._today_collapsed = True
        self._today_collapse_limit = 5
        self._today_all_scored: list[tuple[Task, float]] = []
        today_header = QHBoxLayout()
        self.today_title = QLabel("📋 待办任务")
        self.today_title.setFont(f)
        today_header.addWidget(self.today_title)
        today_header.addStretch()
        self.today_toggle = QPushButton("展开全部")
        self.today_toggle.setFlat(True)
        self.today_toggle.setCursor(Qt.PointingHandCursor)
        self.today_toggle.setStyleSheet("color: #1976d2; text-decoration: underline;")
        self.today_toggle.clicked.connect(self._on_today_toggle)
        self.today_toggle.setVisible(False)
        today_header.addWidget(self.today_toggle)
        layout.addLayout(today_header)
        self.today_table = TaskTable()
        layout.addWidget(self.today_table, stretch=2)

        # 双击任务行打开详情
        self.today_table.cellDoubleClicked.connect(self._on_row_double_clicked)
        self.overdue_table.cellDoubleClicked.connect(self._on_row_double_clicked)

        # ===== 收集箱区（状态为「收集箱」的任务，未排期，占可用高度约 25%） =====
        inbox_header = QHBoxLayout()
        self.inbox_title = QLabel("📥 收集箱")
        self.inbox_title.setFont(f)
        inbox_header.addWidget(self.inbox_title)
        inbox_header.addStretch()
        layout.addLayout(inbox_header)
        self.inbox_table = TaskTable()
        layout.addWidget(self.inbox_table, stretch=1)
        self.inbox_table.cellDoubleClicked.connect(self._on_row_double_clicked)

    # ------------------------------------------------------------------
    # 对外接口
    # ------------------------------------------------------------------
    def render(
        self,
        today_scored: list[tuple[Task, float]],
        overdue_scored: list[tuple[Task, float]],
        inbox_scored: list[tuple[Task, float]] | None = None,
        synced_at: datetime | None = None,
    ) -> None:
        """渲染待办/逾期/收集箱三个任务表格。

        待办列表按优先级分数降序排序后默认折叠，只显示前 N 个最重要的任务，
        其余可通过「展开全部」按钮查看。
        """
        # 待办：保存全部 + 按折叠状态切片显示
        self._today_all_scored = today_scored
        self._render_today()

        self.overdue_table.render(overdue_scored)

        # 收集箱可选（旧调用方未传时按空列表处理）
        inbox_scored = inbox_scored or []
        self.inbox_table.render(inbox_scored)

        # 记录本轮渲染的任务，供详情按钮/双击取用
        self._rendered_tasks = {t.record_id: t for t, _ in today_scored}
        self._rendered_tasks.update({t.record_id: t for t, _ in overdue_scored})
        self._rendered_tasks.update({t.record_id: t for t, _ in inbox_scored})

        self.today_title.setText(f"📋 待办任务（{len(today_scored)}）")
        self.overdue_title.setText(f"⚠️ 逾期任务（{len(overdue_scored)}）")
        self.inbox_title.setText(f"📥 收集箱（{len(inbox_scored)}）")
        if synced_at:
            self.status_label.setText(f"最近同步：{synced_at.strftime('%Y-%m-%d %H:%M')}")

    def _render_today(self) -> None:
        """根据折叠状态渲染待办列表。折叠时只显示前 N 个，并显示「展开全部」按钮。"""
        all_scored = self._today_all_scored
        limit = self._today_collapse_limit
        if self._today_collapsed and len(all_scored) > limit:
            shown = all_scored[:limit]
            self.today_table.render(shown)
            self.today_toggle.setText(
                f"展开全部（剩余 {len(all_scored) - limit} 个）"
            )
            self.today_toggle.setVisible(True)
        else:
            self.today_table.render(all_scored)
            if len(all_scored) > limit:
                self.today_toggle.setText("收起")
                self.today_toggle.setVisible(True)
            else:
                self.today_toggle.setVisible(False)

    def _on_today_toggle(self) -> None:
        """点击「展开全部/收起」按钮：切换折叠状态并重新渲染。"""
        self._today_collapsed = not self._today_collapsed
        self._render_today()

    def set_pending_count(self, n: int) -> None:
        """状态栏追加待推送条数提示。"""
        base = self.status_label.text().split("｜")[0]
        if n > 0:
            self.status_label.setText(f"{base}｜待推送 {n} 条")
        else:
            self.status_label.setText(base)

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

    def set_ai_provider(self, provider: LLMProvider | None, threshold: float = 0.6) -> None:
        """注入 AI 供应商（M3）：已配置 Key 时启用 AI 建任务按钮。"""
        self._ai_provider = provider
        self._ai_threshold = threshold
        self.ai_button.setEnabled(provider is not None)
        if provider is None:
            self.ai_button.setToolTip("未配置 DEEPSEEK_API_KEY（config/.env）")
        else:
            self.ai_button.setToolTip("一句话交给 AI 拆解建任务")

    def show_task_detail(self, task: Task, subtasks: list[SubTask]) -> None:
        """打开任务详情对话框（由 main.py 提供子任务数据）。"""
        dlg = TaskDetailDialog(
            task,
            subtasks,
            parent=self,
            ai_provider=self._ai_provider,
            ai_threshold=self._ai_threshold,
        )
        # 采纳 AI 拆解建议 → 转发给 main.py 走离线队列写表
        dlg.apply_requested.connect(self.breakdown_apply_requested.emit)
        dlg.exec()

    # ------------------------------------------------------------------
    # 内部回调
    # ------------------------------------------------------------------
    def _on_pause_clicked(self) -> None:
        """暂停/恢复按钮回调。"""
        self.set_paused(not self._paused)
        self.pause_toggled.emit(self._paused)

    def _on_new_task(self) -> None:
        """弹出新建任务对话框，确认后发信号给 main.py。"""
        dlg = NewTaskDialog(self)
        if dlg.exec() == QDialog.Accepted:
            self.create_requested.emit(dlg.result_dict())

    def _on_ai_create(self) -> None:
        """M3：AI 一句话建任务，确认后走与手动新建相同的写回链路。"""
        if self._ai_provider is None:
            return
        dlg = AITaskDialog(self._ai_provider, self._ai_threshold, parent=self)
        if dlg.exec() == QDialog.Accepted:
            self.create_requested.emit(dlg.result_dict())

    def _current_selected_record_id(self) -> str:
        """优先取待办表选中行，其次逾期表，再次收集箱表。"""
        rid = self.today_table.selected_record_id()
        if rid:
            return rid
        rid = self.overdue_table.selected_record_id()
        if rid:
            return rid
        return self.inbox_table.selected_record_id()

    def _on_detail(self) -> None:
        """详情按钮：对当前选中行发出查看详情信号。"""
        rid = self._current_selected_record_id()
        if rid:
            self.detail_requested.emit(rid)

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        """双击任务行：发出该行任务的查看详情信号。"""
        table = self.sender()
        if table is self.today_table:
            rid = self.today_table.selected_record_id_from_row(row)
        elif table is self.overdue_table:
            rid = self.overdue_table.selected_record_id_from_row(row)
        elif table is self.inbox_table:
            rid = self.inbox_table.selected_record_id_from_row(row)
        else:
            return
        if rid:
            self.detail_requested.emit(rid)

    def _on_complete(self) -> None:
        rid = self._current_selected_record_id()
        if rid:
            self.complete_requested.emit(rid)

    def _on_defer(self) -> None:
        rid = self._current_selected_record_id()
        if not rid:
            return
        # 快捷预设菜单
        menu = QMenu(self)
        tonight = menu.addAction("今晚 23:59")
        tomorrow = menu.addAction("明天 18:00")
        custom = menu.addAction("自定义时间…")
        chosen = menu.exec(self.defer_button.mapToGlobal(self.defer_button.rect().bottomLeft()))
        if chosen is None:
            return
        now = datetime.now()
        if chosen is tonight:
            new_due = now.replace(hour=23, minute=59, second=0, microsecond=0)
        elif chosen is tomorrow:
            new_due = (now + timedelta(days=1)).replace(hour=18, minute=0, second=0, microsecond=0)
        elif chosen is custom:
            dlg = DeferDialog(self)
            if dlg.exec() != QDialog.Accepted:
                return
            new_due = dlg.new_due()
        else:
            return
        self.defer_requested.emit(rid, new_due)

    def closeEvent(self, event) -> None:
        """点击关闭按钮时不退出程序，隐藏到托盘继续后台运行。"""
        event.ignore()
        self.hide()
