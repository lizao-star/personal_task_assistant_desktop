"""番茄钟单元测试。

纯函数 next_phase 直接测；PomodoroTimer 注入 1~2 秒短时长，
手动调用 _tick() 驱动（不等真实时间），用 offscreen 平台避免弹窗。
"""

import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.services.pomodoro import (  # noqa: E402
    STATE_FOCUS,
    STATE_IDLE,
    STATE_REST,
    PomodoroTimer,
    next_phase,
)

# QTimer 需要 QApplication 存在（即使不跑事件循环）
_app = QApplication.instance() or QApplication([])


class NextPhaseTest(unittest.TestCase):
    """状态转移纯函数。"""

    def test_idle_to_focus(self):
        self.assertEqual(next_phase(STATE_IDLE), STATE_FOCUS)

    def test_focus_to_rest(self):
        self.assertEqual(next_phase(STATE_FOCUS), STATE_REST)

    def test_rest_to_idle(self):
        self.assertEqual(next_phase(STATE_REST), STATE_IDLE)

    def test_unknown_falls_back_to_idle(self):
        self.assertEqual(next_phase("???"), STATE_IDLE)


class PomodoroTimerTest(unittest.TestCase):
    """计时器状态机（手动驱动 tick）。"""

    def setUp(self):
        # 2 秒专注 + 1 秒休息，便于手动驱动完整周期
        self.timer = PomodoroTimer(focus_seconds=2, rest_seconds=1)
        self.state_events: list[tuple[str, int]] = []
        self.finished_events: list[tuple[str, str]] = []
        self.timer.state_changed.connect(
            lambda s, r: self.state_events.append((s, r))
        )
        self.timer.phase_finished.connect(
            lambda p, m: self.finished_events.append((p, m))
        )

    def tearDown(self):
        self.timer.stop()

    def test_initial_idle(self):
        self.assertEqual(self.timer.state, STATE_IDLE)
        self.assertEqual(self.timer.remaining, 0)

    def test_start_enters_focus_with_full_duration(self):
        self.timer.start()
        self.assertEqual(self.timer.state, STATE_FOCUS)
        self.assertEqual(self.timer.remaining, 2)
        self.assertEqual(self.state_events[-1], (STATE_FOCUS, 2))

    def test_tick_counts_down(self):
        self.timer.start()
        self.timer._tick()
        self.assertEqual(self.timer.remaining, 1)
        self.assertEqual(self.state_events[-1], (STATE_FOCUS, 1))
        # 未归零不发 phase_finished
        self.assertEqual(self.finished_events, [])

    def test_focus_end_auto_enters_rest(self):
        self.timer.start()
        self.timer._tick()
        self.timer._tick()  # 专注结束
        self.assertEqual(self.timer.state, STATE_REST)
        self.assertEqual(self.timer.remaining, 1)
        self.assertEqual(len(self.finished_events), 1)
        self.assertEqual(self.finished_events[0][0], STATE_FOCUS)
        self.assertIn("休息", self.finished_events[0][1])

    def test_rest_end_back_to_idle(self):
        self.timer.start()
        self.timer._tick()
        self.timer._tick()  # 进入休息（1 秒）
        self.timer._tick()  # 休息结束
        self.assertEqual(self.timer.state, STATE_IDLE)
        self.assertEqual(self.timer.remaining, 0)
        self.assertEqual([p for p, _ in self.finished_events], [STATE_FOCUS, STATE_REST])
        self.assertEqual(self.state_events[-1], (STATE_IDLE, 0))

    def test_manual_stop_no_phase_finished(self):
        self.timer.start()
        self.timer.stop()
        self.assertEqual(self.timer.state, STATE_IDLE)
        self.assertEqual(self.timer.remaining, 0)
        self.assertEqual(self.finished_events, [])
        self.assertEqual(self.state_events[-1], (STATE_IDLE, 0))

    def test_restart_while_running(self):
        self.timer.start()
        self.timer._tick()  # 剩 1 秒
        self.timer.start()  # 重新开始
        self.assertEqual(self.timer.state, STATE_FOCUS)
        self.assertEqual(self.timer.remaining, 2)


if __name__ == "__main__":
    unittest.main()
