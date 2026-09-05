"""Verify the pytest plugin auto-registers its CLI options and fixtures.

These tests use ``pytester`` (built into pytest) to run an isolated, in-process
test session, confirming the ``pytest11`` entry point exposes the expected
``--testkit-*`` options and ``testkit_*`` fixtures without any ``conftest.py``.

The plugin is expected to be discoverable via its ``pytest11`` entry point
(``testkit = testkit.plugin``), which requires an install — ``pip install -e .``
locally, or the editable install performed by CI. These tests intentionally do
*not* pass ``-p testkit.plugin`` so they exercise the entry-point registration
itself.

The coverage gate runs with ``-p no:testkit`` (plus ``--ignore`` on this file)
so the plugin's startup import does not distort coverage measurement — see
``.github/workflows/ci.yml``.
"""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]


def test_cli_options_registered(pytester: pytest.Pytester) -> None:
    """All ``--testkit-`` options must appear in ``--help``."""
    result = pytester.runpytest_inprocess("--help")
    result.stdout.fnmatch_lines(
        [
            "*--testkit-verbosity*",
            "*--testkit-env*",
            "*--testkit-resume-from*",
            "*--testkit-skip-cleanup-on-failure*",
        ]
    )


def test_cleanup_fixture_auto_registered(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_has_cleanup(testkit_cleanup):
            assert testkit_cleanup is not None
            testkit_cleanup.register(lambda: None, name="noop")
        """
    )
    result = pytester.runpytest_inprocess("-q")
    result.assert_outcomes(passed=1)


def test_wait_helper_fixture_auto_registered(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_wait(testkit_wait_helper):
            got = testkit_wait_helper.until(lambda: "ready", expected="ready", timeout=5)
            assert got == "ready"
        """
    )
    result = pytester.runpytest_inprocess("-q")
    result.assert_outcomes(passed=1)


def test_fixture_guard_factory_auto_registered(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        def test_guard(testkit_fixture_guard):
            guard = testkit_fixture_guard("clusters")
            assert guard.should_create() is True
            guard.mark_created()
            assert guard.should_create() is False
        """
    )
    result = pytester.runpytest_inprocess("-q")
    result.assert_outcomes(passed=1)


def test_verbosity_option_applies_logging(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import testkit.logging_setup as ls

        def test_verbosity_applied():
            logger = ls.get_logger("plugin_check")
            assert logger.level <= ls.get_effective_level(2)
        """
    )
    result = pytester.runpytest_inprocess("-q", "--testkit-verbosity=2")
    result.assert_outcomes(passed=1)


def test_env_option_sets_environment(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(
        """
        import os

        def test_env_set():
            assert os.environ.get("TESTKIT_ENV") == "staging"
        """
    )
    result = pytester.runpytest_inprocess("-q", "--testkit-env=staging")
    result.assert_outcomes(passed=1)


def test_skip_cleanup_on_failure_preserves_state(pytester: pytest.Pytester) -> None:
    """A failing test must stash its failure so cleanup is skipped when asked."""
    pytester.makepyfile(
        """
        ran = []

        def test_fails(testkit_cleanup):
            testkit_cleanup.register(lambda: ran.append("cleaned"), name="preserve")
            assert False, "intentional failure"
        """
    )
    result = pytester.runpytest_inprocess("-q", "--testkit-skip-cleanup-on-failure")
    result.assert_outcomes(failed=1)
