"""本地 HTTP API（M4）。

接收浏览器扩展上报的域名/标题/停留时长，并向扩展下发关标签页指令。
仅监听 127.0.0.1，不暴露到公网；不做任何鉴权（本机回环已足够安全）。

路由：
    POST /report      {domain, title, seconds}     扩展上报当前活跃标签
    POST /close-tab   {domain}                     通知扩展关闭匹配标签
    GET  /health                                    扩展探测客户端存活

线程模型：
    ThreadingHTTPServer 自带线程池，每个请求一个线程；
    MonitorWorker 通过 get_latest_report()/pop_close_request() 取数据（线程安全）。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


# 扩展上报的存活窗口（秒）：超过该时间未上报视为过期
_REPORT_TTL_SECONDS = 15
# 扩展心跳存活窗口（秒）：超过该时间无任何上报/轮询视为离线
_HEARTBEAT_TTL_SECONDS = 30


class LocalApiServer:
    """本地 HTTP 服务，承载扩展与客户端之间的通信。

    数据流：
        扩展 → /report → _latest_report（覆盖式，MonitorWorker 5 秒取走）
        客户端 → /close-tab → _close_requests 队列（扩展下次轮询时取走）
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765):
        self._host = host
        self._port = port
        self._lock = threading.Lock()
        # 最近一次扩展上报（覆盖式，只保留最新）
        self._latest_report: dict[str, Any] | None = None
        # 待下发给扩展的关标签请求队列 [{domain, ts}, ...]
        self._close_requests: list[dict[str, Any]] = []
        # 扩展最近一次心跳时间
        self._last_heartbeat: float = 0.0
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 启停
    # ------------------------------------------------------------------
    def start(self) -> None:
        """在后台线程启动 HTTP 服务（非阻塞）。"""
        if self._server is not None:
            return
        # 用闭包把 self 注入 handler
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # 静默访问日志，避免污染控制台
                pass

            def _json(self, code: int, body: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_OPTIONS(self):  # 预检请求直接放行
                self._json(200, {"ok": True})

            def do_GET(self):
                if self.path.startswith("/health"):
                    outer._touch_heartbeat()
                    self._json(200, {"ok": True, "service": "personal-task-assistant"})
                else:
                    self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except Exception:
                    self._json(400, {"ok": False, "error": "invalid json"})
                    return

                if self.path.startswith("/report"):
                    # 扩展上报即代表存活
                    outer._touch_heartbeat()
                    outer._submit_report(payload)
                    self._json(200, {"ok": True})
                elif self.path.startswith("/close-tab"):
                    # 这是扩展回告"已收到关标签请求"或主动查询关标签请求
                    # 约定：POST /close-tab {domain} 表示客户端要关某 domain
                    #       GET 行为合并到 /poll-close（保留扩展主路径）
                    outer._submit_close_request(payload)
                    self._json(200, {"ok": True})
                elif self.path.startswith("/poll-close"):
                    # 扩展轮询：取走一条待关闭请求。
                    # 轮询本身即代表扩展存活（MV3 service worker 会被挂起，
                    # 不能只靠 background 心跳判断在线）。
                    outer._touch_heartbeat()
                    req = outer._pop_close_request()
                    self._json(200, {"ok": True, "request": req})
                else:
                    self._json(404, {"ok": False, "error": "not found"})

        self._server = ThreadingHTTPServer((self._host, self._port), _Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, daemon=True, name="local-api"
        )
        self._thread.start()

    def stop(self) -> None:
        """停止服务。"""
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None

    @property
    def port(self) -> int:
        return self._port

    # ------------------------------------------------------------------
    # 数据交换（线程安全）
    # ------------------------------------------------------------------
    def _submit_report(self, payload: dict[str, Any]) -> None:
        """扩展上报当前活跃标签信息。覆盖式保留最新一条。"""
        with self._lock:
            self._latest_report = payload

    def get_latest_report(self) -> dict[str, Any] | None:
        """MonitorWorker 取走最近一次上报。

        超过 _REPORT_TTL_SECONDS 未更新的上报视为过期返回 None：
        用户关掉浏览器或切走标签后，扩展会停止上报，若继续沿用旧域名
        会把当前的前台窗口误判为在浏览娱乐网站。
        返回 None 表示扩展未安装/未活跃/上报已过期。
        """
        with self._lock:
            report = self._latest_report
        if not report:
            return None
        ts_ms = report.get("ts")
        if ts_ms:
            try:
                if (time.time() * 1000 - float(ts_ms)) / 1000 > _REPORT_TTL_SECONDS:
                    return None
            except (TypeError, ValueError):
                pass
        return report

    def clear_report(self) -> None:
        """清除最近上报（MonitorWorker 在窗口切走时可调用）。"""
        with self._lock:
            self._latest_report = None

    def _submit_close_request(self, payload: dict[str, Any]) -> None:
        """记录一条关标签请求（客户端调用 close_tab 时通过 kill_tab 注入）。"""
        domain = (payload or {}).get("domain", "")
        if not domain:
            return
        with self._lock:
            self._close_requests.append({"domain": domain})

    def _pop_close_request(self) -> dict[str, Any] | None:
        """扩展轮询取走一条关标签请求。"""
        with self._lock:
            if self._close_requests:
                return self._close_requests.pop(0)
        return None

    def request_close_tab(self, domain: str) -> None:
        """客户端请求关闭某域名的标签页（kill_tab 调用）。"""
        with self._lock:
            self._close_requests.append({"domain": domain})

    def _touch_heartbeat(self) -> None:
        """记录扩展存活时间戳（/health 心跳或 /report、/poll-close 上报）。"""
        with self._lock:
            self._last_heartbeat = time.time()

    def is_extension_online(self) -> bool:
        """扩展是否在线（最近 _HEARTBEAT_TTL_SECONDS 秒内有心跳/上报）。"""
        with self._lock:
            return (
                self._last_heartbeat > 0
                and (time.time() - self._last_heartbeat) < _HEARTBEAT_TTL_SECONDS
            )
