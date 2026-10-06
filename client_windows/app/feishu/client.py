"""飞书开放平台 HTTP 客户端。

职责：
1. 获取并缓存 tenant_access_token（到期前自动刷新）；
2. 封装多维表格记录的分页查询；
3. 对 429（限流）与 5xx（服务端错误）做指数退避重试。

设计为只依赖 requests，便于在后台线程中直接使用。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Iterator

import requests


class FeishuAPIError(Exception):
    """飞书业务错误（响应 code 非 0）。"""

    def __init__(self, code: int, msg: str):
        super().__init__(f"飞书接口错误 code={code}, msg={msg}")
        self.code = code
        self.msg = msg


class FeishuClient:
    """飞书开放平台轻量客户端（线程安全）。"""

    BASE_URL = "https://open.feishu.cn"
    TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"

    def __init__(self, app_id: str, app_secret: str, timeout: int = 10):
        self._app_id = app_id
        self._app_secret = app_secret
        self._timeout = timeout
        # token 缓存
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._token_lock = threading.Lock()

    # ------------------------------------------------------------------
    # token 管理
    # ------------------------------------------------------------------
    def _request_token(self) -> str:
        """向飞书申请新的 tenant_access_token。"""
        resp = requests.post(
            self.BASE_URL + self.TOKEN_PATH,
            json={"app_id": self._app_id, "app_secret": self._app_secret},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise FeishuAPIError(data.get("code", -1), data.get("msg", ""))
        # expire 单位为秒，提前 5 分钟刷新，避免边界过期
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200)) - 300
        return self._token

    def get_token(self, force_refresh: bool = False) -> str:
        """获取有效的 tenant_access_token（带缓存与双重检查锁）。"""
        with self._token_lock:
            if force_refresh or not self._token or time.time() >= self._token_expire_at:
                return self._request_token()
            return self._token

    # ------------------------------------------------------------------
    # 通用请求
    # ------------------------------------------------------------------
    def _request_once(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """发起单次请求，返回解析后的 JSON。"""
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=json_body,
            timeout=self._timeout,
        )

        # token 失效：强制刷新一次后由外层重试
        if resp.status_code == 401:
            self.get_token(force_refresh=True)
            resp.raise_for_status()

        resp.raise_for_status()
        data = resp.json()
        code = data.get("code", 0)
        if code != 0:
            raise FeishuAPIError(code, data.get("msg", ""))
        return data

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        max_retries: int = 3,
    ) -> dict[str, Any]:
        """带指数退避重试的请求。

        - 网络异常 / 5xx / 429：退避后重试（1s、2s、4s）；
        - 401：刷新 token 后重试一次；
        - 其他 4xx 或业务错误：直接抛出。
        """
        url = self.BASE_URL + path
        last_error: Exception | None = None

        for attempt in range(max_retries):
            try:
                try:
                    return self._request_once(method, url, params=params, json_body=json_body)
                except FeishuAPIError as e:
                    # token 相关错误码：刷新后重试
                    if e.code in (99991663, 99991661, 99991664):
                        self.get_token(force_refresh=True)
                        if attempt < max_retries - 1:
                            continue
                    raise
            except requests.HTTPError as e:
                status = e.response.status_code if e.response is not None else 0
                if status in (429, 500, 502, 503, 504) and attempt < max_retries - 1:
                    last_error = e
                else:
                    raise
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < max_retries - 1:
                    last_error = e
                else:
                    raise

            # 指数退避
            time.sleep(2**attempt)

        # 理论上走不到这里
        raise last_error or RuntimeError("请求失败，且无可用错误信息")

    # ------------------------------------------------------------------
    # 多维表格
    # ------------------------------------------------------------------
    def iter_records(
        self,
        app_token: str,
        table_id: str,
        page_size: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """分页迭代多维表格中的全部记录。

        每次 yield 的结构为 {"record_id": ..., "fields": {...}}。
        """
        path = (
            f"/open-apis/bitable/v1/apps/{app_token}"
            f"/tables/{table_id}/records"
        )
        page_token: str | None = None

        while True:
            params: dict[str, Any] = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token

            data = self.request("GET", path, params=params)
            payload = data.get("data", {})
            for item in payload.get("items", []) or []:
                yield item

            if payload.get("has_more"):
                page_token = payload.get("page_token")
            else:
                break
