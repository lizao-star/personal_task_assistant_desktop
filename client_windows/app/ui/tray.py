"""系统托盘控制器。

菜单：显示主窗口 / 立即同步 / 暂停提醒 / 开机自启 / 退出。
双击托盘图标显示主窗口。
"""

from __future__ import annotations

from PySide6.QtGui import QAction, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QStyle,
    QSystemTrayIcon,
    QWidget,
)

from ..config import APP_ICON_PATH
from ..services import autostart


def load_app_icon() -> QIcon:
    """加载自定义应用图标；文件缺失时回退到系统电脑图标。"""
    if APP_ICON_PATH.exists():
        return QIcon(str(APP_ICON_PATH))
    return QApplication.style().standardIcon(QStyle.SP_ComputerIcon)


class TrayController:
    """托盘图标与菜单管理。"""

    def __init__(self, parent: QWidget):
        self._parent = parent
        self.tray = QSystemTrayIcon(parent)
        self.tray.setIcon(load_app_icon())
        self.tray.setToolTip("个人任务助理")

        menu = QMenu(parent)

        self.show_action = QAction("打开主界面", parent)
        menu.addAction(self.show_action)

        self.sync_action = QAction("立即同步", parent)
        menu.addAction(self.sync_action)

        self.pause_action = QAction("暂停提醒", parent)
        self.pause_action.setCheckable(True)
        menu.addAction(self.pause_action)

        menu.addSeparator()

        self.autostart_action = QAction("开机自启", parent)
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(autostart.is_enabled())
        menu.addAction(self.autostart_action)

        menu.addSeparator()

        self.quit_action = QAction("退出", parent)
        menu.addAction(self.quit_action)

        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_activated)

    def show(self) -> None:
        """显示托盘图标。"""
        self.tray.show()

    def show_message(self, title: str, message: str) -> None:
        """在托盘处显示一条气泡消息。"""
        self.tray.showMessage(title, message)

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        """双击托盘图标时显示主窗口。"""
        if reason == QSystemTrayIcon.DoubleClick:
            self._parent.show()
            self._parent.raise_()
            self._parent.activateWindow()
