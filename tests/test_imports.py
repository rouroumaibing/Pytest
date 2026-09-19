"""Smoke tests: public API symbols and submodule imports.

Importing every ``testkit`` submodule forces each ``__init__.py`` to be
executed, covering the package's import surface.
"""

from __future__ import annotations

import importlib

import testkit

# --------------------------------------------------------------------------- #
# Public symbols on the top-level package
# --------------------------------------------------------------------------- #

EXPECTED_SYMBOLS = [
    "K8sClient",
    "K8sError",
    "parallel_map",
    "to_mib",
    "SSHExecutor",
    "SSHResult",
    "HTTPClient",
    "TokenAuth",
    "ApiKeyAuth",
    "CookieAuth",
    "CustomAuth",
    "ResourcePool",
    "ResourceCleanup",
    "ConfigRegistry",
    "WaitHelper",
    "WaitTimeout",
    "Pipeline",
    "StageResult",
    "ConcurrentFixtureGuard",
    "BaseModel",
    "Builder",
]


def test_public_symbols_present():
    missing = [name for name in EXPECTED_SYMBOLS if not hasattr(testkit, name)]
    assert not missing, f"missing public symbols: {missing}"


# --------------------------------------------------------------------------- #
# Submodule imports (executes every __init__.py)
# --------------------------------------------------------------------------- #

SUBMODULES = [
    "testkit.k8s",
    "testkit.k8s.client",
    "testkit.k8s.quantities",
    "testkit.ssh",
    "testkit.ssh.executor",
    "testkit.http",
    "testkit.http.client",
    "testkit.http.auth",
    "testkit.config",
    "testkit.config.loader",
    "testkit.utils",
    "testkit.utils.wait",
    "testkit.utils.parallel",
    "testkit.pool",
    "testkit.pool.resource_pool",
    "testkit.fixture",
    "testkit.fixture.guard",
    "testkit.pipeline",
    "testkit.pipeline.stage",
    "testkit.cleanup",
    "testkit.cleanup.resource_cleanup",
    "testkit.model",
    "testkit.model.base",
    "testkit.logging_setup",
    "testkit.exceptions",
]


def test_submodules_import():
    failures = {}
    for name in SUBMODULES:
        try:
            importlib.import_module(name)
        except Exception as exc:  # noqa: BLE001 - report any import failure
            failures[name] = repr(exc)
    assert not failures, f"submodule import failures: {failures}"
