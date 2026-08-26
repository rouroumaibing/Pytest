"""ResourcePool 单元测试(离线可跑,不依赖真实被测系统)。"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
import yaml

_SRC = str(Path(__file__).resolve().parents[2] / "src")
_CHILD_ENV = {**os.environ,
              "PYTHONPATH": _SRC + os.pathsep + os.environ.get("PYTHONPATH", "")}

from atf.exceptions import (
    ResourceExhaustedError,
    ResourceNotFoundError,
    ResourceStateError,
)
from atf.pool import ResourcePool


@pytest.fixture
def pool(tmp_path):
    """三台机器的临时资源池。"""
    p = ResourcePool(tmp_path / "pool.yaml", lock_timeout=5)
    p.add({"host": "10.0.0.1", "role": "storage", "tags": ["ssd"]}, resource_id="node-1")
    p.add({"host": "10.0.0.2", "role": "compute", "tags": ["gpu"]}, resource_id="node-2")
    p.add({"host": "10.0.0.3", "role": "compute", "tags": ["gpu", "25g"]}, resource_id="node-3")
    return p


class TestBasic:
    def test_add_and_get(self, pool):
        record = pool.get("node-1")
        assert record["host"] == "10.0.0.1"
        assert record["state"] == "free"

    def test_add_rejects_reserved_keys(self, pool):
        with pytest.raises(Exception, match="reserved"):
            pool.add({"state": "busy"})

    def test_duplicate_id_rejected(self, pool):
        with pytest.raises(Exception, match="duplicate"):
            pool.add({"host": "x"}, resource_id="node-1")

    def test_get_missing_raises(self, pool):
        with pytest.raises(ResourceNotFoundError):
            pool.get("nope")

    def test_stats(self, pool):
        assert pool.stats() == {"free": 3, "busy": 0, "disabled": 0}
        pool.acquire(role="storage")
        assert pool.stats()["busy"] == 1

    def test_disable_prevents_allocation(self, pool):
        pool.set_enabled("node-2", False)
        got = pool.acquire(role="compute")
        assert got["id"] == "node-3"
        with pytest.raises(ResourceExhaustedError):
            pool.acquire(role="compute", retries=0)


class TestAcquire:
    def test_acquire_by_query(self, pool):
        got = pool.acquire(role="compute", owner="w0")
        assert got["role"] == "compute"
        assert got["owner"] == "w0"
        assert got["state"] == "busy"

    def test_acquire_by_filter(self, pool):
        got = pool.acquire(filter=lambda r: "25g" in r.get("tags", []), owner="w0")
        assert got["id"] == "node-3"

    def test_acquire_query_and_filter_combined(self, pool):
        pool.acquire(role="compute", owner="w0")  # 先占一台
        got = pool.acquire(
            role="compute", filter=lambda r: r["id"] != "node-2", owner="w1"
        )
        assert got["id"] == "node-3"

    def test_acquire_exhaustion(self, pool):
        pool.acquire(role="compute", owner="w0")
        pool.acquire(role="compute", owner="w1")
        with pytest.raises(ResourceExhaustedError):
            pool.acquire(role="compute", retries=1, interval=0.05)

    def test_acquire_retries_until_released(self, pool, tmp_path):
        pool.acquire(role="compute", owner="w0")
        pool.acquire(role="compute", owner="w1")
        # 另一个持有者在重试窗口内归还一台(独立实例 → 独立 flock,真互斥)
        def releaser():
            other = ResourcePool(tmp_path / "pool.yaml", lock_timeout=5)
            other.release("node-2", owner="w0")

        timer = threading.Timer(0.15, releaser)
        timer.start()
        got = pool.acquire(role="compute", retries=10, interval=0.1)
        timer.join()
        assert got["id"] == "node-2"

    def test_release_owner_mismatch(self, pool):
        pool.acquire(role="storage", owner="w0")
        with pytest.raises(ResourceStateError):
            pool.release("node-1", owner="someone-else")

    def test_release_all_for_owner(self, pool):
        pool.acquire(role="compute", owner="w0")
        pool.acquire(role="storage", owner="w0")
        assert pool.release_all(owner="w0") == ["node-1", "node-2"]
        assert pool.stats() == {"free": 3, "busy": 0, "disabled": 0}

    def test_records_are_copies(self, pool):
        got = pool.acquire(role="storage", owner="w0")
        got["host"] = "tampered"
        assert pool.get("node-1")["host"] == "10.0.0.1"


class TestBatch:
    def test_batch_ok(self, pool):
        got = pool.acquire_batch(2, role="compute", owner="w0")
        assert len(got) == 2
        assert {r["id"] for r in got} == {"node-2", "node-3"}

    def test_batch_not_enough_is_atomic(self, pool):
        with pytest.raises(ResourceExhaustedError):
            pool.acquire_batch(3, role="compute", retries=1, interval=0.05)
        assert pool.stats()["busy"] == 0  # 一个都没占用

    def test_batch_invalid_count(self, pool):
        with pytest.raises(Exception, match="count"):
            pool.acquire_batch(0)


_ACQUIRE_SNIPPET = textwrap.dedent(
    """
    import sys
    from atf.pool import ResourcePool
    pool = ResourcePool(sys.argv[1], lock_timeout=10)
    try:
        rec = pool.acquire(role="compute", owner=sys.argv[2], retries=0)
        print("OK", rec["id"])
    except Exception as exc:
        print("FAIL", type(exc).__name__)
    """
)


class TestCrossProcess:
    def test_two_processes_cannot_take_same_resource(self, tmp_path):
        """两个进程争抢唯一资源:恰好一个成功,另一个耗尽失败。"""
        pool_file = tmp_path / "mp_pool.yaml"
        ResourcePool(pool_file, lock_timeout=10).add(
            {"host": "10.9.9.9", "role": "compute"}, resource_id="solo"
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _ACQUIRE_SNIPPET, str(pool_file), f"proc-{i}"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                env=_CHILD_ENV,
            )
            for i in (1, 2)
        ]
        outs = [p.communicate(timeout=60)[0].strip() for p in procs]
        assert sorted(outs) == ["FAIL ResourceExhaustedError", "OK solo"]
        assert all(p.returncode == 0 for p in procs)

    def test_persistence_across_instances(self, tmp_path):
        """YAML 持久化:另一个进程/实例看到一致的占用状态。"""
        first = ResourcePool(tmp_path / "pp.yaml")
        first.add({"host": "1.1.1.1", "role": "db"}, resource_id="db-1")
        first.acquire(role="db", owner="w0")
        second = ResourcePool(tmp_path / "pp.yaml")
        assert second.stats() == {"free": 0, "busy": 1, "disabled": 0}
        with pytest.raises(ResourceExhaustedError):
            second.acquire(role="db", retries=0)


class TestFindPagination:
    def test_default_returns_all(self, pool):
        assert len(pool.find()) == 3

    def test_sort_by_field_ascending(self, pool):
        got = pool.find(sort_by="host")
        hosts = [r["host"] for r in got]
        assert hosts == sorted(hosts)

    def test_sort_reverse_descending(self, pool):
        got = pool.find(sort_by="host", sort_reverse=True)
        hosts = [r["host"] for r in got]
        assert hosts == sorted(hosts, reverse=True)

    def test_missing_field_sorts_last(self, pool):
        # 给一个资源加 tier 字段;缺失该字段的资源永远排在末尾(与 reverse 无关)
        pool.add({"host": "10.0.0.9", "role": "edge", "tier": 1}, resource_id="node-4")
        got = pool.find(sort_by="tier")
        assert got[0]["id"] == "node-4"  # 有 tier,排最前
        assert set(r["id"] for r in got[1:]) == {"node-1", "node-2", "node-3"}

    def test_limit_and_offset(self, pool):
        asc = pool.find(sort_by="host")
        first_two = pool.find(sort_by="host", limit=2)
        assert len(first_two) == 2
        assert first_two == asc[:2]
        rest = pool.find(sort_by="host", limit=2, offset=1)
        assert len(rest) == 2
        assert rest[0]["id"] == asc[1]["id"]  # 偏移后首条顺延

    def test_limit_none_returns_all(self, pool):
        assert len(pool.find(sort_by="host", limit=None)) == 3
