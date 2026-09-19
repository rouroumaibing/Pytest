"""Regression tests for the unified exception API and parallel_map omissions.

Two behaviours are pinned here:

* every framework exception mirrors its ``context`` entries onto direct
  attributes, so ``err.status_code`` works as well as ``err.context``;
* ``parallel_map`` omits only *failed / skipped* slots — a successful ``None``
  result survives.
"""

from __future__ import annotations

import pytest
from testkit import exceptions as kit_exceptions
from testkit.exceptions import (
    ConfigError,
    HTTPError,
    K8sError,
    ResourceNotFoundError,
    SSHError,
)
from testkit.k8s.client import K8sClient
from testkit.utils.parallel import parallel_map
from testkit.utils.wait import WaitHelper

# -- exceptions: context <-> attribute mirroring ------------------------------


def test_base_error_exposes_context_as_attributes():
    err = kit_exceptions.TestKitError("boom", status_code=502, retries=3)
    assert err.context == {"status_code": 502, "retries": 3}
    assert err.status_code == 502
    assert err.retries == 3


def test_base_error_without_context_has_no_extra_attributes():
    err = kit_exceptions.TestKitError("plain")
    assert err.context == {}
    assert not hasattr(err, "status_code")


def test_reserved_context_keys_do_not_clobber_internals():
    err = kit_exceptions.TestKitError("boom", args=[9], context={"a": 1})
    # Reserved names never overwrite the exception's own attributes: ``args``
    # stays the BaseException tuple and ``context`` keeps holding the entries.
    assert err.args == (str(err),)
    assert err.context == {"args": [9], "context": {"a": 1}}
    assert err.message == "boom"


def test_http_error_status_code_attribute():
    err = HTTPError("bad gateway", status_code=502, response_data={"err": "x"})
    assert err.status_code == 502
    assert err.response_data == {"err": "x"}


def test_resource_not_found_attributes():
    err = ResourceNotFoundError("cluster", "c-1")
    assert err.resource_type == "cluster"
    assert err.resource_id == "c-1"
    assert err.status_code == 404


def test_ssh_error_attributes():
    err = SSHError("cmd failed", command="ls", exit_code=127, stderr="not found")
    assert err.command == "ls"
    assert err.exit_code == 127
    assert err.stderr == "not found"


def test_k8s_error_wrap_attributes():
    class _Status:
        reason = "Forbidden"
        status = 403

    class _ApiExc(Exception):
        status = _Status()

    err = K8sClient._wrap(_ApiExc("boom"), "list pods", namespace="ns")
    assert isinstance(err, K8sError)
    assert err.reason == "Forbidden"
    assert err.status_code == 403
    assert err.namespace == "ns"


def test_config_error_attributes():
    err = ConfigError("bad config", fixture_name="db", errors=[{"loc": ("x",)}])
    assert err.fixture_name == "db"
    assert err.errors == [{"loc": ("x",)}]


def test_wait_until_deleted_detects_framework_404_attribute():
    """§27: a framework ``HTTPError`` carrying 404 counts as deleted."""
    calls = {"n": 0}

    def check_exists():
        calls["n"] += 1
        raise HTTPError("gone", status_code=404)

    assert WaitHelper(timeout=0.5, interval=0.01).wait_until_deleted(check_exists) is True
    assert calls["n"] == 1


def test_wait_until_deleted_ignores_non_404_framework_error():
    def check_exists():
        raise HTTPError("server error", status_code=500)

    # A non-404 error is not a deletion signal; polling times out instead.
    assert WaitHelper(timeout=0.1, interval=0.01).wait_until_deleted(check_exists) is False


# -- parallel_map: only failed/skipped slots are omitted ----------------------


def test_parallel_map_keeps_successful_none_results():
    result = parallel_map(lambda _x: None, [0, 1, 2], max_workers=3)
    assert result == [None, None, None]


def test_parallel_map_omits_only_failed_slots():
    def work(x: int) -> int | None:
        if x == 2:
            raise ValueError("boom")
        return None if x == 1 else x

    result = parallel_map(work, [0, 1, 2, 3], max_workers=4)
    assert result == [0, None, 3]


def test_parallel_map_error_handler_none_is_kept():
    def work(x: int) -> int:
        if x == 1:
            raise ValueError("boom")
        return x

    def handler(_item: int, _exc: Exception) -> None:
        return None

    result = parallel_map(work, [0, 1, 2], max_workers=3, error_handler=handler)
    assert result == [0, None, 2]


def test_parallel_map_invalid_mode_still_raises():
    with pytest.raises(ValueError):
        parallel_map(lambda x: x, [1], on_error="nope")
