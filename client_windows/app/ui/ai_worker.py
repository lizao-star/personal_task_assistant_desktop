"""AI 调用的后台线程封装（M3）。

项目约定：界面线程不做 IO。AI 请求耗时可达数十秒，
必须放到独立 QThread 执行，结果经信号回主线程刷新 UI。
"""

from __future__ import annotations

from typing import Any, Callable

from PySide6.QtCore import QObject, QThread, Signal, Slot


class AIWorker(QObject):
    """在独立线程中执行 fn()，返回值/异常经信号回传。"""

    succeeded = Signal(object)  # fn 的返回值
    failed = Signal(str)        # 错误消息

    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self._fn = fn

    @Slot()
    def run(self) -> None:
        """线程入口：执行并转发结果，任何异常都不让线程崩溃。"""
        try:
            self.succeeded.emit(self._fn())
        except Exception as e:  # noqa: BLE001 - 边界层必须兜住
            self.failed.emit(str(e))


class AIInvoker(QObject):
    """单次 AI 调用的线程管理器（同一时刻只允许一个任务在跑）。

    用法：
        invoker = AIInvoker(self)
        invoker.done.connect(self._on_done)
        invoker.error.connect(self._on_error)
        invoker.start(lambda: parse_task(provider, text))
    """

    done = Signal(object)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._thread: QThread | None = None
        self._worker: AIWorker | None = None

    @property
    def running(self) -> bool:
        """是否有任务在执行。"""
        return self._thread is not None

    def start(self, fn: Callable[[], Any]) -> None:
        """在后台线程执行 fn；已有任务在跑时忽略本次。"""
        if self._thread is not None:
            return
        self._thread = QThread()
        self._worker = AIWorker(fn)
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.succeeded.connect(self.done.emit)
        self._worker.failed.connect(self.error.emit)
        # 结束后退出事件循环并清理引用
        self._worker.succeeded.connect(self._finish)
        self._worker.failed.connect(self._finish)
        self._thread.start()

    @Slot()
    def _finish(self) -> None:
        """任务结束：退出线程事件循环并清理。"""
        if self._thread is None:
            return
        self._thread.quit()
        self._thread.wait()
        self._thread = None
        self._worker = None
