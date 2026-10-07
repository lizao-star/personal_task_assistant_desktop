"""AI 编排层（M3）：DeepSeek 解析与拆解。

结构：
- provider.py：LLMProvider 接口 + DeepSeekProvider 实现（通义/智谱预留）
- prompts.py：解析/拆解提示词（硬编码学术诚信边界）
- parser.py：JSON 契约解析 + Schema 校验 + 失败重试 1 次 + 兜底 + 置信度策略

约束：只在「新建 / 手动拆解 / 手动请求建议」时调用，不做定时轮询（成本控制）。
"""

from .parser import (
    CONFIDENCE_THRESHOLD_DEFAULT,
    BreakdownResult,
    TaskDraft,
    breakdown_task,
    parse_task,
)
from .provider import (
    DeepSeekProvider,
    LLMProvider,
    LLMProviderError,
    ProviderNotConfigured,
    TokenUsage,
)

__all__ = [
    "CONFIDENCE_THRESHOLD_DEFAULT",
    "BreakdownResult",
    "TaskDraft",
    "breakdown_task",
    "parse_task",
    "DeepSeekProvider",
    "LLMProvider",
    "LLMProviderError",
    "ProviderNotConfigured",
    "TokenUsage",
]
