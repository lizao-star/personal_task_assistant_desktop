"""关闭娱乐网页（M4，L4 动作之二）。

双路径：
1. 优先通过本地 API 通知浏览器扩展关标签页（chrome.tabs.remove，只关匹配标签）；
2. 扩展离线时降级用 pywin32 找匹配窗口标题的浏览器窗口并 PostMessage WM_CLOSE。

只关匹配的窗口/标签，不杀进程。
"""

from __future__ import annotations

import threading
from typing import Any

from .local_api import LocalApiServer


# Win32 消息常量（避免在模块顶部强依赖 pywin32，缺失时降级）
WM_CLOSE = 0x0010


def close_tab(
    domain: str,
    title: str,
    api_server: LocalApiServer | None,
    timeout_seconds: float = 3.0,
) -> dict[str, Any]:
    """关闭匹配 domain 的娱乐网页。

    返回 dict：
        method: "extension" | "win32" | "none"
        ok: bool
        detail: str
    """
    domain = (domain or "").strip()
    title = (title or "").strip()

    # 路径 1：扩展在线才走本地 API 下发关标签请求。
    # 扩展离线（无心跳）时不能把请求塞进队列，否则会永久滞留。
    if api_server is not None and domain and api_server.is_extension_online():
        api_server.request_close_tab(domain)
        # 不阻塞等待扩展回告（扩展下次轮询 /poll-close 会取走并执行）
        # 客户端只负责把请求塞进队列；扩展异步执行
        return {
            "method": "extension",
            "ok": True,
            "detail": f"已下发关标签请求，扩展下次轮询时关闭 {domain} 的标签",
        }

    # 路径 2：扩展离线时降级用 Win32 关匹配窗口
    if not domain and not title:
        return {"method": "none", "ok": False, "detail": "无 domain 与 title"}

    # 用关键词匹配窗口标题（domain 优先，其次 title）
    keyword = domain or title
    closed_count = _close_windows_by_keyword(keyword)
    return {
        "method": "win32",
        "ok": closed_count > 0,
        "detail": f"已通过 Win32 关闭 {closed_count} 个匹配窗口（关键词：{keyword}）",
    }


def _close_windows_by_keyword(keyword: str) -> int:
    """枚举所有顶层窗口，标题包含 keyword 的发 WM_CLOSE。

    返回成功发送 WM_CLOSE 的窗口数（不保证窗口真的关闭）。
    """
    try:
        import win32gui
        import win32api
        import win32con
    except ImportError:
        # 非 Windows 或未安装 pywin32
        return 0

    if not keyword:
        return 0

    keyword_lower = keyword.lower()
    closed = 0

    def _enum_handler(hwnd: int, _result: list[int]) -> bool:
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            text = win32gui.GetWindowText(hwnd)
        except Exception:
            return True
        if not text:
            return True
        if keyword_lower in text.lower():
            # 排除我们的遮罩窗口（标题为"该回去学习了"）
            if "该回去学习了" in text:
                return True
            try:
                win32gui.PostMessage(hwnd, WM_CLOSE, 0, 0)
                _result.append(hwnd)
            except Exception:
                pass
        return True

    matched: list[int] = []
    try:
        win32gui.EnumWindows(_enum_handler, matched)
        closed = len(matched)
    except Exception:
        pass
    return closed


def close_tab_async(
    domain: str,
    title: str,
    api_server: LocalApiServer | None,
) -> threading.Thread:
    """异步关闭（不在主线程阻塞）。返回线程对象。"""
    t = threading.Thread(
        target=close_tab,
        args=(domain, title, api_server),
        daemon=True,
        name="kill-tab",
    )
    t.start()
    return t
