"""conftest 示例:六大模块的总装现场。

演示如何把 ConfigLoader / BaseClient / SSHExecutor / ResourcePool /
SharedFixtureGuard / TestContext 组装成 pytest fixtures,支持 xdist 并发。

层级与作用域::

    session   app_config      ConfigLoader 加载并校验配置
    session   resource_pool   资源池(文件锁跨进程互斥)
    session   fixture_guard   共享 fixture 守护
    session   shared_env      “创建一次、各 worker 复用”的环境(示例)
    session   api_client      REST 客户端(认证 + 重试 + 摘要日志)
    session   ssh_executor    SSH 执行器(直连,惰性连接)
    function  ctx             用例级资源登记簿(用例结束自动清理)
    function  compute_host    从资源池申请一台机器,自动归还
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from atf.config import ConfigLoader
from atf.context import TestContext
from atf.fixtures import SharedFixtureGuard
from atf.http import BaseClient
from atf.http.auth import build_auth
from atf.pool import ResourcePool
from atf.ssh import SSHExecutor
from atf.utils import setup_logging

from schemas import AppConfig

# examples/ 作为可 clone 改造的模板,自包含:ROOT 指向本目录,
# 其下的 config/(examples/config)与运行时 .state/ 都落在 examples/ 内。
ROOT = Path(__file__).resolve().parent


@pytest.fixture(scope="session")
def worker_id() -> str:
    """当前 xdist worker 标识(单进程运行为 "master")。"""
    return os.environ.get("PYTEST_XDIST_WORKER", "master")


@pytest.fixture(scope="session")
def app_config() -> AppConfig:
    """加载多环境配置:环境优先级 显式入参 > ATF_ENV > default_env。"""
    setup_logging("INFO", log_file=ROOT / ".state" / "atf.log")
    return ConfigLoader(AppConfig, ROOT / "config" / "config.yaml").load(
        env=os.environ.get("ATF_ENV") or None
    )


@pytest.fixture(scope="session")
def resource_pool(app_config: AppConfig, pool_owner: str) -> ResourcePool:
    """打开资源池;session 结束时归还本 worker 未释放的资源(防泄漏)。"""
    pool = ResourcePool(
        ROOT / app_config.pool.path,
        lock_timeout=app_config.pool.lock_timeout,
    )
    yield pool
    pool.release_all(owner=pool_owner)


@pytest.fixture(scope="session")
def pool_owner(worker_id: str) -> str:
    """资源池持有者标识:worker id(xdist)或 master。"""
    return worker_id


@pytest.fixture(scope="session")
def fixture_guard(app_config: AppConfig) -> SharedFixtureGuard:
    """共享 fixture 守护:状态落在 .state/guard.json(跨进程可见)。"""
    return SharedFixtureGuard(
        ROOT / app_config.guard.state_file,
        wait_ready_timeout=app_config.guard.wait_ready_timeout,
        takeover_after=app_config.guard.takeover_after,
    )


@pytest.fixture(scope="session")
def shared_env(app_config: AppConfig, fixture_guard: SharedFixtureGuard, worker_id: str):
    """示例:昂贵环境“创建一次、各 worker 复用、最后退出者清理”。

    真实项目里 create 可以是“部署一套临时拓扑”“申请 License”等;
    这里返回一个可 JSON 化的标记,演示 xdist 下只有一个进程执行 create。
    """
    marker_file = ROOT / ".state" / "env_created.marker"

    def create() -> dict:
        marker_file.parent.mkdir(parents=True, exist_ok=True)
        marker_file.write_text("created", encoding="utf-8")
        return {"name": "demo-env", "created_by": worker_id}

    def teardown(value: dict) -> None:
        marker_file.unlink(missing_ok=True)

    with fixture_guard.shared("demo-env", create, teardown, owner=worker_id) as fx:
        yield fx


@pytest.fixture(scope="session")
def api_client(app_config: AppConfig) -> BaseClient:
    """REST 客户端:认证策略、全局附加头、重试、摘要日志一次配齐。"""
    http = app_config.http
    client = BaseClient(
        http.base_url,
        auth=build_auth(http.auth.model_dump(exclude_none=True)),
        extra_headers=http.extra_headers,
        timeout=http.timeout,
        verify=http.verify_ssl,
    )
    yield client
    client.close()


@pytest.fixture(scope="session")
def ssh_executor(app_config: AppConfig) -> SSHExecutor:
    """SSH 执行器(惰性连接,首个命令时才建链)。"""
    cfg = app_config.ssh
    executor = SSHExecutor(
        cfg.host,
        username=cfg.username,
        password=cfg.password,
        port=cfg.port,
        key_file=cfg.key_file,
        timeout=cfg.timeout,
    )
    yield executor
    executor.close()


@pytest.fixture
def ctx(request: pytest.FixtureRequest) -> TestContext:
    """用例级资源登记簿:用例/fixture 结束时 LIFO 自动清理。

    与 SharedFixtureGuard 配合的姿势见 compute_host / unit 测试。
    """
    context = TestContext(name=f"{request.node.nodeid}")
    yield context
    executed, failures = context.cleanup()
    assert not failures, f"cleanup failures: {failures}"


@pytest.fixture
def compute_host(
    ctx: TestContext,
    resource_pool: ResourcePool,
    pool_owner: str,
) -> dict:
    """申请一台 role=compute 的机器,用例结束自动归还(占满则等待重试)。

    xdist 下多个 worker 同时申请,FileLock 保证互斥;归还动作注册进
    TestContext,断言失败也会执行。
    """
    host = resource_pool.acquire(
        role="compute",
        owner=pool_owner,
        retries=10,
        interval=0.5,
    )
    return ctx.register(
        host,
        lambda record: resource_pool.release(record["id"], owner=pool_owner),
        description=f"release pool resource {host['id']}",
    )
