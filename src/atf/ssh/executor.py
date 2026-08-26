"""SSHExecutor:paramiko 封装,支持直连。

能力:

- **命令执行**:流式读取 stdout/stderr,带超时与退出码检查;
- **文件传输**:SFTP 上传/下载,或 ``scp`` 协议变体(零额外依赖);
- **架构探测**:`detect_arch` 返回 ``x86_64`` / ``aarch64`` 等,
  `probe_system` 返回 os/kernel/hostname 等概要;
- **日志脱敏**:命令与输出进入日志前统一过 :func:`atf.utils.sanitizer.mask_text`,
  口令、令牌、私钥不会落盘。

零业务耦合:不预设任何命令模板,业务侧自行组装命令字符串。
"""

from __future__ import annotations

import select
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Union

import paramiko

from atf.exceptions import TransportError
from atf.utils.log import get_logger
from atf.utils.sanitizer import DEFAULT_SANITIZER

_logger = get_logger("atf.ssh")


class SSHConnectError(TransportError):
    """建立 SSH 连接失败。"""


class SSHCommandError(TransportError):
    """命令退出码非零(check=True 时抛出)。

    Attributes:
        result: 完整的 :class:`CommandResult`,含脱敏后的输出。
    """

    def __init__(self, result: "CommandResult") -> None:
        self.result = result
        excerpt = (result.stderr or result.stdout).strip()[-500:]
        super().__init__(
            f"command exited with {result.exit_code}: {result.command!r}\n{excerpt}"
        )


class SSHTransferError(TransportError):
    """SFTP / SCP 传输失败。"""


class SSHTimeoutError(TransportError):
    """命令执行超过 timeout。"""


@dataclass
class CommandResult:
    """一次远程命令的执行结果。

    Attributes:
        command: 原始命令字符串。
        exit_code: 远端退出码(-1 表示未取到)。
        stdout / stderr: 合并了标准输出/错误(文本)。
        duration: 执行耗时秒数。
    """

    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration: float

    @property
    def ok(self) -> bool:
        """退出码是否为 0。"""
        return self.exit_code == 0

    @property
    def lines(self) -> List[str]:
        """stdout 按行切分(已去除行尾空白)。"""
        return [ln.rstrip() for ln in self.stdout.splitlines()]


class SSHExecutor:
    """paramiko 封装的远程执行器(线程不安全,每个线程各持一个实例)。"""

    def __init__(
        self,
        host: str,
        *,
        username: str = "root",
        password: Optional[str] = None,
        port: int = 22,
        key_file: Optional[str] = None,
        key_passphrase: Optional[str] = None,
        timeout: float = 10.0,
        sanitizer: Optional[Callable[[str], str]] = None,
        keepalive: int = 15,
    ) -> None:
        """创建执行器(惰性连接,首次 :meth:`run` 前会自动 :meth:`connect`)。

        Args:
            host: 主机地址。
            username: 登录用户。
            password: 口令认证(与 key_file 二选一,亦可都为空走 ssh-agent)。
            port: SSH 端口。
            key_file: 私钥文件路径。
            key_passphrase: 私钥口令。
            timeout: TCP/认证超时秒数。
            sanitizer: 日志脱敏器;缺省用进程级默认实例。
            keepalive: SSH keepalive 包间隔秒数(0 关闭)。
        """
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._key_file = key_file
        self._key_passphrase = key_passphrase
        self._timeout = timeout
        self._sanitizer = sanitizer or DEFAULT_SANITIZER
        self._keepalive = keepalive
        self._client: Optional[paramiko.SSHClient] = None

    # ------------------------------------------------------------ 生命周期

    @property
    def connected(self) -> bool:
        """底层 transport 是否处于活跃状态。"""
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active())

    def connect(self) -> "SSHExecutor":
        """建立连接(幂等,已连接则直接返回)。

        Raises:
            SSHConnectError: 连接(或认证)失败。
        """
        if self.connected:
            return self
        try:
            self._client = self._open()
            _logger.info("ssh connected %s@%s:%d",
                         self._username, self._host, self._port)
        except Exception as exc:
            self.close()
            raise SSHConnectError(
                f"cannot connect to {self._username}@{self._host}:{self._port}: {exc}"
            ) from exc
        return self

    def close(self) -> None:
        """关闭底层连接(幂等)。"""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001 - 关闭路径不抛
                pass
        self._client = None

    def __enter__(self) -> "SSHExecutor":
        return self.connect()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _open(self, sock: Optional[Any] = None) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: Dict[str, Any] = dict(
            hostname=self._host,
            port=self._port,
            username=self._username,
            timeout=self._timeout,
            banner_timeout=self._timeout,
            auth_timeout=self._timeout,
            allow_agent=True,
            look_for_keys=False,
            sock=sock,
        )
        if self._key_file:
            kwargs["key_filename"] = self._key_file
            if self._key_passphrase:
                kwargs["passphrase"] = self._key_passphrase
        elif self._password:
            kwargs["password"] = self._password
            kwargs["look_for_keys"] = False
        client.connect(**kwargs)
        if self._keepalive > 0:
            transport = client.get_transport()
            if transport is not None:
                transport.set_keepalive(self._keepalive)
        return client

    # ------------------------------------------------------------ 命令执行

    def run(
        self,
        command: str,
        *,
        timeout: Optional[float] = None,
        check: bool = True,
        env: Optional[Dict[str, str]] = None,
    ) -> CommandResult:
        """执行远程命令并等待结束。

        Args:
            command: shell 命令字符串(远端经 ``bash -lc`` 执行以获得登录 PATH)。
            timeout: 单命令超时秒数;缺省不限。
            check: 退出码非零时抛 :class:`SSHCommandError`。
            env: 附加环境变量(拼成 ``export K=V`` 前缀注入)。

        Returns:
            :class:`CommandResult`,输出为文本(stderr 与 stdout 分开)。

        Raises:
            SSHCommandError: ``check=True`` 且退出码非零。
            SSHTimeoutError: 超时。
            TransportError: 未连接或通道打开失败。
        """
        if not self.connected:
            self.connect()
        assert self._client is not None
        full_cmd = command
        if env:
            exports = " ".join(f"{k}={shell_quote(str(v))}" for k, v in env.items())
            full_cmd = f"export {exports}; {command}"
        wrapped = "bash -lc " + shell_quote(full_cmd)
        _logger.info("ssh run: %s", self._sanitizer(wrapped))
        start = time.monotonic()
        try:
            stdin, stdout, stderr = self._client.exec_command(wrapped)
        except Exception as exc:
            raise TransportError(f"failed to exec command on {self._host}: {exc}") from exc
        channel = stdout.channel
        out_chunks: List[bytes] = []
        err_chunks: List[bytes] = []
        deadline = (time.monotonic() + timeout) if timeout else None
        while True:
            while channel.recv_ready():
                out_chunks.append(channel.recv(65536))
            while channel.recv_stderr_ready():
                err_chunks.append(channel.recv_stderr(65536))
            if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                break
            if deadline is not None and time.monotonic() > deadline:
                channel.close()
                raise SSHTimeoutError(
                    f"command timed out after {timeout}s: {self._sanitizer(command)}"
                )
            select.select([channel], [], [], 0.1)
        exit_code = channel.recv_exit_status()
        # drain 残余数据
        while channel.recv_ready():
            out_chunks.append(channel.recv(65536))
        while channel.recv_stderr_ready():
            err_chunks.append(channel.recv_stderr(65536))
        duration = time.monotonic() - start
        result = CommandResult(
            command=self._sanitizer(command),
            exit_code=exit_code,
            stdout=b"".join(out_chunks).decode(errors="replace"),
            stderr=b"".join(err_chunks).decode(errors="replace"),
            duration=duration,
        )
        _logger.info(
            "ssh done: exit=%d %.2fs stdout=%dB stderr=%dB",
            exit_code, duration, len(result.stdout), len(result.stderr),
        )
        if check and exit_code != 0:
            raise SSHCommandError(result)
        return result

    # ------------------------------------------------------------ 文件传输

    def upload(
        self,
        local_path: Union[str, Path],
        remote_path: str,
        *,
        transfer: str = "sftp",
    ) -> None:
        """上传本地文件到远端。

        Args:
            local_path: 本地文件路径。
            remote_path: 远端目标路径。
            transfer: 传输后端,``"sftp"``(默认)或 ``"scp"``。

        Raises:
            SSHTransferError: 传输失败或后端不支持。
        """
        if transfer == "sftp":
            self._upload_sftp(local_path, remote_path)
        elif transfer == "scp":
            self._transfer_scp(str(local_path), remote_path, direction="put")
        else:
            raise SSHTransferError(f"unsupported transfer backend: {transfer!r}")

    def _upload_sftp(self, local_path: Union[str, Path], remote_path: str) -> None:
        local = Path(local_path)
        if not local.is_file():
            raise SSHTransferError(f"local file not found: {local}")
        sftp = self._sftp()
        try:
            sftp.put(str(local), remote_path)
            _logger.info("sftp uploaded %s -> %s@%s:%s",
                         local, self._username, self._host, remote_path)
        except Exception as exc:
            raise SSHTransferError(f"upload {local} -> {remote_path} failed: {exc}") from exc
        finally:
            sftp.close()

    def download(
        self,
        remote_path: str,
        local_path: Union[str, Path],
        *,
        transfer: str = "sftp",
    ) -> None:
        """下载远端文件到本地。

        Args:
            remote_path: 远端源路径。
            local_path: 本地目标路径。
            transfer: 传输后端,``"sftp"``(默认)或 ``"scp"``。

        Raises:
            SSHTransferError: 传输失败或后端不支持。
        """
        if transfer == "sftp":
            self._download_sftp(remote_path, local_path)
        elif transfer == "scp":
            self._transfer_scp(remote_path, str(local_path), direction="get")
        else:
            raise SSHTransferError(f"unsupported transfer backend: {transfer!r}")

    def _download_sftp(self, remote_path: str, local_path: Union[str, Path]) -> None:
        sftp = self._sftp()
        try:
            sftp.get(remote_path, str(local_path))
            _logger.info("sftp downloaded %s@%s:%s -> %s",
                         self._username, self._host, remote_path, local_path)
        except Exception as exc:
            raise SSHTransferError(f"download {remote_path} -> {local_path} failed: {exc}") from exc
        finally:
            sftp.close()

    def _sftp(self) -> Any:
        if not self.connected:
            self.connect()
        assert self._client is not None
        try:
            return self._client.open_sftp()
        except Exception as exc:
            raise SSHTransferError(f"cannot open sftp session: {exc}") from exc

    # ------------------------------------------------------------ SCP 传输变体

    def _transfer_scp(self, src: str, dst: str, *, direction: str) -> None:
        """通过 ``scp`` 协议(基于 paramiko channel,不引入额外依赖)传输文件。

        ``direction="put"`` 时 ``src`` 为本地路径、``dst`` 为远端路径;
        ``direction="get"`` 时相反。远端需具备 ``scp`` 命令(``scp -t`` / ``scp -f``)。
        """
        if not self.connected:
            self.connect()
        assert self._client is not None
        transport = self._client.get_transport()
        if transport is None:
            raise SSHTransferError("ssh transport unavailable for scp transfer")
        chan = transport.open_session()
        try:
            if direction == "put":
                self._scp_put(chan, src, dst)
            else:
                self._scp_get(chan, src, dst)
        except Exception as exc:
            raise SSHTransferError(
                f"scp {direction} {src} -> {dst} failed: {exc}"
            ) from exc
        finally:
            chan.close()

    @staticmethod
    def _scp_put(chan: Any, local_path: str, remote_path: str) -> None:
        local = Path(local_path)
        if not local.is_file():
            raise SSHTransferError(f"local file not found: {local}")
        chan.exec_command(f"scp -t {shell_quote(str(remote_path))}")
        SSHExecutor._scp_expect_ack(chan)  # 服务端就绪
        size = local.stat().st_size
        chan.send(f"C0644 {size} {local.name}\n".encode())
        SSHExecutor._scp_expect_ack(chan)  # 同意接收
        with local.open("rb") as fh:
            while True:
                chunk = fh.read(16384)
                if not chunk:
                    break
                chan.send(chunk)
        chan.send(b"\0")  # 文件结束标记
        SSHExecutor._scp_expect_ack(chan)  # 接收确认
        chan.send(b"\0")  # 传输结束

    @staticmethod
    def _scp_get(chan: Any, remote_path: str, local_path: str) -> None:
        local = Path(local_path)
        local.parent.mkdir(parents=True, exist_ok=True)
        chan.exec_command(f"scp -f {shell_quote(str(remote_path))}")
        chan.send(b"\0")  # 通知服务端开始发送
        line = SSHExecutor._scp_read_line(chan)
        if not line.startswith(b"C"):
            raise SSHTransferError(f"unexpected scp response: {line!r}")
        try:
            _, rest = line.split(b" ", 1)
            size_str, _name = rest.split(b" ", 1)
            size = int(size_str)
        except ValueError as exc:
            raise SSHTransferError(f"malformed scp C message: {line!r}") from exc
        chan.send(b"\0")  # 确认接收
        data = SSHExecutor._scp_recv_exact(chan, size)
        chan.send(b"\0")  # 数据确认
        SSHExecutor._scp_expect_ack(chan)  # 结束确认
        local.write_bytes(data)

    @staticmethod
    def _scp_expect_ack(chan: Any) -> None:
        """读取 1 字节:``\\0`` 表示成功;非 0 则其后为错误消息。"""
        resp = chan.recv(1)
        if not resp:
            raise SSHTransferError("scp connection closed before ack")
        if resp[0] != 0:
            msg = bytearray()
            while True:
                b = chan.recv(1)
                if not b or b == b"\n":
                    break
                msg += b
            raise SSHTransferError(
                f"scp error: {bytes(msg).decode(errors='replace')}"
            )

    @staticmethod
    def _scp_read_line(chan: Any) -> bytes:
        line = bytearray()
        while True:
            b = chan.recv(1)
            if not b:
                raise SSHTransferError("scp connection closed while reading response")
            if b == b"\n":
                break
            line += b
        return bytes(line)

    @staticmethod
    def _scp_recv_exact(chan: Any, n: int) -> bytes:
        buf = bytearray()
        while len(buf) < n:
            chunk = chan.recv(n - len(buf))
            if not chunk:
                raise SSHTransferError("scp connection closed mid-transfer")
            buf += chunk
        return bytes(buf)

    # ------------------------------------------------------------ 探测

    def detect_arch(self) -> str:
        """探测 CPU 架构,如 ``x86_64`` / ``aarch64``。

        Raises:
            SSHCommandError: ``uname -m`` 执行失败。
        """
        return self.run("uname -m", check=True).stdout.strip()

    def probe_system(self) -> Dict[str, str]:
        """探测系统概要:os / kernel / arch / hostname。

        通过一条命令批量取值,减少往返;解析失败的键值为 ``"unknown"``。
        """
        script = (
            'echo "hostname=$(hostname)"; '
            'echo "kernel=$(uname -r)"; '
            'echo "arch=$(uname -m)"; '
            'echo "os=$(grep PRETTY_NAME /etc/os-release 2>/dev/null '
            '| cut -d= -f2 | tr -d \'"\' || uname -s)"'
        )
        result = self.run(script, check=True)
        info: Dict[str, str] = {"host": self._host, "os": "unknown"}
        for line in result.lines:
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip() or "unknown"
        return info


def shell_quote(text: str) -> str:
    """POSIX 单引号转义,把任意字符串安全地作为一个 shell 词。"""
    return "'" + text.replace("'", "'\"'\"'") + "'"
