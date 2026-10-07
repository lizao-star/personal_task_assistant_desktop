"""番茄钟（M4 补充）。

纯计时器：idle → focus → rest → idle，不绑定任务、不写飞书、
不与娱乐监督联动（监督照常运行）。

线程模型：主线程 QTimer 每秒 tick，无 IO 无阻塞，不需要独立 QThread。
时长由构造参数注入（秒），便于单元测试用 1~2 秒驱动完整周期。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, Signal, Slot


# 状态常量
STATE_IDLE = "idle"    # 空闲（未开始 / 已停止）
STATE_FOCUS = "focus"  # 专注中
STATE_REST = "rest"    # 休息中

# 各阶段结束时的提示文案
_PHASE_MESSAGES = {
    STATE_FOCUS: "专注时间到，站起来休息一下",
    STATE_REST: "休息结束，可以开始下一个番茄钟",
}


def next_phase(state: str) -> str:
    """状态转移纯函数：idle→focus→rest→idle；未知状态回 idle。"""
    return {
        STATE_IDLE: STATE_FOCUS,
        STATE_FOCUS: STATE_REST,
        STATE_REST: STATE_IDLE,
    }.get(state, STATE_IDLE)


class PomodoroTimer(QObject):
    """番茄钟计时器：start/stop 控制，每秒发 state_changed，阶段结束发 phase_finished。"""

    # (state, remaining_seconds)：状态变化或每秒 tick 时发出，供托盘更新倒计时文案
    state_changed = Signal(str, int)
    # (finished_phase, message)：专注/休息阶段自然结束时发出（手动 stop 不发）
    phase_finished = Signal(str, str)

    def __init__(
        self,
        focus_seconds: int = 25 * 60,
        rest_seconds: int = 5 * 60,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._durations = {
            STATE_FOCUS: max(1, int(focus_seconds)),
            STATE_REST: max(1, int(rest_seconds)),
        }
        self._state = STATE_IDLE
        self._remaining = 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)

    # ------------------------------------------------------------------
    # 状态查询
    # ------------------------------------------------------------------
    @property
    def state(self) -> str:
        return self._state

    @property
    def remaining(self) -> int:
        return self._remaining

    # ------------------------------------------------------------------
    # 控制
    # ------------------------------------------------------------------
    @Slot()
    def start(self) -> None:
        """从空闲开始一个专注番茄；运行中调用则重新开始。"""
        self._enter(STATE_FOCUS)

    @Slot()
    def stop(self) -> None:
        """手动停止：回到空闲，不发 phase_finished。"""
        self._timer.stop()
        self._state = STATE_IDLE
        self._remaining = 0
        self.state_changed.emit(STATE_IDLE, 0)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _enter(self, state: str) -> None:
        """进入指定阶段并开始计时。"""
        self._state = state
        self._remaining = self._durations.get(state, 0)
        self.state_changed.emit(state, self._remaining)
        self._timer.start()

    @Slot()
    def _tick(self) -> None:
        """每秒减一；归零时自然切换到下一阶段并发 phase_finished。"""
        if self._state == STATE_IDLE:
            self._timer.stop()
            return
        self._remaining -= 1
        if self._remaining > 0:
            self.state_changed.emit(self._state, self._remaining)
            return

        # 当前阶段自然结束
        finished = self._state
        self._timer.stop()
        message = _PHASE_MESSAGES.get(finished, "")
        self.phase_finished.emit(finished, message)
        next_state = next_phase(finished)
        if next_state == STATE_IDLE:
            self._state = STATE_IDLE
            self._remaining = 0
            self.state_changed.emit(STATE_IDLE, 0)
        else:
            self._enter(next_state)
