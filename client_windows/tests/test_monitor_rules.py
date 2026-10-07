"""娱乐监督纯函数模块单元测试（M4）。

运行方式（在 client_windows 目录下）：
    python -m unittest discover -s tests -v
"""

import unittest
from datetime import datetime, timedelta

from app.constants import (
    MONITOR_ACTION_KILL_TAB,
    MONITOR_ACTION_OVERLAY,
    MONITOR_LEVEL_L1,
    MONITOR_LEVEL_L2,
    MONITOR_LEVEL_L3,
    MONITOR_LEVEL_L4,
    MONITOR_LEVEL_NONE,
)
from app.services.monitor_rules import (
    ACTION_ESCALATE,
    ACTION_FINISH,
    ACTION_NONE,
    ACTION_NOTIFY,
    aggregate_session,
    decide_action,
    is_distraction,
    is_exempt,
    normalize_thresholds,
    pick_l4_action,
    pick_level,
)


class IsExemptTest(unittest.TestCase):
    """白名单豁免判断测试。"""

    def test_title_match(self):
        self.assertTrue(is_exempt("main.py - PyCharm", "", ["PyCharm"], []))

    def test_domain_match(self):
        self.assertTrue(is_exempt("", "github.com", [], ["github.com"]))

    def test_no_match(self):
        self.assertFalse(is_exempt("抖音 - Google Chrome", "douyin.com", ["PyCharm"], ["github.com"]))

    def test_case_insensitive(self):
        self.assertTrue(is_exempt("HELLO WORLD", "", ["world"], []))

    def test_empty_whitelist(self):
        self.assertFalse(is_exempt("anything", "any.com", [], []))


class IsDistractionTest(unittest.TestCase):
    """娱乐黑名单命中判断测试。"""

    def test_domain_hit(self):
        self.assertTrue(
            is_distraction(
                "首页", "douyin.com", "chrome.exe",
                ["douyin.com"], [], [],
            )
        )

    def test_title_keyword_hit(self):
        self.assertTrue(
            is_distraction(
                "哔哩哔哩 - Chrome", "", "chrome.exe",
                [], ["哔哩哔哩"], [],
            )
        )

    def test_process_hit(self):
        self.assertTrue(
            is_distraction("", "", "yuanshen.exe", [], [], ["yuanshen.exe"])
        )

    def test_plain_window_not_distraction(self):
        # 记事本、资源管理器等不应被误判为娱乐
        self.assertFalse(
            is_distraction(
                "无标题 - 记事本", "", "notepad.exe",
                ["douyin.com"], ["抖音"], ["yuanshen.exe"],
            )
        )

    def test_empty_blacklist_never_distraction(self):
        self.assertFalse(
            is_distraction("抖音 - Chrome", "douyin.com", "chrome.exe", [], [], [])
        )

    def test_case_insensitive(self):
        self.assertTrue(
            is_distraction("", "DOUYIN.com", "", ["douyin.com"], [], [])
        )


class PickLevelTest(unittest.TestCase):
    """升级阈值判断测试。"""

    def setUp(self):
        self.thr = {1: 10, 2: 20, 3: 30, 4: 45}

    def test_below_l1(self):
        # 5 分钟
        self.assertEqual(pick_level(5 * 60, self.thr), MONITOR_LEVEL_NONE)

    def test_reach_l1(self):
        # 10 分钟整
        self.assertEqual(pick_level(10 * 60, self.thr), MONITOR_LEVEL_L1)

    def test_reach_l2(self):
        # 25 分钟
        self.assertEqual(pick_level(25 * 60, self.thr), MONITOR_LEVEL_L2)

    def test_reach_l4(self):
        # 45 分钟整
        self.assertEqual(pick_level(45 * 60, self.thr), MONITOR_LEVEL_L4)

    def test_over_l4(self):
        # 60 分钟仍为 L4
        self.assertEqual(pick_level(60 * 60, self.thr), MONITOR_LEVEL_L4)


class NormalizeThresholdsTest(unittest.TestCase):
    """配置阈值规范化测试。"""

    def test_full_config(self):
        cfg = {"l1_minutes": 5, "l2_minutes": 15, "l3_minutes": 25, "l4_minutes": 40}
        thr = normalize_thresholds(cfg)
        self.assertEqual(thr[MONITOR_LEVEL_L1], 5)
        self.assertEqual(thr[MONITOR_LEVEL_L4], 40)

    def test_partial_config_uses_defaults(self):
        cfg = {"l1_minutes": 8}
        thr = normalize_thresholds(cfg)
        self.assertEqual(thr[MONITOR_LEVEL_L1], 8)
        # 缺失项使用默认值
        self.assertEqual(thr[MONITOR_LEVEL_L2], 20)
        self.assertEqual(thr[MONITOR_LEVEL_L3], 30)
        self.assertEqual(thr[MONITOR_LEVEL_L4], 45)

    def test_none_config(self):
        thr = normalize_thresholds(None)
        self.assertEqual(thr[MONITOR_LEVEL_L1], 10)


class DecideActionTest(unittest.TestCase):
    """动作决策测试。"""

    def test_idle_to_idle_no_action(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_NONE,
            target_level=MONITOR_LEVEL_NONE,
            fired_levels=set(),
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_NONE)

    def test_first_level_notify(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_NONE,
            target_level=MONITOR_LEVEL_L1,
            fired_levels=set(),
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_NOTIFY)
        self.assertEqual(result["level"], MONITOR_LEVEL_L1)

    def test_already_fired_no_repeat(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_L1,
            target_level=MONITOR_LEVEL_L1,
            fired_levels={MONITOR_LEVEL_L1},
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_NONE)

    def test_escalate_to_l2(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_L1,
            target_level=MONITOR_LEVEL_L2,
            fired_levels={MONITOR_LEVEL_L1},
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_NOTIFY)
        self.assertEqual(result["level"], MONITOR_LEVEL_L2)

    def test_l4_escalate(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_L3,
            target_level=MONITOR_LEVEL_L4,
            fired_levels={MONITOR_LEVEL_L1, MONITOR_LEVEL_L2, MONITOR_LEVEL_L3},
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_ESCALATE)
        self.assertEqual(result["level"], MONITOR_LEVEL_L4)

    def test_l4_already_fired_no_repeat(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_L4,
            target_level=MONITOR_LEVEL_L4,
            fired_levels={MONITOR_LEVEL_L1, MONITOR_LEVEL_L2, MONITOR_LEVEL_L3, MONITOR_LEVEL_L4},
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_NONE)

    def test_finish_when_back_to_idle(self):
        result = decide_action(
            cur_level=MONITOR_LEVEL_L2,
            target_level=MONITOR_LEVEL_NONE,
            fired_levels={MONITOR_LEVEL_L1, MONITOR_LEVEL_L2},
            closed=False,
        )
        self.assertEqual(result["action"], ACTION_FINISH)

    def test_finish_after_closed_flag(self):
        # 即使仍处于娱乐域，但 closed=True 且娱乐结束（target=NONE）也要 finish
        result = decide_action(
            cur_level=MONITOR_LEVEL_L4,
            target_level=MONITOR_LEVEL_NONE,
            fired_levels={MONITOR_LEVEL_L1, MONITOR_LEVEL_L2, MONITOR_LEVEL_L3, MONITOR_LEVEL_L4},
            closed=True,
        )
        self.assertEqual(result["action"], ACTION_FINISH)


class PickL4ActionTest(unittest.TestCase):
    """L4 动作配置规范化测试。"""

    def test_overlay(self):
        self.assertEqual(pick_l4_action("overlay"), MONITOR_ACTION_OVERLAY)

    def test_kill_tab(self):
        self.assertEqual(pick_l4_action("kill_tab"), MONITOR_ACTION_KILL_TAB)

    def test_invalid_falls_back_to_overlay(self):
        self.assertEqual(pick_l4_action("nonsense"), MONITOR_ACTION_OVERLAY)
        self.assertEqual(pick_l4_action(""), MONITOR_ACTION_OVERLAY)


class AggregateSessionTest(unittest.TestCase):
    """会话聚合测试。"""

    def setUp(self):
        self.started = datetime(2026, 10, 7, 14, 0, 0)
        self.ended = self.started + timedelta(minutes=35)

    def test_basic_aggregate(self):
        samples = [
            {"timestamp": self.started, "process_name": "chrome.exe",
             "title": "抖音 - Chrome", "domain": "douyin.com", "seconds": 5},
            {"timestamp": self.started + timedelta(minutes=10),
             "process_name": "chrome.exe",
             "title": "bilibili - Chrome", "domain": "bilibili.com", "seconds": 5},
            {"timestamp": self.started + timedelta(minutes=20),
             "process_name": "chrome.exe",
             "title": "bilibili - Chrome", "domain": "bilibili.com", "seconds": 5},
        ]
        result = aggregate_session(
            samples, self.started, self.ended, fired_count=3, closed=True,
            note="L4 触发",
        )
        self.assertEqual(result["duration_seconds"], 35 * 60)
        # 取最长样本（这里前两个 seconds 都是 5，max 取第一个即 douyin）
        # 修改测试：最长样本应为 seconds 最大的；都相同时取第一个
        self.assertEqual(result["process_name"], "chrome.exe")
        self.assertTrue(result["notified"])
        self.assertEqual(result["notify_count"], 3)
        self.assertTrue(result["closed"])
        self.assertEqual(result["note"], "L4 触发")

    def test_longest_sample_picked(self):
        """停留时间最长的样本被选中作为代表。"""
        samples = [
            {"timestamp": self.started, "process_name": "chrome.exe",
             "title": "短停留", "domain": "douyin.com", "seconds": 5},
            {"timestamp": self.started + timedelta(minutes=1),
             "process_name": "chrome.exe",
             "title": "长停留", "domain": "bilibili.com", "seconds": 30},
        ]
        result = aggregate_session(
            samples, self.started, self.ended, fired_count=0, closed=False,
        )
        self.assertEqual(result["window_title"], "长停留")
        self.assertEqual(result["url"], "bilibili.com")

    def test_empty_samples(self):
        result = aggregate_session(
            [], self.started, self.ended, fired_count=0, closed=False,
        )
        self.assertEqual(result["duration_seconds"], 0)
        self.assertEqual(result["process_name"], "")
        self.assertFalse(result["notified"])

    def test_zero_duration_falls_back_to_seconds_sum(self):
        """起止差为 0 时用采样 seconds 求和兜底。"""
        samples = [
            {"timestamp": self.started, "process_name": "chrome.exe",
             "title": "A", "domain": "a.com", "seconds": 5},
            {"timestamp": self.started, "process_name": "chrome.exe",
             "title": "B", "domain": "b.com", "seconds": 7},
        ]
        result = aggregate_session(
            samples, self.started, self.started, fired_count=0, closed=False,
        )
        self.assertEqual(result["duration_seconds"], 12)

    def test_away_seconds_deducted(self):
        """容忍窗口内离开娱乐的时间应从总时长中扣除。"""
        samples = [
            {"timestamp": self.started, "process_name": "chrome.exe",
             "title": "抖音", "domain": "douyin.com", "seconds": 5},
        ]
        result = aggregate_session(
            samples, self.started, self.ended, fired_count=0, closed=False,
            away_seconds=120,
        )
        self.assertEqual(result["duration_seconds"], 35 * 60 - 120)

    def test_away_seconds_never_negative(self):
        """away_seconds 异常大时时长不小于 0。"""
        samples = [
            {"timestamp": self.started, "process_name": "chrome.exe",
             "title": "抖音", "domain": "douyin.com", "seconds": 5},
        ]
        result = aggregate_session(
            samples, self.started, self.ended, fired_count=0, closed=False,
            away_seconds=10 ** 9,
        )
        self.assertEqual(result["duration_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
