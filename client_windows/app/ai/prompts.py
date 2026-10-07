"""AI 提示词模板（M3）。

两条硬性要求：
1. 学术诚信红线写死在系统提示词里：只做拆解/提纲/检查清单，绝不代写；
2. 统一 JSON 输出契约（对齐 docs/设计总纲.md 5.4），字段缺失时留空不瞎猜。
"""

from __future__ import annotations

from datetime import datetime

# 学术诚信红线（必须出现在所有提示词中，不可删除）
INTEGRITY_RULE = (
    "学术诚信红线（最高优先级，任何情况下不可违反）："
    "你绝不代写作业、论文、代码、考试答案等可直接提交的成品；"
    "你只输出任务拆解、步骤提纲、检查清单和自测问题。"
)

SYSTEM_PROMPT = (
    "你是研究生个人任务助理，负责把自然语言转成结构化任务数据。\n"
    f"{INTEGRITY_RULE}\n"
    "你必须且只能输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。\n"
    "信息不足的字段填 null，不要编造。"
)

# 解析任务的 JSON 输出契约
_PARSE_CONTRACT = """输出 JSON 字段定义：
{
  "title": "一句话任务名（必填）",
  "priority": "P0|P1|P2|P3，P0=当天/紧急，P1=本周内，P2=本月内，P3=更远；不确定填 null",
  "task_type": "课程|科研|作业|考试|组会|生活|其他",
  "due_at": "截止时间，ISO 8601 格式如 2026-10-10T23:59:00+08:00；没提到填 null，绝不瞎猜",
  "estimate_min": "预计耗时（分钟数字），不确定填 null",
  "energy": "高|中|低，按任务难度判断；不确定填 null",
  "tags": ["标签1", "标签2"]，可空数组，
  "subtasks": ["子任务1", "子任务2"]，3-5 个具体可执行动作，按顺序；简单任务可空数组,
  "checklist": ["完成前要确认的事项1", "…"]，可空数组,
  "confidence": 0.0到1.0之间的小数，表示你对以上解析的把握程度
}
置信度自评标准：描述清晰且关键信息齐全 > 0.7；部分信息需推断 0.4~0.7；描述模糊或几乎无法理解 < 0.4（此时 title 尽量保留原话）。"""


def build_parse_user_prompt(text: str, now: datetime) -> str:
    """构造「一句话 → 结构化任务」的用户消息。

    附带当前时间，让「明天下午」「下周一」这类相对时间可以正确换算；
    再次重复学术诚信红线（红线要求写入所有 AI 提示词，双重加固）。
    """
    weekday = "一二三四五六日"[now.weekday()]
    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（星期{weekday}，时区 UTC+8）\n"
        f"请把下面这句话解析为结构化任务。\n\n用户输入：{text}\n\n{_PARSE_CONTRACT}\n\n"
        f"{INTEGRITY_RULE}"
    )


def build_breakdown_user_prompt(title: str, description: str, now: datetime) -> str:
    """构造「已有任务 → 拆解建议」的用户消息。"""
    desc = description.strip() or "（无补充描述）"
    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（时区 UTC+8）\n"
        "请为下面这个已有任务生成拆解建议（只是建议，我不会自动改状态）。\n\n"
        f"任务名：{title}\n"
        f"补充描述：{desc}\n\n"
        "输出 JSON 字段定义：\n"
        "{\n"
        '  "subtasks": ["具体可执行的步骤1", "步骤2", …]，3-8 步，按执行顺序，'
        "每步都是可独立完成验证的动作,\n"
        '  "checklist": ["完成前要确认的事项", …]，可空数组,\n'
        '  "advice": "下一步怎么做的一段简短建议（50 字以内）",\n'
        '  "confidence": 0.0到1.0之间的小数\n'
        "}\n"
        f"{INTEGRITY_RULE}"
    )
