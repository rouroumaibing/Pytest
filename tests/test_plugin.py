"""Mock-based unit tests for the testkit pytest plugin (testkit.plugin).

The plugin exposes plain module-level functions, so each hook/fixture is
exercised directly with mocked ``pytest`` objects — no real test sessions are
run, keeping the suite deterministic.
"""

from __future__ import annotations

from unittest import mock

import pytest

# The fixture functions are decorated with ``@pytest.fixture``; unwrap the
# underlying callables so we can invoke them directly (no real test session).
import testkit.plugin as _plugin_module
from testkit.cleanup.resource_cleanup import ResourceCleanup
from testkit.fixture.guard import ConcurrentFixtureGuard
from testkit.plugin import (
    _FAILED_STASH_KEY,
    _item_failed,
    pytest_addoption,
    pytest_configure,
    pytest_runtest_makereport,
)
from testkit.utils.wait import WaitHelper

_testkit_cleanup = _plugin_module.testkit_cleanup._fixture_function
_testkit_wait_helper = _plugin_module.testkit_wait_helper._fixture_function
_testkit_fixture_guard = _plugin_module.testkit_fixture_guard._fixture_function


# --------------------------------------------------------------------------- #
# pytest_addoption
# --------------------------------------------------------------------------- #


def test_pytest_addoption_registers_all_options():
    parser = mock.MagicMock()
    group = mock.MagicMock()
    parser.getgroup.return_value = group

    pytest_addoption(parser)

    parser.getgroup.assert_called_once_with("testkit", "testkit framework options")
    # four --testkit- prefixed options
    assert group.addoption.call_count == 4
    registered = {c.args[0] for c in group.addoption.call_args_list}
    assert registered == {
        "--testkit-verbosity",
        "--testkit-env",
        "--testkit-resume-from",
        "--testkit-skip-cleanup-on-failure",
    }


# --------------------------------------------------------------------------- #
# pytest_configure
# --------------------------------------------------------------------------- #


def test_pytest_configure_applies_verbosity(monkeypatch):
    config = mock.MagicMock()

    def _getoption(name, default=None):
        return {"--testkit-verbosity": 3, "--testkit-env": None}.get(name, default)

    config.getoption.side_effect = _getoption

    with mock.patch("testkit.plugin.setup_logging") as setup_logging:
        pytest_configure(config)

    setup_logging.assert_called_once_with(log_verbosity=3)
    # env not set when no --testkit-env
    assert "TESTKIT_ENV" not in __import__("os").environ


def test_pytest_configure_applies_env_and_skips_logging(monkeypatch):
    config = mock.MagicMock()

    def _getoption(name, default=None):
        return {"--testkit-verbosity": 0, "--testkit-env": "staging"}.get(name, default)

    config.getoption.side_effect = _getoption

    with mock.patch("testkit.plugin.setup_logging") as setup_logging:
        pytest_configure(config)
        setup_logging.assert_not_called()

    assert __import__("os").environ.get("TESTKIT_ENV") == "staging"
    monkeypatch.undo()  # restore env if changed


# --------------------------------------------------------------------------- #
# pytest_runtest_makereport (hookwrapper)
# --------------------------------------------------------------------------- #


def test_makereport_stashes_failed_false_on_call():
    item = mock.MagicMock()
    item.stash = {}

    report = mock.MagicMock()
    report.when = "call"
    report.failed = False
    outcome = mock.MagicMock()
    outcome.get_result.return_value = report

    gen = pytest_runtest_makereport(item, mock.MagicMock())
    next(gen)  # advance to the `yield`
    with pytest.raises(StopIteration):
        gen.send(outcome)

    assert item.stash[_FAILED_STASH_KEY] is False


def test_makereport_stashes_failed_true_on_call():
    item = mock.MagicMock()
    item.stash = {}

    report = mock.MagicMock()
    report.when = "call"
    report.failed = True
    outcome = mock.MagicMock()
    outcome.get_result.return_value = report

    gen = pytest_runtest_makereport(item, mock.MagicMock())
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(outcome)

    assert item.stash[_FAILED_STASH_KEY] is True


def test_makereport_ignores_non_call_phase():
    item = mock.MagicMock()
    item.stash = {}

    report = mock.MagicMock()
    report.when = "setup"
    report.failed = True
    outcome = mock.MagicMock()
    outcome.get_result.return_value = report

    gen = pytest_runtest_makereport(item, mock.MagicMock())
    next(gen)
    with pytest.raises(StopIteration):
        gen.send(outcome)

    # no stash entry written for non-"call" phases
    assert _FAILED_STASH_KEY not in item.stash


# --------------------------------------------------------------------------- #
# _item_failed
# --------------------------------------------------------------------------- #


def test_item_failed_detects_true():
    item = mock.MagicMock()
    item.stash = {_FAILED_STASH_KEY: True}
    assert _item_failed(item) is True


def test_item_failed_detects_false():
    item = mock.MagicMock()
    item.stash = {_FAILED_STASH_KEY: False}
    assert _item_failed(item) is False


def test_item_failed_no_stash_attribute():
    item = mock.MagicMock(spec=[])  # no .stash
    assert _item_failed(item) is False


# --------------------------------------------------------------------------- #
# testkit_cleanup fixture
# --------------------------------------------------------------------------- #


def _make_request(skip: bool, node_stash: dict):
    request = mock.MagicMock()
    request.config.getoption.return_value = skip
    node = mock.MagicMock()
    node.stash = node_stash
    request.node = node
    return request


def test_testkit_cleanup_yields_resource_and_calls_cleanup():
    request = _make_request(skip=False, node_stash={})

    gen = _testkit_cleanup(request)
    cleanup = next(gen)
    assert isinstance(cleanup, ResourceCleanup)
    assert cleanup.skip_cleanup_on_failure is False

    with mock.patch.object(ResourceCleanup, "cleanup") as m_clean:
        with pytest.raises(StopIteration):
            next(gen)
        m_clean.assert_called_once_with(failed=False)


def test_testkit_cleanup_respects_skip_and_failed_node():
    request = _make_request(skip=True, node_stash={_FAILED_STASH_KEY: True})

    gen = _testkit_cleanup(request)
    cleanup = next(gen)
    assert cleanup.skip_cleanup_on_failure is True

    with mock.patch.object(ResourceCleanup, "cleanup") as m_clean:
        with pytest.raises(StopIteration):
            next(gen)
        # node failed=True, so cleanup is invoked with failed=True
        m_clean.assert_called_once_with(failed=True)


# --------------------------------------------------------------------------- #
# testkit_wait_helper fixture
# --------------------------------------------------------------------------- #


def test_testkit_wait_helper_returns_wait_helper():
    helper = _testkit_wait_helper()
    assert isinstance(helper, WaitHelper)


# --------------------------------------------------------------------------- #
# testkit_fixture_guard fixture
# --------------------------------------------------------------------------- #


def test_testkit_fixture_guard_returns_factory(tmp_path):
    factory = _testkit_fixture_guard(tmp_path)
    guard = factory("clusters")
    assert isinstance(guard, ConcurrentFixtureGuard)
    # default name works too
    assert isinstance(factory(), ConcurrentFixtureGuard)
