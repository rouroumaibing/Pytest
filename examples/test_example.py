"""Example tests exercising each framework module in isolation.

These tests run without any external services — they demonstrate the API
contracts of the framework (resource pool, wait helper, builder/model,
pipeline, cleanup, config registry, exceptions).
"""

from __future__ import annotations

import pytest
from testkit import (
    BaseModel,
    Builder,
    Pipeline,
    ResourceCleanup,
    ResourceNotFoundError,
    ResourcePool,
    WaitHelper,
    WaitTimeout,
)

# -- ResourcePool ------------------------------------------------------------


def test_resource_pool_acquire_release(tmp_path):
    pool = ResourcePool(tmp_path / "pool.yaml")
    pool.add({"id": "r1", "tags": ["fast"]})
    pool.add({"id": "r2", "tags": ["slow"]})

    allocated = pool.acquire(count=1, predicate=lambda r: "fast" in r.get("tags", []))
    assert len(allocated) == 1
    assert allocated[0]["id"] == "r1"
    assert allocated[0]["status"] == "allocated"

    pool.release(allocated)
    assert all(r["status"] == "free" for r in pool.list())


def test_resource_pool_status_is_strict(tmp_path):
    pool = ResourcePool(tmp_path / "pool.yaml")
    pool.add({"id": "r1"})
    # A resource with an illegal status is rejected on read.
    pool_path = tmp_path / "pool.yaml"
    import yaml

    with pool_path.open("w") as fh:
        yaml.safe_dump([{"id": "r1", "status": "busy"}], fh)
    from testkit.exceptions import PoolError

    with pytest.raises(PoolError):
        pool.list()


# -- WaitHelper --------------------------------------------------------------


def test_wait_helper_until_true():
    counter = {"n": 0}

    def cond() -> bool:
        counter["n"] += 1
        return counter["n"] >= 3

    result = WaitHelper(interval=0.01).until(cond)
    assert result is True


def test_wait_helper_timeout():
    with pytest.raises(WaitTimeout):
        WaitHelper(timeout=0.05, interval=0.01).until(lambda: False)


# -- Builder / BaseModel -----------------------------------------------------


class _CreateRequest(Builder):
    def with_name(self, name: str) -> _CreateRequest:
        return self._with("name", name)

    def with_replicas(self, replicas: int) -> _CreateRequest:
        return self._with("replicas", replicas)


def test_builder_chaining():
    body = _CreateRequest().with_name("demo").with_replicas(3).build()
    assert body == {"name": "demo", "replicas": 3}


class _Cluster(BaseModel):
    _fields = ["name", "status"]
    _aliases = {"id": "metadata.uid"}


def test_from_api_response_mapping():
    cluster = _Cluster.from_api_response(
        {"name": "c1", "status": "Running", "metadata": {"uid": "u-123"}}
    )
    assert cluster.name == "c1"
    assert cluster.status == "Running"
    assert cluster.id == "u-123"  # convenience alias


# -- Pipeline ----------------------------------------------------------------


def test_pipeline_skips_after_failure():
    order: list[str] = []
    pipeline = Pipeline()
    pipeline.add_stage("s1", lambda: order.append("s1"))

    def boom() -> None:
        order.append("s2")
        raise RuntimeError("boom")

    pipeline.add_stage("s2", boom)
    pipeline.add_stage("s3", lambda: order.append("s3"))

    results = pipeline.run()
    assert [r.status for r in results] == ["passed", "failed", "skipped"]
    assert not pipeline.success
    assert order == ["s1", "s2"]


def test_pipeline_resume_from():
    order: list[str] = []
    pipeline = Pipeline()
    pipeline.add_stage("s1", lambda: order.append("s1"))
    pipeline.add_stage("s2", lambda: order.append("s2"))
    pipeline.add_stage("s3", lambda: order.append("s3"))

    results = pipeline.run(resume_from="s2")
    assert [r.status for r in results] == ["skipped", "passed", "passed"]
    assert pipeline.success
    assert order == ["s2", "s3"]


# -- ResourceCleanup ---------------------------------------------------------


def test_cleanup_runs_lifo():
    order: list[str] = []
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)
    cleanup.register(lambda: order.append("first"))
    cleanup.register(lambda: order.append("second"))
    cleanup.cleanup()
    assert order == ["second", "first"]  # LIFO


def test_cleanup_failure_does_not_block_others():
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0)
    cleanup.register(lambda: (_ for _ in ()).throw(RuntimeError("fail")), name="bad")
    cleanup.register(lambda: None, name="good")
    results = cleanup.cleanup()
    # LIFO: "good" (registered last) runs first and succeeds, then "bad" fails
    # — the failure must not block subsequent recoveries.
    assert results[0].name == "good" and results[0].success is True
    assert results[1].name == "bad" and results[1].success is False


def test_cleanup_skip_on_failure():
    ran: list[str] = []
    cleanup = ResourceCleanup(retry_count=0, retry_interval=0, skip_cleanup_on_failure=True)
    cleanup.register(lambda: ran.append("x"))
    cleanup.cleanup(failed=True)
    assert ran == []


# -- Config registry (uses examples/conftest.py) -----------------------------


def test_global_config_fixture(global_config):
    assert global_config.api_base_url == "http://localhost:8080"
    assert global_config.timeout == 30.0


def test_env_overlay_merge(tmp_path, monkeypatch):
    import yaml
    from pydantic import BaseModel as PydBaseModel
    from testkit import ConfigRegistry

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "default": {"common": {"api_base_url": "http://default", "timeout": 30.0}},
                "envs": {
                    "staging": {"common": {"api_base_url": "http://staging"}},
                },
            }
        )
    )

    class Cfg(PydBaseModel):
        api_base_url: str
        timeout: float = 30.0

    monkeypatch.setenv("TESTKIT_ENV", "staging")
    registry = ConfigRegistry(str(cfg))
    registry.register(["common"], Cfg, fixture_name="c")
    model = registry.get("c")
    assert model.api_base_url == "http://staging"  # overridden by env
    assert model.timeout == 30.0  # inherited from default


# -- Exceptions --------------------------------------------------------------


def test_resource_not_found_carries_context():
    err = ResourceNotFoundError("cluster", "c-9")
    assert err.context["resource_type"] == "cluster"
    assert err.context["resource_id"] == "c-9"
    assert "cluster" in str(err)


# -- ConcurrentFixtureGuard --------------------------------------------------


def test_fixture_guard_creator_flow(tmp_path):
    from testkit import ConcurrentFixtureGuard

    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert guard.should_create() is True  # first caller becomes creator
    assert guard.should_create() is False  # subsequent callers reuse
    assert guard.is_creator() is True

    guard.mark_created({"cluster": "c-1"})
    # A fresh guard instance (different process id) sees the created state.
    other = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert other.should_create() is False
    assert other.is_creator() is False


def test_fixture_guard_wait_until_created(tmp_path):
    from testkit import ConcurrentFixtureGuard

    guard = ConcurrentFixtureGuard(tmp_path / "fixture.json", poll_interval=0.01)
    # The readiness check is a caller-defined predicate, not a state-file read.
    ready = {"yes": False}
    assert guard.wait_until_created(lambda: ready["yes"], timeout=0.05) is False
    ready["yes"] = True
    assert guard.wait_until_created(lambda: ready["yes"], timeout=0.5) is True


def test_fixture_guard_mark_failed_then_takeover(tmp_path):
    from testkit import ConcurrentFixtureGuard

    first = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert first.should_create() is True
    first.mark_failed()

    # After failure, a fresh caller can claim creation (takeover).
    second = ConcurrentFixtureGuard(tmp_path / "fixture.json")
    assert second.should_create() is True
    assert second.cleanup() is True
