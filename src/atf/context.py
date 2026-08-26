"""TestContext:测试级资源注册 + 自动清理。

定位:一个用例(fixture/测试函数)拿到的所有"需要归还/销毁的东西"
都注册进来,结束时按 **LIFO** 自动清理,任何一步失败都不影响后续步骤
(错误被收集并记日志,汇总上报)。

与 SharedFixtureGuard 的典型配合::

    with TestContext("test_x") as ctx:
        with guard.shared("env", create_env) as fx:
            # 把"退出持有"这个动作注册为资源清理,失败也会执行
            ctx.add_finalizer(lambda: guard.release_all(owner=worker_id))
            host = pool.acquire(role="node", owner=worker_id)
            ctx.register(host, lambda h: pool.release(h["id"], owner=worker_id))

零业务耦合:任何带"释放动作"的对象(客户端、执行器、临时文件、
容器句柄……)都能注册,清理动作由调用方用闭包声明。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Tuple, TypeVar

from atf.utils.log import get_logger

T = TypeVar("T")
_logger = get_logger("atf.context")

CleanupResult = Tuple[List[str], List[tuple]]


@dataclass
class _Finalizer:
    description: str
    func: Callable[[], Any]


class TestContext:
    """用例级资源登记簿:LIFO 清理、错误隔离、可重复 cleanup。"""
    __test__ = False  # 非 pytest 测试类

    def __init__(self, name: Optional[str] = None) -> None:
        """创建上下文。

        Args:
            name: 上下文名(通常为用例/fixture 名),用于日志定位。
        """
        self._name = name or "context"
        self._finalizers: List[_Finalizer] = []
        self._closed = False

    @property
    def name(self) -> str:
        """上下文名。"""
        return self._name

    @property
    def pending(self) -> int:
        """尚未清理的注册项数量。"""
        return len(self._finalizers)

    # ------------------------------------------------------------- 注册

    def register(
        self,
        value: T,
        finalizer: Optional[Callable[[T], Any]] = None,
        *,
        description: str = "",
    ) -> T:
        """注册一个资源及其清理动作(原样返回 value,方便链式赋值)。

        Args:
            value: 任意资源对象(客户端、执行器、池中记录……)。
            finalizer: 清理函数,入参为 ``value``;缺省时按顺序尝试
                ``value.close()`` → ``value.release()`` 都没有则不清理
                (仅登记)。
            description: 日志里的资源描述,缺省取 ``value`` 的类型名。

        Returns:
            原样返回 ``value``。
        """
        if self._closed:
            raise RuntimeError("cannot register on an already-cleaned TestContext")
        desc = description or f"{type(value).__name__}@{id(value):#x}"
        if finalizer is not None:
            # 用户显式传的 Callable[[T], Any],闭包捕获 value
            self._finalizers.append(_Finalizer(desc, lambda _f=finalizer, _v=value: _f(_v)))
        else:
            default = _default_finalizer(value)
            if default is not None:
                self._finalizers.append(_Finalizer(desc, default))
            else:
                self._finalizers.append(_Finalizer(desc, None))
        _logger.debug("[%s] registered resource: %s", self._name, desc)
        return value

    def add_finalizer(
        self,
        func: Callable[[], Any],
        *,
        description: str = "",
    ) -> None:
        """注册一个无参清理动作(不需要绑定资源对象时使用)。"""
        if self._closed:
            raise RuntimeError("cannot register on an already-cleaned TestContext")
        desc = description or getattr(func, "__name__", "finalizer")
        self._finalizers.append(_Finalizer(desc, func))
        _logger.debug("[%s] registered finalizer: %s", self._name, desc)

    # ------------------------------------------------------------- 清理

    def cleanup(self) -> CleanupResult:
        """执行全部清理(LIFO),错误被收集而不是抛出。

        返回 ``(executed, failures)``:成功执行的步骤描述列表、失败的
        ``(描述, 异常字符串)`` 列表。幂等:重复调用不会再执行已清理的项。
        """
        executed: List[str] = []
        failures: List[tuple] = []
        started = time.monotonic()
        while self._finalizers:
            item = self._finalizers.pop()
            if item.func is None:
                executed.append(f"{item.description} (noop)")
                continue
            try:
                item.func()
                executed.append(item.description)
                _logger.debug("[%s] cleaned: %s", self._name, item.description)
            except Exception as exc:  # noqa: BLE001 - 单项失败不阻断其余清理
                failures.append((item.description, f"{type(exc).__name__}: {exc}"))
                _logger.warning("[%s] cleanup failed for %s: %s", self._name, item.description, exc)
        self._closed = True
        if failures:
            _logger.error("[%s] cleanup finished with %d failure(s)", self._name, len(failures))
        return executed, failures

    # ------------------------------------------------------------- 协议

    def __enter__(self) -> "TestContext":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.cleanup()

    def __repr__(self) -> str:
        state = "closed" if self._closed else f"{self.pending} pending"
        return f"<TestContext {self._name!r} {state}>"


def _default_finalizer(value: Any) -> Optional[Callable[[], Any]]:
    """为未显式给清理函数的资源挑选默认清理:close() → release()。

    返回无参闭包(已捕获 value),以便 register 统一按 ``() -> None`` 调用。
    """
    for attr in ("close", "release"):
        fn = getattr(value, attr, None)
        if callable(fn):
            return lambda _fn=fn: _fn()
    return None
