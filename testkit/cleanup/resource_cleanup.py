"""Test-resource cleanup with LIFO ordering, retries and failure isolation.

Registered cleanup callbacks run in reverse registration order (LIFO) so that
resources depending on each other are torn down in the correct order. A
single cleanup failure never blocks the recovery of remaining resources.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from testkit.logging_setup import get_logger

logger = get_logger("cleanup")


@dataclass
class CleanupResult:
    """Outcome of a single cleanup callback."""

    name: str
    success: bool
    error: Optional[BaseException] = None

    def __bool__(self) -> bool:
        return self.success


@dataclass
class _Entry:
    cleanup_fn: Callable[[], Any]
    wait_fn: Optional[Callable[[], bool]]
    name: str
    timeout: float


class ResourceCleanup:
    """Collects cleanup callbacks and runs them LIFO at teardown.

    Parameters
    ----------
    retry_count:
        Retries for a failing cleanup callback before giving up.
    retry_interval:
        Seconds to wait between cleanup retries.
    skip_cleanup_on_failure:
        When ``True`` and :meth:`cleanup` is called with ``failed=True``, all
        cleanup is skipped so state is preserved for debugging.
    """

    def __init__(
        self,
        retry_count: int = 3,
        retry_interval: float = 1.0,
        skip_cleanup_on_failure: bool = False,
    ) -> None:
        self._stack: list[_Entry] = []
        self._retry_count = retry_count
        self._retry_interval = retry_interval
        self._skip_cleanup_on_failure = skip_cleanup_on_failure

    @property
    def skip_cleanup_on_failure(self) -> bool:
        return self._skip_cleanup_on_failure

    @skip_cleanup_on_failure.setter
    def skip_cleanup_on_failure(self, value: bool) -> None:
        self._skip_cleanup_on_failure = value

    def register(
        self,
        cleanup_fn: Callable[[], Any],
        wait_fn: Optional[Callable[[], bool]] = None,
        name: Optional[str] = None,
        timeout: float = 300.0,
    ) -> "ResourceCleanup":
        """Register a cleanup callback (LIFO).

        Parameters
        ----------
        cleanup_fn:
            Zero-argument callable performing the teardown. Bind arguments
            with ``functools.partial`` or a ``lambda`` if needed.
        wait_fn:
            Optional zero-argument predicate. When provided, it is polled
            after ``cleanup_fn`` succeeds until it returns ``True`` (or
            *timeout*), supporting asynchronous resources whose deletion
            completes in the background.
        name:
            Optional label for reporting.
        timeout:
            Maximum seconds to wait for ``wait_fn`` to return ``True``.

        Returns
        -------
        ResourceCleanup
            ``self``, to allow chaining.
        """
        self._stack.append(
            _Entry(
                cleanup_fn=cleanup_fn,
                wait_fn=wait_fn,
                name=name or getattr(cleanup_fn, "__name__", "cleanup"),
                timeout=timeout,
            )
        )
        return self

    def _run_once(self, entry: _Entry) -> None:
        entry.cleanup_fn()
        if entry.wait_fn is not None:
            deadline = time.monotonic() + entry.timeout
            while not entry.wait_fn():
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"cleanup wait timeout after {entry.timeout}s for {entry.name}"
                    )
                time.sleep(min(1.0, self._retry_interval or 0.5))

    def _run_with_retry(self, entry: _Entry) -> Optional[BaseException]:
        last_exc: Optional[BaseException] = None
        for attempt in range(self._retry_count + 1):
            try:
                self._run_once(entry)
                logger.v2("cleanup succeeded: %s", entry.name)
                return None
            except Exception as exc:  # noqa: BLE001 - cleanup must never raise
                last_exc = exc
                logger.v4("cleanup attempt %d/%d failed for %s: %s",
                          attempt + 1, self._retry_count + 1, entry.name, exc)
                if attempt < self._retry_count:
                    time.sleep(self._retry_interval)
        return last_exc

    def cleanup(self, failed: bool = False) -> list[CleanupResult]:
        """Run all registered cleanups LIFO.

        Parameters
        ----------
        failed:
            Whether the owning test failed. If ``skip_cleanup_on_failure`` is
            set, a ``True`` value here causes cleanup to be skipped entirely.

        Returns
        -------
        list[CleanupResult]
            One result per registered cleanup (in execution order).
        """
        if self._skip_cleanup_on_failure and failed:
            logger.v2("cleanup skipped (skip_cleanup_on_failure=True, test failed)")
            return [CleanupResult(name=e.name, success=False) for e in self._stack]

        results: list[CleanupResult] = []
        for entry in reversed(self._stack):
            error = self._run_with_retry(entry)
            results.append(CleanupResult(name=entry.name, success=error is None, error=error))
            if error is not None:
                # A failure must not block subsequent recoveries.
                logger.v4("cleanup failed but continuing: %s (%s)", entry.name, error)
        self._stack.clear()
        return results

    def __len__(self) -> int:
        return len(self._stack)
