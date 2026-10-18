"""M3 AI 模块单元测试。

覆盖：
- extract_json：JSON 提取与容错（Markdown 围栏 / 非法输出）
- normalize_task：Schema 校验、取值钳制、置信度策略
- title_only_draft：规则兜底
- parse_task / breakdown_task：失败重试 1 次 + 兜底不崩
- build_create_fields 的 M3 扩展参数与 build_subtask_fields
- 提示词契约（当前时间注入与 JSON 字段定义）
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


class TestNormalizeLevel(unittest.TestCase):
    """M4.5：层级判定与目标级拆解校验。"""

    def test_goal_keeps_tasks(self):
        data = {
            "title": "完成毕业论文初稿",
            "level": "goal",
            "due_at": "2026-12-20",
            "description": "12 月底前完成",
            "key_results": ["完成文献综述", "完成实验", "", "写完初稿"],
            "tasks": [
                {"title": "写文献综述", "priority": "P1", "estimate_min": 600},
                {"title": "跑完实验并整理数据"},
                {"title": ""},  # 空标题被过滤
            ],
            "confidence": 0.9,
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertTrue(draft.is_goal)
        self.assertEqual(len(draft.tasks), 2)
        self.assertEqual(draft.tasks[0].title, "写文献综述")
        self.assertEqual(draft.tasks[0].priority, "P1")
        self.assertEqual(draft.tasks[1].priority, "P2")
        # 关键结果最多 4 条且去掉空项
        self.assertEqual(draft.key_results, ["完成文献综述", "完成实验", "写完初稿"])
        self.assertEqual(draft.description, "12 月底前完成")
        self.assertEqual(draft.level_note, "")

    def test_goal_without_enough_tasks_downgraded(self):
        """目标必须拆解：不足 2 个任务 → 降级为 task 且低置信度待确认。"""
        data = {
            "title": "学完机器学习",
            "level": "goal",
            "due_at": "2027-01-31",
            "tasks": [{"title": "只看一个视频"}],
            "confidence": 0.95,
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.level, "task")
        self.assertEqual(draft.tasks, [])
        self.assertLessEqual(draft.confidence, 0.4)
        self.assertLessEqual(draft.level_confidence, 0.4)
        self.assertTrue(draft.needs_review)
        self.assertEqual(draft.status_hint, STATUS_INBOX)

    def test_unknown_level_falls_back_to_task(self):
        draft = normalize_task({"title": "x", "level": "project"}, 0.6)
        self.assertEqual(draft.level, "task")

    def test_subtask_level_never_breaks_down(self):
        data = {
            "title": "改参考文献格式",
            "level": "subtask",
            "subtasks": ["改正文", "改编号"],
            "tasks": [{"title": "a"}, {"title": "b"}],
        }
        draft = normalize_task(data, 0.6)
        self.assertEqual(draft.level, "subtask")
        self.assertEqual(draft.subtasks, [])
        self.assertEqual(draft.tasks, [])

    def test_subtask_needed_false_clears_subtasks(self):
        """明确不需要拆时清空，避免模型硬凑细碎任务。"""
        data = dict(GOOD_OUTPUT, subtask_needed=False)
        draft = normalize_task(data, 0.6)
        self.assertEqual(draft.subtasks, [])

    def test_subtask_needed_true_keeps_subtasks(self):
        data = dict(GOOD_OUTPUT, subtask_needed=True)
        draft = normalize_task(data, 0.6)
        self.assertEqual(draft.subtasks, GOOD_OUTPUT["subtasks"])

    def test_goal_task_subtasks_clamped(self):
        data = {
            "title": "目标",
            "level": "goal",
            "due_at": "2026-12-20",
            "tasks": [
                {"title": "任务A", "subtasks": [f"步骤{i}" for i in range(10)]},
                {"title": "任务B"},
            ],
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(len(draft.tasks[0].subtasks), 6)

    def test_goal_task_depends_on(self):
        """目标级任务解析 depends_on：合法序号保留，越界/自身/重复被清洗。"""
        data = {
            "title": "备考六级",
            "level": "goal",
            "due_at": "2026-12-20",
            "tasks": [
                {"title": "报名缴费", "depends_on": []},
                {"title": "打印准考证", "depends_on": [0, 0, 1, "x", 9, -1]},
                {"title": "参加考试", "depends_on": [1]},
            ],
            "confidence": 0.9,
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.tasks[0].depends_on, [])
        # 重复的 0 去重、自身序号 1 去掉、非数字/越界/负数全部丢弃
        self.assertEqual(draft.tasks[1].depends_on, [0])
        self.assertEqual(draft.tasks[2].depends_on, [1])

    def test_goal_task_depends_on_non_list(self):
        """depends_on 不是数组时按空处理，不影响解析。"""
        data = {
            "title": "准备毕设",
            "level": "goal",
            "due_at": "2027-03-20",
            "tasks": [
                {"title": "开题", "depends_on": "0"},
                {"title": "写初稿"},
            ],
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.tasks[0].depends_on, [])
        self.assertEqual(draft.tasks[1].depends_on, [])

    def test_goal_without_due_downgraded(self):
        """没有截止时间就无法证明是长期投入，按 task 处理并提示。"""
        data = {
            "title": "学完机器学习",
            "level": "goal",
            "tasks": [{"title": "a"}, {"title": "b"}],
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.level, "task")
        self.assertEqual(draft.tasks, [])
        self.assertIn("截止时间", draft.level_note)
        self.assertTrue(draft.level_uncertain)

    def test_near_due_goal_downgraded(self):
        """距截止不足 30 天的不算目标。"""
        data = {
            "title": "复习期末考试",
            "level": "goal",
            "due_at": "2026-10-20",
            "tasks": [{"title": "a"}, {"title": "b"}],
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.level, "task")
        self.assertIn("30 天", draft.level_note)

    def test_far_one_shot_event_stays_task(self):
        """时间远但一次性能办完的事（开会/体检/报到）不能算目标。"""
        data = {
            "title": "参加学院年度学术会议",
            "level": "goal",
            "due_at": "2027-03-01",
            "tasks": [{"title": "a"}, {"title": "b"}],
        }
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.level, "task")
        self.assertIn("一次性事件", draft.level_note)

    def test_far_meeting_without_prep_is_plain_task(self):
        """模型判 task 的远期会议也不会被升级规则误伤。"""
        data = {"title": "参加例会", "level": "task", "due_at": "2027-03-01"}
        draft = normalize_task(data, 0.6, NOW)
        self.assertEqual(draft.level, "task")

    def test_advice_passed_through(self):
        data = dict(GOOD_OUTPUT, advice="先按薄弱项分配精力")
        draft = normalize_task(data, 0.6)
        self.assertEqual(draft.advice, "先按薄弱项分配精力")

    def test_subtasks_and_checklist_deduped(self):
        """同一件事不要同时出现在两处：以 subtasks 为准。"""
        data = dict(
            GOOD_OUTPUT,
            subtask_needed=True,
            subtasks=["复习第三章", "完成习题"],
            checklist=["复习第三章", "带学生证"],
        )
        draft = normalize_task(data, 0.6)
        self.assertEqual(draft.checklist, ["带学生证"])


class TestParseLevelRetry(unittest.TestCase):
    """层级纠错：模型判低时追加一次按目标拆解的追问。"""

    FIRST_TASK = {
        "title": "准备六级笔试",
        "level": "task",
        "task_type": "考试",
        "due_at": "2026-12-12T15:00:00+08:00",
        "confidence": 0.8,
    }
    GOAL = {
        "title": "准备六级笔试",
        "level": "goal",
        "task_type": "考试",
        "due_at": "2026-12-12T15:00:00+08:00",
        "tasks": [{"title": "背单词"}, {"title": "刷真题"}],
        "advice": "先做一套真题摸底，再按失分项分配时间",
        "confidence": 0.8,
    }

    def test_task_upgraded_after_goal_retry(self):
        provider = FakeProvider([dict(self.FIRST_TASK), dict(self.GOAL)])
        draft, usage = parse_task(
            provider, "六级笔试 12-12 15:00-17:25", now=NOW
        )
        self.assertEqual(provider.calls, 2)
        self.assertTrue(draft.is_goal)
        self.assertEqual(len(draft.tasks), 2)
        self.assertIn("摸底", draft.advice)
        self.assertEqual(usage.total_tokens, 15)

    def test_one_shot_event_not_retried(self):
        """远期但一次性能办完的会议不追问，避免误升格。"""
        provider = FakeProvider(
            [
                {
                    "title": "参加学术会议",
                    "level": "task",
                    "due_at": "2026-12-12T09:00:00+08:00",
                    "confidence": 0.8,
                }
            ]
        )
        draft, _ = parse_task(provider, "12-12 参加学术会议", now=NOW)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(draft.level, "task")

    def test_keeps_task_when_model_insists(self):
        provider = FakeProvider([dict(self.FIRST_TASK), dict(self.FIRST_TASK)])
        draft, _ = parse_task(provider, "六级笔试 12-12", now=NOW)
        self.assertEqual(provider.calls, 2)
        self.assertEqual(draft.level, "task")

    def test_near_due_task_not_retried(self):
        provider = FakeProvider(
            [
                {
                    "title": "复习期末考试",
                    "level": "task",
                    "due_at": "2026-10-20T09:00:00+08:00",
                    "confidence": 0.8,
                }
            ]
        )
        draft, _ = parse_task(provider, "10-20 复习期末考试", now=NOW)
        self.assertEqual(provider.calls, 1)
        self.assertEqual(draft.level, "task")


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
            ai_advice="先按薄弱项分配精力",
        )
        self.assertEqual(fields[TaskFields.STATUS], STATUS_INBOX)
        self.assertEqual(fields[TaskFields.ENERGY], "中")
        self.assertEqual(fields[TaskFields.TAGS], ["数学"])
        self.assertEqual(fields[TaskFields.AI_BREAKDOWN], "1. 复习\n2. 做题")
        self.assertEqual(fields[TaskFields.CHECKLIST], "核对答案")
        self.assertEqual(fields[TaskFields.AI_ADVICE], "先按薄弱项分配精力")

    def test_create_fields_without_advice_omits_key(self):
        """没有建议时不写该列，避免把已有内容清空。"""
        due = datetime(2026, 10, 8, 18, 0, 0)
        fields = build_create_fields(
            title="x", due_at=due, priority="P1", task_type="其他", external_id="e"
        )
        self.assertNotIn(TaskFields.AI_ADVICE, fields)

    def test_edit_fields_supports_advice(self):
        from app.feishu.repository import build_edit_fields

        fields = build_edit_fields({"ai_advice": "先摸底再分配时间"})
        self.assertEqual(fields[TaskFields.AI_ADVICE], "先摸底再分配时间")

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
    """提示词契约。"""

    def test_parse_prompt_has_current_time_and_contract(self):
        prompt = build_parse_user_prompt("明天交作业", NOW)
        self.assertIn("2026-10-07", prompt)
        self.assertIn("confidence", prompt)

    def test_breakdown_prompt_has_task_title(self):
        prompt = build_breakdown_user_prompt("写论文", "", NOW)
        self.assertIn("写论文", prompt)


if __name__ == "__main__":
    unittest.main()
