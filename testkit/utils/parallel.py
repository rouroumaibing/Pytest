"""Ordered parallel map utility.

Run independent callables concurrently with :class:`concurrent.futures.
ThreadPoolExecutor` while preserving input order in the result list. Failed or
skipped tasks are omitted so the caller decides how to interpret missing slots.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, TypeVar

from testkit.logging_setup import get_logger

logger = get_logger("utils.parallel")

T = TypeVar("T")
R = TypeVar("R")


def parallel_map(
    func: Callable[[T], R],
    items: Iterable[T],
    max_workers: int = 5,
    timeout: float | None = None,
    on_error: str = "collect",
    desc: str = "tasks",
    error_handler: Callable[[T, Exception], Any] | None = None,
) -> list[R]:
    """Map *func* over *items* concurrently, preserving order.

    Parameters
    ----------
    func:
        Callable applied to each item.
    items:
        Iterable of inputs.
    max_workers:
        Maximum number of worker threads.
    timeout:
        Overall wait timeout in seconds (``None`` = no timeout).
    on_error:
        ``"collect"`` (default) — pre-task exceptions are logged (or converted
        via *error_handler*) and the task is skipped; execution continues.
        ``"raise"`` — the first exception aborts the whole batch.
    desc:
        Human-readable description used in logs.
    error_handler:
        Optional ``(item, exc) -> value`` that converts a failed task into a
        result value (only used with ``on_error="collect"``).

    Returns
    -------
    list[R]
        Results in the same order as *items*. Failed/skipped tasks are omitted,
        so the list may be shorter than *items* when ``on_error="collect"``.
    """
    if on_error not in ("collect", "raise"):
        raise ValueError(f"on_error must be 'collect' or 'raise', got {on_error!r}")

    seq: Sequence[T] = list(items)
    # Only slots that actually produced a value are recorded, so a successful
    # ``None`` result is never confused with a failed/skipped task.
    produced: dict[int, R] = {}

    def _run(index: int, item: T) -> None:
        produced[index] = func(item)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run, i, item): i for i, item in enumerate(seq)}
        try:
            for fut in as_completed(futures, timeout=timeout):
                idx = futures[fut]
                try:
                    fut.result()
                except Exception as exc:  # noqa: BLE001
                    if on_error == "raise":
                        for pending in futures:
                            pending.cancel()
                        raise
                    item = seq[idx]
                    if error_handler is not None:
                        logger.v4("parallel %s task %d failed, using error_handler", desc, idx)
                        produced[idx] = error_handler(item, exc)
                    else:
                        logger.warning("parallel %s task %d failed: %s", desc, idx, exc)
        except Exception as exc:  # noqa: BLE001 - as_completed timeout etc.
            if on_error == "raise":
                raise
            logger.v4("parallel %s aborted: %s", desc, exc)

    # Omit skipped/failed slots; successful results (including ``None``) are kept.
    return [produced[i] for i in range(len(seq)) if i in produced]
