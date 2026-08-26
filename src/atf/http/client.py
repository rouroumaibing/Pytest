"""BaseClient:requests 封装的 REST 客户端。

特性:

- **可插拔认证**:任意 :class:`~atf.http.auth.AuthStrategy`(Token /
  Cookie / ApiKey / 自定义函数 / 继承扩展),401 时自动 ``refresh`` 并重试一次;
- **extra_header**:实例级全局附加头 + 单请求级覆盖头;
- **重试**:网络异常与 429/502/503/504 按 :class:`~atf.utils.retry.RetryPolicy`
  重试(默认 2 次指数退避);
- **摘要日志**:每次请求一行摘要(方法、URL、状态码、耗时、截断的
  请求/响应体),敏感字段脱敏后落盘;
- **统一异常**:对外只抛 ``atf.http.exceptions`` 体系,携带原始异常或响应。
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping, Optional, TypeVar, Union

import requests

from atf.http.auth import AuthStrategy
from atf.http.exceptions import ApiHTTPStatusError, ApiTransportError, wrap_requests_error
from atf.utils.log import get_logger
from atf.utils.retry import RetryPolicy
from atf.utils.sanitizer import DEFAULT_SANITIZER, Sanitizer

T = TypeVar("T")
_logger = get_logger("atf.http")

#: 触发状态码级重试的 HTTP 状态(网关抖动与限流)
RETRYABLE_STATUS = frozenset({408, 425, 429, 502, 503, 504})

_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")


class BaseClient:
    """带认证、重试与摘要日志的 REST 客户端(线程安全:每线程一个实例)。"""

    def __init__(
        self,
        base_url: str,
        *,
        auth: Optional[AuthStrategy] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        timeout: float = 10.0,
        retry: Optional[RetryPolicy] = None,
        verify: Union[bool, str] = True,
        session: Optional[requests.Session] = None,
        sanitizer: Optional[Sanitizer] = None,
        log_body_bytes: int = 512,
    ) -> None:
        """初始化客户端。

        Args:
            base_url: API 根地址;请求 path 会拼在其后。
            auth: 认证策略;None 表示匿名。
            extra_headers: 全局附加头(如租户头、追踪头)。
            timeout: 请求默认超时(连接与读取共用)。
            retry: 重试策略;缺省 2 次、0.5s 起、指数退避。
            verify: TLS 证书校验(可传 CA bundle 路径)。
            session: 复用外部 session(如需自定义 adapter/代理)。
            sanitizer: 日志脱敏器,缺省进程级默认实例。
            log_body_bytes: 摘要日志中请求/响应体的最大字节数。
        """
        self._base_url = base_url.rstrip("/")
        self._auth = auth
        self._timeout = timeout
        self._retry = retry if retry is not None else RetryPolicy(
            retries=2, interval=0.5, backoff=2.0,
            exceptions=(ApiTransportError,),
        )
        self._sanitizer = sanitizer or DEFAULT_SANITIZER
        self._log_body_bytes = log_body_bytes
        self.session = session if session is not None else requests.Session()
        self.session.verify = verify
        if extra_headers:
            self.session.headers.update(extra_headers)
        if self._auth is not None:
            self._auth.apply(self.session)

    # ------------------------------------------------------------- 请求

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Mapping[str, Any]] = None,
        json_body: Optional[Any] = None,
        data: Optional[Union[Mapping[str, Any], str, bytes]] = None,
        headers: Optional[Mapping[str, str]] = None,
        timeout: Optional[float] = None,
        retry: Optional[RetryPolicy] = None,
        raise_for_status: Optional[bool] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """发送 HTTP 请求(其余便捷方法都汇聚到这里)。

        Args:
            method: HTTP 方法(大写)。
            path: 相对 ``base_url`` 的路径(以 ``http`` 开头时视为绝对 URL)。
            params: 查询参数。
            json_body: JSON 请求体(与 data 二选一)。
            data: 表单或原始体。
            headers: 单请求附加头(与全局 extra_headers 合并,优先级更高)。
            timeout: 本请求超时,覆盖实例默认值。
            retry: 本请求重试策略,覆盖实例默认值。
            raise_for_status: 是否对非 2xx 抛 :class:`ApiHTTPStatusError`,
                覆盖实例默认(默认开启;断言错误码的负向用例可关掉)。
            **kwargs: 透传给 ``requests.Session.request``。

        Returns:
            :class:`requests.Response`。

        Raises:
            ApiTransportError / ApiTimeoutError: 网络层失败(重试耗尽后)。
            ApiHTTPStatusError: ``raise_for_status`` 且状态码非 2xx。
        """
        url = path if path.startswith(("http://", "https://")) else f"{self._base_url}/{path.lstrip('/')}"
        policy = retry if retry is not None else self._retry
        kwargs.setdefault("timeout", timeout if timeout is not None else self._timeout)

        def _send() -> requests.Response:
            self._apply_auth()
            try:
                return self.session.request(
                    method.upper(), url, params=params,
                    json=json_body, data=data, headers=dict(headers) if headers else None,
                    **kwargs,
                )
            except requests.RequestException as exc:
                raise wrap_requests_error(exc) from exc

        started = time.monotonic()
        refreshed = False
        response = self._send_with_retry(_send, policy)
        # 401 → 刷新凭证重试一次(不计入普通重试)
        if response.status_code == 401 and self._auth is not None and self._auth.refresh(self.session):
            refreshed = True
            response = self._send_with_retry(_send, policy)
        self._log_summary(method, url, response, started, json_body, data)
        should_raise = raise_for_status if raise_for_status is not None else True
        if should_raise:
            self._raise_for_status(response)
        if refreshed:
            _logger.debug("request succeeded after credential refresh: %s %s", method, path)
        return response

    def _send_with_retry(
        self,
        send: Callable[[], requests.Response],
        policy: RetryPolicy,
    ) -> requests.Response:
        """按策略执行发送:网络异常与可重试状态码都触发重试。"""

        def attempt() -> requests.Response:
            response = send()
            if response.status_code in RETRYABLE_STATUS:
                # 让 RetryPolicy 的结果谓词接手“状态码重试”
                response.raise_for_status()
            return response

        inner = RetryPolicy(
            retries=policy.retries,
            interval=policy.interval,
            backoff=policy.backoff,
            max_interval=policy.max_interval,
            exceptions=policy.exceptions + (requests.HTTPError,),
        )
        try:
            return inner.execute(attempt, description=f"{self._base_url}")
        except requests.HTTPError as exc:
            response = exc.response
            if response is not None and response.status_code in RETRYABLE_STATUS:
                # 可重试状态耗尽:返回响应交由 raise_for_status 逻辑处理
                return response
            raise

    def _apply_auth(self) -> None:
        if self._auth is not None:
            self._auth.apply(self.session)

    @staticmethod
    def _raise_for_status(response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise ApiHTTPStatusError(response) from exc

    # ------------------------------------------------------------- 日志

    def _log_summary(
        self,
        method: str,
        url: str,
        response: requests.Response,
        started: float,
        json_body: Optional[Any],
        data: Optional[Any],
    ) -> None:
        """打一行请求摘要;体内容截断并脱敏。"""
        duration_ms = (time.monotonic() - started) * 1000
        req_body = json_body if json_body is not None else data
        try:
            req_text = json.dumps(req_body, ensure_ascii=False) if not isinstance(req_body, (str, bytes, type(None))) else str(req_body or "")
        except (TypeError, ValueError):
            req_text = "<unserializable>"
        resp_text = response.text or ""
        line = (
            f"{method.upper()} {sanitized_url(url, self._sanitizer)} -> "
            f"{response.status_code} ({duration_ms:.0f}ms) "
            f"req={_truncate(self._sanitizer.mask_text(req_text), self._log_body_bytes)} "
            f"resp={_truncate(self._sanitizer.mask_text(resp_text), self._log_body_bytes)}"
        )
        _logger.info(line)

    # ------------------------------------------------------------- 便捷

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        """GET 便捷方法,参数同 :meth:`request`。"""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        """POST 便捷方法,参数同 :meth:`request`。"""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        """PUT 便捷方法,参数同 :meth:`request`。"""
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        """PATCH 便捷方法,参数同 :meth:`request`。"""
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        """DELETE 便捷方法,参数同 :meth:`request`。"""
        return self.request("DELETE", path, **kwargs)

    # ------------------------------------------------------------- 生命周期

    def close(self) -> None:
        """关闭底层 session(连接池释放)。"""
        self.session.close()

    def __enter__(self) -> "BaseClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text or "-"
    return text[:limit] + f"...({len(text)}B)"


def sanitized_url(url: str, sanitizer: Sanitizer) -> str:
    """遮蔽 URL query 中的敏感参数(如 ?token=xxx / ?sign=yyy)。"""
    if "?" not in url:
        return url
    base, _, query = url.partition("?")
    masked = sanitizer.mask_text(query.replace("&", " ").replace("=", "="))
    return f"{base}?{masked.replace(' ', '&')}"
