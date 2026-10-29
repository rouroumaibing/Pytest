"""Shared fixtures for the test suite."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def pin_pytester_rootdir(request: pytest.FixtureRequest) -> None:
    """Pin pytester's inner rootdir to its own directory.

    ``pyproject.toml`` sets ``--basetemp=.pytest_tmp`` *inside* the project, so
    pytester's temp dirs live under the repo and the inner pytest run finds the
    project's ``pyproject.toml`` by walking up. The inner rootdir then becomes
    the repo: inner nodeids grow a directory prefix (breaking full-nodeid
    dependency matching) and the project's ``addopts`` leak into the run.

    Writing a minimal local ``pytest.ini`` into ``pytester.path`` stops the
    walk-up there, keeping nodeids and option inheritance deterministic for
    every pytester-based test. Applies only to tests that actually request
    ``pytester``.
    """
    if "pytester" not in request.fixturenames:
        return
    pytester = request.getfixturevalue("pytester")
    pytester.makefile(".ini", pytest="[pytest]\n")
