"""LLM 供应商抽象与 DeepSeek 实现（M3）。

设计要点：
- 统一接口 chat_json(system, user)：要求模型输出单个 JSON 对象；
- DeepSeek 走 OpenAI 兼容的 /chat/completions，开启 json_object 模式；
- 本层不做重试（由 parser 层统一编排），只负责「调一次、拿结果、记 token」；
- 超时与非 200 都抛 LLMProviderError，调用方据此走兜底逻辑；
- Key 只来自本地配置（config/.env），绝不入库、不打日志。
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


class LLMProviderError(Exception):
    """LLM 调用失败（网络/HTTP/响应解析）。"""


class ProviderNotConfigured(LLMProviderError):
    """API Key 未配置。"""


@dataclass(frozen=True)
class TokenUsage:
    """单次调用的 token 用量（用于成本核算）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class LLMProvider(ABC):
    """大模型供应商接口；通义/智谱等可按此接口扩展。"""

    @abstractmethod
    def chat_json(self, system: str, user: str) -> tuple[dict, TokenUsage]:
        """发起一次对话并返回 (JSON 对象, token 用量)。

        实现必须保证返回值是 dict（模型输出应为单个 JSON 对象）。
        """


class DeepSeekProvider(LLMProvider):
    """DeepSeek Chat（OpenAI 兼容协议）实现。"""

    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_MODEL = "deepseek-chat"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout: int = 60,
    ):
        if not api_key:
            raise ProviderNotConfigured("DEEPSEEK_API_KEY 未配置")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout
        # 会话级累计用量（成本核算用）
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat_json(self, system: str, user: str) -> tuple[dict, TokenUsage]:
        """调用 DeepSeek 并解析 JSON 输出。"""
        url = f"{self._base_url}/chat/completions"
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            # 温度调低：解析/拆解要的是稳定结构，不要发散
            "temperature": 0.2,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(
                url, json=payload, headers=headers, timeout=self._timeout
            )
        except requests.RequestException as e:
            raise LLMProviderError(f"DeepSeek 请求失败：{e}") from e

        if resp.status_code != 200:
            snippet = resp.text[:200]
            raise LLMProviderError(
                f"DeepSeek 返回 HTTP {resp.status_code}：{snippet}"
            )

        try:
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage_raw = data.get("usage", {})
        except (KeyError, ValueError, IndexError) as e:
            raise LLMProviderError(f"DeepSeek 响应结构异常：{e}") from e

        usage = TokenUsage(
            prompt_tokens=int(usage_raw.get("prompt_tokens", 0) or 0),
            completion_tokens=int(usage_raw.get("completion_tokens", 0) or 0),
        )
        self.total_prompt_tokens += usage.prompt_tokens
        self.total_completion_tokens += usage.completion_tokens
        logger.info(
            "AI token 用量：输入 %d + 输出 %d = %d（累计 %d）",
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            self.total_prompt_tokens + self.total_completion_tokens,
        )

        try:
            obj = json.loads(content)
        except ValueError as e:
            raise LLMProviderError(f"DeepSeek 输出不是合法 JSON：{e}") from e
        if not isinstance(obj, dict):
            raise LLMProviderError("DeepSeek 输出的 JSON 不是对象")
        return obj, usage
