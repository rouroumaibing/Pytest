"""通用重试策略。

ResourcePool(等待资源释放)、BaseClient(网络抖动 / 5xx 重试)共用;
独立使用也完全可以。

核心语义:

- ``retries`` 表示“失败后的额外尝试次数”,总尝试次数 = ``retries + 1``;
- 每次重试前按 ``interval * backoff ** (n-1)`` 休眠,单次休眠不超过 ``max_interval``;
- 可通过 ``exceptions`` 限定哪些异常触发重试,``is_retryable`` 支持“结果不
  满足条件也重试”(例如 acquire 没拿到资源返回 None)。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Type, TypeVar, Union

from atf.exceptions import ATFError
from atf.utils.log import get_logger

T = TypeVar("T")
_logger = get_logger("atf.retry")


class RetryExhaustedError(ATFError):
    """结果谓词在全部尝试次数后仍不满足。"""


@dataclass
class RetryPolicy:
    """声明式重试策略。

    Attributes:
        retries: 失败后的额外尝试次数(0 表示不重试)。
        interval: 首次重试前的基础等待秒数。
        backoff: 退避倍率;1.0 表示固定间隔,2.0 表示指数退避。
        max_interval: 单次休眠的上限秒数。
        exceptions: 触发重试的异常类型。
        is_retryable: 结果级谓词,返回 True 表示该结果需要重试。
    """

    retries: int = 3
    interval: float = 1.0
    backoff: float = 1.0
    max_interval: float = 30.0
    exceptions: tuple[Type[BaseException], ...] = (Exception,)
    is_retryable: Union[Callable[[object], bool], None] = None

    def execute(
        self,
        func: Callable[[], T],
        *,
        description: str = "",
        is_retryable: Union[Callable[[T], bool], None] = None,
    ) -> T:
        """执行 ``func`` 并按策略重试。

        Args:
            func: 无参可调用(请用闭包携带参数)。
            description: 日志中的操作描述,便于定位是哪个动作在重试。
            is_retryable: 覆盖策略级谓词,对返回值判断是否需要重试。

        Returns:
            ``func`` 的返回值(满足非重试条件的那一次)。

        Raises:
            BaseException: 最后一次尝试仍失败时,原样抛出 ``exceptions``
                中匹配的异常;非重试异常立即抛出。
        """
        predicate = is_retryable if is_retryable is not None else self.is_retryable
        what = description or getattr(func, "__name__", "anonymous")
        attempt = 0
        while True:
            attempt += 1
            try:
                result = func()
            except self.exceptions as exc:
                if attempt > self.retries:
                    _logger.error("'%s' failed after %d attempt(s): %s", what, attempt, exc)
                    raise
                delay = self._delay(attempt)
                _logger.debug("'%s' attempt %d failed (%s), retry in %.2fs",
                              what, attempt, f"{type(exc).__name__}: {exc}", delay)
                time.sleep(delay)
                continue
            if predicate is not None and predicate(result):
                if attempt > self.retries:
                    _logger.error("'%s' still unsatisfied after %d attempt(s)", what, attempt)
                    raise RetryExhaustedError(f"'{what}' exhausted after {attempt} attempt(s)")
                delay = self._delay(attempt)
                _logger.debug("'%s' attempt %d unsatisfied, retry in %.2fs",
                              what, attempt, delay)
                time.sleep(delay)
                continue
            return result

    def _delay(self, attempt: int) -> float:
        return min(self.interval * (self.backoff ** (attempt - 1)), self.max_interval)
