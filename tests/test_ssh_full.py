"""Full coverage tests for SSHExecutor (executor.py).

These tests mock ``paramiko.SSHClient`` so no real SSH connection is ever
opened. Each ``paramiko.SSHClient()`` call returns a freshly built MagicMock
client pre-wired with canned transport / exec / sftp behaviour.
"""

from __future__ import annotations

import os
import tempfile
from unittest import mock

import paramiko
import pytest
from testkit import SSHExecutor, SSHResult
from testkit.exceptions import SSHError

_VERIFY_TOKEN = "__testkit_verify__"


def make_ssh_client():
    """Build a fresh MagicMock SSHClient with canned SSH/SFTP behaviour."""
    client = mock.MagicMock(name="SSHClient")

    transport = mock.MagicMock(name="Transport")
    transport.is_active.return_value = True
    channel = mock.MagicMock(name="Channel")
    transport.open_channel.return_value = channel
    client.get_transport.return_value = transport

    stdout = mock.MagicMock(name="stdout")
    stdout.read.return_value = b"hello\n"
    stdout.channel.recv_exit_status.return_value = 0
    stderr = mock.MagicMock(name="stderr")
    stderr.read.return_value = b""
    client.exec_command.return_value = (mock.MagicMock(), stdout, stderr)

    sftp = mock.MagicMock(name="SFTP")
    client.open_sftp.return_value = sftp
    return client


@pytest.fixture
def patched(monkeypatch):
    """Patch ``paramiko.SSHClient`` so every construction yields a mock client."""
    monkeypatch.setattr(paramiko, "SSHClient", make_ssh_client)


# -- execute ----------------------------------------------------------------


def test_execute_happy_path(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    result = ex.execute("echo hi")
    assert isinstance(result, SSHResult)
    assert result.exit_code == 0
    assert result.stdout == "hello\n"
    assert result.ok is True


def test_execute_raise_on_error_nonzero(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].channel.recv_exit_status.return_value = 3
    ex._client.exec_command.return_value[2].read.return_value = b"boom"
    with pytest.raises(SSHError):
        ex.execute("bad", raise_on_error=True)


def test_execute_wraps_exec_exception(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.side_effect = RuntimeError("transport died")
    with pytest.raises(SSHError):
        ex.execute("anything")


# -- raw property -----------------------------------------------------------


def test_raw_returns_client_when_connected(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    assert ex.raw is ex._client


def test_raw_raises_when_not_connected():
    ex = SSHExecutor("target", username="u")
    with pytest.raises(SSHError):
        _ = ex.raw


# -- _is_alive --------------------------------------------------------------


def test_is_alive_true_when_transport_active(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    assert ex._is_alive() is True


def test_is_alive_false_when_transport_inactive(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.get_transport.return_value.is_active.return_value = False
    assert ex._is_alive() is False


def test_is_alive_false_when_no_client():
    ex = SSHExecutor("target", username="u")
    assert ex._is_alive() is False


# -- _with_reconnect --------------------------------------------------------


def test_with_reconnect_raises_when_transport_alive():
    ex = SSHExecutor("target", username="u")
    ex.connect = mock.MagicMock()
    ex._is_alive = mock.MagicMock(return_value=True)
    fn = mock.MagicMock(side_effect=RuntimeError("dead"))
    with pytest.raises(SSHError):
        ex._with_reconnect("act", fn)
    ex.connect.assert_not_called()


def test_with_reconnect_retries_once_when_transport_dead():
    ex = SSHExecutor("target", username="u")
    ex.connect = mock.MagicMock()
    ex._is_alive = mock.MagicMock(return_value=False)
    calls = {"n": 0}

    def fn():
        if calls["n"] == 0:
            calls["n"] += 1
            raise RuntimeError("dead")
        return "OK"

    result = ex._with_reconnect("act", fn)
    assert result == "OK"
    ex.connect.assert_called_once()


def test_with_reconnect_raises_after_reconnect_when_always_fails():
    ex = SSHExecutor("target", username="u")
    ex.connect = mock.MagicMock()
    ex._is_alive = mock.MagicMock(return_value=False)
    fn = mock.MagicMock(side_effect=RuntimeError("always"))
    with pytest.raises(SSHError):
        ex._with_reconnect("act", fn)
    ex.connect.assert_called_once()


# -- verify ----------------------------------------------------------------


def test_verify_true_when_token_echoed(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].read.return_value = f"{_VERIFY_TOKEN}\n".encode()
    assert ex.verify(_VERIFY_TOKEN) is True


def test_verify_false_when_token_absent(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    # default stdout "hello\n" does not contain the token
    assert ex.verify(_VERIFY_TOKEN) is False


# -- file_exist -------------------------------------------------------------


def test_file_exist_true_when_exit_zero(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].channel.recv_exit_status.return_value = 0
    assert ex.file_exist("/some/path") is True


def test_file_exist_false_when_exit_nonzero(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].channel.recv_exit_status.return_value = 2
    assert ex.file_exist("/missing/path") is False


# -- find_package -----------------------------------------------------------


def test_find_package_match_with_remote_dir(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    sftp = ex._client.open_sftp.return_value
    sftp.listdir.return_value = ["pkg-1.0.tar.gz", "other.txt"]
    assert ex.find_package("pkg-*.tar.gz", remote_dir="/opt/pkgs") == "/opt/pkgs/pkg-1.0.tar.gz"


def test_find_package_match_default_dir(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    sftp = ex._client.open_sftp.return_value
    sftp.listdir.return_value = ["pkg-1.0.tar.gz"]
    assert ex.find_package("pkg-*.tar.gz") == "pkg-1.0.tar.gz"


def test_find_package_no_match(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    sftp = ex._client.open_sftp.return_value
    sftp.listdir.return_value = ["other.txt"]
    assert ex.find_package("pkg-*.tar.gz") is None


def test_find_package_listing_error_raises(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    sftp = ex._client.open_sftp.return_value
    sftp.listdir.side_effect = OSError("permission denied")
    with pytest.raises(SSHError):
        ex.find_package("pkg-*.tar.gz")


# -- scp_via_jump -----------------------------------------------------------


def test_scp_via_jump_writes_chunks(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    try:
        tmp.write(b"x" * (100 * 1024))  # > 64KiB to exercise chunk loop
        tmp.close()
        sftp = ex._client.open_sftp.return_value
        remote_file = sftp.open.return_value
        # `with sftp.open(...) as remote:` must yield the same (configured) mock.
        remote_file.__enter__.return_value = remote_file
        ex.scp_via_jump(tmp.name, "/remote/path")
        sftp.open.assert_called_with("/remote/path", "wb")
        assert remote_file.write.called
    finally:
        os.unlink(tmp.name)


def test_scp_via_jump_missing_local_file_raises(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    with pytest.raises(SSHError):
        ex.scp_via_jump("/no/such/local/file", "/remote/path")


def test_scp_via_jump_open_sftp_failure_raises(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    try:
        tmp.write(b"data")
        tmp.close()
        ex._client.open_sftp.side_effect = OSError("no sftp")
        with pytest.raises(SSHError):
            ex.scp_via_jump(tmp.name, "/remote/path")
    finally:
        os.unlink(tmp.name)


# -- scp_via_double_jump ----------------------------------------------------


def test_scp_via_double_jump_forwards_bytes(patched):
    source = SSHExecutor("source", username="u")
    source.connect()
    src_sftp = source._client.open_sftp.return_value
    src_file = src_sftp.open.return_value
    src_file.__enter__.return_value = src_file  # `with` must yield the same mock
    state = {"read": 0}

    def _read(*_args):
        state["read"] += 1
        return b"PAYLOAD-DATA" if state["read"] == 1 else b""

    src_file.read.side_effect = _read

    middle = SSHExecutor("middle", username="u")
    middle.connect()

    target_client = make_ssh_client()
    dst_file = target_client.open_sftp.return_value.open.return_value
    dst_file.__enter__.return_value = dst_file  # `with` must yield the same mock
    with mock.patch.object(middle, "_new_client", return_value=target_client):
        middle.scp_via_double_jump(source, "/src/file", "10.0.0.5", "/dst/file")

    written = [c.args[0] for c in dst_file.write.call_args_list]
    assert b"PAYLOAD-DATA" in written


def test_scp_via_double_jump_target_transport_none_raises(patched):
    source = SSHExecutor("source", username="u")
    source.connect()
    src_sftp = source._client.open_sftp.return_value

    middle = SSHExecutor("middle", username="u")
    middle.connect()
    middle._client.get_transport.return_value = None

    with pytest.raises(SSHError):
        middle.scp_via_double_jump(source, "/src/file", "10.0.0.5", "/dst/file")
    assert src_sftp.close.called


def test_scp_via_double_jump_target_connect_fails_raises(patched):
    source = SSHExecutor("source", username="u")
    source.connect()
    src_sftp = source._client.open_sftp.return_value

    middle = SSHExecutor("middle", username="u")
    middle.connect()

    target_client = make_ssh_client()
    target_client.connect.side_effect = RuntimeError("refused")
    with mock.patch.object(middle, "_new_client", return_value=target_client):
        with pytest.raises(SSHError):
            middle.scp_via_double_jump(source, "/src/file", "10.0.0.5", "/dst/file")
    assert src_sftp.close.called
    assert target_client.close.called


# -- close / context manager ------------------------------------------------


def test_close_closes_target_and_jump(patched):
    ex = SSHExecutor("target", username="u", jump_host="jump")
    ex.connect()
    target = ex._client
    jump = ex._jump_client
    ex.close()
    assert target.close.called
    assert jump.close.called
    assert ex._client is None
    assert ex._jump_client is None


def test_context_manager(patched):
    ex = SSHExecutor("target", username="u")
    with ex:
        assert ex._client is not None
    assert ex._client is None


# -- get_architecture -------------------------------------------------------


def test_get_architecture_cached(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].read.return_value = b"aarch64\n"
    a1 = ex.get_architecture()
    a2 = ex.get_architecture()
    assert a1 == a2 == "aarch64"
    assert ex._client.exec_command.call_count == 1


def test_get_architecture_refresh(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].read.return_value = b"x86_64\n"
    ex.get_architecture()
    ex.get_architecture(refresh=True)
    assert ex._client.exec_command.call_count == 2


def test_get_architecture_ssh_error_on_nonzero(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].channel.recv_exit_status.return_value = 1
    with pytest.raises(SSHError):
        ex.get_architecture()


def test_get_architecture_ssh_error_on_empty(patched):
    ex = SSHExecutor("target", username="u")
    ex.connect()
    ex._client.exec_command.return_value[1].read.return_value = b""
    ex._client.exec_command.return_value[1].channel.recv_exit_status.return_value = 0
    with pytest.raises(SSHError):
        ex.get_architecture()


# -- host-key policy --------------------------------------------------------


def test_host_key_policy_invalid_at_init():
    with pytest.raises(SSHError):
        SSHExecutor("target", username="u", host_key_policy="bogus")


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("reject", paramiko.RejectPolicy),
        ("warn", paramiko.WarningPolicy),
        ("auto", paramiko.AutoAddPolicy),
    ],
)
def test_host_key_policy_applied(patched, policy, expected):
    ex = SSHExecutor("target", username="u", host_key_policy=policy)
    ex.connect()
    assert ex._client.set_missing_host_key_policy.call_count == 1
    arg = ex._client.set_missing_host_key_policy.call_args.args[0]
    assert isinstance(arg, expected)


# -- keepalive --------------------------------------------------------------


def test_keepalive_default_interval_applied(patched):
    ex = SSHExecutor("target", username="u")  # default keepalive_interval=60
    ex.connect()
    transport = ex._client.get_transport.return_value
    transport.set_keepalive.assert_called_once_with(60)


def test_keepalive_zero_disables(patched):
    ex = SSHExecutor("target", username="u", keepalive_interval=0)
    ex.connect()
    transport = ex._client.get_transport.return_value
    transport.set_keepalive.assert_not_called()


# -- ported from test_ssh.py (unique execute / connect behaviours) --


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
        executor.execute("uname -m")
        connect.assert_not_called()


def test_connect_failure_raises_ssh_error():
    executor = SSHExecutor("host", username="root")
    with mock.patch("paramiko.SSHClient") as client_cls:
        inst = client_cls.return_value
        inst.connect.side_effect = Exception("refused")
        with pytest.raises(SSHError):
            executor.connect()


def test_ssh_error_carries_context():
    err = SSHError("boom", command="ls", exit_code=127, stderr="not found")
    assert err.context["exit_code"] == 127
    assert err.context["command"] == "ls"
