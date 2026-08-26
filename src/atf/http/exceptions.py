"""HTTP 客户端统一异常辅助。

框架异常体系收敛在 :mod:`atf.exceptions`,此处仅提供把 ``requests`` 异常
翻译为 :class:`~atf.exceptions.TransportError` / :class:`~atf.exceptions.HTTPStatusError`
的辅助函数;调用方统一捕获根异常即可。
"""

from __future__ import annotations

import requests

from atf.exceptions import HTTPStatusError, TransportError


def wrap_requests_error(exc: requests.RequestException) -> TransportError:
    """把 requests 异常翻译成框架异常(保留原始异常为 ``__cause__``)。"""
    request = exc.request
    where = getattr(request, "url", "")
    if isinstance(exc, requests.Timeout):
        return TransportError(f"request timed out: {where}")
    if isinstance(exc, requests.ConnectionError):
        return TransportError(f"connection failed: {where}")
    return TransportError(f"request failed: {where or exc}", reason=exc)
