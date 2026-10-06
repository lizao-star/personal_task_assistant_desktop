"""通知与提醒模块。

包含：
1. Notifier：Windows 系统通知（winotify）+ 离线语音（pyttsx3），
   语音在独立线程播放，避免卡界面；
2. ReminderEngine：根据任务的“截止时间 + 提醒规则”计算提醒触发点，
   结合本地 reminder_log 去重、勿扰时段与补发窗口决定是否提醒。

规则驱动：本模块不调用任何 AI，断网也能正常工作。
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from ..constants import REMINDER_OFFSET_MINUTES
from ..feishu.repository import Task
from .cache import LocalCache


# ----------------------------------------------------------------------
# 通知器
# ----------------------------------------------------------------------
class Notifier:
    """系统通知 + 语音播报。"""

    APP_ID = "PersonalTaskAssistant"

    def __init__(self, tts_engine: str = "pyttsx3", tts_rate: int = 180):
        self._tts_engine = tts_engine
        self._tts_rate = tts_rate
        self._speak_lock = threading.Lock()

    def notify(self, title: str, message: str) -> None:
        """发送 Windows 系统通知；winotify 不可用时降级为打印。"""
        try:
            # 延迟导入，非 Windows 或未安装时不影响其他功能
            from winotify import Notification

            toast = Notification(
                app_id="个人任务助理",
                title=title,
                msg=message,
                duration="short",
            )
            toast.show()
        except Exception:
            print(f"[通知] {title} | {message}")

    def speak(self, text: str) -> None:
        """语音播报（后台线程执行，不阻塞调用方）。"""
        if self._tts_engine != "pyttsx3":
            # edge-tts 等引擎在后续阶段实现
            print(f"[语音-未启用的引擎 {self._tts_engine}] {text}")
            return
        threading.Thread(
            target=self._speak_with_pyttsx3, args=(text,), daemon=True
        ).start()

    def _speak_with_pyttsx3(self, text: str) -> None:
        """使用 pyttsx3 同步播放（运行在子线程）。"""
        try:
            import pyttsx3

            # 同一时刻只允许一个语音引擎在工作，避免声音叠加
            with self._speak_lock:
                engine = pyttsx3.init()
                engine.setProperty("rate", self._tts_rate)
                engine.say(text)
                engine.runAndWait()
                engine.stop()
        except Exception as e:
            print(f"[语音失败] {e}")


# ----------------------------------------------------------------------
# 勿扰判断
# ----------------------------------------------------------------------
def in_do_not_disturb(now: datetime, start_text: str, end_text: str) -> bool:
    """判断当前是否处于勿扰时段（支持跨午夜）。"""
    try:
        start_h, start_m = (int(x) for x in start_text.split(":"))
        end_h, end_m = (int(x) for x in end_text.split(":"))
    except (ValueError, AttributeError):
        return False

    minutes = now.hour * 60 + now.minute
    start = start_h * 60 + start_m
    end = end_h * 60 + end_m

    if start <= end:
        # 同日时段，如 13:00-14:00
        return start <= minutes < end
    # 跨午夜时段，如 23:00-07:00
    return minutes >= start or minutes < end


# ----------------------------------------------------------------------
# 提醒引擎
# ----------------------------------------------------------------------
class ReminderEngine:
    """按规则检查并触发任务提醒。"""

    def __init__(
        self,
        cache: LocalCache,
        notifier: Notifier,
        settings: dict[str, Any],
    ):
        self._cache = cache
        self._notifier = notifier
        reminder_cfg = settings.get("reminder", {})
        self._missed_window = timedelta(
            minutes=reminder_cfg.get("missed_window_minutes", 60)
        )
        self._default_offsets = reminder_cfg.get(
            "default_offsets", ["提前1天", "提前2小时", "提前10分钟"]
        )
        dnd = settings.get("do_not_disturb", {})
        self._dnd_start = dnd.get("start", "23:00")
        self._dnd_end = dnd.get("end", "07:00")

    def check(
        self,
        tasks: list[Task],
        now: datetime | None = None,
        muted: bool = False,
    ) -> int:
        """检查全部任务并触发到期提醒，返回本次实际触发的条数。"""
        now = now or datetime.now()
        # 全局暂停或处于勿扰时段：本次不提醒
        if muted or in_do_not_disturb(now, self._dnd_start, self._dnd_end):
            return 0

        fired = 0
        for task in tasks:
            if task.is_finished or task.due_at is None:
                continue
            # 任务未配置提醒规则时使用默认提前量
            rules = task.reminder_rules or self._default_offsets
            for rule in rules:
                offset_min = REMINDER_OFFSET_MINUTES.get(rule)
                if offset_min is None:
                    continue
                fire_at = task.due_at - timedelta(minutes=offset_min)
                # 只在 [触发时间, 触发时间+补发窗口] 内提醒
                if fire_at <= now <= fire_at + self._missed_window:
                    reminder_key = f"{task.record_id}:{rule}"
                    if self._cache.is_reminder_fired(reminder_key):
                        continue
                    self._fire(task, rule)
                    self._cache.mark_reminder_fired(reminder_key)
                    fired += 1
        return fired

    def _fire(self, task: Task, rule: str) -> None:
        """触发单条提醒：通知 + （可选）语音。"""
        due_text = task.due_at.strftime("%H:%M") if task.due_at else ""
        title = f"任务提醒（{rule}）"
        message = f"「{task.title}」将于 {due_text} 截止"
        self._notifier.notify(title, message)
        # 仅勾了“是否语音提醒”的重要任务才发声
        if task.voice:
            self._notifier.speak(f"提醒：{task.title}即将截止，请及时处理。")
