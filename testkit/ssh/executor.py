"""SSH command executor with direct and jump-host (direct-tcpip) modes.

Commands return a structured :class:`SSHResult` (stdout / stderr /
exit_code / duration). In jump-host mode the channel is opened through the
existing SSH connection's transport via a ``direct-tcpip`` channel, with no
intermediate shell on the jump host.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from types import TracebackType

import paramiko

from testkit.exceptions import SSHError
from testkit.logging_setup import get_logger

logger = get_logger("ssh")


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

        self._client: paramiko.SSHClient | None = None
        self._jump_client: paramiko.SSHClient | None = None
        self._arch: str | None = None

    # -- connection lifecycle -------------------------------------------------

    def connect(self) -> None:
        """Establish the (possibly tunnelled) SSH connection."""
        sock: paramiko.Channel | None = None
        if self._jump_host is not None:
            sock = self._open_jump_tunnel()

        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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

        self._client = client
        logger.v2("ssh connected host=%s:%s jump=%s", self.host, self.port, self._jump_host)

    def _open_jump_tunnel(self) -> paramiko.Channel:
        """Open a ``direct-tcpip`` channel through the jump host transport."""
        jump_host = self._jump_host
        assert jump_host is not None  # only called when a jump host is configured

        jump = paramiko.SSHClient()
        jump.set_missing_host_key_policy(paramiko.AutoAddPolicy())
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
