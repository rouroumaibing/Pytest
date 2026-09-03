"""Unit tests for the concurrent fixture guard (ConcurrentFixtureGuard)."""

from __future__ import annotations

import time

import pytest

from testkit import ConcurrentFixtureGuard
from testkit.exceptions import FixtureError


def test_first_caller_becomes_creator(tmp_path):
    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert guard.should_create() is True
    assert guard.should_create() is False
    assert guard.is_creator() is True


def test_other_instance_reuses_created_fixture(tmp_path):
    first = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert first.should_create() is True
    first.mark_created({"cluster": "c-1"})

    other = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert other.should_create() is False
    assert other.is_creator() is False


def test_mark_created_only_by_creator(tmp_path):
    first = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    first.should_create()
    other = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    with pytest.raises(FixtureError):
        other.mark_created()


def test_mark_failed_then_takeover(tmp_path):
    first = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert first.should_create() is True
    first.mark_failed()

    second = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert second.should_create() is True  # failed state allows takeover


def test_mark_failed_only_by_creator(tmp_path):
    first = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    first.should_create()
    other = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    with pytest.raises(FixtureError):
        other.mark_failed()


def test_wait_until_created_uses_user_predicate(tmp_path):
    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json", poll_interval=0.01)
    ready = {"yes": False}
    assert guard.wait_until_created(lambda: ready["yes"], timeout=0.05) is False
    ready["yes"] = True
    assert guard.wait_until_created(lambda: ready["yes"], timeout=0.5) is True


def test_wait_until_created_tolerates_exceptions(tmp_path):
    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json", poll_interval=0.01)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        return True

    assert guard.wait_until_created(flaky, timeout=1.0) is True


def test_wait_until_created_aborts_on_failure(tmp_path):
    creator = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    creator.should_create()
    creator.mark_failed()

    waiter = ConcurrentFixtureGuard(tmp_path / "fixture.json", poll_interval=0.01)
    with pytest.raises(FixtureError):
        waiter.wait_until_created(lambda: False, timeout=1.0)


def test_timeout_takeover(tmp_path):
    # A stale 'creating' state older than the timeout is taken over.
    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json", timeout=0.05)
    assert guard.should_create() is True

    time.sleep(0.1)
    other = ConcurrentFixtureGuard(tmp_path / "fixture.json", timeout=0.05)
    assert other.should_create() is True  # took over the stale creation


def test_cleanup_only_by_creator(tmp_path):
    first = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    first.should_create()
    first.mark_created()

    other = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert other.cleanup() is False  # non-creator cannot clean up

    assert first.cleanup() is True  # creator cleans up
    assert not (tmp_path / "fixture.json").exists()


def test_exclusive_section_is_serializable(tmp_path):
    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    with guard.exclusive():
        pass  # acquiring the exclusive lock must not raise
