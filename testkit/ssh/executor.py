"""SSH command executor with direct and jump-host (direct-tcpip) modes.

Commands return a structured :class:`SSHResult` (stdout / stderr /
exit_code / duration). In jump-host mode the channel is opened through the
existing SSH connection's transport via a ``direct-tcpip`` channel, with no
intermediate shell on the jump host.
"""

from __future__ import annotations

import fnmatch
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

import paramiko

from testkit.exceptions import SSHError
from testkit.logging_setup import get_logger

logger = get_logger("ssh")

# Map the user-facing host-key policy names to paramiko policies.
_HOST_KEY_POLICIES = {
    "reject": paramiko.RejectPolicy,
    "warn": paramiko.WarningPolicy,
    "auto": paramiko.AutoAddPolicy,
}

# Chunk size for streamed SFTP transfers (never load a whole file in memory).
_SFTP_CHUNK = 64 * 1024

_DEFAULT_VERIFY_TOKEN = "__testkit_verify__"


@dataclass
class SSHResult:
    """Structured result of a single command execution."""

    command: str
    stdout: str
    stderr: str
    exit_code: int
    duration: float

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    def __str__(self) -> str:
        return (
            f"SSHResult(command={self.command!r}, exit_code={self.exit_code}, "
            f"duration={self.duration:.3f}s)"
        )


class SSHExecutor:
    """Execute commands over SSH, optionally tunnelling through a jump host.

    Parameters
    ----------
    host:
        Target host address.
    port:
        Target SSH port.
    username:
        Login user for the target host.
    password:
        Password authentication for the target host.
    key_filename:
        Path to a private key for the target host.
    timeout:
        Connection / command timeout (seconds).
    jump_host:
        Optional jump (bastion) host. When set, the target connection is
        tunnelled through it via a ``direct-tcpip`` channel.
    jump_port:
        Jump host SSH port.
    jump_username:
        Login user for the jump host (defaults to *username*).
    jump_password:
        Password for the jump host.
    jump_key_filename:
        Private key path for the jump host.
    host_key_policy:
        Missing-host-key policy: ``"reject"`` (RejectPolicy), ``"warn"``
        (WarningPolicy) or ``"auto"`` (AutoAddPolicy). Defaults to ``"auto"``
        for backward compatibility. Never hard-coded.
    keepalive_interval:
        TCP keepalive interval (seconds) applied to every transport. ``0``
        disables keepalive.
    """

    def __init__(
        self,
        host: str,
        port: int = 22,
        username: str | None = None,
        password: str | None = None,
        key_filename: str | None = None,
        timeout: float = 10.0,
        jump_host: str | None = None,
        jump_port: int = 22,
        jump_username: str | None = None,
        jump_password: str | None = None,
        jump_key_filename: str | None = None,
        host_key_policy: str = "auto",
        keepalive_interval: int = 60,
    ) -> None:
        self.host = host
        self.port = port
        self.username = username
        self._password = password
        self._key_filename = key_filename
        self.timeout = timeout

        self._jump_host = jump_host
        self._jump_port = jump_port
        self._jump_username = jump_username or username
        self._jump_password = jump_password
        self._jump_key_filename = jump_key_filename

        if host_key_policy not in _HOST_KEY_POLICIES:
            raise SSHError(
                "invalid host_key_policy",
                policy=host_key_policy,
                valid=list(_HOST_KEY_POLICIES),
            )
        self._host_key_policy = host_key_policy
        self._keepalive_interval = keepalive_interval

        self._client: paramiko.SSHClient | None = None
        self._jump_client: paramiko.SSHClient | None = None
        self._arch: str | None = None

    # -- connection lifecycle -------------------------------------------------

    def _new_client(self) -> paramiko.SSHClient:
        """Create an SSHClient with the configured missing-host-key policy."""
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(_HOST_KEY_POLICIES[self._host_key_policy]())
        return client

    def _apply_keepalive(self, client: paramiko.SSHClient) -> None:
        if self._keepalive_interval <= 0:
            return
        transport = client.get_transport()
        if transport is not None:
            transport.set_keepalive(self._keepalive_interval)

    def connect(self) -> None:
        """Establish the (possibly tunnelled) SSH connection."""
        sock: paramiko.Channel | None = None
        if self._jump_host is not None:
            sock = self._open_jump_tunnel()

        client = self._new_client()
        try:
            client.connect(
                hostname=self.host,
                port=self.port,
                username=self.username,
                password=self._password,
                key_filename=self._key_filename,
                timeout=self.timeout,
                sock=sock,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:  # noqa: BLE001
            client.close()
            raise SSHError(
                "failed to connect to target host",
                host=self.host,
                port=self.port,
                original_exception=exc,
            ) from exc

        self._apply_keepalive(client)
        self._client = client
        logger.v2("ssh connected host=%s:%s jump=%s", self.host, self.port, self._jump_host)

    def _open_jump_tunnel(self) -> paramiko.Channel:
        """Open a ``direct-tcpip`` channel through the jump host transport."""
        jump_host = self._jump_host
        assert jump_host is not None  # only called when a jump host is configured

        jump = self._new_client()
        try:
            jump.connect(
                hostname=jump_host,
                port=self._jump_port,
                username=self._jump_username,
                password=self._jump_password,
                key_filename=self._jump_key_filename,
                timeout=self.timeout,
                allow_agent=False,
                look_for_keys=False,
            )
        except Exception as exc:  # noqa: BLE001
            jump.close()
            raise SSHError(
                "failed to connect to jump host",
                host=jump_host,
                port=self._jump_port,
                original_exception=exc,
            ) from exc

        self._apply_keepalive(jump)
        self._jump_client = jump
        transport = jump.get_transport()
        if transport is None:
            raise SSHError("jump host transport unavailable", jump_host=jump_host)
        try:
            # direct-tcpip: (destination host/port), (source host/port)
            channel = transport.open_channel(
                "direct-tcpip",
                (self.host, self.port),
                (jump_host, self._jump_port),
                timeout=self.timeout,
            )
        except Exception as exc:  # noqa: BLE001
            raise SSHError(
                "failed to open direct-tcpip tunnel",
                jump_host=jump_host,
                target=f"{self.host}:{self.port}",
                original_exception=exc,
            ) from exc
        logger.v2("opened direct-tcpip tunnel via %s -> %s:%s", jump_host, self.host, self.port)
        return channel

    # -- connection introspection --------------------------------------------

    @property
    def raw(self) -> paramiko.SSHClient:
        """Expose the underlying ``paramiko.SSHClient`` for advanced use.

        Raises :class:`SSHError` if the connection is not established.
        """
        if self._client is None:
            raise SSHError("not connected", host=self.host)
        return self._client

    def _is_alive(self) -> bool:
        """Return ``True`` if the transport is connected and authenticated."""
        if self._client is None:
            return False
        transport = self._client.get_transport()
        return transport is not None and transport.is_active()

    def _with_reconnect(self, action: str, fn: Callable[[], Any]) -> Any:
        """Run *fn*, auto-reconnecting once on a dead transport.

        Detects an inactive transport and reconnects a single time before
        retrying the failed operation.
        """
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            if self._is_alive():
                raise SSHError(f"{action} failed", host=self.host, original_exception=exc) from exc
            # Transport is dead -> reconnect once and retry.
            logger.v2("ssh transport inactive, reconnecting once host=%s", self.host)
            self.connect()
            try:
                return fn()
            except Exception as retry_exc:  # noqa: BLE001
                raise SSHError(
                    f"{action} failed after reconnect",
                    host=self.host,
                    original_exception=retry_exc,
                ) from retry_exc

    def verify(self, token: str = _DEFAULT_VERIFY_TOKEN) -> bool:
        """Connectivity gate: echo a sentinel token and assert it round-trips.

        Use after every (re)connect in multi-stage flows. Returns ``True`` when
        the echoed token appears in stdout.
        """
        result = self.execute(f"echo {token}")
        return result.ok and token in result.stdout

    def file_exist(self, remote_path: str) -> bool:
        """Return ``True`` if *remote_path* exists (via ``ls``)."""
        result = self.execute(f"ls -- {remote_path!r}")
        return result.exit_code == 0

    def close(self) -> None:
        """Close the target (and jump, if any) SSH connections."""
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._jump_client is not None:
            self._jump_client.close()
            self._jump_client = None

    def __enter__(self) -> SSHExecutor:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- command execution ----------------------------------------------------

    def _require_client(self) -> paramiko.SSHClient:
        if self._client is None:
            self.connect()
        assert self._client is not None
        return self._client

    def execute(
        self,
        command: str,
        timeout: float | None = None,
        raise_on_error: bool = False,
    ) -> SSHResult:
        """Execute a command and return a structured result.

        Parameters
        ----------
        command:
            Shell command to run.
        timeout:
            Per-command timeout (defaults to the connection timeout).
        raise_on_error:
            When ``True``, a non-zero exit code raises :class:`SSHError`.

        Returns
        -------
        SSHResult
            Structured stdout / stderr / exit_code / duration.
        """
        result = self._with_reconnect(
            "command execution",
            lambda: self._exec_once(command, timeout, raise_on_error),
        )
        assert isinstance(result, SSHResult)
        return result

    def _exec_once(
        self,
        command: str,
        timeout: float | None,
        raise_on_error: bool,
    ) -> SSHResult:
        client = self._require_client()
        started = time.monotonic()
        try:
            _stdin, stdout, stderr = client.exec_command(command, timeout=timeout or self.timeout)
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            exit_code = stdout.channel.recv_exit_status()
        except Exception as exc:  # noqa: BLE001
            raise SSHError(
                "command execution failed",
                command=command,
                host=self.host,
                original_exception=exc,
            ) from exc

        result = SSHResult(
            command=command,
            stdout=out,
            stderr=err,
            exit_code=exit_code,
            duration=time.monotonic() - started,
        )
        logger.v2("ssh exec exit=%s dur=%.3fs cmd=%r", exit_code, result.duration, command)
        if raise_on_error and exit_code != 0:
            raise SSHError(
                "command returned non-zero exit code",
                command=command,
                exit_code=exit_code,
                stderr=err,
            )
        return result

    # -- file transfer (SFTP) ------------------------------------------------

    def _open_sftp(self) -> paramiko.SFTPClient:
        client = self._require_client()
        try:
            return client.open_sftp()
        except Exception as exc:  # noqa: BLE001
            raise SSHError(
                "failed to open SFTP session", host=self.host, original_exception=exc
            ) from exc

    def scp_via_jump(self, local_path: str, remote_path: str) -> None:
        """Stream a *local* file to the target through this SSH connection.

        The file is uploaded in 64 KiB chunks so it is never loaded fully into
        memory. The target is reachable only via the current (possibly
        jump-tunnelled) connection.

        Parameters
        ----------
        local_path:
            Path to the local source file (on the machine running the test).
        remote_path:
            Destination path on the target host.
        """
        src = Path(local_path)
        if not src.is_file():
            raise SSHError("local source file not found", path=str(local_path))

        def _put() -> None:
            sftp = self._open_sftp()
            try:
                with src.open("rb") as fh:
                    with sftp.open(remote_path, "wb") as remote:
                        while True:
                            chunk = fh.read(_SFTP_CHUNK)
                            if not chunk:
                                break
                            remote.write(chunk)
            finally:
                sftp.close()

        self._with_reconnect("scp_via_jump", _put)
        logger.v2("scp via jump local=%s -> remote=%s", local_path, remote_path)

    def scp_via_double_jump(
        self,
        source_ssh: SSHExecutor,
        local_path: str,
        target_ip: str,
        remote_path: str,
        target_username: str | None = None,
        target_password: str | None = None,
        target_key_filename: str | None = None,
        target_port: int = 22,
    ) -> None:
        """Copy a file from a *source* host to a *target* via this connection.

        Three-hop flow: ``source_ssh`` (public IP) -> this host (private IP) ->
        ``target_ip`` (private IP). Bytes are read in chunks from ``source_ssh``
        and forwarded through a ``direct-tcpip`` tunnel opened from this host's
        transport to ``target_ip``; nothing is buffered in memory.

        Parameters
        ----------
        source_ssh:
            Executor already connected to the source host.
        local_path:
            Remote source file path on ``source_ssh``.
        target_ip:
            Destination host IP, reachable only from this host.
        remote_path:
            Destination path on ``target_ip``.
        target_username / target_password / target_key_filename / target_port:
            Credentials for the tunneled connection to ``target_ip``.
        """
        # 1) Read side: SFTP on the source executor.
        src_sftp = source_ssh._open_sftp()
        # 2) Write side: tunnel from this host's transport to target_ip:22.
        transport = self._require_client().get_transport()
        if transport is None:
            src_sftp.close()
            raise SSHError("transport unavailable for double jump", host=self.host)
        tunnel = transport.open_channel(
            "direct-tcpip",
            (target_ip, target_port),
            (self.host, self.port),
            timeout=self.timeout,
        )
        target_client = self._new_client()
        try:
            target_client.connect(
                hostname=target_ip,
                port=target_port,
                username=target_username,
                password=target_password,
                key_filename=target_key_filename,
                timeout=self.timeout,
                sock=tunnel,
                allow_agent=False,
                look_for_keys=False,
            )
            self._apply_keepalive(target_client)
            target_sftp = target_client.open_sftp()
        except Exception as exc:  # noqa: BLE001
            target_client.close()
            src_sftp.close()
            raise SSHError(
                "double-jump tunnel to target failed",
                target=target_ip,
                original_exception=exc,
            ) from exc

        try:
            with src_sftp.open(local_path, "rb") as src_fh:
                with target_sftp.open(remote_path, "wb") as dst_fh:
                    while True:
                        chunk = src_fh.read(_SFTP_CHUNK)
                        if not chunk:
                            break
                        dst_fh.write(chunk)
        finally:
            target_sftp.close()
            target_client.close()
            src_sftp.close()
        logger.v2(
            "scp double jump src=%s -> %s:%s%s",
            local_path,
            target_ip,
            target_port,
            remote_path,
        )

    def find_package(self, pattern: str, remote_dir: str = ".") -> str | None:
        """Glob-match a remote file name and return the first match.

        Parameters
        ----------
        pattern:
            ``fnmatch`` glob pattern (e.g. ``"pkg-*.tar.gz"``).
        remote_dir:
            Directory to list (default current directory).

        Returns
        -------
        str | None
            The full matched path, or ``None`` when nothing matches.
        """

        def _scan() -> str | None:
            sftp = self._open_sftp()
            try:
                entries = sftp.listdir(remote_dir)
            except Exception as exc:  # noqa: BLE001
                raise SSHError(
                    "failed to list remote dir", remote_dir=remote_dir, original_exception=exc
                ) from exc
            finally:
                sftp.close()
            for name in entries:
                if fnmatch.fnmatch(name, pattern):
                    return f"{remote_dir.rstrip('/')}/{name}" if remote_dir != "." else name
            return None

        result: str | None = self._with_reconnect("find_package", _scan)
        return result

    # -- architecture detection ----------------------------------------------

    def get_architecture(self, refresh: bool = False) -> str:
        """Return the target machine architecture (``uname -m``), cached."""
        if self._arch is not None and not refresh:
            return self._arch
        result = self.execute("uname -m")
        arch = result.stdout.strip()
        if result.exit_code != 0 or not arch:
            raise SSHError(
                "failed to detect architecture", command="uname -m", stderr=result.stderr
            )
        self._arch = arch
        logger.v2("detected architecture arch=%s", arch)
        return arch
