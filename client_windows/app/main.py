"""应用启动与总控模块。

职责：
1. 创建主窗口、托盘、本地缓存与提醒引擎；
2. 用后台 QThread 执行飞书同步，避免阻塞界面；
3. QTimer 定时自动同步（默认 45s）与定时检查提醒（默认 30s）；
4. 接线托盘菜单、暂停提醒、开机自启等操作。
"""

from __future__ import annotations

import sys
from datetime import datetime

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QApplication

from .config import DATA_DIR, load_config
from .feishu.client import FeishuClient
from .feishu.repository import (
    Task,
    get_overdue_tasks,
    get_today_tasks,
    list_tasks,
)
from .services import autostart
from .services.cache import LocalCache
from .services.priority import rank_tasks
from .services.reminder import Notifier, ReminderEngine
from .ui.main_window import MainWindow
from .ui.tray import TrayController, load_app_icon


class SyncWorker(QObject):
    """飞书同步后台任务（在独立线程中运行）。"""

    succeeded = Signal(list)   # list[Task]
    failed = Signal(str)

    def __init__(self, credential, timeout: int):
        super().__init__()
        self._credential = credential
        self._timeout = timeout

    @Slot()
    def run(self) -> None:
        """线程入口：拉取任务表全部记录。"""
        try:
            c = self._credential
            client = FeishuClient(c.app_id, c.app_secret, timeout=self._timeout)
            tasks = list_tasks(client, c.app_token, c.task_table_id)
            self.succeeded.emit(tasks)
        except Exception as e:  # 任何异常都回传主线程提示，不能让后台线程崩溃
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
        timeout = int(sync_cfg.get("request_timeout_seconds", 10))
        self._timeout = timeout

        self.sync_timer = QTimer(self)
        self.sync_timer.setInterval(interval * 1000)
        self.sync_timer.timeout.connect(self.start_sync)
        self.sync_timer.start()

        reminder_cfg = self._settings.get("reminder", {})
        check_interval = int(reminder_cfg.get("check_interval_seconds", 30))
        self.reminder_timer = QTimer(self)
        self.reminder_timer.setInterval(check_interval * 1000)
        self.reminder_timer.timeout.connect(self._check_reminders)
        self.reminder_timer.start()

    def _bootstrap(self) -> None:
        """启动时：凭证齐全则立即同步；否则用本地缓存渲染并提示。"""
        # 启动即显示主窗口（关闭窗口只是隐藏到托盘，不会退出）
        self._show_window()
        # 先用缓存渲染，界面不空
        self._render(self._cache.load_tasks())
        if self._credential.is_ready:
            self.start_sync()
        else:
            self.window.show_warning(
                "飞书凭证未配置：请复制 config/.env.example 为 config/.env，"
                "填写 App ID、App Secret、app_token 与任务表 table_id 后重启。"
            )

    # ------------------------------------------------------------------
    # 同步
    # ------------------------------------------------------------------
    def start_sync(self) -> None:
        """发起一次后台同步（已在运行则忽略）。"""
        if self._thread is not None:
            return
        if not self._credential.is_ready:
            self.window.show_warning("飞书凭证未配置，无法同步（当前显示本地缓存）。")
            return

        self.window.set_syncing(True)
        self._thread = QThread()
        self._worker = SyncWorker(self._credential, getattr(self, "_timeout", 10))
        self._worker.moveToThread(self._thread)

        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self._on_sync_succeeded)
        self._worker.failed.connect(self._on_sync_failed)
        # 结束后清理线程
        self._worker.succeeded.connect(self._cleanup_thread)
        self._worker.failed.connect(self._cleanup_thread)

        self._thread.start()

    @Slot(list)
    def _on_sync_succeeded(self, tasks: list[Task]) -> None:
        """同步成功：更新缓存、渲染并立即检查一次提醒。"""
        self._cache.replace_tasks(tasks)
        self._cache.set_meta("last_sync", datetime.now().isoformat(timespec="seconds"))
        self.window.show_warning("")  # 成功后清除警告
        self._render(tasks)
        # 立即检查提醒（定时器也会检查，这里保证实时性）
        self._engine.check(tasks, muted=self._paused)

    @Slot(str)
    def _on_sync_failed(self, message: str) -> None:
        """同步失败：用本地缓存渲染，并在状态栏提示。"""
        self._render(self._cache.load_tasks())
        self.window.show_warning(f"同步失败：{message}（当前显示本地缓存）")

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
