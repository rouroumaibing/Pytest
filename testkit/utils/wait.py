"""Generic asynchronous polling utility (:class:`WaitHelper`).

The helper polls a caller-supplied condition until it yields an expected
value or a timeout elapses. Condition exceptions never interrupt polling, and
an optional token-refresh callback keeps credentials alive during long waits.
The framework imposes no business state machine — readiness is defined by the
caller.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from testkit.exceptions import ResourceNotFoundError
from testkit.logging_setup import get_logger

logger = get_logger("utils.wait")

_MISSING = object()


class WaitTimeout(TimeoutError):
    """Raised when a poll condition is not satisfied within the timeout."""

    def __init__(self, message: str, last_value: Any = None) -> None:
        super().__init__(message)
        self.last_value = last_value


class WaitHelper:
    """Poll a condition until satisfied or timed out.

    Parameters
    ----------
    timeout:
        Default total wait time (seconds).
    interval:
        Default sleep between polls (seconds).
    refresh_fn:
        Optional zero-argument callable invoked periodically during a long
        wait (e.g. to refresh an expiring token).
    refresh_interval:
        Seconds between ``refresh_fn`` invocations (default 300).
    """

    def __init__(
        self,
        timeout: float = 60.0,
        interval: float = 1.0,
        refresh_fn: Callable[[], None] | None = None,
        refresh_interval: float = 300.0,
    ) -> None:
        self._timeout = timeout
        self._interval = interval
        self._refresh_fn = refresh_fn
        self._refresh_interval = refresh_interval

    def until(
        self,
        condition: Callable[[], Any],
        expected: Any = _MISSING,
        timeout: float | None = None,
        interval: float | None = None,
    ) -> Any:
        """Poll ``condition`` until it returns *expected* (or a truthy value).

        Parameters
        ----------
        condition:
            Zero-argument callable returning the value to compare.
        expected:
            Value to match. When omitted, any truthy value satisfies the wait.
        timeout:
            Override the default timeout.
        interval:
            Override the default poll interval.

        Returns
        -------
        Any
            The value returned by ``condition`` when it satisfied the wait.

        Raises
        ------
        WaitTimeout
            If the condition is not satisfied before the timeout.
        """
        deadline = time.monotonic() + (self._timeout if timeout is None else timeout)
        delay = self._interval if interval is None else interval
        last_value: Any = None
        last_refresh = time.monotonic()

        while True:
            try:
                value = condition()
                last_value = value
                if expected is _MISSING:
                    satisfied = bool(value)
                else:
                    satisfied = value == expected
                if satisfied:
                    return value
            except Exception as exc:  # noqa: BLE001 - tolerate transient errors
                last_value = exc
                logger.v5("poll condition raised (continuing): %s", exc)

            if (
                self._refresh_fn is not None
                and time.monotonic() - last_refresh >= self._refresh_interval
            ):
                logger.v2("refreshing credentials during long poll")
                self._refresh_fn()
                last_refresh = time.monotonic()

            if time.monotonic() >= deadline:
                raise WaitTimeout("poll condition not satisfied within timeout", last_value)

            time.sleep(delay)

    def until_true(
        self,
        condition: Callable[[], Any],
        timeout: float | None = None,
        interval: float | None = None,
    ) -> Any:
        """Convenience wrapper: wait until ``condition`` returns a truthy value."""
        return self.until(condition, timeout=timeout, interval=interval)

    def wait_until_deleted(
        self,
        check_exists: Callable[[], bool],
        timeout: float | None = None,
    ) -> bool:
        """Poll until a resource no longer exists (proven by business usages).

        Parameters
        ----------
        check_exists:
            Callable returning ``True`` while the resource still exists. A
            raised :class:`~testkit.exceptions.ResourceNotFoundError` (or any
            exception carrying ``status_code == 404``) also counts as deleted.
        timeout:
            Override the default timeout.

        Returns
        -------
        bool
            ``True`` if the resource is gone within *timeout*, ``False`` if it
            is still present when the timeout elapses.
        """

        def _deleted() -> bool:
            try:
                return not check_exists()
            except ResourceNotFoundError:
                return True
            except Exception as exc:  # noqa: BLE001 - 404 also means deleted
                if getattr(exc, "status_code", None) == 404:
                    return True
                raise

        try:
            self.until(_deleted, expected=True, timeout=timeout)
            return True
        except WaitTimeout:
            return False
