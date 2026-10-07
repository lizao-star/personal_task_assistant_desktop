"""全屏遮罩窗口（M4，L4 动作之一）。

L4 触发时弹出置顶全屏半透明红色遮罩，强制用户中断娱乐、回到学习。
点击「我已收到，关闭」按钮后遮罩消失，会话标记 closed=True。

设计要点：
- WindowStaysOnTopHint + FramelessWindowHint + Tool：置顶、无标题栏、不抢任务栏
- 半透明背景（rgba）：既能挡住视线又能看到背后窗口的轮廓
- 全部 UI 在主线程创建与显示（PySide6 要求）
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QPushButton,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class Overlay(QWidget):
    """全屏遮罩窗口。

    信号：
        closed()：用户点击按钮或按 Esc 关闭后发出，
                  MonitorWorker 收到后标记会话 closed=True。
    """

    closed = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        # 只有走 _on_close（按钮 / Esc）才允许真正关闭，
        # 用于拦截 Alt+F4 等系统关闭手势。由 main.py 调 show() 显示。
        self._allow_close = False
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        # 半透明红色背景
        self.setStyleSheet(
            "Overlay { background-color: rgba(180, 30, 30, 230); }"
            "QLabel { color: white; }"
            "QPushButton { "
            "  background-color: white; color: #b22020;"
            "  border: none; padding: 12px 32px;"
            "  border-radius: 6px; font-size: 16px; font-weight: bold;"
            "}"
            "QPushButton:hover { background-color: #ffe5e5; }"
        )

        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        title = QLabel("该回去学习了", self)
        title.setFont(QFont("Microsoft YaHei", 48, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        hint = QLabel(
            "你已经连续娱乐达到设定阈值，本次提醒已记录到飞书日志。\n"
            "关闭遮罩后会话即视为「已结束」。",
            self,
        )
        hint.setFont(QFont("Microsoft YaHei", 16))
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(hint)

        self._minutes_label = QLabel("", self)
        self._minutes_label.setFont(QFont("Microsoft YaHei", 20))
        self._minutes_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self._minutes_label)

        btn = QPushButton("我已收到，关闭", self)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.clicked.connect(self._on_close)
        layout.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)

        # Esc 也能关
        esc = QShortcut(QKeySequence(Qt.Key.Key_Escape), self)
        esc.activated.connect(self._on_close)

    def set_minutes(self, minutes: int) -> None:
        """显示已连续娱乐多少分钟。"""
        self._minutes_label.setText(f"已连续娱乐 {minutes} 分钟")

    def showEvent(self, event) -> None:  # type: ignore[override]
        """显示时铺满当前屏幕（覆盖任务栏，多显示器下取所在屏）。"""
        super().showEvent(event)
        screen = QApplication.screenAt(self.cursor().pos()) or QApplication.primaryScreen()
        if screen is not None:
            self.setGeometry(screen.geometry())

    def _on_close(self) -> None:
        """关闭遮罩并发出 closed 信号。"""
        self._allow_close = True
        self.closed.emit()
        self.close()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        """拦截外部关闭手势（Alt+F4 等），仅允许按钮与 Esc 关闭。"""
        if self._allow_close:
            event.accept()
        else:
            event.ignore()
