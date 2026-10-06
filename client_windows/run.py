"""客户端启动入口（双击或命令行运行本文件）。

用法（在 client_windows 目录下）：
    python run.py

开机自启时由 autostart.py 自动拼接 pythonw.exe + 本文件路径，无控制台窗口。
"""

import sys
from pathlib import Path

# 确保无论从哪个目录启动，都能正确导入 app 包
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402  （需在 sys.path 设置之后导入）

if __name__ == "__main__":
    sys.exit(main())
