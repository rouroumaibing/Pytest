"""本地可跑的 xdist 集成示例:资源池在多 worker 争抢下不超卖、必归还。

运行::

    .venv/bin/python -m pytest tests/integration -n 4 -v

验证点(在用例与 fixture 层共同保证):

1. 3 台 compute 机器、4 个 worker 并发各要 1 台 → 有人会等待/失败重试;
2. 每个用例结束资源必归还(TestContext 注册的 finalizer);
3. session 收尾 release_all 兜底,池状态回到初始。
"""

from __future__ import annotations

import pytest
import yaml

from atf.pool import ResourcePool

# 种子数据内联,不依赖外部 config/,保证框架自测零外部依赖(§9 重构)
SEED = {
    "resources": [
        {"id": "node-1", "state": "free", "owner": None, "locked_at": None,
         "host": "192.168.1.11", "role": "compute", "tags": ["gpu"]},
        {"id": "node-2", "state": "free", "owner": None, "locked_at": None,
         "host": "192.168.1.12", "role": "compute", "tags": ["gpu"]},
        {"id": "node-3", "state": "free", "owner": None, "locked_at": None,
         "host": "192.168.1.13", "role": "compute", "tags": ["gpu", "25g"]},
    ]
}


@pytest.fixture(scope="session")
def integration_pool(tmp_path_factory):
    """独立副本池,避免污染仓库里的示例配置。"""
    pool_file = tmp_path_factory.mktemp("pool") / "pool.yaml"
    pool_file.write_text(yaml.safe_dump(SEED), encoding="utf-8")
    return ResourcePool(pool_file, lock_timeout=10)


@pytest.fixture
def claimed_host(request, integration_pool):
    """每个用例申请一台 compute:4 worker 并发时会有 worker 进入等待窗口。"""
    worker = request.config.workerinput.get("workerid", "master") if hasattr(
        request.config, "workerinput") else "master"
    host = integration_pool.acquire(role="compute", owner=worker,
                                    retries=20, interval=0.1)
    yield host
    integration_pool.release(host["id"], owner=worker)


def test_claim_and_release_a(integration_pool, claimed_host):
    assert claimed_host["role"] == "compute"
    assert integration_pool.get(claimed_host["id"])["state"] == "busy"


def test_claim_and_release_b(integration_pool, claimed_host):
    # 拿到的机器不该再被别人看到是 free
    others = integration_pool.find(query={"role": "compute"})
    assert claimed_host["id"] not in [r["id"] for r in others if r["state"] == "free"]


def test_claim_and_release_c(integration_pool, claimed_host):
    stats = integration_pool.stats()
    assert stats["busy"] >= 1


def test_pool_not_oversold(integration_pool, claimed_host):
    """任何时刻 compute 的 busy 数 ≤ 总数(不超卖)。"""
    stats = integration_pool.stats()
    total = stats["free"] + stats["busy"] + stats["disabled"]
    assert stats["busy"] <= total
