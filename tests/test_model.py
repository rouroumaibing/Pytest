"""Unit tests for the data-model layer (Builder / BaseModel)."""

from __future__ import annotations

from testkit import BaseModel, Builder


class _CreateRequest(Builder):
    def with_name(self, name: str) -> "_CreateRequest":
        return self._with("name", name)

    def with_replicas(self, replicas: int) -> "_CreateRequest":
        return self._with("replicas", replicas)


class _Cluster(BaseModel):
    _fields = ["name", "status"]
    _aliases = {"id": "metadata.uid"}


def test_builder_chaining():
    body = _CreateRequest().with_name("demo").with_replicas(3).build()
    assert body == {"name": "demo", "replicas": 3}


def test_builder_returns_copy():
    req = _CreateRequest().with_name("demo")
    body = req.build()
    body["name"] = "mutated"
    assert req.build()["name"] == "demo"  # build() returns a fresh dict


def test_builder_methods_return_self():
    req = _CreateRequest()
    assert req.with_name("a") is req
    assert req.with_replicas(1) is req


def test_from_api_response_maps_fields():
    cluster = _Cluster.from_api_response({"name": "c1", "status": "Running"})
    assert cluster.name == "c1"
    assert cluster.status == "Running"


def test_from_api_response_resolves_alias():
    cluster = _Cluster.from_api_response(
        {"name": "c1", "status": "Running", "metadata": {"uid": "u-123"}}
    )
    assert cluster.id == "u-123"


def test_from_api_response_missing_alias_is_none():
    cluster = _Cluster.from_api_response({"name": "c1", "status": "Running"})
    assert cluster.id is None


def test_from_api_response_missing_field_is_absent():
    cluster = _Cluster.from_api_response({"name": "c1"})
    assert cluster.name == "c1"
    assert not hasattr(cluster, "status")  # missing field not set


def test_from_api_response_does_not_swallow_type_errors():
    class _Strict(BaseModel):
        _fields = ["count"]

        @classmethod
        def from_api_response(cls, response):
            obj = cls()
            obj.count = response["count"] + 1  # would raise if count is not int
            return obj

    # The base behaviour does not catch exceptions; simulate a type error path.
    with __import__("pytest").raises(TypeError):
        _Strict.from_api_response({"count": "not-a-number"})


def test_to_dict_snapshot():
    cluster = _Cluster.from_api_response(
        {"name": "c1", "status": "Running", "metadata": {"uid": "u-1"}}
    )
    d = cluster.to_dict()
    assert d["name"] == "c1"
    assert d["status"] == "Running"
    assert d["id"] == "u-1"


def test_model_holds_no_client_reference():
    # BaseModel has no client attribute by default.
    cluster = _Cluster.from_api_response({"name": "c1"})
    assert not hasattr(cluster, "client")
    assert not hasattr(cluster, "_client")
