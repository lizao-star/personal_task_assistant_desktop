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

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication, QMessageBox

from .config import DATA_DIR, load_config
from .constants import STATUS_DONE, STATUS_TODO
from .feishu.client import FeishuClient
from .feishu.repository import (
    Task,
    build_complete_fields,
    build_create_fields,
    build_defer_fields,
    generate_external_id,
    get_overdue_tasks,
    get_today_tasks,
)
from .services import autostart
from .services.cache import LocalCache
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
            svc = SyncService(client, c.app_token, c.task_table_id, self._cache)
            if self._full:
                tasks = svc.full_pull()
                self.succeeded.emit(tasks)
            else:
                _, conflicts, last_error = svc.push_pending()
                if last_error:
                    self.push_error.emit(last_error)
                if conflicts:
                    # 冲突先回主线程，让用户决定后再触发下一轮
                    self.conflicts.emit(conflicts)
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

        # 同步线程相关
        self._thread: QThread | None = None
        self._worker: SyncWorker | None = None
        self._paused = False
        self._timeout = 10

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

        # 托盘
        self.tray.show_action.triggered.connect(self._show_window)
        self.tray.sync_action.triggered.connect(self.start_sync)
        self.tray.pause_action.triggered.connect(self._on_pause_changed)
        self.tray.autostart_action.triggered.connect(self._on_autostart_toggled)
        self.tray.quit_action.triggered.connect(self._quit)

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
            action = "完成" if op_type == "complete" else "延期"
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
        """新建任务：入队并触发推送（不乐观更新，等拉取回来再显示）。"""
        external_id = generate_external_id()
        fields = build_create_fields(external_id=external_id, **payload)
        self._cache.enqueue_op("create", {"external_id": external_id, "fields": fields})
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
        """计算优先级并渲染今日/逾期表格。"""
        today_scored = rank_tasks(get_today_tasks(tasks))
        overdue_scored = rank_tasks(get_overdue_tasks(tasks))
        self.window.render(today_scored, overdue_scored, datetime.now())

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
        """统一处理暂停提醒（窗口与托盘状态保持一致）。"""
        self._paused = paused
        self.window.set_paused(paused)
        self.tray.pause_action.setChecked(paused)

    def _on_autostart_toggled(self, checked: bool) -> None:
        """开机自启开关。"""
        autostart.set_enabled(checked)
        # 注册表写入可能失败，回读真实状态纠正勾选
        self.tray.autostart_action.setChecked(autostart.is_enabled())

    def shutdown(self) -> None:
        """退出前收尾：停定时器，等待同步线程结束。"""
        self.sync_timer.stop()
        self.reminder_timer.stop()
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
