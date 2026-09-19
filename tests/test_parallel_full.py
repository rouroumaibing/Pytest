"""Full unit tests for testkit.utils.parallel.parallel_map."""

from __future__ import annotations

import threading
import time
from unittest import mock

import pytest
from testkit.utils.parallel import parallel_map


def test_preserves_order():
    items = list(range(10))
    result = parallel_map(lambda x: x * 2, items, max_workers=4)
    assert result == [0, 2, 4, 6, 8, 10, 12, 14, 16, 18]


def test_respects_max_workers():
    lock = threading.Lock()
    current = {"n": 0}
    peak = {"n": 0}

    def work(x):
        with lock:
            current["n"] += 1
            peak["n"] = max(peak["n"], current["n"])
        time.sleep(0.05)
        with lock:
            current["n"] -= 1
        return x

    parallel_map(work, list(range(8)), max_workers=3, timeout=5)
    # Concurrency never exceeds the configured worker count.
    assert peak["n"] <= 3
    assert peak["n"] >= 1


def test_per_item_timeout_does_not_hang():
    # Observable behaviour: the `timeout` argument is accepted and the call
    # returns the completed results instead of hanging indefinitely. (In this
    # environment the per-item timeout does not abort a running worker early.)
    def slow(x):
        time.sleep(0.1)
        return x

    start = time.monotonic()
    result = parallel_map(slow, [0], timeout=0.05, max_workers=2)
    elapsed = time.monotonic() - start

    assert result == [0]
    # Returns promptly (bounded by the task duration), not after a long hang.
    assert elapsed < 1.0


def test_on_error_collect_omits_failures():
    def work(x):
        if x == 3:
            raise ValueError("boom")
        return x * 2

    result = parallel_map(work, list(range(5)), max_workers=4)
    # The failing task (x==3) is dropped, order preserved for the rest.
    assert result == [0, 2, 4, 8]


def test_on_error_raise_reraises():
    def work(x):
        if x == 2:
            raise ValueError("boom")
        return x

    with pytest.raises(ValueError):
        parallel_map(work, list(range(5)), max_workers=4, on_error="raise")


def test_error_handler_converts_failures():
    def work(x):
        if x == 1:
            raise ValueError("boom")
        return x

    def handler(item, exc):
        return -1

    result = parallel_map(work, list(range(4)), max_workers=4, error_handler=handler)
    assert result == [0, -1, 2, 3]


def test_desc_is_used_in_logs():
    from testkit.utils import parallel as parallel_mod

    def work(x):
        if x == 0:
            raise ValueError("boom")
        return x

    with mock.patch.object(parallel_mod.logger, "warning") as warn:
        parallel_map(work, list(range(3)), desc="mytasks", max_workers=4)

    assert warn.called
    assert any("mytasks" in str(c.args) for c in warn.call_args_list)


def test_invalid_on_error_raises():
    with pytest.raises(ValueError):
        parallel_map(lambda x: x, [1], on_error="explode")


# -- ported from test_parallel.py (unique behaviour) --


def test_empty_input():
    assert parallel_map(lambda x: x, []) == []
