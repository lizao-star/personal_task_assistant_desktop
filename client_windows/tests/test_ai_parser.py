"""M3 AI 模块单元测试。

覆盖：
- extract_json：JSON 提取与容错（Markdown 围栏 / 非法输出）
- normalize_task：Schema 校验、取值钳制、置信度策略
- title_only_draft：规则兜底
- parse_task / breakdown_task：失败重试 1 次 + 兜底不崩
- build_create_fields 的 M3 扩展参数与 build_subtask_fields
- 提示词的学术诚信红线
"""

from __future__ import annotations

import unittest
from datetime import datetime

from app.ai.parser import (
    CONFIDENCE_THRESHOLD_DEFAULT,
    TaskDraft,
    breakdown_task,
    extract_json,
    normalize_task,
    parse_task,
    title_only_draft,
)
from app.ai.prompts import (
    INTEGRITY_RULE,
    SYSTEM_PROMPT,
    build_breakdown_user_prompt,
    build_parse_user_prompt,
)
from app.ai.provider import LLMProvider, LLMProviderError, TokenUsage
from app.constants import STATUS_INBOX, TaskFields
from app.feishu.repository import build_create_fields, build_subtask_fields

NOW = datetime(2026, 10, 7, 14, 0, 0)


class FakeProvider(LLMProvider):
    """假供应商：按预设脚本依次返回 dict 或抛异常，不联网。"""

    def __init__(self, script):
        # script: list[dict | Exception]，逐次消费
        self._script = list(script)
        self.calls = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0

    def chat_json(self, system: str, user: str):
        self.calls += 1
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, TokenUsage(prompt_tokens=10, completion_tokens=5)


GOOD_OUTPUT = {
    "title": "交高数作业 第三章习题1-10",
    "priority": "P1",
    "task_type": "作业",
    "due_at": "2026-10-08T18:00:00+08:00",
    "estimate_min": 90,
    "energy": "中",
    "tags": ["数学"],
    "subtasks": ["复习第三章", "完成习题1-5", "完成习题6-10"],
    "checklist": ["核对答案", "检查姓名学号"],
    "confidence": 0.85,
}


class TestExtractJson(unittest.TestCase):
    """JSON 提取与容错。"""

    def test_plain_json(self):
        obj = extract_json('{"title": "x"}')
        self.assertEqual(obj, {"title": "x"})

    def test_markdown_fenced(self):
        obj = extract_json('```json\n{"title": "x"}\n```')
        self.assertEqual(obj, {"title": "x"})

    def test_invalid_raises(self):
        with self.assertRaises(ValueError):
            extract_json("这不是 JSON")

    def test_array_raises(self):
        # 顶层不是对象
        with self.assertRaises(ValueError):
            extract_json('[1, 2]')


class TestNormalizeTask(unittest.TestCase):
    """Schema 校验与归一化。"""

    def test_full_valid(self):
        draft = normalize_task(dict(GOOD_OUTPUT), 0.6)
        self.assertEqual(draft.title, "交高数作业 第三章习题1-10")
        self.assertEqual(draft.priority, "P1")
        self.assertEqual(draft.task_type, "作业")
        self.assertEqual(draft.due_at, datetime(2026, 10, 8, 18, 0, 0))
        self.assertEqual(draft.estimate_min, 90)
        self.assertEqual(draft.energy, "中")
        self.assertEqual(draft.subtasks, GOOD_OUTPUT["subtasks"])
        self.assertEqual(draft.checklist, GOOD_OUTPUT["checklist"])
        self.assertEqual(draft.confidence, 0.85)
        self.assertFalse(draft.needs_review)
        self.assertEqual(draft.status_hint, "待办")

    def test_low_confidence_goes_inbox(self):
        data = dict(GOOD_OUTPUT, confidence=0.3, priority="P0")
        draft = normalize_task(data, CONFIDENCE_THRESHOLD_DEFAULT)
        self.assertTrue(draft.needs_review)
        self.assertEqual(draft.status_hint, STATUS_INBOX)
        # 不自动定优先级：即使 AI 给了 P0，也回落保守 P2
        self.assertEqual(draft.priority, "P2")

    def test_invalid_values_clamped(self):
        data = {
            "title": "x",
            "priority": "P9",       # 非法 → P2
            "task_type": "旅行",     # 非法 → 其他
            "energy": "超强",        # 非法 → ""
            "estimate_min": "abc",  # 非法 → 0
            "confidence": 5,        # 越界 → 1.0
            "due_at": "2026/10/08",  # 解析不了 → None
            "subtasks": "不是数组",   # 非数组 → []
            "tags": ["", "  ", "论文"],  # 空项被清掉
        }
        draft = normalize_task(data, 0.6)
        self.assertEqual(draft.priority, "P2")
        self.assertEqual(draft.task_type, "其他")
        self.assertEqual(draft.energy, "")
        self.assertEqual(draft.estimate_min, 0)
        self.assertEqual(draft.confidence, 1.0)
        self.assertIsNone(draft.due_at)
        self.assertEqual(draft.subtasks, [])
        self.assertEqual(draft.tags, ["论文"])

    def test_missing_title_raises(self):
        with self.assertRaises(ValueError):
            normalize_task({"confidence": 0.9}, 0.6)

    def test_date_only_due(self):
        draft = normalize_task(dict(GOOD_OUTPUT, due_at="2026-10-10"), 0.6)
        self.assertEqual(draft.due_at, datetime(2026, 10, 10, 0, 0, 0))

    def test_missing_optional_fields(self):
        draft = normalize_task({"title": "只写了标题"}, 0.6)
        self.assertEqual(draft.title, "只写了标题")
        self.assertIsNone(draft.due_at)
        self.assertEqual(draft.priority, "P2")


class TestTitleOnlyDraft(unittest.TestCase):
    """规则兜底。"""

    def test_keeps_text_as_title(self):
        draft = title_only_draft("下周三组会汇报文献阅读进展")
        self.assertEqual(draft.title, "下周三组会汇报文献阅读进展")
        self.assertTrue(draft.is_fallback)
        self.assertTrue(draft.needs_review)
        self.assertEqual(draft.confidence, 0.0)

    def test_long_input_truncated(self):
        draft = title_only_draft("啊" * 100)
        self.assertLessEqual(len(draft.title), 60)


class TestParseTask(unittest.TestCase):
    """解析编排：重试 1 次 + 兜底不崩。"""

    def test_success_no_retry(self):
        provider = FakeProvider([dict(GOOD_OUTPUT)])
        draft, usage = parse_task(provider, "明天下午交高数作业")
        self.assertEqual(provider.calls, 1)
        self.assertFalse(draft.is_fallback)
        self.assertEqual(usage.total_tokens, 15)

    def test_retry_once_on_bad_output(self):
        # 第一次输出非法（缺 title 会抛 ValueError），第二次成功
        provider = FakeProvider([{"foo": 1}, dict(GOOD_OUTPUT)])
        draft, _ = parse_task(provider, "明天下午交高数作业")
        self.assertEqual(provider.calls, 2)
        self.assertEqual(draft.title, GOOD_OUTPUT["title"])

    def test_fallback_on_repeated_error(self):
        # 断网/Key 错误：两次都失败 → 退化为只取标题，程序不崩
        provider = FakeProvider([LLMProviderError("网络错误"), LLMProviderError("网络错误")])
        draft, usage = parse_task(provider, "明天下午交高数作业")
        self.assertEqual(provider.calls, 2)
        self.assertTrue(draft.is_fallback)
        self.assertIn("高数作业", draft.title)
        self.assertIsNone(usage)

    def test_fallback_on_invalid_json_twice(self):
        provider = FakeProvider([{"foo": 1}, {"foo": 1}])
        draft, _ = parse_task(provider, "那个东西")
        self.assertTrue(draft.is_fallback)
        self.assertEqual(draft.title, "那个东西")


class TestBreakdownTask(unittest.TestCase):
    """拆解建议编排。"""

    def test_success(self):
        output = {
            "subtasks": ["检索文献", "精读并做笔记"],
            "checklist": ["记录3个创新点"],
            "advice": "先从综述类文献入手",
            "confidence": 0.8,
        }
        provider = FakeProvider([output])
        result, usage = breakdown_task(provider, "写文献综述")
        self.assertEqual(result.subtasks, output["subtasks"])
        self.assertEqual(result.advice, "先从综述类文献入手")
        self.assertEqual(usage.total_tokens, 15)

    def test_raises_after_retry(self):
        provider = FakeProvider([LLMProviderError("x"), LLMProviderError("x")])
        with self.assertRaises(LLMProviderError):
            breakdown_task(provider, "写文献综述")

    def test_invalid_values_clamped(self):
        provider = FakeProvider([{"subtasks": "不是数组", "confidence": "abc"}])
        result, _ = breakdown_task(provider, "写文献综述")
        self.assertEqual(result.subtasks, [])
        self.assertEqual(result.confidence, 0.0)


class TestBuildFieldsM3(unittest.TestCase):
    """M3 扩展的写表字段构建。"""

    def test_create_fields_with_ai_options(self):
        due = datetime(2026, 10, 8, 18, 0, 0)
        fields = build_create_fields(
            title="交作业",
            due_at=due,
            priority="P2",
            task_type="作业",
            external_id="win-xxx",
            status=STATUS_INBOX,
            energy="中",
            tags=["数学"],
            ai_breakdown="1. 复习\n2. 做题",
            checklist="核对答案",
        )
        self.assertEqual(fields[TaskFields.STATUS], STATUS_INBOX)
        self.assertEqual(fields[TaskFields.ENERGY], "中")
        self.assertEqual(fields[TaskFields.TAGS], ["数学"])
        self.assertEqual(fields[TaskFields.AI_BREAKDOWN], "1. 复习\n2. 做题")
        self.assertEqual(fields[TaskFields.CHECKLIST], "核对答案")

    def test_create_fields_default_status_unchanged(self):
        due = datetime(2026, 10, 8, 18, 0, 0)
        fields = build_create_fields(
            title="x", due_at=due, priority="P1", task_type="其他", external_id="e"
        )
        self.assertEqual(fields[TaskFields.STATUS], "待办")
        self.assertNotIn(TaskFields.ENERGY, fields)

    def test_subtask_fields(self):
        fields_list = build_subtask_fields("recParent", ["第一步", "", "第三步"])
        # 空标题被过滤
        self.assertEqual(len(fields_list), 2)
        self.assertEqual(fields_list[0]["所属任务"], ["recParent"])
        self.assertEqual(fields_list[0]["顺序"], 1)
        self.assertEqual(fields_list[1]["顺序"], 2)
        self.assertTrue(all(f["状态"] == "待办" for f in fields_list))


class TestPrompts(unittest.TestCase):
    """提示词红线与契约。"""

    def test_integrity_in_system_prompt(self):
        self.assertIn(INTEGRITY_RULE, SYSTEM_PROMPT)

    def test_parse_prompt_has_current_time_and_contract(self):
        prompt = build_parse_user_prompt("明天交作业", NOW)
        self.assertIn("2026-10-07", prompt)
        self.assertIn("confidence", prompt)
        self.assertIn(INTEGRITY_RULE, prompt)

    def test_breakdown_prompt_has_integrity(self):
        prompt = build_breakdown_user_prompt("写论文", "", NOW)
        self.assertIn("写论文", prompt)
        self.assertIn(INTEGRITY_RULE, prompt)


if __name__ == "__main__":
    unittest.main()
