"""SSHExecutor 离线单元测试(不依赖真实 SSH 服务)。

连接/执行/传输的正向路径由 tests/sut/ 下的真实环境用例覆盖;
这里验证可离线验证的部分:目标建模、引号转义、连接失败的异常语义。
"""

from __future__ import annotations

import pytest

from atf.ssh import SSHExecutor, SSHTarget
from atf.ssh.executor import SSHConnectError, shell_quote


class TestShellQuote:
    def test_plain(self):
        assert shell_quote("ls -la") == "'ls -la'"

    def test_embedded_single_quote(self):
        assert shell_quote("it's") == "'it'\"'\"'s'"

    def test_roundtrip_through_bash_eval(self):
        # 转义后的字符串作为 bash 单词,值必须原样保留
        import subprocess
        import sys

        value = "a b'c\"d $HOME `id`"
        code = (
            "import sys; print(sys.argv[1])"
        )
        out = subprocess.run(
            [sys.executable, "-c", code, value], capture_output=True, text=True
        )
        assert out.stdout.strip() == value


class TestSSHTarget:
    def test_defaults(self):
        t = SSHTarget(host="1.2.3.4")
        assert (t.port, t.username, t.via) == (22, "root", None)

    def test_jump_chain_single(self):
        jump = SSHTarget(host="10.0.0.1", username="jump")
        target = SSHTarget(host="10.0.0.2", via=jump)
        assert target.via is jump

    def test_jump_chain_multi(self):
        # 支持链式跳板:target -> j1 -> j2(最外层)
        j2 = SSHTarget(host="10.0.0.9", username="j2")
        j1 = SSHTarget(host="10.0.0.8", username="j1", via=j2)
        target = SSHTarget(host="10.0.0.7", username="app", via=j1)
        assert target.via is j1 and j1.via is j2 and j2.via is None
        chain = SSHExecutor._hop_chain(target)
        assert [c.host for c in chain] == ["10.0.0.7", "10.0.0.8", "10.0.0.9"]


class TestMultiHopTunnel:
    def test_build_tunnel_opens_channels_inward(self, monkeypatch):
        import paramiko
        from unittest.mock import MagicMock

        # 构造 target -> j1 -> j2(最外层) 三层链
        j2 = SSHTarget(host="10.0.0.9", username="j2")
        j1 = SSHTarget(host="10.0.0.8", username="j1", via=j2)
        target = SSHTarget(host="10.0.0.7", username="app", via=j1)

        open_channels: list = []
        created: list = []

        def make_transport():
            t = MagicMock()
            t.is_active.return_value = True

            def open_channel(kind, dest, src):
                open_channels.append(dest)
                return MagicMock()

            t.open_channel.side_effect = open_channel
            return t

        def fake_ssh_client():
            c = MagicMock()
            c.get_transport.return_value = make_transport()
            created.append(c)
            return c

        monkeypatch.setattr(paramiko, "SSHClient", fake_ssh_client)

        ex = SSHExecutor(target)
        chain = SSHExecutor._hop_chain(target)
        sock = ex._build_tunnel(chain)

        # 最终通道目标必须是最终目标本身
        assert open_channels[-1] == ("10.0.0.7", 22)
        # 中间通道应包含第一跳 j1
        assert ("10.0.0.8", 22) in open_channels
        # 最外层 j2 是真实 TCP 直连,不通过 open_channel(不计入)
        # 每层跳板建立一个 SSH client(最外层 + 第一跳)
        assert len(created) == 2
        assert sock is not None

    def test_hop_chain_detects_cycle(self):
        j = SSHTarget(host="10.0.0.1")
        target = SSHTarget(host="10.0.0.2", via=j)
        j.via = target  # 人为制造环
        with pytest.raises(SSHConnectError, match="cyclic"):
            SSHExecutor._hop_chain(target)


class TestConnectFailure:
    def test_refused_connection_raises_connect_error(self):
        # 端口 1 上没有监听 → 连接被拒绝,快速失败并转成领域异常
        executor = SSHExecutor(SSHTarget(host="127.0.0.1", port=1, timeout=2.0))
        with pytest.raises(SSHConnectError):
            executor.connect()

    def test_close_is_idempotent(self):
        executor = SSHExecutor(SSHTarget(host="127.0.0.1", port=1))
        executor.close()
        executor.close()
        assert executor.connected is False

    def test_context_manager_closes(self):
        executor = SSHExecutor(SSHTarget(host="127.0.0.1", port=1, timeout=1.0))
        with pytest.raises(SSHConnectError):
            with executor:
                pass
        assert executor._client is None and executor._jump_clients == []


class TestCommandResult:
    def test_lines_and_ok(self):
        from atf.ssh.executor import CommandResult

        r = CommandResult("cmd", 0, "a\nb \n c", "", 0.1)
        assert r.ok and r.lines == ["a", "b", " c"]
        r2 = CommandResult("cmd", 1, "", "err", 0.1)
        assert not r2.ok


class _FakeScpChannel:
    """模拟 scp 服务端(sink/source),配合 SSHExecutor._transfer_scp 离线测试。

    只实现 ``exec_command`` / ``send`` / ``recv`` 三个方法,足以驱动
    ``_scp_put`` / ``_scp_get`` 的协议握手。
    """

    def __init__(self, mode: str, file_bytes: bytes = b"hello scp world\n"):
        self.mode = mode
        self.file_bytes = file_bytes
        self.exec_command_called = None
        self.sent = bytearray()
        self._buf = bytearray()
        self._phase = 0

    def exec_command(self, cmd):
        self.exec_command_called = cmd
        if self.mode == "put":
            self._buf += b"\0"  # 服务端就绪

    def send(self, data):
        self.sent += data
        if self.mode == "put":
            if data.lstrip().startswith(b"C"):
                self._buf += b"\0"  # 同意接收 C 消息
            else:
                self._buf += b"\0"  # 文件/结束确认
        else:  # get:source
            if self._phase == 0 and data == b"\0":
                self._buf += b"C0644 " + str(len(self.file_bytes)).encode() + b" remote.bin\n"
                self._phase = 1
            elif self._phase == 1 and data == b"\0":
                self._buf += self.file_bytes
                self._phase = 2
            elif self._phase == 2 and data == b"\0":
                self._buf += b"\0"

    def recv(self, n):
        if not self._buf:
            return b""
        chunk = bytes(self._buf[:n])
        del self._buf[:n]
        return chunk


class TestScpTransfer:
    def test_scp_put_protocol(self, tmp_path):
        chan = _FakeScpChannel("put")
        local = tmp_path / "f.txt"
        local.write_text("hello scp world\n")
        SSHExecutor._scp_put(chan, str(local), "/remote/f.txt")
        sent = bytes(chan.sent)
        assert chan.exec_command_called == "scp -t '/remote/f.txt'"
        assert b"C0644 " in sent
        assert b"hello scp world\n" in sent

    def test_scp_get_protocol(self, tmp_path):
        chan = _FakeScpChannel("get", file_bytes=b"remote payload\n")
        local = tmp_path / "out.bin"
        SSHExecutor._scp_get(chan, "/remote/f.txt", str(local))
        assert chan.exec_command_called == "scp -f '/remote/f.txt'"
        assert local.read_bytes() == b"remote payload\n"

    def test_upload_dispatch_to_scp(self, tmp_path, monkeypatch):
        ex = SSHExecutor(SSHTarget(host="1.2.3.4"))
        calls: dict = {}

        def fake_transfer(src, dst, *, direction):
            calls["direction"] = direction

        monkeypatch.setattr(ex, "_transfer_scp", fake_transfer)
        ex.upload("/local", "/remote", transfer="scp")
        assert calls["direction"] == "put"
        ex.download("/remote", "/local", transfer="scp")
        assert calls["direction"] == "get"

    def test_unknown_backend_rejected(self):
        from atf.ssh.executor import SSHTransferError

        ex = SSHExecutor(SSHTarget(host="1.2.3.4"))
        with pytest.raises(SSHTransferError, match="unsupported"):
            ex.upload("/x", "/y", transfer="ftp")
