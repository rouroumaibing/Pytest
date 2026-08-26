"""框架级异常体系。

所有子模块的异常均继承 :class:`ATFError`,调用方可仅捕获一个基类,
也可按模块精细捕获。各模块异常在其所属包内定义(如 ``atf.http.exceptions``),
但根类型统一收敛到这里,保证“模块可独立使用”的同时语义一致。
"""

from __future__ import annotations


class ATFError(Exception):
    """框架所有异常的基类。"""


class ConfigError(ATFError):
    """配置文件缺失、格式非法、环境不存在或校验失败。"""


class ResourcePoolError(ATFError):
    """资源池操作失败基类。"""


class ResourceNotFoundError(ResourcePoolError):
    """指定资源 ID 在池中不存在。"""


class ResourceExhaustedError(ResourcePoolError):
    """在给定重试次数内未能分配到满足条件的资源。"""


class ResourceStateError(ResourcePoolError):
    """资源状态与操作不匹配(如释放他人占用的资源)。"""


class FixtureGuardError(ATFError):
    """SharedFixtureGuard 状态异常或等待超时。"""


class SSHError(ATFError):
    """SSH 执行器异常基类(细分见 ``atf.ssh.executor``)。"""


class ApiError(ATFError):
    """HTTP 客户端异常基类(细分见 ``atf.http.exceptions``)。"""
