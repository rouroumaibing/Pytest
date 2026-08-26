"""SSHExecutor:paramiko 封装,支持直连与跳板(tunnel 转发)。

能力:

- **直连 / 多层跳板**:``SSHTarget(via=...)`` 声明跳板,``via`` 可嵌套形成多层
  跳板链;``connect()`` 沿链逐跳建立 ``direct-tcpip`` 隧道(对上层完全透明);
- **命令执行**:流式读取 stdout/stderr,带超时与退出码检查;
- **文件传输**:SFTP 上传/下载;
- **架构探测**:`detect_arch` 返回 ``x86_64`` / ``aarch64`` 等,
  `probe_system` 返回 os/kernel/hostname 等概要;
- **日志脱敏**:命令与输出进入日志前统一过 :class:`Sanitizer`,
  口令、令牌、私钥不会落盘。

零业务耦合:不预设任何命令模板,业务侧自行组装命令字符串。
"""

from __future__ import annotations

import select
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import paramiko

from atf.exceptions import SSHError
from atf.utils.log import get_logger
from atf.utils.sanitizer import DEFAULT_SANITIZER, Sanitizer

_logger = get_logger("atf.ssh")


class SSHConnectError(SSHError):
    """建立 SSH 连接(直连或隧道)失败。"""


class SSHCommandError(SSHError):
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


class SSHTransferError(SSHError):
    """SFTP 传输失败。"""


class SSHTimeoutError(SSHError):
    """命令执行超过 timeout。"""


@dataclass
class SSHTarget:
    """一个 SSH 连接目标(直连目标或跳板机各用一个实例)。

    Attributes:
        host: 主机地址。
        port: SSH 端口。
        username: 登录用户。
        password: 口令认证(与 key_file 二选一,亦可都为空走 ssh-agent)。
        key_file: 私钥文件路径。
        key_passphrase: 私钥口令。
        timeout: TCP/认证超时秒数。
        via: 跳板机;可嵌套形成多层跳板链(target 经 j1 经 j2 … 最终到目标),
            由 ``connect()`` 沿 ``via`` 链逐跳建立 ``direct-tcpip`` 隧道。
    """

    host: str
    port: int = 22
    username: str = "root"
    password: Optional[str] = None
    key_file: Optional[str] = None
    key_passphrase: Optional[str] = None
    timeout: float = 10.0
    via: Optional["SSHTarget"] = None


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
        target: SSHTarget,
        *,
        sanitizer: Optional[Sanitizer] = None,
        keepalive: int = 15,
    ) -> None:
        """创建执行器(惰性连接,首次 :meth:`run` 前会自动 :meth:`connect`)。

        Args:
            target: 连接目标,含可选跳板。
            sanitizer: 日志脱敏器;缺省用进程级默认实例。
            keepalive: SSH keepalive 包间隔秒数(0 关闭)。
        """
        self._target = target
        self._sanitizer = sanitizer or DEFAULT_SANITIZER
        self._keepalive = keepalive
        self._client: Optional[paramiko.SSHClient] = None
        self._jump_clients: List[paramiko.SSHClient] = []

    # ------------------------------------------------------------ 生命周期

    @property
    def connected(self) -> bool:
        """底层 transport 是否处于活跃状态。"""
        transport = self._client.get_transport() if self._client else None
        return bool(transport and transport.is_active())

    def connect(self) -> "SSHExecutor":
        """建立连接(幂等,已连接则直接返回)。

        无跳板时直连;有跳板时沿 ``via`` 链逐跳建立 ``direct-tcpip`` 隧道,
        支持多层跳板链(``via`` 可嵌套)。

        Raises:
            SSHConnectError: 直连或任一跳板环节失败。
        """
        if self.connected:
            return self
        try:
            chain = self._hop_chain(self._target)
            if len(chain) == 1:
                self._client = self._open(self._target)
                _logger.info("ssh connected %s@%s:%d",
                             self._target.username, self._target.host, self._target.port)
            else:
                sock = self._build_tunnel(chain)
                self._client = self._open(self._target, sock=sock)
                hops = " -> ".join(f"{c.username}@{c.host}" for c in reversed(chain))
                _logger.info("ssh tunneled through %d jump(s): %s",
                             len(chain) - 1, hops)
        except Exception as exc:
            self.close()
            raise SSHConnectError(
                f"cannot connect to {self._target.username}@{self._target.host}:"
                f"{self._target.port}: {exc}"
            ) from exc
        return self

    @staticmethod
    def _hop_chain(target: SSHTarget) -> List[SSHTarget]:
        """展开跳板链:[最终目标, 第一跳, ..., 最外层跳板]。

        检测环(``via`` 回指已出现节点)避免无限递归。
        """
        chain: List[SSHTarget] = [target]
        seen = {id(target)}
        node = target.via
        while node is not None:
            if id(node) in seen:
                raise SSHConnectError("cyclic jump chain detected")
            seen.add(id(node))
            chain.append(node)
            node = node.via
        return chain

    def _build_tunnel(self, chain: List[SSHTarget]) -> Any:
        """沿跳板链从最外层向内逐跳建立隧道,返回最终到目标的通道。

        所有中间跳板 client 存入 ``self._jump_clients`` 以便关闭。调用方负责
        把返回值作为 ``sock`` 传给 ``self._open(最终目标)``。
        """
        hops_inward = list(reversed(chain))  # [最外层, ..., 最终目标]
        client = self._open(hops_inward[0])  # 真实连接最外层跳板
        self._jump_clients.append(client)
        transport = client.get_transport()
        for nxt in hops_inward[1:-1]:  # 跳过最外层(已连)与最终目标(最后开通道)
            chan = transport.open_channel(
                "direct-tcpip", (nxt.host, nxt.port), ("127.0.0.1", 0)
            )
            client = self._open(nxt, sock=chan)  # 经通道连下一跳
            self._jump_clients.append(client)
            transport = client.get_transport()
        return transport.open_channel(
            "direct-tcpip", (chain[0].host, chain[0].port), ("127.0.0.1", 0)
        )

    def close(self) -> None:
        """关闭目标连接与所有跳板连接(幂等)。"""
        for client in (*self._jump_clients, self._client):
            if client is not None:
                try:
                    client.close()
                except Exception:  # noqa: BLE001 - 关闭路径不抛
                    pass
        self._client = None
        self._jump_clients = []

    def __enter__(self) -> "SSHExecutor":
        return self.connect()

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def _open(self, target: SSHTarget, sock: Optional[Any] = None) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        kwargs: Dict[str, Any] = dict(
            hostname=target.host,
            port=target.port,
            username=target.username,
            timeout=target.timeout,
            banner_timeout=target.timeout,
            auth_timeout=target.timeout,
            allow_agent=True,
            look_for_keys=False,
            sock=sock,
        )
        if target.key_file:
            kwargs["key_filename"] = target.key_file
            if target.key_passphrase:
                kwargs["passphrase"] = target.key_passphrase
        elif target.password:
            kwargs["password"] = target.password
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
            SSHError: 未连接或通道打开失败。
        """
        if not self.connected:
            self.connect()
        assert self._client is not None
        full_cmd = command
        if env:
            exports = " ".join(f"{k}={shell_quote(str(v))}" for k, v in env.items())
            full_cmd = f"export {exports}; {command}"
        wrapped = "bash -lc " + shell_quote(full_cmd)
        _logger.info("ssh run: %s", self._sanitizer.mask_text(wrapped))
        start = time.monotonic()
        try:
            stdin, stdout, stderr = self._client.exec_command(wrapped)
        except Exception as exc:
            raise SSHError(f"failed to exec command on {self._target.host}: {exc}") from exc
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
                    f"command timed out after {timeout}s: {self._sanitizer.mask_text(command)}"
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
            command=self._sanitizer.mask_text(command),
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
                         local, self._target.username, self._target.host, remote_path)
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
                         self._target.username, self._target.host, remote_path, local_path)
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
        info: Dict[str, str] = {"host": self._target.host, "os": "unknown"}
        for line in result.lines:
            if "=" in line:
                key, _, value = line.partition("=")
                info[key.strip()] = value.strip() or "unknown"
        return info


def shell_quote(text: str) -> str:
    """POSIX 单引号转义,把任意字符串安全地作为一个 shell 词。"""
    return "'" + text.replace("'", "'\"'\"'") + "'"
