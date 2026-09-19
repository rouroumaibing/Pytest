"""Unit tests for the data-model layer (Builder / BaseModel).

These complement the existing ``test_model.py`` and target the exact
behaviours called out for coverage: :meth:`Builder._with` / :meth:`build`,
:class:`BaseModel.from_api_response` (field copy, nested dotted aliases with
missing paths resolving to ``None``), :meth:`to_dict`, and that construction
type errors are propagated rather than swallowed.
"""

from __future__ import annotations

import pytest
from testkit import BaseModel, Builder


class _CreateRequest(Builder):
    def with_name(self, name: str) -> _CreateRequest:
        return self._with("name", name)

    def with_replicas(self, replicas: int) -> _CreateRequest:
        return self._with("replicas", replicas)


class _Cluster(BaseModel):
    _fields = ["name", "status"]
    _aliases = {"id": "metadata.uid", "zone": "metadata.labels.zone"}


def test_with_returns_self_for_chaining():
    req = _CreateRequest()
    assert req.with_name("demo") is req
    assert req.with_replicas(3) is req


def test_build_returns_accumulated_dict():
    body = _CreateRequest().with_name("demo").with_replicas(3).build()
    assert body == {"name": "demo", "replicas": 3}


def test_build_returns_fresh_copy():
    req = _CreateRequest().with_name("demo")
    first = req.build()
    first["name"] = "mutated"
    assert req.build()["name"] == "demo"


def test_from_api_response_copies_fields():
    cluster = _Cluster.from_api_response({"name": "c1", "status": "Running"})
    assert cluster.name == "c1"
    assert cluster.status == "Running"


def test_from_api_response_missing_field_not_set():
    cluster = _Cluster.from_api_response({"name": "c1"})
    assert cluster.name == "c1"
    assert not hasattr(cluster, "status")


def test_from_api_response_resolves_nested_alias():
    cluster = _Cluster.from_api_response(
        {"name": "c1", "status": "Running", "metadata": {"uid": "u-123"}}
    )
    assert cluster.id == "u-123"


def test_from_api_response_missing_alias_path_is_none():
    cluster = _Cluster.from_api_response({"name": "c1", "status": "Running"})
    # Top-level alias missing entirely.
    assert cluster.id is None


def test_from_api_response_missing_nested_alias_path_is_none():
    cluster = _Cluster.from_api_response(
        {"name": "c1", "status": "Running", "metadata": {"uid": "u-1"}}
    )
    # metadata exists but metadata.labels does not -> None.
    assert cluster.zone is None


def test_to_dict_snapshot_includes_fields_and_aliases():
    cluster = _Cluster.from_api_response(
        {"name": "c1", "status": "Running", "metadata": {"uid": "u-1"}}
    )
    d = cluster.to_dict()
    assert d["name"] == "c1"
    assert d["status"] == "Running"
    assert d["id"] == "u-1"


def test_to_dict_omits_unset_fields():
    cluster = _Cluster.from_api_response({"name": "c1"})
    d = cluster.to_dict()
    assert "status" not in d


def test_from_api_response_propagates_type_error():
    # A non-mapping response makes `field in response` raise TypeError, and the
    # base implementation must NOT swallow it.
    with pytest.raises(TypeError):
        _Cluster.from_api_response(None)  # type: ignore[arg-type]


# -- ported from test_model.py (unique behaviour) --


def test_model_holds_no_client_reference():
    # BaseModel has no client attribute by default.
    cluster = _Cluster.from_api_response({"name": "c1"})
    assert not hasattr(cluster, "client")
    assert not hasattr(cluster, "_client")
