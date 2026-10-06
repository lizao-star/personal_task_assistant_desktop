"""Windows 开机自启管理。

通过写入当前用户注册表 Run 项实现：
    HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run

启动命令使用 pythonw.exe（无控制台窗口）运行客户端入口。
非 Windows 平台调用时所有函数安全降级。
"""

from __future__ import annotations

import sys
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "PersonalTaskAssistant"


def _build_command() -> str:
    """构造开机启动命令：pythonw.exe + 入口脚本路径。"""
    # 入口脚本：client_windows/run.py
    entry = Path(__file__).resolve().parents[2] / "run.py"
    # 优先用同目录下的 pythonw.exe（无黑窗口）
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    python = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{python}" "{entry}"'


def is_enabled() -> bool:
    """是否已开启开机自启。"""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE_NAME)
        return True
    except OSError:
        return False
    except ImportError:
        return False


def enable() -> None:
    """开启开机自启。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _build_command())
    except ImportError:
        print("当前系统不支持注册表，开机自启不可用")


def disable() -> None:
    """关闭开机自启。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        # 本来就没有该启动项，视为已关闭
        pass
    except OSError:
        pass
    except ImportError:
        pass


def set_enabled(enabled: bool) -> None:
    """按布尔值统一开关。"""
    if enabled:
        enable()
    else:
        disable()
