"""娱乐监督监控引擎（M4）。

监控前台窗口与扩展上报的域名，按 L1~L4 升级提醒，L4 触发用户配置的动作
（全屏遮挡 | 强制关闭网页）。会话结束聚合写飞书日志表。

线程模型：
- MonitorWorker(QObject) moveToThread 独立 QThread，5 秒 QTimer 触发 _sample()；
- 采样与状态机在工作线程跑，UI 动作（显示遮罩、系统通知）通过 Qt 信号回到主线程；
- 网络请求（push_monitor_logs）由主线程接到 session_ended 信号后入队给 SyncWorker。

引擎不直接读写飞书，只负责状态维护与动作分发；
日志写入通过 LocalCache 入队，由 SyncWorker.push_monitor_logs 推送。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..constants import (
    MONITOR_ACTION_KILL_TAB,
    MONITOR_ACTION_OVERLAY,
    MONITOR_LEVEL_L1,
    MONITOR_LEVEL_L2,
    MONITOR_LEVEL_L3,
    MONITOR_LEVEL_L4,
    MONITOR_LEVEL_NONE,
)
from .cache import LocalCache
from .local_api import LocalApiServer
from . import monitor_rules


# 各级提醒文案模板（{minutes} 由实际配置阈值填充，避免与 settings.yaml 不同步）
_LEVEL_MESSAGE_TEMPLATES = {
    MONITOR_LEVEL_L1: "已连续娱乐 {minutes} 分钟，该回去学习了",
    MONITOR_LEVEL_L2: "已连续娱乐 {minutes} 分钟，请注意时间",  # L2 同时触发语音
    MONITOR_LEVEL_L3: "已连续娱乐 {minutes} 分钟，强烈建议立即停止",
    MONITOR_LEVEL_L4: "已连续娱乐 {minutes} 分钟，触发强制监督动作",
}

# 会话过期保护：距离上次采样超过该秒数，视为重启前残留的陈旧会话，直接丢弃
_STALE_SESSION_SECONDS = 120

# 浏览器进程名（含常见国产套壳）；只有前台是浏览器时，才采信扩展上报的域名，
# 否则用户切到别的应用时旧域名会残留，造成误判。
_BROWSER_PROCESSES = {
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "vivaldi.exe", "iexplore.exe", "360se.exe", "360chrome.exe",
    "qqbrowser.exe", "sogouexplorer.exe", "maxthon.exe",
}


class MonitorWorker(QObject):
    """监控引擎（运行在独立 QThread 中）。

    信号（全部在主线程接收）：
        notify_requested(int level, str text)：触发通知/语音
        action_requested(str kind, str domain, str title, int minutes)：触发 L4 动作
        session_ended(dict aggregate)：会话结束，主线程负责入队日志
        status_changed(str state, int level, int elapsed_seconds)：状态变更，供 UI 显示
    """

    notify_requested = Signal(int, str)
    action_requested = Signal(str, str, str, int)
    session_ended = Signal(dict)
    status_changed = Signal(str, int, int)

    def __init__(
        self,
        cache: LocalCache,
        api_server: LocalApiServer | None,
        settings: dict[str, Any],
    ):
        super().__init__()
        self._cache = cache
        self._api = api_server
        self._settings = settings or {}
        monitor_cfg = self._settings.get("monitor", {}) or {}
        self._enabled = bool(monitor_cfg.get("enabled", True))
        self._sample_interval = int(monitor_cfg.get("sample_interval_seconds", 5))
        self._whitelist_titles = list(monitor_cfg.get("whitelist_titles", []))
        self._whitelist_domains = list(monitor_cfg.get("whitelist_domains", []))
        self._blacklist_domains = list(monitor_cfg.get("blacklist_domains", []))
        self._blacklist_keywords = list(monitor_cfg.get("blacklist_keywords", []))
        self._blacklist_processes = list(monitor_cfg.get("blacklist_processes", []))
        # 容忍窗口（秒）：短暂切走不结束会话，避免同一段娱乐被拆成多条日志
        self._grace_seconds = int(monitor_cfg.get("grace_seconds", 60))
        self._thresholds = monitor_rules.normalize_thresholds(
            monitor_cfg.get("escalation")
        )
        self._l4_action = monitor_rules.pick_l4_action(
            monitor_cfg.get("l4_action", MONITOR_ACTION_OVERLAY)
        )
        self._pause_with_reminder = bool(monitor_cfg.get("pause_with_reminder", True))

        self._paused = False
        self._timer: QTimer | None = None
        # 当前会话已触发提醒的级别集合（避免每个级别重复提醒）
        self._fired_levels: set[int] = set()
        # 采样历史（用于聚合）
        self._samples: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    @Slot()
    def start(self) -> None:
        """启动定时采样（在工作线程中调用）。"""
        if not self._enabled:
            return
        if self._timer is not None:
            return
        self._timer = QTimer(self)
        self._timer.setInterval(self._sample_interval * 1000)
        self._timer.timeout.connect(self._sample)
        self._timer.start()

    @Slot()
    def stop(self) -> None:
        """停止采样（应用退出或被禁用时调用）。"""
        if self._timer is not None:
            self._timer.stop()
            self._timer = None
        # 收尾活跃会话
        self._finish_session(reason="stop")

    @Slot(bool)
    def set_paused(self, paused: bool) -> None:
        """暂停/恢复监督（与托盘「暂停提醒」联动）。"""
        self._paused = paused
        if paused and self._pause_with_reminder:
            # 暂停期间收尾活跃会话
            self._finish_session(reason="paused")
        elif not paused and self._timer is None:
            # 从暂停恢复
            self.start()

    # ------------------------------------------------------------------
    # 采样核心
    # ------------------------------------------------------------------
    @Slot()
    def _sample(self) -> None:
        """单次采样：取前台窗口/扩展上报，状态机决策，分发动作。"""
        if self._paused and self._pause_with_reminder:
            return

        process_name, window_title, domain = self._collect_signals()
        now = datetime.now()

        # 命中白名单视为非娱乐；命中黑名单才计时
        exempt = monitor_rules.is_exempt(
            window_title, domain, self._whitelist_titles, self._whitelist_domains
        )
        # 必须显式命中黑名单（娱乐域名/标题关键词/进程）才计时，
        # 否则记事本、资源管理器等任意窗口都会被误判为娱乐。
        distraction = (not exempt) and monitor_rules.is_distraction(
            window_title,
            domain,
            process_name,
            self._blacklist_domains,
            self._blacklist_keywords,
            self._blacklist_processes,
        )

        session = self._cache.load_monitor_session()
        if session is not None and not self._is_session_fresh(session):
            # 上次运行残留的陈旧会话（如程序被强杀），直接丢弃避免一启动就弹 L4
            self._cache.end_monitor_session()
            session = None

        if not distraction:
            if session is None:
                self.status_changed.emit("idle", MONITOR_LEVEL_NONE, 0)
                return
            # 容忍窗口：短暂切走（查资料、回消息等）不结束会话，
            # 回来后沿用同一会话继续累计，避免同一段娱乐被拆成多条日志。
            if self._is_within_grace(session, now):
                # 按「距上次采样的增量」累计离开时长，避免重复计数
                last_dt = self._parse_dt(session.get("last_sample_at")) or now
                gap = max(0.0, (now - last_dt).total_seconds())
                self._cache.update_monitor_session(
                    payload={
                        **session["payload"],
                        "away_seconds": float(
                            session["payload"].get("away_seconds", 0) or 0
                        ) + gap,
                    }
                )
                self.status_changed.emit(
                    "grace", session["level"], self._elapsed_seconds(session, now)
                )
                return
            # 超过容忍窗口：真正结束，聚合写一条日志
            self._finish_session(reason="idle")
            self.status_changed.emit("idle", MONITOR_LEVEL_NONE, 0)
            return

        # 娱乐：所有娱乐平台共用一个会话/计时器，切换平台不清零
        if session is None:
            payload = {
                "process_name": process_name,
                "title": window_title,
                "domain": domain,
                "started_at": now.isoformat(timespec="seconds"),
                "last_distraction_at": now.isoformat(timespec="seconds"),
            }
            self._cache.start_monitor_session(payload)
            session = self._cache.load_monitor_session()
            self._fired_levels = set()
            self._samples = []
        else:
            # 每次采样刷新存活时间戳（避免长会话被误判为陈旧而清零）
            # 并记录最近娱乐时刻（供容忍窗口判断）
            self._cache.update_monitor_session(
                payload={
                    **session["payload"],
                    "last_distraction_at": now.isoformat(timespec="seconds"),
                }
            )
            session = self._cache.load_monitor_session()

        # 记录采样
        self._samples.append(
            {
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "process_name": process_name,
                "title": window_title,
                "domain": domain,
                "seconds": self._sample_interval,
            }
        )

        # 计算已持续秒数（扣除容忍窗口内离开娱乐的时间）
        elapsed = self._elapsed_seconds(session, now) if session else 0

        # 决策
        target_level = monitor_rules.pick_level(elapsed, self._thresholds)
        cur_level = session["level"] if session else MONITOR_LEVEL_NONE
        closed = session["closed"] if session else False
        action = monitor_rules.decide_action(
            cur_level, target_level, self._fired_levels, closed
        )

        # 分发
        if action["action"] == monitor_rules.ACTION_NOTIFY:
            level = action["level"]
            self._fired_levels.add(level)
            text = self._level_message(level)
            self.notify_requested.emit(level, text)
            # L2 同时语音
            # 注：语音由主线程的 Notifier 调用，这里只发通知信号；
            # 主线程根据 level 决定是否 speak（level >= L2 即语音）
            self._cache.update_monitor_session(
                level=target_level,
                fired_count=session["fired_count"] + 1,
                payload={
                    **session["payload"],
                    "last_title": window_title,
                    "last_domain": domain,
                    "last_process": process_name,
                },
            )
        elif action["action"] == monitor_rules.ACTION_ESCALATE:
            level = action["level"]
            self._fired_levels.add(level)
            minutes = elapsed // 60
            self.action_requested.emit(
                self._l4_action, domain, window_title, minutes
            )
            self._cache.update_monitor_session(
                level=target_level,
                fired_count=session["fired_count"] + 1,
                payload={
                    **session["payload"],
                    "last_title": window_title,
                    "last_domain": domain,
                    "l4_triggered": True,
                },
            )
        elif action["action"] == monitor_rules.ACTION_FINISH:
            self._finish_session(reason="back_to_idle")
            return

        self.status_changed.emit("active", target_level, elapsed)

    # ------------------------------------------------------------------
    # 收尾
    # ------------------------------------------------------------------
    def _finish_session(self, reason: str = "") -> None:
        """结束当前活跃会话，聚合并发 session_ended 信号。"""
        session = self._cache.end_monitor_session()
        if session is None:
            return
        try:
            started = datetime.fromisoformat(session["started_at"])
        except Exception:
            started = datetime.now()
        ended = datetime.now()
        aggregate = monitor_rules.aggregate_session(
            self._samples,
            started,
            ended,
            fired_count=session["fired_count"],
            closed=session["closed"],
            note=reason,
            away_seconds=int(session.get("payload", {}).get("away_seconds", 0) or 0),
        )
        self._samples = []
        self._fired_levels = set()
        self.session_ended.emit(aggregate)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def _level_message(self, level: int) -> str:
        """按当前配置阈值生成提醒文案（分钟数与 settings.yaml 保持一致）。"""
        template = _LEVEL_MESSAGE_TEMPLATES.get(level)
        if template is None:
            return "娱乐时间过长"
        return template.format(minutes=self._thresholds.get(level, 0))

    def _is_session_fresh(self, session: dict[str, Any]) -> bool:
        """会话是否仍然新鲜（距上次采样未超过阈值）。

        程序被强杀时本地表可能残留旧会话，若直接沿用会导致
        elapsed 虚高、一启动就触发 L4，故此处做陈旧剔除。
        """
        last = session.get("last_sample_at") or session.get("started_at")
        if not last:
            return False
        try:
            last_dt = datetime.fromisoformat(last)
        except Exception:
            return False
        return (datetime.now() - last_dt).total_seconds() <= _STALE_SESSION_SECONDS

    # ------------------------------------------------------------------
    # 信号采集
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_dt(value: str | None) -> datetime | None:
        """解析 ISO 时间字符串，失败返回 None。"""
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None

    def _elapsed_seconds(self, session: dict[str, Any], now: datetime) -> int:
        """会话已累计娱乐秒数（扣除容忍窗口内切走的时间）。"""
        started = self._parse_dt(session.get("started_at"))
        if started is None:
            return 0
        away = float(session.get("payload", {}).get("away_seconds", 0) or 0)
        return max(0, int((now - started).total_seconds() - away))

    def _is_within_grace(self, session: dict[str, Any], now: datetime) -> bool:
        """距离上次娱乐是否仍在容忍窗口内（用于切走不结束会话）。"""
        if self._grace_seconds <= 0:
            return False
        last = self._parse_dt(
            session.get("payload", {}).get("last_distraction_at")
        ) or self._parse_dt(session.get("last_sample_at"))
        if last is None:
            return False
        return (now - last).total_seconds() <= self._grace_seconds

    def _collect_signals(self) -> tuple[str, str, str]:
        """采集当前前台窗口的 (process_name, title, domain)。

        domain 来自浏览器扩展上报（无扩展则空），process_name/title 来自 Win32。
        """
        process_name = ""
        window_title = ""
        domain = ""

        # Win32 取前台窗口（不依赖扩展）
        try:
            import win32gui
            import psutil

            hwnd = win32gui.GetForegroundWindow()
            if hwnd:
                try:
                    window_title = win32gui.GetWindowText(hwnd) or ""
                except Exception:
                    pass
                try:
                    _, pid = win32gui.GetWindowThreadProcessId(hwnd)
                    try:
                        process_name = psutil.Process(pid).name()
                    except Exception:
                        pass
                except Exception:
                    pass
        except ImportError:
            # 非 Windows 或未装 pywin32：只能靠扩展上报，此时不做浏览器校验
            pass

        # 扩展上报：只有前台确实是浏览器时才采信，且标题与域名必须取自
        # 同一次上报，否则会出现「标题=飞书、URL=bilibili」这种错配。
        if self._api is not None:
            report = self._api.get_latest_report()
            if report and (not process_name or process_name.lower() in _BROWSER_PROCESSES):
                domain = report.get("domain", "") or ""
                report_title = report.get("title", "") or ""
                # 页面标题优先用扩展上报（与 domain 同源），无则回退 Win32 标题
                if report_title:
                    window_title = report_title

        return process_name, window_title, domain
