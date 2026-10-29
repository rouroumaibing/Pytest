"""Case-level failure blocking: the ``testkit_depends_on`` marker (§28)."""

from __future__ import annotations

import pytest

pytest_plugins = ["pytester"]

_FAILED_BASE = """
import pytest


def test_base():
    raise RuntimeError("boom")


def test_unrelated():
    assert True


@pytest.mark.testkit_depends_on("test_base")
def test_dependent():
    assert True
"""

_PASSED_BASE = """
import pytest


def test_base():
    assert True


@pytest.mark.testkit_depends_on("test_base")
def test_dependent():
    assert True
"""

_NOT_RUN = """
import pytest


@pytest.mark.testkit_depends_on("test_base")
def test_dependent():
    assert True


def test_base():
    assert True
"""

_CASCADE = """
import pytest


def test_base():
    raise RuntimeError("boom")


@pytest.mark.testkit_depends_on("test_base")
def test_middle():
    assert True


@pytest.mark.testkit_depends_on("test_middle")
def test_leaf():
    assert True
"""

_NODEID = """
import pytest


def test_base():
    assert True


@pytest.mark.testkit_depends_on("test_mod.py::test_base")
def test_dependent():
    assert True
"""

_MULTI = """
import pytest


def test_ok():
    assert True


def test_bad():
    raise RuntimeError("boom")


@pytest.mark.testkit_depends_on("test_ok", "test_bad")
def test_dependent():
    assert True
"""

_SETUP_FAIL = """
import pytest


@pytest.fixture
def boom():
    raise RuntimeError("setup boom")


def test_base(boom):
    assert True


def test_unrelated():
    assert True


@pytest.mark.testkit_depends_on("test_base")
def test_dependent():
    assert True
"""

_TYPO = """
import pytest


def test_real():
    assert True


@pytest.mark.testkit_depends_on("test_typo_name")
def test_dependent():
    assert True
"""

_XDIST_CROSS = """
import pytest


@pytest.mark.xdist_group("a")
def test_base():
    raise RuntimeError("boom")


@pytest.mark.xdist_group("b")
@pytest.mark.testkit_depends_on("test_base")
def test_dependent():
    assert True
"""

_XDIST_UNGROUPED = """
import pytest


def test_base():
    raise RuntimeError("boom")


@pytest.mark.testkit_depends_on("test_base")
def test_dependent():
    assert True
"""


def _output_text(result: pytest.RunResult) -> str:
    """Return combined stdout + stderr of a pytester run."""
    warnings_attr = getattr(result, "warnings", None)
    if warnings_attr:
        return "\n".join(str(w.message) for w in warnings_attr)
    return result.stdout.str() + "\n" + result.stderr.str()


def test_failed_dependency_skips_dependent_only(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(_FAILED_BASE)
    result = pytester.runpytest("-ra")
    result.assert_outcomes(passed=1, failed=1, skipped=1)
    assert "testkit_depends_on: dependency 'test_base' is failed" in result.stdout.str()


def test_passed_dependency_does_not_skip(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(_PASSED_BASE)
    result = pytester.runpytest("-ra")
    result.assert_outcomes(passed=2)


def test_dependency_without_result_skips(pytester: pytest.Pytester) -> None:
    # The dependent is collected first, so at its setup the dependency has no
    # result yet - the conservative outcome is a skip.
    pytester.makepyfile(_NOT_RUN)
    result = pytester.runpytest("-ra")
    result.assert_outcomes(passed=1, skipped=1)
    assert "is not run yet" in result.stdout.str()


def test_skip_cascades(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(_CASCADE)
    result = pytester.runpytest("-ra")
    result.assert_outcomes(failed=1, skipped=2)
    assert "dependency 'test_middle' is skipped" in result.stdout.str()


def test_full_nodeid_dependency(pytester: pytest.Pytester) -> None:
    # tests/conftest.py pins the inner rootdir via a local pytest.ini, so the
    # full nodeid below is stable regardless of where basetemp points.
    pytester.makepyfile(test_mod=_NODEID)
    result = pytester.runpytest("-ra", "test_mod.py")
    result.assert_outcomes(passed=2)


def test_multiple_dependencies_with_strict_markers(pytester: pytest.Pytester) -> None:
    pytester.makepyfile(_MULTI)
    result = pytester.runpytest("-ra", "--strict-markers")
    result.assert_outcomes(passed=1, failed=1, skipped=1)
    assert "dependency 'test_bad' is failed" in result.stdout.str()


def test_setup_failure_dependency_is_recorded(pytester: pytest.Pytester) -> None:
    # A dependency that fails during setup (no call phase, reported by pytest
    # as an error) must still be recorded as failed, so its dependent skips
    # with an accurate reason rather than "not run yet".
    pytester.makepyfile(_SETUP_FAIL)
    result = pytester.runpytest("-ra")
    result.assert_outcomes(passed=1, errors=1, skipped=1)
    assert "testkit_depends_on: dependency 'test_base' is failed" in result.stdout.str()


def test_unresolved_dependency_name_warns(pytester: pytest.Pytester) -> None:
    # A dependency name that matches no collected test is a silent skip
    # (typo, or a bare name pointing at a parametrized variant) - a warning
    # surfaces it so the broken dependency is not invisible.
    pytester.makepyfile(_TYPO)
    result = pytester.runpytest("-ra")
    result.assert_outcomes(passed=1, skipped=1)
    assert "matches no collected test nodeid" in _output_text(result)


def test_xdist_cross_worker_dependency_warns(pytester: pytest.Pytester) -> None:
    # Distinct xdist groups force the two cases onto different workers; the
    # dependency's outcome is then invisible to the dependent, which must skip
    # and emit a cross-worker warning.
    pytester.makepyfile(_XDIST_CROSS)
    result = pytester.runpytest("-ra", "-n2", "--dist", "loadgroup")
    result.assert_outcomes(failed=1, skipped=1)
    assert "may run on another worker" in _output_text(result)


def test_xdist_ungrouped_dependency_still_skips(pytester: pytest.Pytester) -> None:
    # Without a shared xdist group the dependent may land on another worker
    # and see no result; the conservative skip must still hold either way.
    pytester.makepyfile(_XDIST_UNGROUPED)
    result = pytester.runpytest("-ra", "-n2", "--dist", "load")
    result.assert_outcomes(failed=1, skipped=1)
