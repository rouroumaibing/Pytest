"""Full unit tests for testkit.utils.wait (WaitHelper / WaitTimeout)."""

from __future__ import annotations

import time

import pytest
from testkit.exceptions import ResourceNotFoundError
from testkit.utils.wait import WaitHelper, WaitTimeout


def test_until_succeeds_before_timeout():
    w = WaitHelper(timeout=1.0, interval=0.01)
    calls = {"n": 0}

    def cond():
        calls["n"] += 1
        return calls["n"] >= 3

    assert w.until(cond) is True


def test_until_timeout_raises_wait_timeout():
    w = WaitHelper(timeout=0.03, interval=0.01)
    with pytest.raises(WaitTimeout):
        w.until(lambda: False)


def test_until_timeout_carries_last_value():
    w = WaitHelper(timeout=0.03, interval=0.01)

    def cond():
        raise ValueError("transient")

    with pytest.raises(WaitTimeout) as exc_info:
        w.until(cond)

    # The exception raised by the condition is captured as last_value.
    assert isinstance(exc_info.value.last_value, ValueError)


def test_until_with_expected_param():
    w = WaitHelper(timeout=0.03, interval=0.01)
    with pytest.raises(WaitTimeout) as exc_info:
        w.until(lambda: 1, expected=2)
    assert exc_info.value.last_value == 1

    w2 = WaitHelper(timeout=0.5, interval=0.01)
    assert w2.until(lambda: 2, expected=2) == 2


def test_until_respects_timeout_bound():
    w = WaitHelper(timeout=0.03, interval=0.01)
    start = time.monotonic()
    with pytest.raises(WaitTimeout):
        w.until(lambda: False)
    elapsed = time.monotonic() - start
    # Should give up near the timeout, not hang.
    assert elapsed < 1.0


def test_until_true_convenience():
    w = WaitHelper(timeout=0.5, interval=0.01)
    assert w.until_true(lambda: True) is True

    w2 = WaitHelper(timeout=0.03, interval=0.01)
    with pytest.raises(WaitTimeout):
        w2.until_true(lambda: False)


def test_wait_until_deleted_becomes_false():
    w = WaitHelper(timeout=0.5, interval=0.01)
    calls = {"n": 0}

    def exists():
        calls["n"] += 1
        return calls["n"] < 3  # present twice, then gone

    assert w.wait_until_deleted(exists) is True


def test_wait_until_deleted_resource_not_found():
    w = WaitHelper(timeout=0.5, interval=0.01)

    def exists():
        raise ResourceNotFoundError("pod", "xyz")

    assert w.wait_until_deleted(exists) is True


def test_wait_until_deleted_status_code_404():
    w = WaitHelper(timeout=0.5, interval=0.01)

    class _NotFound(Exception):
        status_code = 404

    def exists():
        raise _NotFound()

    assert w.wait_until_deleted(exists) is True


def test_wait_until_deleted_still_present_at_timeout():
    w = WaitHelper(timeout=0.03, interval=0.01)

    def exists():
        return True

    assert w.wait_until_deleted(exists) is False
