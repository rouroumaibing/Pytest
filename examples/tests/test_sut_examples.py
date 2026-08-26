"""真实被测环境(SUT)示例用例。

这些用例演示框架的完整用法,需要真实的 REST API / SSH 端点:

    ATF_ENV=qa .venv/bin/python -m pytest tests/sut -m sut -v

没有真实环境时它们会被跳过(见下方 fixture)。
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.sut


@pytest.fixture(scope="session")
def sut_available(app_config) -> bool:
    """真实环境探测:ATF_SUT=1 显式开启,否则跳过。"""
    return os.environ.get("ATF_SUT") == "1"


@pytest.fixture(autouse=True)
def _require_sut(sut_available):
    if not sut_available:
        pytest.skip("set ATF_SUT=1 (and point config/ to a real system) to run SUT tests")


class TestRestApi:
    """REST API 冒烟:登录态、业务读、负向用例。"""

    def test_echo_roundtrip(self, api_client):
        resp = api_client.get("/echo")
        assert resp.status_code == 200

    def test_create_and_read_back(self, api_client, ctx):
        created = api_client.post("/items", json_body={"name": "atf-demo"}).json()

        # 业务对象注册进上下文:即使断言失败也会清理
        ctx.add_finalizer(
            lambda: api_client.delete(f"/items/{created['id']}", raise_for_status=False),
            description=f"delete item {created.get('id')}",
        )
        fetched = api_client.get(f"/items/{created['id']}").json()
        assert fetched["name"] == "atf-demo"

    def test_negative_case(self, api_client):
        resp = api_client.get("/items/definitely-missing", raise_for_status=False)
        assert resp.status_code == 404


class TestSsh:
    """SSH 冒烟:命令、探测、文件往返。"""

    def test_run_command(self, ssh_executor):
        result = ssh_executor.run("echo hello-atf")
        assert "hello-atf" in result.stdout

    def test_detect_arch(self, ssh_executor):
        assert ssh_executor.detect_arch() in {"x86_64", "aarch64", "arm64", "i686", "riscv64"}

    def test_probe_system(self, ssh_executor):
        info = ssh_executor.probe_system()
        assert info.get("hostname", "unknown") != ""

    def test_upload_download_roundtrip(self, ssh_executor, tmp_path):
        local = tmp_path / "payload.txt"
        local.write_text("roundtrip", encoding="utf-8")
        remote = "/tmp/atf-payload.txt"
        ssh_executor.upload(local, remote)
        back = tmp_path / "back.txt"
        ssh_executor.download(remote, back)
        assert back.read_text() == "roundtrip"
        ssh_executor.run(f"rm -f {remote}", check=False)


class TestResourcePool:
    """资源池在真实用例中的姿势:申请 compute 机器 → 执行 → 归还。"""

    def test_use_compute_host(self, compute_host, ssh_executor):
        # compute_host 已从池中分配并在用例结束自动归还(conftest 里接线)
        assert compute_host["role"] == "compute"
