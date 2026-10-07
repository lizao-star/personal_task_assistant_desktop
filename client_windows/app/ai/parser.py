"""AI 输出解析与校验（M3）。

职责（对齐路线图 M3 第 3、4 条）：
1. JSON 解析：容错处理模型输出（剥 Markdown 围栏等）；
2. Schema 校验与归一化：字段类型/取值范围钳制，非法值回落默认；
3. 失败重试 1 次；仍失败退化为「只取标题」的规则兜底，程序不崩；
4. 置信度策略：confidence < 阈值(默认 0.6) → 写入「收集箱」待人工确认，
   不自动定优先级（用最保守的 P2 占位）。

本模块是纯逻辑（除调用 provider 外无 IO），配单元测试。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime

from ..constants import (
    ENERGY_HIGH,
    ENERGY_LOW,
    ENERGY_MEDIUM,
    PRIORITY_OPTIONS,
    STATUS_INBOX,
    STATUS_TODO,
    TASK_TYPE_OPTIONS,
)
from .prompts import SYSTEM_PROMPT, build_breakdown_user_prompt, build_parse_user_prompt
from .provider import LLMProvider, LLMProviderError, TokenUsage

logger = logging.getLogger(__name__)

# 置信度阈值：低于该值写入收集箱待确认（可在 settings.yaml ai.confidence_threshold 调整）
CONFIDENCE_THRESHOLD_DEFAULT = 0.6

_VALID_ENERGY = {ENERGY_HIGH, ENERGY_MEDIUM, ENERGY_LOW}


# ----------------------------------------------------------------------
# 领域对象
# ----------------------------------------------------------------------
@dataclass
class TaskDraft:
    """AI 解析出的任务草稿（尚未写表）。"""

    title: str
    due_at: datetime | None = None
    priority: str = "P2"
    task_type: str = "其他"
    energy: str = ""
    estimate_min: int = 0
    tags: list[str] = field(default_factory=list)
    subtasks: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    confidence: float = 0.0
    # 置信度不足时的标记：调用方据此写入「收集箱」
    status_hint: str = STATUS_INBOX
    needs_review: bool = False
    # 是否走了「只取标题」规则兜底（用于 UI 提示）
    is_fallback: bool = False


@dataclass
class BreakdownResult:
    """AI 对已有任务的拆解建议（只建议，采纳与否由用户决定）。"""

    subtasks: list[str] = field(default_factory=list)
    checklist: list[str] = field(default_factory=list)
    advice: str = ""
    confidence: float = 0.0


# ----------------------------------------------------------------------
# JSON 提取与字段归一化
# ----------------------------------------------------------------------
def extract_json(text: str) -> dict:
    """从模型输出中提取 JSON 对象。

    json_object 模式下输出应为纯 JSON，但为稳妥起见兼容剥离 ```json 围栏。
    """
    if not text:
        raise ValueError("空输出")
    cleaned = text.strip()
    # 剥离 Markdown 代码围栏
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", cleaned, re.DOTALL)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        obj = json.loads(cleaned)
    except ValueError as e:
        raise ValueError(f"不是合法 JSON：{e}") from e
    if not isinstance(obj, dict):
        raise ValueError("JSON 不是对象")
    return obj


def _parse_due(value) -> datetime | None:
    """解析 due_at：支持 ISO 8601 / 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DD'。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    # ISO 8601（带时区）：转成本地 naive 时间，与全客户端保持一致
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    logger.warning("无法解析 AI 给出的截止时间 %r，已忽略", value)
    return None


def _clean_str_list(value) -> list[str]:
    """把模型给的数组归一化为非空字符串数组。"""
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]


def normalize_task(data: dict, threshold: float) -> TaskDraft:
    """把 AI 返回的 dict 校验并归一化为 TaskDraft。

    非法值一律回落保守默认，不抛异常（解析容错）。
    """
    title = str(data.get("title") or "").strip()
    if not title:
        raise ValueError("缺少 title")

    priority = str(data.get("priority") or "").strip().upper()
    if priority not in PRIORITY_OPTIONS:
        priority = "P2"  # 不自动猜优先级时的保守占位

    task_type = str(data.get("task_type") or "").strip()
    if task_type not in TASK_TYPE_OPTIONS:
        task_type = "其他"

    energy = str(data.get("energy") or "").strip()
    if energy not in _VALID_ENERGY:
        energy = ""

    try:
        estimate = int(float(data.get("estimate_min") or 0))
    except (TypeError, ValueError):
        estimate = 0
    estimate = max(0, min(estimate, 24 * 60))

    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(confidence, 1.0))

    # 置信度策略：低置信 → 收集箱 + 待人工确认，不自动定优先级（保守 P2 占位）
    needs_review = confidence < threshold
    if needs_review:
        priority = "P2"

    return TaskDraft(
        title=title,
        due_at=_parse_due(data.get("due_at")),
        priority=priority,
        task_type=task_type,
        energy=energy,
        estimate_min=estimate,
        tags=_clean_str_list(data.get("tags")),
        subtasks=_clean_str_list(data.get("subtasks")),
        checklist=_clean_str_list(data.get("checklist")),
        confidence=confidence,
        status_hint=STATUS_INBOX if needs_review else STATUS_TODO,
        needs_review=needs_review,
    )


def title_only_draft(text: str) -> TaskDraft:
    """规则兜底：AI 不可用时只取标题，不猜任何字段。"""
    title = text.strip()
    # 截掉过长输入，避免把整段话塞进任务名
    if len(title) > 60:
        title = title[:60]
    return TaskDraft(
        title=title or "(未命名任务)",
        priority="P2",
        task_type="其他",
        confidence=0.0,
        status_hint=STATUS_INBOX,
        needs_review=True,
        is_fallback=True,
    )


def _normalize_breakdown(data: dict) -> BreakdownResult:
    """校验并归一化拆解建议输出。"""
    try:
        confidence = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return BreakdownResult(
        subtasks=_clean_str_list(data.get("subtasks")),
        checklist=_clean_str_list(data.get("checklist")),
        advice=str(data.get("advice") or "").strip(),
        confidence=max(0.0, min(confidence, 1.0)),
    )


# ----------------------------------------------------------------------
# 编排：调用 + 重试 1 次 + 兜底
# ----------------------------------------------------------------------
def parse_task(
    provider: LLMProvider,
    text: str,
    now: datetime | None = None,
    threshold: float = CONFIDENCE_THRESHOLD_DEFAULT,
) -> tuple[TaskDraft, TokenUsage | None]:
    """一句话 → TaskDraft。

    失败重试 1 次（网络错误与 Schema 校验失败都算失败）；
    仍失败退化为「只取标题」兜底（不抛异常，保证 UI 可用）。
    返回 (草稿, token 用量)；兜底路径用量为 None。
    """
    now = now or datetime.now()
    user = build_parse_user_prompt(text, now)

    for attempt in (1, 2):
        try:
            obj, usage = provider.chat_json(SYSTEM_PROMPT, user)
            draft = normalize_task(obj, threshold)
            return draft, (usage if attempt == 1 else None)
        except (LLMProviderError, ValueError) as e:
            logger.warning("AI 解析第 %d 次失败: %s", attempt, e)

    # 断网 / Key 错误 / 输出非法：退化为只取标题，程序不崩
    logger.warning("AI 解析最终失败，走「只取标题」兜底")
    return title_only_draft(text), None


def breakdown_task(
    provider: LLMProvider,
    title: str,
    description: str = "",
    now: datetime | None = None,
) -> tuple[BreakdownResult, TokenUsage | None]:
    """已有任务 → 拆解建议（子任务 + 检查清单 + 下一步建议）。

    失败重试 1 次；仍失败抛 LLMProviderError（拆解没有规则兜底，
    由 UI 层捕获并提示用户）。
    """
    now = now or datetime.now()
    user = build_breakdown_user_prompt(title, description, now)
    usage: TokenUsage | None = None
    try:
        obj, usage = provider.chat_json(SYSTEM_PROMPT, user)
    except LLMProviderError:
        logger.warning("AI 拆解失败，重试 1 次", exc_info=True)
        obj, usage = provider.chat_json(SYSTEM_PROMPT, user)
    return _normalize_breakdown(obj), usage
