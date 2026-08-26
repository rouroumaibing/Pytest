"""框架级异常体系(核心收敛为少量可组合异常)。

所有异常继承 :class:`ATFError`,调用方可仅捕获基类,也可按语义精细捕获:

- :class:`TransportError` —— 网络/连接/SSH/HTTP 传输层失败(连接拒绝、超时、TLS 等);
- :class:`HTTPStatusError` —— HTTP 状态码非 2xx(携带完整 ``response``);
- :class:`ConfigError` —— 配置缺失/非法/校验失败;
- :class:`FixtureGuardError` —— 共享 fixture 状态异常或等待超时;
- :class:`ResourcePoolError` —— 资源池操作失败(含 3 个细分子类)。

模块可独立使用,但根类型统一收敛于此,保证语义一致。
"""

from __future__ import annotations

from typing import Optional

import requests


class ATFError(Exception):
    """框架所有异常的基类。"""


class TransportError(ATFError):
    """网络/连接/SSH/HTTP 传输层失败(不含 HTTP 状态码问题)。

    Attributes:
        reason: 可选的原始异常(requests 抛出的)。
    """

    def __init__(self, message: str, reason: Optional[BaseException] = None) -> None:
        super().__init__(message)
        self.reason = reason


class HTTPStatusError(ATFError):
    """HTTP 状态码非 2xx(``raise_for_status`` 开启时抛出)。

    Attributes:
        response: 完整的 :class:`requests.Response`。
    """

    def __init__(self, response: requests.Response) -> None:
        self.response = response
        super().__init__(
            f"HTTP {response.status_code} {response.reason} for "
            f"{response.request.method} {response.url}"
        )


class ConfigError(ATFError):
    """配置文件缺失、格式非法、环境不存在或校验失败。"""


class FixtureGuardError(ATFError):
    """SharedFixtureGuard 状态异常或等待超时。"""


class ResourcePoolError(ATFError):
    """资源池操作失败基类。"""


class ResourceNotFoundError(ResourcePoolError):
    """指定资源 ID 在池中不存在。"""


class ResourceExhaustedError(ResourcePoolError):
    """在给定重试次数内未能分配到满足条件的资源。"""


class ResourceStateError(ResourcePoolError):
    """资源状态与操作不匹配(如释放他人占用的资源)。"""
