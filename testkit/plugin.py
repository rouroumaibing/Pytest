"""Auto-registered pytest plugin.

Registers itself through the ``pytest11`` entry point, so a fresh project that
only ``pip install testkit`` (no ``conftest.py``) immediately gains:

* CLI options, all ``--testkit-`` prefixed:
  ``--testkit-verbosity``, ``--testkit-env``, ``--testkit-resume-from``,
  ``--testkit-skip-cleanup-on-failure``
* Fixtures, all ``testkit_`` prefixed:
  ``testkit_cleanup``, ``testkit_wait_helper``, ``testkit_fixture_guard``
* Automatic test-failure detection via ``pytest_runtest_makereport``
  (``hookwrapper=True`` + ``yield``), stashing the outcome on the item so that
  ``ResourceCleanup.skip_cleanup_on_failure`` works with zero user wiring.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from testkit.cleanup.resource_cleanup import ResourceCleanup
from testkit.fixture.guard import ConcurrentFixtureGuard
from testkit.logging_setup import setup_logging
from testkit.utils.wait import WaitHelper

# Stash key used to record per-item failure on ``item.stash``.
_FAILED_STASH_KEY = pytest.StashKey[bool]()
_ENV_OVERRIDE = "TESTKIT_ENV"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register all ``--testkit-`` prefixed CLI options."""
    group = parser.getgroup("testkit", "testkit framework options")
    group.addoption(
        "--testkit-verbosity",
        action="store",
        type=int,
        default=0,
        help="Framework log verbosity (0-5), k8s --v=N style.",
    )
    group.addoption(
        "--testkit-env",
        action="store",
        default=None,
        metavar="NAME",
        help="Environment name for ConfigRegistry (sets TESTKIT_ENV).",
    )
    group.addoption(
        "--testkit-resume-from",
        action="store",
        default=None,
        metavar="STAGE",
        help="Pipeline stage name to resume from.",
    )
    group.addoption(
        "--testkit-skip-cleanup-on-failure",
        action="store_true",
        default=False,
        help="Skip resource cleanup when a test fails (preserve state for debugging).",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Apply verbosity and environment-name options at session start."""
    verbosity = config.getoption("--testkit-verbosity", default=0)
    if verbosity:
        setup_logging(log_verbosity=int(verbosity))

    env_name = config.getoption("--testkit-env", default=None)
    if env_name:
        os.environ[_ENV_OVERRIDE] = env_name


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    """Auto-detect test failure and stash it on the item.

    The ``hookwrapper`` + ``yield`` pattern lets us read the final report. The
    ``testkit_cleanup`` fixture then reads this stash at teardown, so failure
    detection requires no user code.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.stash[_FAILED_STASH_KEY] = bool(report.failed)


def _item_failed(item: pytest.Item) -> bool:
    stash = getattr(item, "stash", None)
    return bool(stash and stash.get(_FAILED_STASH_KEY, False))


@pytest.fixture
def testkit_cleanup(request: pytest.FixtureRequest) -> Iterator[ResourceCleanup]:
    """Yield a :class:`ResourceCleanup` that auto-runs (LIFO) at teardown.

    The test outcome is detected automatically, so
    ``--testkit-skip-cleanup-on-failure`` behaves correctly with no user wiring.
    """
    skip = bool(request.config.getoption("--testkit-skip-cleanup-on-failure", default=False))
    cleanup = ResourceCleanup(skip_cleanup_on_failure=skip)
    yield cleanup
    cleanup.cleanup(failed=_item_failed(request.node))


@pytest.fixture
def testkit_wait_helper() -> WaitHelper:
    """Return a ready-to-use :class:`WaitHelper`."""
    return WaitHelper()


@pytest.fixture
def testkit_fixture_guard(tmp_path: Any) -> Callable[..., ConcurrentFixtureGuard]:
    """Return a factory for :class:`ConcurrentFixtureGuard` bound to a temp state file.

    Usage::

        def test_shared(guard):
            g = guard("clusters")          # -> ConcurrentFixtureGuard
            if g.should_create():
                ...                        # build shared fixture
                g.mark_created()
    """

    def _make(name: str = "shared", **kwargs: Any) -> ConcurrentFixtureGuard:
        state_path = tmp_path / f"testkit-{name}-state.json"
        return ConcurrentFixtureGuard(str(state_path), **kwargs)

    return _make
