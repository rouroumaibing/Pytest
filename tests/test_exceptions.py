"""Unit tests for the exception hierarchy (testkit.exceptions)."""

from __future__ import annotations

import pytest
from testkit.exceptions import (
    CleanupError,
    ConfigError,
    FixtureError,
    HTTPError,
    PipelineError,
    PoolError,
    ResourceNotFoundError,
    SSHError,
)
from testkit.exceptions import (
    TestKitError as BaseError,  # aliased so pytest does not collect it as a test class
)


def test_base_error_formats_context():
    err = BaseError("boom", foo=1, bar="x")
    assert err.message == "boom"
    assert err.context == {"foo": 1, "bar": "x"}
    assert "foo=1" in str(err)
    assert "bar='x'" in str(err)


def test_base_error_without_context():
    err = BaseError("plain")
    assert str(err) == "plain"


def test_original_exception_preserved():
    cause = ValueError("root")
    err = BaseError("wrapped", original_exception=cause)
    assert err.original_exception is cause


def test_repr_contains_class_name():
    err = BaseError("boom", x=1)
    assert repr(err).startswith("TestKitError(")


def test_http_error_carries_status_and_response_data():
    err = HTTPError("failed", status_code=500, response_data={"e": "x"}, path="/a")
    assert err.context["status_code"] == 500
    assert err.context["response_data"] == {"e": "x"}
    assert err.context["path"] == "/a"


def test_ssh_error_carries_command_exit_code_stderr():
    err = SSHError("run failed", command="ls /x", exit_code=127, stderr="not found")
    assert err.context["command"] == "ls /x"
    assert err.context["exit_code"] == 127
    assert err.context["stderr"] == "not found"


def test_resource_not_found_carries_type_and_id():
    err = ResourceNotFoundError("cluster", "c-9")
    assert err.context["resource_type"] == "cluster"
    assert err.context["resource_id"] == "c-9"
    assert err.context["status_code"] == 404
    assert "cluster" in str(err)
    assert "c-9" in str(err)


def test_hierarchy_is_rooted_at_testkit_error():
    from testkit.exceptions import TestKitError

    for cls in (
        ConfigError,
        PoolError,
        SSHError,
        HTTPError,
        FixtureError,
        CleanupError,
        PipelineError,
        ResourceNotFoundError,
    ):
        assert issubclass(cls, TestKitError)


def test_resource_not_found_is_http_error():
    assert issubclass(ResourceNotFoundError, HTTPError)


def test_exceptions_are_raisable():
    with pytest.raises(ConfigError):
        raise ConfigError("bad config")
