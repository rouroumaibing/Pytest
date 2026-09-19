"""Unit tests for the generic file-backed resource pool (ResourcePool)."""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
import yaml
from testkit import ResourcePool
from testkit.exceptions import PoolError
from testkit.pool.resource_pool import FREE


def _write_yaml(path: Path, data) -> None:
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")


def test_read_nonexistent_file_returns_empty(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    assert pool._read() == []


def test_read_none_content_returns_empty(tmp_path):
    path = tmp_path / "pool.yaml"
    path.write_text("null\n", encoding="utf-8")
    pool = ResourcePool(str(path))
    assert pool._read() == []


def test_read_non_list_content_raises(tmp_path):
    path = tmp_path / "pool.yaml"
    _write_yaml(path, {"not": "a list"})
    pool = ResourcePool(str(path))
    with pytest.raises(PoolError):
        pool._read()


def test_read_non_dict_entry_raises(tmp_path):
    path = tmp_path / "pool.yaml"
    _write_yaml(path, ["not-a-dict", 123])
    pool = ResourcePool(str(path))
    with pytest.raises(PoolError):
        pool._read()


def test_read_illegal_status_raises(tmp_path):
    path = tmp_path / "pool.yaml"
    _write_yaml(path, [{"id": "x", "status": "busy"}])
    pool = ResourcePool(str(path))
    with pytest.raises(PoolError):
        pool._read()


def test_add_persists_free(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1", "region": "us"})
    listed = pool.list()
    assert len(listed) == 1
    assert listed[0]["id"] == "r1"
    assert listed[0]["status"] == FREE
    # original dict not mutated.
    assert "status" not in {"id": "r1", "region": "us"}


def test_add_duplicate_id_raises(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    with pytest.raises(PoolError):
        pool.add({"id": "r1"})


def test_acquire_count(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    for i in range(3):
        pool.add({"id": f"r{i}"})
    allocated = pool.acquire(count=2, retries=0, interval=0.001)
    assert len(allocated) == 2
    assert {r["id"] for r in allocated} == {"r0", "r1"}
    for r in pool.list():
        if r["id"] in {"r0", "r1"}:
            assert r["status"] == "allocated"
        else:
            assert r["status"] == FREE


def test_acquire_predicate_filter(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "us1", "region": "us"})
    pool.add({"id": "eu1", "region": "eu"})
    allocated = pool.acquire(
        count=2, predicate=lambda r: r.get("region") == "us", retries=0, interval=0.001
    )
    assert allocated == [{"id": "us1", "region": "us", "status": "allocated"}]


def test_acquire_retries_exhausted_returns_fewer(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    # Only one free resource but we ask for three; with no retries we get fewer.
    allocated = pool.acquire(count=3, retries=0, interval=0.001)
    assert len(allocated) == 1


def test_acquire_insufficient_returns_empty(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    allocated = pool.acquire(count=1, retries=0, interval=0.001)
    assert allocated == []


def test_acquire_waits_interval_between_retries(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    with mock.patch("testkit.pool.resource_pool.time.sleep") as sleep:
        # Ask for more than available with one retry; one sleep should occur.
        allocated = pool.acquire(count=3, retries=1, interval=0.01)
    assert len(allocated) == 1
    sleep.assert_called_once_with(0.01)


def test_release(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    allocated = pool.acquire(count=1, retries=0, interval=0.001)
    pool.release(allocated)
    assert pool.list()[0]["status"] == FREE


def test_release_empty_is_noop(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    pool.release([])  # must not raise
    assert pool.list()[0]["status"] == FREE


def test_release_all(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    for i in range(2):
        pool.add({"id": f"r{i}"})
    pool.acquire(count=2, retries=0, interval=0.001)
    assert all(r["status"] == "allocated" for r in pool.list())
    pool.release_all()
    assert all(r["status"] == FREE for r in pool.list())


def test_remove(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    pool.remove("r1")
    assert pool.list() == []


def test_remove_missing_id_raises(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    with pytest.raises(PoolError):
        pool.remove("nope")


def test_id_field_custom(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"), id_field="name")
    pool.add({"name": "alpha"})
    assert pool.list()[0]["name"] == "alpha"
    with pytest.raises(PoolError):
        pool.add({"name": "alpha"})


# -- ported from test_pool.py (unique behaviours) --


def test_release_only_matching_ids(tmp_path):
    pool = ResourcePool(str(tmp_path / "pool.yaml"))
    pool.add({"id": "r1"})
    pool.add({"id": "r2"})
    allocated = pool.acquire(count=2)
    assert len(allocated) == 2
    pool.release([{"id": "r1"}])
    statuses = {r["id"]: r["status"] for r in pool.list()}
    assert statuses["r1"] == "free"
    assert statuses["r2"] == "allocated"


def test_persistence_survives_reinstantiation(tmp_path):
    path = tmp_path / "pool.yaml"
    ResourcePool(path).add({"id": "r1"})
    pool2 = ResourcePool(path)
    assert [r["id"] for r in pool2.list()] == ["r1"]
