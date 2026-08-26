"""HTTP 客户端统一异常。

所有异常继承 :class:`atf.exceptions.ApiError`,调用方可只捕获 ``ApiError``
拿到带 ``requests`` 原始异常/响应的上下文,也可细分捕获。
"""

from __future__ import annotations

from typing import Optional

import requests

from atf.exceptions import ApiError


class ApiTransportError(ApiError):
    """网络层失败(连接拒绝、DNS、SSL 等),不含 HTTP 状态码问题。

    Attributes:
        reason: requests 抛出的原始异常。
    """

    def __init__(self, message: str, reason: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.reason = reason


class ApiTimeoutError(ApiTransportError):
    """请求超时(连接或读取)。"""


class ApiHTTPStatusError(ApiError):
    """HTTP 状态码非 2xx(仅在 raise_for_status 开启时抛出)。

    Attributes:
        response: 完整的 :class:`requests.Response`。
    """

    def __init__(self, response: requests.Response) -> None:
        self.response = response
        super().__init__(
            f"HTTP {response.status_code} {response.reason} for "
            f"{response.request.method} {response.url}"
        )


def wrap_requests_error(exc: requests.RequestException) -> ApiError:
    """把 requests 异常翻译成框架异常(保留原始异常为 __cause__)。"""
    request = exc.request
    where = getattr(request, "url", "")
    if isinstance(exc, requests.Timeout):
        return ApiTimeoutError(f"request timed out: {where}")
    if isinstance(exc, requests.ConnectionError):
        return ApiTransportError(f"connection failed: {where}")
    return ApiTransportError(f"request failed: {where or exc}", reason=exc)
