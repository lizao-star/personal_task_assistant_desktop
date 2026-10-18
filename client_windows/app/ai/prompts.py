"""AI 提示词模板（M3 / M4.5）。

要求：统一 JSON 输出契约（对齐 docs/设计总纲.md 5.4），字段缺失时留空不瞎猜。

M4.5 起增加「层级判定」：
- level=goal（目标/项目，1 个月以上）必须向下拆解 2~5 个任务；
- level=task（任务，1 天~1 个月）按需拆解子任务，宁缺勿碎。
"""

from __future__ import annotations

from datetime import datetime

SYSTEM_PROMPT = (
    "你是研究生个人任务助理，负责把自然语言转成结构化任务数据。\n"
    "你必须且只能输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。\n"
    "信息不足的字段填 null，不要编造。"
)

# 解析任务的 JSON 输出契约
_PARSE_CONTRACT = """输出 JSON 字段定义：
{
  "level": "goal|task|subtask"，先按时间尺度判定层级：需要持续 1 个月以上推进的长期目标/项目填 goal；1 天到 1 个月能完成的具体任务填 task；1 天内能完成的小步骤填 subtask；拿不准一律填 task,
  "title": "一句话任务名/目标名（必填）",
  "priority": "P0|P1|P2|P3，P0=当天/紧急，P1=本周内，P2=本月内，P3=更远；不确定填 null",
  "task_type": "课程|科研|作业|考试|组会|生活|其他",
  "due_at": "截止/结束时间，ISO 8601 格式如 2026-10-10T23:59:00+08:00；没提到填 null，绝不瞎猜",
  "estimate_min": "预计耗时（分钟数字），不确定填 null",
  "energy": "高|中|低，按任务难度判断；不确定填 null",
  "tags": ["标签1", "标签2"]，可空数组,
  "subtask_needed": true或false，先判断该任务是否有必要拆解子任务：一次动作即可完成（如取快递、回邮件、交表）则为false，多步骤任务为true（level=goal 时忽略本字段）,
  "subtasks": ["子任务1", "子任务2"]，按需拆解，宁缺勿碎：subtask_needed为false时必须为空数组，绝不为凑数硬拆；为true时给 2-6 个具体可执行动作，按执行顺序（level=goal 时留空数组）,
  "tasks": [{"title": "任务名", "due_at": "截止时间或null", "priority": "P0|P1|P2|P3", "estimate_min": 90, "subtask_needed": false, "subtasks": []}],
  "key_results": ["关键结果1", "关键结果2"],
  "description": "补充描述；没有则填空串",
  "checklist": ["完成前要确认的事项1", "…"]，可空数组,
  "confidence": 0.0到1.0之间的小数，表示你对以上解析的把握程度
}
层级与拆解规则（必须遵守）：
1. level=goal 时：tasks 必填，给 2~5 个能推进该目标的 L2 任务（按执行顺序，每项是一整天到数周可完成的具体任务）；key_results 给 2~4 条可验收的关键结果；subtasks 留空数组。
2. level=task 时：tasks 必须为空数组；再按 subtask_needed 判断是否拆分子任务，宁可少拆也绝不为凑数硬拆成细碎动作。
3. level=subtask 时：tasks 与 subtasks 都必须为空数组。
置信度自评标准：描述清晰且关键信息齐全 > 0.7；部分信息需推断 0.4~0.7；描述模糊或几乎无法理解 < 0.4（此时 title 尽量保留原话）。"""



def build_parse_user_prompt(text: str, now: datetime) -> str:
    """构造「一句话 → 结构化任务」的用户消息。

    附带当前时间，让「明天下午」「下周一」这类相对时间可以正确换算。
    """
    weekday = "一二三四五六日"[now.weekday()]
    return (
        f"当前时间：{now.strftime('%Y-%m-%d %H:%M')}（星期{weekday}，时区 UTC+8）\n"
        f"请把下面这句话解析为结构化任务。\n\n用户输入：{text}\n\n{_PARSE_CONTRACT}"
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
        '  "subtasks": ["具体可执行的步骤1", "步骤2", …]，按需拆解，宁缺勿碎：'
        "任务本身一次就能完成或步骤高度单一时输出空数组，绝不为凑数硬拆；"
        "确需拆解时给 2-8 步，按执行顺序，每步都是可独立完成验证的动作,\n"
        '  "checklist": ["完成前要确认的事项", …]，可空数组,\n'
        '  "advice": "下一步怎么做的一段简短建议（50 字以内）",\n'
        '  "confidence": 0.0到1.0之间的小数\n'
        "}\n"
    )
