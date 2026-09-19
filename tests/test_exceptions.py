"""Unit tests for the testkit exception hierarchy (testkit.exceptions)."""

from __future__ import annotations

from testkit.exceptions import (
    CleanupError,
    ConfigError,
    FixtureError,
    HTTPError,
    HttpTimeoutError,
    K8sError,
    NetworkError,
    PipelineError,
    PoolError,
    ResourceNotFoundError,
    SSHError,
)
from testkit.exceptions import (
    TestKitError as _TestKitError,  # aliased so pytest does not collect it as a test class
)

# --------------------------------------------------------------------------- #
# Generic hierarchy / construction behavior
# --------------------------------------------------------------------------- #


def test_all_subclasses_of_testkit_error():
    classes = [
        ConfigError,
        PoolError,
        HTTPError,
        HttpTimeoutError,
        NetworkError,
        SSHError,
        FixtureError,
        CleanupError,
        PipelineError,
        K8sError,
        ResourceNotFoundError,
    ]
    for cls in classes:
        assert issubclass(cls, _TestKitError)
        assert issubclass(cls, Exception)


def test_testkit_error_is_exception():
    assert issubclass(_TestKitError, Exception)


def test_constructible_with_message_and_context():
    classes = [
        _TestKitError,
        ConfigError,
        PoolError,
        HTTPError,
        HttpTimeoutError,
        NetworkError,
        SSHError,
        FixtureError,
        CleanupError,
        PipelineError,
        K8sError,
    ]
    for cls in classes:
        inst = cls("boom", foo="bar", code=7)
        assert inst.message == "boom"
        assert inst.context.get("foo") == "bar"
        assert inst.context.get("code") == 7
        # context is rendered into the message
        assert "foo='bar'" in str(inst)
        assert "code=7" in str(inst)


def test_empty_context_renders_plain_message():
    inst = _TestKitError("just a message")
    assert inst.context == {}
    assert str(inst) == "just a message"
    assert "[]" not in str(inst)


def test_repr_roundtrips_class_name():
    inst = ConfigError("bad config", path="/etc/x")
    assert "ConfigError" in repr(inst)
    assert "bad config" in repr(inst)


# --------------------------------------------------------------------------- #
# Exception chaining + original_exception preservation
# --------------------------------------------------------------------------- #


def test_original_exception_stored_and_chain_preserved():
    try:
        try:
            raise ValueError("root cause")
        except ValueError as root:
            raise SSHError("command failed", command="ls", exit_code=127) from root
    except SSHError as err:
        assert err.__cause__ is not None
        assert isinstance(err.__cause__, ValueError)
        assert err.context["command"] == "ls"
        assert err.context["exit_code"] == 127


def test_original_exception_default_none():
    err = PoolError("no resources")
    assert err.original_exception is None


# --------------------------------------------------------------------------- #
# SSHError specialized context
# --------------------------------------------------------------------------- #


def test_ssh_error_specialized_fields():
    err = SSHError("boom", command="uname -m", exit_code=42, stderr="oops")
    assert err.context["command"] == "uname -m"
    assert err.context["exit_code"] == 42
    assert err.context["stderr"] == "oops"
    assert "command='uname -m'" in str(err)


def test_ssh_error_omits_unset_fields():
    err = SSHError("boom")
    assert "command" not in err.context
    assert "exit_code" not in err.context
    assert "stderr" not in err.context


# --------------------------------------------------------------------------- #
# HTTPError / subclasses
# --------------------------------------------------------------------------- #


def test_http_error_status_code_defaults_to_none():
    # NOTE: source default for status_code is None (not 0) when not provided.
    err = HTTPError("bad gateway")
    assert err.context.get("status_code") is None
    assert "status_code" not in err.context


def test_http_error_status_code_can_be_set():
    err = HTTPError("bad gateway", status_code=502, response_data={"err": "x"})
    assert err.context["status_code"] == 502
    assert err.context["response_data"] == {"err": "x"}
    assert "status_code=502" in str(err)


def test_http_timeout_is_http_error_subclass():
    assert issubclass(HttpTimeoutError, HTTPError)
    err = HttpTimeoutError("timed out", status_code=504)
    assert err.context["status_code"] == 504
    assert isinstance(err, HTTPError)


def test_network_error_is_http_error_subclass():
    assert issubclass(NetworkError, HTTPError)
    err = NetworkError("connection reset")
    assert isinstance(err, HTTPError)
    assert isinstance(err, _TestKitError)


# --------------------------------------------------------------------------- #
# ResourceNotFoundError specialized fields
# --------------------------------------------------------------------------- #


def test_resource_not_found_error_fields():
    err = ResourceNotFoundError("cluster", "c-123")
    assert err.context["resource_type"] == "cluster"
    assert err.context["resource_id"] == "c-123"
    # status_code defaults to 404
    assert err.context["status_code"] == 404
    assert "resource not found: cluster='c-123'" in str(err)


def test_resource_not_found_error_override_status_code():
    err = ResourceNotFoundError("node", "n-9", status_code=410)
    assert err.context["resource_type"] == "node"
    assert err.context["resource_id"] == "n-9"
    assert err.context["status_code"] == 410


def test_resource_not_found_error_extra_context():
    err = ResourceNotFoundError("pod", "p-1", detail="evicted")
    assert err.context["detail"] == "evicted"
    assert err.context["resource_type"] == "pod"


# --------------------------------------------------------------------------- #
# K8sError / basic subclasses
# --------------------------------------------------------------------------- #


def test_k8s_error_constructible():
    err = K8sError("api server unreachable", endpoint="/pods", code=500)
    assert err.message == "api server unreachable"
    assert err.context["endpoint"] == "/pods"
    assert err.context["code"] == 500


def test_config_error_constructible():
    err = ConfigError("missing section", section="ssh")
    assert err.context["section"] == "ssh"


def test_cleaner_and_pipeline_errors_constructible():
    assert CleanupError("deletion failed", resource="vol").context["resource"] == "vol"
    assert PipelineError("stage 2 failed", stage="deploy").context["stage"] == "deploy"
    assert FixtureError("guard conflict", guard="clusters").context["guard"] == "clusters"
