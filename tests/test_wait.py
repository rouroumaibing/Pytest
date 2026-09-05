"""Unit tests for the polling utility (WaitHelper)."""

from __future__ import annotations

import pytest
from testkit import WaitHelper, WaitTimeout


def test_until_truthy():
    counter = {"n": 0}

    def cond():
        counter["n"] += 1
        return counter["n"] >= 3

    result = WaitHelper(interval=0.01).until(cond)
    assert result is True


def test_until_expected_value():
    counter = {"n": 0}

    def cond():
        counter["n"] += 1
        return counter["n"]

    result = WaitHelper(interval=0.01).until(cond, expected=2)
    assert result == 2


def test_until_timeout_raises():
    with pytest.raises(WaitTimeout):
        WaitHelper(timeout=0.05, interval=0.01).until(lambda: False)


def test_until_timeout_carries_last_value():
    def cond():
        return "still-waiting"

    with pytest.raises(WaitTimeout) as exc_info:
        WaitHelper(timeout=0.05, interval=0.01).until(cond, expected="done")
    assert exc_info.value.last_value == "still-waiting"


def test_until_tolerates_exceptions():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return True

    assert WaitHelper(interval=0.01).until(flaky) is True


def test_until_false_value_not_satisfying_by_default():
    # A False-y return value is not considered satisfied by default.
    with pytest.raises(WaitTimeout):
        WaitHelper(timeout=0.05, interval=0.01).until(lambda: 0)


def test_until_false_is_satisfying_when_expected():
    result = WaitHelper(interval=0.01).until(lambda: False, expected=False)
    assert result is False


def test_until_true_wrapper():
    counter = {"n": 0}

    def cond():
        counter["n"] += 1
        return counter["n"] >= 2

    assert WaitHelper(interval=0.01).until_true(cond) is True


def test_refresh_fn_invoked_during_long_poll():
    refreshed = {"n": 0}
    counter = {"n": 0}

    def cond():
        counter["n"] += 1
        return counter["n"] >= 5

    def refresh():
        refreshed["n"] += 1

    WaitHelper(interval=0.01, refresh_fn=refresh, refresh_interval=0.02).until(cond)
    assert refreshed["n"] >= 1


def test_wait_timeout_subclasses_timeout_error():
    assert issubclass(WaitTimeout, TimeoutError)
