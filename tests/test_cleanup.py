"""Unit tests for test-resource cleanup (ResourceCleanup)."""

from __future__ import annotations

from testkit import ResourceCleanup


def test_cleanup_runs_lifo():
    order = []
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)
    cleanup.register(lambda: order.append("first"))
    cleanup.register(lambda: order.append("second"))
    cleanup.cleanup()
    assert order == ["second", "first"]


def test_cleanup_failure_does_not_block_others():
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)

    def boom():
        raise RuntimeError("fail")

    cleanup.register(boom, name="bad")
    cleanup.register(lambda: None, name="good")
    results = cleanup.cleanup()
    # LIFO: "good" (registered last) runs first.
    assert results[0].name == "good"
    assert results[0].success is True
    assert results[1].name == "bad"
    assert results[1].success is False


def test_cleanup_result_bool():
    from testkit.cleanup.resource_cleanup import CleanupResult

    assert bool(CleanupResult("x", True)) is True
    assert bool(CleanupResult("x", False)) is False


def test_cleanup_retries_then_gives_up():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        raise RuntimeError("still failing")

    cleanup = ResourceCleanup(retry_count=2, retry_interval=0.01)
    cleanup.register(flaky, name="flaky")
    results = cleanup.cleanup()
    assert results[0].success is False
    assert attempts["n"] == 3  # 1 initial + 2 retries


def test_cleanup_succeeds_on_retry():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise RuntimeError("transient")
        return None

    cleanup = ResourceCleanup(retry_count=3, retry_interval=0.01)
    cleanup.register(flaky, name="flaky")
    results = cleanup.cleanup()
    assert results[0].success is True
    assert attempts["n"] == 2


def test_skip_cleanup_on_failure():
    ran = []
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0, skip_cleanup_on_failure=True)
    cleanup.register(lambda: ran.append("x"))
    cleanup.cleanup(failed=True)
    assert ran == []


def test_skip_cleanup_flag_off_does_not_skip():
    ran = []
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0, skip_cleanup_on_failure=False)
    cleanup.register(lambda: ran.append("x"))
    cleanup.cleanup(failed=True)
    assert ran == ["x"]


def test_wait_fn_polls_until_ready():
    ready = {"yes": False}
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0.01)
    cleanup.register(lambda: ready.update(yes=True), wait_fn=lambda: ready["yes"], timeout=1.0)
    results = cleanup.cleanup()
    assert results[0].success is True


def test_register_returns_self_for_chaining():
    cleanup = ResourceCleanup()
    assert cleanup.register(lambda: None) is cleanup
    assert len(cleanup) == 1


def test_cleanup_clears_stack():
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)
    cleanup.register(lambda: None)
    cleanup.cleanup()
    assert len(cleanup) == 0


# -- ported from test_cleanup_full.py / test_cleanup_enhance.py (unique behaviours) --


def test_remove_guard_against_double_free():
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)
    cleanup.register(lambda: None, name="a")
    cleanup.remove("a")
    cleanup.remove("a")  # removing again is a no-op, not an error
    assert len(cleanup) == 0


def test_pending_count_and_len():
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)
    cleanup.register(lambda: None, name="a")
    cleanup.register(lambda: None, name="b")
    assert len(cleanup) == 2
    assert cleanup.pending_count == 2


def test_cleanup_isolates_raising_callbacks():
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)

    def boom():
        raise RuntimeError("fail")

    cleanup.register(boom, name="bad")
    cleanup.register(lambda: None, name="good")
    results = cleanup.cleanup()
    assert results[0].name == "good"
    assert results[0].success is True
    assert results[1].name == "bad"
    assert results[1].success is False
