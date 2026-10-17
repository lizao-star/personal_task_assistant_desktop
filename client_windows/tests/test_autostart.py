"""开机自启命令构造的单元测试。

重点：自启命令必须优先使用项目 .venv 的 pythonw.exe，
避免在 Spyder 等宿主环境里开启自启时把宿主解释器写进注册表，
导致任务栏显示宿主程序的图标。
"""

import unittest
from pathlib import Path

from app.services import autostart

CLIENT_ROOT = Path(__file__).resolve().parents[1]


class TestBuildCommand(unittest.TestCase):
    """_build_command：自启命令的解析器选择与路径拼接。"""

    def setUp(self):
        self.command = autostart._build_command()

    def test_uses_venv_pythonw_when_available(self):
        """项目 venv 的 pythonw.exe 存在时，必须优先使用它。"""
        venv_pythonw = CLIENT_ROOT / ".venv" / "Scripts" / "pythonw.exe"
        if not venv_pythonw.exists():
            self.skipTest("本机无 .venv/Scripts/pythonw.exe")
        self.assertIn(str(venv_pythonw), self.command)

    def test_points_to_run_py_entry(self):
        """命令应指向 client_windows/run.py 入口脚本。"""
        self.assertIn("run.py", self.command)
        self.assertIn(str(CLIENT_ROOT / "run.py"), self.command)

    def test_paths_are_quoted(self):
        """解释器与脚本路径都必须带引号，防止路径含空格时被截断。"""
        self.assertTrue(self.command.startswith('"'))
        self.assertIn('" "', self.command)


if __name__ == "__main__":
    unittest.main()
