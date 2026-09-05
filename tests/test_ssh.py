"""Unit tests for the SSH executor (SSHExecutor / SSHResult)."""

from __future__ import annotations

from unittest import mock

import pytest
from testkit import SSHExecutor, SSHResult
from testkit.exceptions import SSHError


def test_ssh_result_structure_and_ok():
    r = SSHResult(command="uname -m", stdout="x86_64\n", stderr="", exit_code=0, duration=0.01)
    assert r.ok is True
    assert r.command == "uname -m"
    assert "exit_code=0" in str(r)


def test_ssh_result_nonzero_not_ok():
    r = SSHResult(command="ls /nope", stdout="", stderr="not found", exit_code=127, duration=0.01)
    assert r.ok is False


def test_ssh_error_carries_context():
    err = SSHError("boom", command="ls", exit_code=127, stderr="not found")
    assert err.context["exit_code"] == 127
    assert err.context["command"] == "ls"


def _fake_client():
    """Return a mock paramiko client whose exec_command returns canned output."""
    client = mock.MagicMock()
    stdout = mock.MagicMock()
    stdout.read.return_value = b"x86_64\n"
    stdout.channel.recv_exit_status.return_value = 0
    stderr = mock.MagicMock()
    stderr.read.return_value = b""
    client.exec_command.return_value = (None, stdout, stderr)
    return client


def test_execute_returns_structured_result():
    executor = SSHExecutor("host", username="root")
    executor._client = _fake_client()
    result = executor.execute("uname -m")
    assert isinstance(result, SSHResult)
    assert result.exit_code == 0
    assert result.stdout == "x86_64\n"
    assert result.ok is True


def test_execute_raise_on_error():
    client = _fake_client()
    client.exec_command.return_value[1].channel.recv_exit_status.return_value = 127
    executor = SSHExecutor("host", username="root")
    executor._client = client
    with pytest.raises(SSHError):
        executor.execute("bad", raise_on_error=True)


def test_execute_no_raise_on_error_by_default():
    client = _fake_client()
    client.exec_command.return_value[1].channel.recv_exit_status.return_value = 127
    executor = SSHExecutor("host", username="root")
    executor._client = client
    result = executor.execute("bad")
    assert result.exit_code == 127  # no exception raised


def test_execute_auto_connects_when_no_client():
    executor = SSHExecutor("host", username="root")
    fake = _fake_client()
    calls = {"n": 0}

    def _connect():
        calls["n"] += 1
        executor._client = fake

    with mock.patch.object(executor, "connect", side_effect=_connect):
        result = executor.execute("uname -m")
    assert calls["n"] == 1  # auto-connected
    assert result.exit_code == 0


def test_execute_does_not_reconnect_when_client_present():
    executor = SSHExecutor("host", username="root")
    with (
        mock.patch.object(executor, "connect") as connect,
        mock.patch.object(executor, "_client", create=True, new=_fake_client()),
    ):
        # _client already set, so connect should NOT be called.
        executor.execute("uname -m")
        connect.assert_not_called()


def test_get_architecture_cached():
    executor = SSHExecutor("host", username="root")
    executor._client = _fake_client()
    arch1 = executor.get_architecture()
    arch2 = executor.get_architecture()
    assert arch1 == arch2 == "x86_64"
    # Cached: second call does not execute again.
    executor._client.exec_command.assert_called_once()


def test_get_architecture_refresh():
    executor = SSHExecutor("host", username="root")
    executor._client = _fake_client()
    executor.get_architecture()
    executor.get_architecture(refresh=True)
    assert executor._client.exec_command.call_count == 2


def test_connect_failure_raises_ssh_error():
    executor = SSHExecutor("host", username="root")
    with mock.patch("paramiko.SSHClient") as client_cls:
        inst = client_cls.return_value
        inst.connect.side_effect = Exception("refused")
        with pytest.raises(SSHError):
            executor.connect()


def test_context_manager_closes():
    executor = SSHExecutor("host", username="root")
    with (
        mock.patch.object(executor, "connect") as connect,
        mock.patch.object(executor, "close") as close,
    ):
        with executor:
            pass
        connect.assert_called_once()
        close.assert_called_once()
