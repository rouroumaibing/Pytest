"""atf —— 通用 REST API + SSH 自动化测试框架。

零业务耦合:各模块(requests 封装、paramiko 封装、资源池、配置加载、
fixture 互斥、测试上下文)均可独立使用,不依赖被测系统细节。
"""

from atf.context import TestContext
from atf.exceptions import (
    ATFError,
    ConfigError,
    FixtureGuardError,
    HTTPStatusError,
    ResourcePoolError,
    TransportError,
)

__version__ = "0.1.0"

__all__ = [
    "ATFError",
    "TestContext",
    "__version__",
    "TransportError",
    "HTTPStatusError",
    "ConfigError",
    "FixtureGuardError",
    "ResourcePoolError",
]
