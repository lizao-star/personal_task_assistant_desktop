"""应用启动与总控模块。

职责：
1. 创建主窗口、托盘、本地缓存与提醒引擎；
2. 用后台 QThread 执行飞书同步（推送离线队列 + 拉取），避免阻塞界面；
3. QTimer 定时自动同步（默认 45s）与定时检查提醒（默认 30s）；
4. 接线托盘菜单、暂停提醒、开机自启、新建/完成/延期写操作；
5. 冲突时弹窗让用户决定覆盖或放弃。

设计要点：
- 写操作统一走「先入队 + 乐观更新 + 后台推送」，断网时也能记账；
- 拉取分手动全量与自动增量两种，增量失败自动回退全量；
- 推送与拉取共用同一个后台线程，避免并发互相打架。
"""

from __future__ import annotations

import sys
from datetime import datetime
from typing import Any

from PySide6.QtCore import (
    Q_ARG,
    QMetaObject,
    QObject,
    Qt,
    QThread,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtWidgets import QApplication, QMessageBox

from .ai.provider import DeepSeekProvider
from .config import DATA_DIR, load_config
from .constants import STATUS_DONE, STATUS_TODO
from .feishu.client import FeishuClient
from .feishu.repository import (
    Task,
    build_ai_breakdown_fields,
    build_complete_fields,
    build_create_fields,
    build_defer_fields,
    generate_external_id,
    get_overdue_tasks,
    get_today_tasks,
    get_inbox_tasks,
    group_subtasks,
)
from .services import autostart
from .services.cache import LocalCache
from .services.kill_tab import close_tab
from .services.local_api import LocalApiServer
from .services.monitor import MonitorWorker
from .services.overlay import Overlay
from .services.pomodoro import STATE_IDLE, PomodoroTimer
from .services.priority import rank_tasks
from .services.reminder import Notifier, ReminderEngine
from .services.sync import SyncService
from .ui.main_window import MainWindow
from .ui.tray import TrayController, load_app_icon


class SyncWorker(QObject):
    """飞书同步后台任务（在独立线程中运行）。

    full=True  → 全量拉取（启动/手动按钮）
    full=False → 推送离线队列 + 增量拉取（自动轮询/写完触发）
    """

    succeeded = Signal(list)            # list[Task]
    failed = Signal(str)
    conflicts = Signal(list)            # list[dict]：冲突项
    push_error = Signal(str)            # 推送失败时透传给 UI 提示

    def __init__(self, credential, timeout: int, cache: LocalCache, full: bool):
        super().__init__()
        self._credential = credential
        self._timeout = timeout
        self._cache = cache
        self._full = full

    @Slot()
    def run(self) -> None:
        """线程入口：先推送后拉取。"""
        try:
            c = self._credential
            client = FeishuClient(c.app_id, c.app_secret, timeout=self._timeout)
            svc = SyncService(
                client,
                c.app_token,
                c.task_table_id,
                self._cache,
                subtask_table_id=c.subtask_table_id,
                monitor_log_table_id=c.monitor_log_table_id,
            )
            if self._full:
                tasks = svc.full_pull()
                # 全量同步时也顺带推一下娱乐日志（避免堆积）
                svc.push_monitor_logs()
                self.succeeded.emit(tasks)
            else:
                _, conflicts, last_error = svc.push_pending()
                if last_error:
                    self.push_error.emit(last_error)
                if conflicts:
                    # 冲突先回主线程，让用户决定后再触发下一轮
                    self.conflicts.emit(conflicts)
                # 娱乐日志推送（无冲突检测，建记录即删）
                svc.push_monitor_logs()
                tasks = svc.incremental_pull()
                self.succeeded.emit(tasks)
        except Exception as e:
            # 任何异常都回传主线程提示，不能让后台线程崩溃
            self.failed.emit(str(e))


class AssistantApp(QObject):
    """应用总控。"""

    def __init__(self, qt_app: QApplication):
        super().__init__()
        self._qt_app = qt_app
        config = load_config()
        self._credential = config.credential
        self._settings = config.settings

        # ----- 本地缓存与提醒 -----
        self._cache = LocalCache(DATA_DIR / "cache.db")
        tts_cfg = self._settings.get("reminder", {}).get("tts", {})
        self._notifier = Notifier(
            tts_engine=tts_cfg.get("engine", "pyttsx3"),
            tts_rate=int(tts_cfg.get("rate", 180)),
        )
        self._engine = ReminderEngine(self._cache, self._notifier, self._settings)

        # ----- 界面 -----
        self.window = MainWindow()
        self.tray = TrayController(self.window)
        self.tray.show()

        # ----- M3：AI 供应商（Key 未配置时保持 None，AI 功能置灰） -----
        self._ai_conf = config.ai
        self._ai_provider: DeepSeekProvider | None = None
        if self._ai_conf.is_ready:
            self._ai_provider = DeepSeekProvider(
                self._ai_conf.api_key,
                base_url=self._ai_conf.base_url,
                model=self._ai_conf.model,
                timeout=self._ai_conf.timeout_seconds,
            )
        self.window.set_ai_provider(
            self._ai_provider, threshold=self._ai_conf.confidence_threshold
        )

        # 同步线程相关
        self._thread: QThread | None = None
        self._worker: SyncWorker | None = None
        self._paused = False
        self._timeout = 10

        # ----- M4：娱乐监督 -----
        # 全屏遮罩（懒加载，避免启动即弹出）
        self._overlay: Overlay | None = None
        # 本地 API（接收扩展上报 / 下发关标签请求）
        monitor_cfg = self._settings.get("monitor", {})
        api_port = int(monitor_cfg.get("local_api_port", 8765))
        self._local_api = LocalApiServer(port=api_port)
        # 监控引擎（在独立 QThread）
        self._monitor_thread: QThread | None = None
        self._monitor_worker: MonitorWorker | None = None
        self._monitor_enabled = bool(monitor_cfg.get("enabled", True))
        self._init_monitor()

        # ----- M4 补充：番茄钟（纯计时，主线程 QTimer，不与监督联动） -----
        pomo_cfg = self._settings.get("pomodoro", {})
        self._pomodoro = PomodoroTimer(
            focus_seconds=int(pomo_cfg.get("focus_minutes", 25)) * 60,
            rest_seconds=int(pomo_cfg.get("rest_minutes", 5)) * 60,
            parent=self.window,
        )

        self._wire_signals()
        self._start_timers()
        self._bootstrap()

    # ------------------------------------------------------------------
    # 接线
    # ------------------------------------------------------------------
    def _wire_signals(self) -> None:
        """连接界面与托盘信号。"""
        # 主窗口
        self.window.sync_requested.connect(self.start_sync)
        self.window.pause_toggled.connect(self._on_pause_changed)
        self.window.create_requested.connect(self._on_create)
        self.window.complete_requested.connect(self._on_complete)
        self.window.defer_requested.connect(self._on_defer)
        self.window.detail_requested.connect(self._on_detail)
        self.window.breakdown_apply_requested.connect(self._on_breakdown_apply)

        # 托盘
        self.tray.show_action.triggered.connect(self._show_window)
        self.tray.sync_action.triggered.connect(self.start_sync)
        self.tray.pause_action.triggered.connect(self._on_pause_changed)
        self.tray.monitor_action.triggered.connect(self._on_monitor_toggled)
        self.tray.autostart_action.triggered.connect(self._on_autostart_toggled)
        self.tray.quit_action.triggered.connect(self._quit)

        # M4 补充：番茄钟
        self.tray.pomodoro_action.triggered.connect(self._on_pomodoro_clicked)
        self._pomodoro.state_changed.connect(self.tray.set_pomodoro_state)
        self._pomodoro.phase_finished.connect(self._on_pomodoro_phase_finished)

        # M4：监控引擎信号
        if self._monitor_worker is not None:
            self._monitor_worker.notify_requested.connect(self._on_monitor_notify)
            self._monitor_worker.action_requested.connect(self._on_monitor_action)
            self._monitor_worker.session_ended.connect(self._on_monitor_session_ended)

    def _start_timers(self) -> None:
        """启动自动同步与提醒检查定时器。"""
        sync_cfg = self._settings.get("sync", {})
        interval = int(sync_cfg.get("interval_seconds", 45))
        self._timeout = int(sync_cfg.get("request_timeout_seconds", 10))

        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(interval * 1000)
        # 自动轮询：增量 + 推送
        self.sync_timer.timeout.connect(self.start_incremental_sync)
        self.sync_timer.start()

        reminder_cfg = self._settings.get("reminder", {})
        check_interval = int(reminder_cfg.get("check_interval_seconds", 30))
        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(check_interval * 1000)
        self.reminder_timer.timeout.connect(self._check_reminders)
        self.reminder_timer.start()

    def _bootstrap(self) -> None:
        """启动时：凭证齐全则立即全量同步；否则用本地缓存渲染并提示。"""
        # 启动即显示主窗口（关闭窗口只是隐藏到托盘，不会退出）
        self._show_window()
        # 先用缓存渲染，界面不空
        self._render(self._cache.load_tasks())
        self._refresh_pending_label()
        if self._credential.is_ready:
            self.start_sync()
        else:
            self.window.show_warning(
                "飞书凭证未配置：请复制 config/.env.example 为 config/.env，"
                "填写 App ID、App Secret、app_token 与任务表 table_id 后重启。"
            )

    # ------------------------------------------------------------------
    # 同步（全量 / 增量共用同一个线程）
    # ------------------------------------------------------------------
    def start_sync(self) -> None:
        """手动全量同步（启动/立即同步按钮）。"""
        self._launch_sync(full=True)

    def start_incremental_sync(self) -> None:
        """自动增量同步：先推送离线队列，再按游标拉增量。"""
        self._launch_sync(full=False)

    def _launch_sync(self, full: bool) -> None:
        """启动后台同步线程；已有线程在跑则忽略本次。"""
        if self._thread is not None:
            return
        if not self._credential.is_ready:
            self.window.show_warning("飞书凭证未配置，无法同步（当前显示本地缓存）。")
            return

        self.window.set_syncing(True)
        self._thread = QThread()
        self._worker = SyncWorker(self._credential, self._timeout, self._cache, full)
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._on_sync_succeeded)
        self._worker.failed.connect(self._on_sync_failed)
        self._worker.conflicts.connect(self._on_conflicts)
        self._worker.push_error.connect(self._on_push_error)
        # 结束后清理线程
        self._worker.succeeded.connect(self._cleanup_thread)
        self._worker.failed.connect(self._cleanup_thread)

        self._thread.start()

    @Slot(list)
    def _on_sync_succeeded(self, tasks: list[Task]) -> None:
        """同步成功：渲染并立即检查一次提醒。"""
        self._cache.set_meta("last_sync", datetime.now().isoformat(timespec="seconds"))
        self.window.show_warning("")  # 成功后清除警告
        self._render(tasks)
        self._refresh_pending_label()
        # 立即检查提醒（定时器也会检查，这里保证实时性）
        self._engine.check(tasks, muted=self._paused)

    @Slot(str)
    def _on_sync_failed(self, message: str) -> None:
        """同步失败：用本地缓存渲染，并在状态栏提示。"""
        self._render(self._cache.load_tasks())
        self._refresh_pending_label()
        self.window.show_warning(f"同步失败：{message}（当前显示本地缓存）")

    @Slot(str)
    def _on_push_error(self, message: str) -> None:
        """推送失败：在警告区显示具体错误（常见为权限不足）。"""
        hint = ""
        if "99991679" in message or "Forbidden" in message or "403" in message:
            hint = "\n提示：请检查飞书应用权限是否已升级为 bitable:app（读写），并已重新发布版本。"
        self.window.show_warning(f"推送失败：{message}{hint}")

    @Slot(list)
    def _on_conflicts(self, conflicts: list) -> None:
        """冲突处理：逐个弹窗问用户。覆盖→force 重推；放弃→删 op 并拉取。"""
        for c in conflicts:
            title = c.get("title", "")
            op_id = int(c.get("op_id", 0))
            op_type = c.get("op_type", "")
            action = SyncService.OP_LABELS.get(op_type, op_type)
            btn = QMessageBox.question(
                self.window,
                "检测到云端变更",
                f"任务「{title}」的{action}操作检测到云端已被其他人/设备修改。\n"
                "是否仍要用本地版本覆盖云端？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if btn == QMessageBox.Yes:
                self._cache.set_op_force(op_id)
            else:
                self._cache.delete_op(op_id)
        # 处理完所有冲突后，触发一次增量同步把 force 的 op 推上去
        self._refresh_pending_label()
        self.start_incremental_sync()

    @Slot()
    def _cleanup_thread(self) -> None:
        """收尾同步线程。"""
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait()
            self._thread = None
            self._worker = None
        self.window.set_syncing(False)

    # ------------------------------------------------------------------
    # 写操作：先入队 + 乐观更新 + 后台推送
    # ------------------------------------------------------------------
    def _on_create(self, payload: dict) -> None:
        """新建任务：入队并触发推送（不乐观更新，等拉取回来再显示）。

        M3 起 AI 建任务的 payload 会额外带 subtasks / low_confidence，
        这两个键不进 build_create_fields，子任务在推送落表后一并写入。
        """
        subtasks = payload.pop("subtasks", [])
        payload.pop("low_confidence", None)
        external_id = generate_external_id()
        fields = build_create_fields(external_id=external_id, **payload)
        self._cache.enqueue_op(
            "create",
            {"external_id": external_id, "fields": fields, "subtasks": subtasks},
        )
        self._refresh_pending_label()
        self.start_incremental_sync()

    def _on_breakdown_apply(self, payload: dict) -> None:
        """M3：采纳 AI 拆解建议——回填 AI拆解/检查清单/AI建议 + 写子任务。

        与完成/延期一致：先入队（带冲突检测基准）+ 乐观更新本地缓存，
        再由后台线程推送；AI 只给建议，写不写永远由用户点「采纳」决定。
        """
        record_id = payload.get("record_id", "")
        task = self._find_cached(record_id)
        if task is None:
            return
        subtasks = payload.get("subtasks") or []
        fields = build_ai_breakdown_fields(
            payload.get("ai_breakdown", ""),
            payload.get("checklist", ""),
            payload.get("advice", ""),
        )
        self._cache.enqueue_op(
            "ai_breakdown",
            {
                "record_id": record_id,
                "fields": fields,
                "subtasks": subtasks,
                "base_ms": task.modified_ms,
                "title": task.title,
            },
        )
        # 乐观更新：立即在详情数据上可见（子任务等同步拉取后出现）
        task.ai_breakdown = payload.get("ai_breakdown", "")
        task.checklist = payload.get("checklist", "")
        self._cache.upsert_tasks([task])
        self._render(self._cache.load_tasks())
        self._refresh_pending_label()
        self.start_incremental_sync()

    def _on_complete(self, record_id: str) -> None:
        """完成任务：入队 + 乐观更新本地缓存（状态置为已完成）。"""
        task = self._find_cached(record_id)
        if task is None:
            return
        fields = build_complete_fields()
        self._cache.enqueue_op(
            "complete",
            {
                "record_id": record_id,
                "fields": fields,
                "base_ms": task.modified_ms,
                "title": task.title,
            },
        )
        # 乐观更新：直接改本地缓存，让任务从今日/逾期列表消失
        task.status = STATUS_DONE
        task.completed_at = datetime.now()
        self._cache.upsert_tasks([task])
        self._render(self._cache.load_tasks())
        self._refresh_pending_label()
        self.start_incremental_sync()

    def _on_defer(self, record_id: str, new_due: datetime) -> None:
        """延期任务：入队 + 乐观更新本地缓存（新截止时间 + 次数+1）。"""
        task = self._find_cached(record_id)
        if task is None:
            return
        fields = build_defer_fields(new_due, task.delay_count)
        self._cache.enqueue_op(
            "defer",
            {
                "record_id": record_id,
                "fields": fields,
                "base_ms": task.modified_ms,
                "title": task.title,
            },
        )
        # 乐观更新
        task.due_at = new_due
        task.delay_count += 1
        task.status = STATUS_TODO
        self._cache.upsert_tasks([task])
        self._render(self._cache.load_tasks())
        self._refresh_pending_label()
        self.start_incremental_sync()

    def _on_detail(self, record_id: str) -> None:
        """查看任务详情：从缓存取任务与子任务，交给主窗口弹窗展示。"""
        task = self._find_cached(record_id)
        if task is None:
            return
        subtasks = group_subtasks(self._cache.load_subtasks(), record_id)
        self.window.show_task_detail(task, subtasks)

    def _find_cached(self, record_id: str) -> Task | None:
        """从本地缓存中按 record_id 找任务。"""
        for t in self._cache.load_tasks():
            if t.record_id == record_id:
                return t
        return None

    def _refresh_pending_label(self) -> None:
        """刷新状态栏待推送条数。"""
        self.window.set_pending_count(self._cache.pending_count())

    # ------------------------------------------------------------------
    # 渲染与提醒
    # ------------------------------------------------------------------
    def _render(self, tasks: list[Task]) -> None:
        """计算优先级并渲染待办/逾期/收集箱表格。"""
        today_scored = rank_tasks(get_today_tasks(tasks))
        overdue_scored = rank_tasks(get_overdue_tasks(tasks))
        inbox_scored = rank_tasks(get_inbox_tasks(tasks))
        self.window.render(today_scored, overdue_scored, inbox_scored, datetime.now())

    def _check_reminders(self) -> None:
        """定时提醒检查：基于最近一次缓存的数据。"""
        tasks = self._cache.load_tasks()
        self._engine.check(tasks, muted=self._paused)

    # ------------------------------------------------------------------
    # 界面动作
    # ------------------------------------------------------------------
    def _show_window(self) -> None:
        """显示并激活主窗口。"""
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def _on_pause_changed(self, paused: bool) -> None:
        """统一处理暂停提醒（窗口与托盘状态保持一致）。

        M4 起：暂停提醒同时暂停娱乐监督（pause_with_reminder=True 时）。
        """
        self._paused = paused
        self.window.set_paused(paused)
        self.tray.pause_action.setChecked(paused)
        if self._monitor_worker is not None:
            # 通过信号把暂停状态投递到监控线程
            QMetaObject.invokeMethod(
                self._monitor_worker, "set_paused", Qt.QueuedConnection,
                Q_ARG(bool, paused),
            )

    def _on_autostart_toggled(self, checked: bool) -> None:
        """开机自启开关。"""
        autostart.set_enabled(checked)
        # 注册表写入可能失败，回读真实状态纠正勾选
        self.tray.autostart_action.setChecked(autostart.is_enabled())

    # ------------------------------------------------------------------
    # M4：娱乐监督接线
    # ------------------------------------------------------------------
    def _init_monitor(self) -> None:
        """初始化监控引擎（独立 QThread + 本地 API 服务）。"""
        if not self._monitor_enabled:
            return
        # 启动本地 API
        try:
            self._local_api.start()
        except OSError as e:
            # 端口被占用：监督仍可跑（无扩展上报，只靠 Win32 取窗口）
            print(f"[监督] 本地 API 启动失败（端口可能被占用）：{e}")
        # 启动监控线程
        self._monitor_thread = QThread()
        self._monitor_worker = MonitorWorker(
            self._cache, self._local_api, self._settings
        )
        self._monitor_worker.moveToThread(self._monitor_thread)
        self._monitor_thread.started.connect(self._monitor_worker.start)
        self._monitor_thread.start()

    def _on_monitor_toggled(self, checked: bool) -> None:
        """托盘「娱乐监督」开关。"""
        self._monitor_enabled = checked
        if self._monitor_worker is None:
            return
        if checked:
            QMetaObject.invokeMethod(
                self._monitor_worker, "start", Qt.QueuedConnection,
            )
        else:
            QMetaObject.invokeMethod(
                self._monitor_worker, "stop", Qt.QueuedConnection,
            )

    @Slot(int, str)
    def _on_monitor_notify(self, level: int, text: str) -> None:
        """监控引擎要求通知/语音：level >= L2 时同时语音。"""
        from .constants import MONITOR_LEVEL_L2
        self._notifier.notify("娱乐监督提醒", text)
        if level >= MONITOR_LEVEL_L2:
            self._notifier.speak(text)

    @Slot(str, str, str, int)
    def _on_monitor_action(self, kind: str, domain: str, title: str, minutes: int) -> None:
        """监控引擎要求执行 L4 动作：overlay 或 kill_tab。"""
        from .constants import MONITOR_ACTION_KILL_TAB, MONITOR_ACTION_OVERLAY
        if kind == MONITOR_ACTION_OVERLAY:
            # 已有遮罩在显示则不重复创建
            if self._overlay is not None:
                return
            self._overlay = Overlay(self.window)
            self._overlay.set_minutes(minutes)
            # 关闭信号回写监控会话 closed 标志
            self._overlay.closed.connect(self._mark_monitor_closed)
            self._overlay.show()
            self._overlay.raise_()
            self._overlay.activateWindow()
        elif kind == MONITOR_ACTION_KILL_TAB:
            # 异步关网页，不阻塞主线程
            result = close_tab(domain, title, self._local_api)
            if result.get("ok"):
                # 关闭成功：标记会话 closed，避免同一会话反复触发
                self._mark_monitor_closed()

    def _mark_monitor_closed(self) -> None:
        """L4 动作已生效：回写监控会话 closed=True，并释放遮罩资源。"""
        session = self._cache.load_monitor_session()
        if session is not None:
            self._cache.update_monitor_session(closed=True)
        if self._overlay is not None:
            self._overlay.deleteLater()
            self._overlay = None

    # ------------------------------------------------------------------
    # M4 补充：番茄钟
    # ------------------------------------------------------------------
    @Slot()
    def _on_pomodoro_clicked(self) -> None:
        """托盘菜单点击：空闲则开始，运行中则停止。"""
        if self._pomodoro.state == STATE_IDLE:
            self._pomodoro.start()
        else:
            self._pomodoro.stop()

    @Slot(str, str)
    def _on_pomodoro_phase_finished(self, phase: str, text: str) -> None:
        """番茄钟阶段自然结束：系统通知 + 语音（遵守 Notifier 勿扰/降级逻辑）。"""
        self._notifier.notify("番茄钟", text)
        self._notifier.speak(text)

    @Slot(dict)
    def _on_monitor_session_ended(self, aggregate: dict) -> None:
        """会话结束：入队日志并触发增量同步推送。"""
        # 把聚合字段转成飞书 fields 并入队
        from datetime import datetime as _dt
        from .feishu.repository import build_monitor_log_fields
        try:
            occurred_at = aggregate.get("occurred_at")
            if isinstance(occurred_at, str):
                occurred_at = _dt.fromisoformat(occurred_at)
            fields = build_monitor_log_fields(
                occurred_at=occurred_at or _dt.now(),
                duration_seconds=int(aggregate.get("duration_seconds", 0)),
                process_name=aggregate.get("process_name", ""),
                window_title=aggregate.get("window_title", ""),
                url=aggregate.get("url", ""),
                notified=bool(aggregate.get("notified", False)),
                notify_count=int(aggregate.get("notify_count", 0)),
                closed=bool(aggregate.get("closed", False)),
                note=aggregate.get("note", ""),
            )
            self._cache.enqueue_monitor_log(fields)
        except Exception as e:
            print(f"[监督] 会话日志入队失败：{e}")
        # 触发增量同步把日志推到飞书
        self.start_incremental_sync()

    def shutdown(self) -> None:
        """退出前收尾：停定时器，等待同步线程结束。"""
        self.sync_timer.stop()
        self.reminder_timer.stop()
        # M4：停监控线程与本地 API
        if self._monitor_worker is not None:
            QMetaObject.invokeMethod(
                self._monitor_worker, "stop", Qt.QueuedConnection,
            )
        if self._monitor_thread is not None:
            self._monitor_thread.quit()
            self._monitor_thread.wait(5000)
            self._monitor_thread = None
            self._monitor_worker = None
        try:
            self._local_api.stop()
        except Exception:
            pass
        # M4 补充：停番茄钟
        self._pomodoro.stop()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(15000)
            self._thread = None
            self._worker = None

    def _quit(self) -> None:
        """退出应用。"""
        self.shutdown()
        self._cache.close()
        self.tray.tray.hide()
        self._qt_app.quit()


def main() -> int:
    """程序入口。"""
    app = QApplication(sys.argv)
    # 关闭最后一个窗口不退出（托盘常驻）
    app.setQuitOnLastWindowClosed(False)
    # 全局应用图标：影响任务栏、Alt+Tab、窗口左上角
    app.setWindowIcon(load_app_icon())

    # 必须持有引用：无引用时 Python 垃圾回收会销毁 AssistantApp，
    # 连带销毁正在运行的同步线程，报 "QThread: Destroyed while thread is still running"
    assistant = AssistantApp(app)
    app.aboutToQuit.connect(assistant.shutdown)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
