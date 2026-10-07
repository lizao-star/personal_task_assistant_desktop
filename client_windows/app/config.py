"""配置加载模块：读取 config/.env（密钥）与 config/settings.yaml（运行参数）。

目录约定：
    work/                       <- 工作区根目录
      config/.env               <- 飞书凭证（不提交）
      config/settings.yaml      <- 运行配置（可提交）
      client_windows/           <- 本客户端
        app/config.py
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

# 本文件位于 client_windows/app/config.py
# parents[0]=app  [1]=client_windows  [2]=工作区根目录
WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = WORKSPACE_ROOT / "config"
CLIENT_ROOT = WORKSPACE_ROOT / "client_windows"
DATA_DIR = CLIENT_ROOT / "data"
ASSETS_DIR = CLIENT_ROOT / "assets"
APP_ICON_PATH = ASSETS_DIR / "app_icon.png"


@dataclass(frozen=True)
class FeishuCredential:
    """飞书应用与数据表标识。"""

    app_id: str
    app_secret: str
    app_token: str
    task_table_id: str
    subtask_table_id: str = ""  # 可选：子任务表 table_id（M3 详情页展示子任务用）
    monitor_log_table_id: str = ""  # 可选：娱乐监控日志表 table_id（M4 聚合写日志用）

    @property
    def is_ready(self) -> bool:
        """凭证是否齐全（决定能否发起同步）。"""
        return all([self.app_id, self.app_secret, self.app_token, self.task_table_id])


@dataclass(frozen=True)
class AIConfig:
    """DeepSeek 接入配置（M3）。Key 只放 config/.env，绝不入库。"""

    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    confidence_threshold: float = 0.6  # 低于该值写入收集箱待人工确认
    timeout_seconds: int = 60

    @property
    def is_ready(self) -> bool:
        """是否已配置 Key（决定 AI 功能是否可用）。"""
        return bool(self.api_key)


@dataclass(frozen=True)
class AppConfig:
    """应用完整配置。"""

    credential: FeishuCredential
    settings: dict[str, Any]
    ai: AIConfig


def _load_settings() -> dict[str, Any]:
    """读取 settings.yaml，文件缺失时返回空字典（调用方需自行兜底）。"""
    settings_path = CONFIG_DIR / "settings.yaml"
    if not settings_path.exists():
        return {}
    with settings_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data


def load_config() -> AppConfig:
    """加载并组合 .env 与 settings.yaml。"""
    # 确保本地数据目录存在（SQLite 缓存要用）
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    # 加载 .env（若存在）
    env_path = CONFIG_DIR / ".env"
    if env_path.exists():
        load_dotenv(env_path)

    credential = FeishuCredential(
        app_id=os.getenv("FEISHU_APP_ID", "").strip(),
        app_secret=os.getenv("FEISHU_APP_SECRET", "").strip(),
        app_token=os.getenv("FEISHU_APP_TOKEN", "").strip(),
        task_table_id=os.getenv("FEISHU_TASK_TABLE_ID", "").strip(),
        subtask_table_id=os.getenv("FEISHU_SUBTASK_TABLE_ID", "").strip(),
        monitor_log_table_id=os.getenv("FEISHU_MONITOR_LOG_TABLE_ID", "").strip(),
    )
    # AI 配置：Key 从 .env 读，其余参数从 settings.yaml 读（可覆盖默认值）
    settings = _load_settings()
    ai_settings = settings.get("ai", {})
    ai_conf = AIConfig(
        api_key=os.getenv("DEEPSEEK_API_KEY", "").strip(),
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip(),
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat").strip(),
        confidence_threshold=float(ai_settings.get("confidence_threshold", 0.6)),
        timeout_seconds=int(ai_settings.get("timeout_seconds", 60)),
    )
    return AppConfig(credential=credential, settings=settings, ai=ai_conf)
