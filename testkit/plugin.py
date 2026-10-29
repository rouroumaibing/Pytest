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
* The ``testkit_depends_on`` marker: a test marked with it is skipped (never
  failed) when a case it depends on failed, skipped, or has no result yet.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from testkit.cleanup.resource_cleanup import ResourceCleanup
from testkit.fixture.guard import ConcurrentFixtureGuard
from testkit.logging_setup import setup_logging
from testkit.utils.wait import WaitHelper

# Stash key used to record per-item failure on ``item.stash``.
_FAILED_STASH_KEY = pytest.StashKey[bool]()
# Per-session registry: nodeid -> "passed" | "failed" | "skipped".
_RESULTS_KEY = pytest.StashKey[dict[str, str]]()
# Per-session set of every collected nodeid (for dependency-name resolution).
_COLLECTED_KEY = pytest.StashKey[set[str]]()
# Per-session map: nodeid -> xdist group name or None (for cross-worker checks).
_XDIST_GROUP_KEY = pytest.StashKey[dict[str, str | None]]()
# Per-session set of already-emitted dependency warnings (de-duplication).
_WARNED_KEY = pytest.StashKey[set[str]]()
_ENV_OVERRIDE = "TESTKIT_ENV"
_DEPENDS_ON_MARKER = "testkit_depends_on"


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

    config.addinivalue_line(
        "markers",
        "testkit_depends_on(*names): skip this test when any named test did "
        "not pass (failed, skipped, or no result yet).",
    )
    config.stash[_RESULTS_KEY] = {}
    config.stash[_WARNED_KEY] = set()


def pytest_collection_modifyitems(
    session: pytest.Session, config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Record every collected nodeid and its xdist group for dependency checks."""
    config.stash[_COLLECTED_KEY] = {item.nodeid for item in items}
    config.stash[_XDIST_GROUP_KEY] = {item.nodeid: _xdist_group_name(item) for item in items}


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[Any]) -> Any:
    """Auto-detect test failure, stash it, and record the outcome.

    The ``hookwrapper`` + ``yield`` pattern lets us read the final report. The
    ``testkit_cleanup`` fixture reads the failure stash at teardown, and the
    ``testkit_depends_on`` evaluation reads the outcome registry at setup, so
    neither requires any user code.
    """
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item.stash[_FAILED_STASH_KEY] = bool(report.failed)
        _record_outcome(item, "passed" if report.passed else "failed")
    elif report.when == "setup" and report.skipped:
        _record_outcome(item, "skipped")
    elif report.when == "setup" and report.failed:
        _record_outcome(item, "failed")


def _record_outcome(item: pytest.Item, outcome: str) -> None:
    item.config.stash[_RESULTS_KEY][item.nodeid] = outcome


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Evaluate ``testkit_depends_on`` before the dependent test runs.

    A dependency whose latest outcome is not ``passed`` (failed, skipped, or
    no result recorded yet - e.g. it runs later or never) causes a skip, so
    related cases never run on top of a broken scene and never turn the
    original failure into their own.

    Two best-effort warnings guard against silent pseudo-green outcomes:

    * A dependency name that matches no collected test nodeid (typo, or a bare
      name pointing at a parametrized variant whose nodeid carries a ``[...]``
      suffix) skip the dependent without any signal - a warning is emitted in
      the non-xdist case where the collection set is authoritative.
    * Under pytest-xdist, the registry is per-process. If the dependency may
      run on another worker (its nodeid is absent here, or it is not in the
      same ``xdist_group`` as the dependent), the same silent skip can occur -
      a warning is emitted so the missing ``--dist loadgroup`` grouping is
      visible.
    """
    marker = item.get_closest_marker(_DEPENDS_ON_MARKER)
    if marker is None:
        return
    config = item.config
    results = config.stash[_RESULTS_KEY]
    collected = config.stash.get(_COLLECTED_KEY, set())
    xdist_groups = config.stash.get(_XDIST_GROUP_KEY, {})
    warned = config.stash.setdefault(_WARNED_KEY, set())
    is_worker = getattr(config, "workerinput", None) is not None
    self_group = _xdist_group_name(item)
    for dep in marker.args:
        dep_str = str(dep)
        outcome = _lookup_outcome(results, dep_str)
        no_result_yet = outcome == "not run yet"
        unknown_name = not is_worker and not _matches_any_nodeid(collected, dep_str)
        if no_result_yet and unknown_name:
            _maybe_warn(
                warned,
                "typo:" + dep_str,
                f"testkit_depends_on: dependency {dep_str!r} matches no collected test nodeid; "
                f"the dependent will be skipped (possible typo or a parametrized base name).",
            )
        if is_worker and outcome != "passed":
            matched = _match_nodeid(collected, dep_str)
            if matched is None or xdist_groups.get(matched) != self_group:
                _maybe_warn(
                    warned,
                    "xdist:" + dep_str,
                    f"testkit_depends_on: dependency {dep_str!r} may run on another worker "
                    f"(no matching xdist_group); the dependent may be skipped silently.",
                )
        if outcome != "passed":
            pytest.skip(f"testkit_depends_on: dependency {dep_str!r} is {outcome}")


def _lookup_outcome(results: dict[str, str], dep: str) -> str:
    """Return the latest outcome for *dep* (a nodeid or a bare test name)."""
    found: str | None = None
    for nodeid, outcome in results.items():
        if nodeid == dep or nodeid.endswith("::" + dep):
            found = outcome
    return found if found is not None else "not run yet"


def _matches_any_nodeid(collected: set[str], dep: str) -> bool:
    """True if *dep* (nodeid or bare name) resolves to any collected nodeid."""
    return _match_nodeid(collected, dep) is not None


def _match_nodeid(collected: set[str], dep: str) -> str | None:
    """Return the first collected nodeid that *dep* resolves to, else None."""
    for nodeid in collected:
        if nodeid == dep or nodeid.endswith("::" + dep):
            return nodeid
    return None


def _xdist_group_name(item: pytest.Item) -> str | None:
    """Return the ``xdist_group`` name of *item*, or None when ungrouped."""
    marker = item.get_closest_marker("xdist_group")
    if marker and marker.args:
        return str(marker.args[0])
    return None


def _maybe_warn(warned: set[str], key: str, message: str) -> None:
    """Emit *message* once per *key* across the whole session."""
    if key in warned:
        return
    warned.add(key)
    warnings.warn(message, stacklevel=2)


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
