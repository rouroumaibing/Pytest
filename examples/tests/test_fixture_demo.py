"""六大模块 fixture 注册示例(离线、无需真实被测环境)。

演示如何在 conftest 已注册的 fixture 之上编写用例,覆盖:

- :class:`~atf.config.ConfigLoader`  → ``app_config``(配置加载与校验)
- :class:`~atf.pool.ResourcePool`    → ``resource_pool`` / ``compute_host``(跨进程资源池)
- :class:`~atf.fixtures.SharedFixtureGuard` → ``fixture_guard`` / ``shared_env``(创建/清理互斥)
- :class:`~atf.http.BaseClient`      → ``api_client``(REST 客户端,见 test_sut_examples)
- :class:`~atf.ssh.SSHExecutor`      → ``ssh_executor``(SSH 执行器,见 test_sut_examples)
- :class:`~atf.context.TestContext`  → ``ctx``(用例级清理登记簿)

真实 SSH / REST 用例见 ``test_sut_examples.py``(需 ``ATF_SUT=1`` 才运行)。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from atf.context import TestContext
from atf.fixtures import SharedFixtureGuard
from atf.pool import ResourcePool

EXAMPLES_ROOT = Path(__file__).resolve().parents[1]


def test_config_loader_fixture(app_config) -> None:
    """``app_config`` 由 ConfigLoader 加载并校验(零业务耦合)。"""
    assert app_config.http.base_url
    assert app_config.pool.path


def test_context_registers_and_cleans_up() -> None:
    """TestContext:LIFO 自动清理,错误隔离。"""
    events: list[str] = []

    def cleanup(tag: str):
        events.append(f"cleanup-{tag}")

    with TestContext("demo") as ctx:
        ctx.register("a", lambda _: cleanup("a"), description="a")
        ctx.register("b", lambda _: cleanup("b"), description="b")
    # LIFO:b 先清理,再 a
    assert events == ["cleanup-b", "cleanup-a"]


def test_shared_fixture_guard_tmp(tmp_path) -> None:
    """SharedFixtureGuard:create 一次、多方复用、最后退出者清理。"""
    state = tmp_path / "guard.json"
    guard = SharedFixtureGuard(state, lock_timeout=5, poll_interval=0.02)
    creates: list[int] = []

    def create() -> dict:
        creates.append(1)
        return {"ok": True}

    with guard.shared("demo", create, owner="w0") as fx0:
        assert fx0.role == "creator"
        with guard.shared("demo", create, owner="w1") as fx1:
            assert fx1.role == "user"
    assert creates.count(1) == 1  # create 全局仅一次
    assert "demo" not in guard.entries()


def test_resource_pool_acquire_release(tmp_path) -> None:
    """ResourcePool:从种子文件分配并归还(用副本,不污染示例库存)。"""
    seed = EXAMPLES_ROOT / "config" / "resource_pool.yaml"
    pool_file = tmp_path / "pool.yaml"
    if seed.is_file():
        shutil.copy(seed, pool_file)
    pool = ResourcePool(pool_file, lock_timeout=5)
    if pool.stats().get("free"):
        got = pool.acquire(role="compute", owner="demo")
        assert got["role"] == "compute"
        pool.release(got["id"], owner="demo")
        assert pool.get(got["id"])["state"] == "free"
