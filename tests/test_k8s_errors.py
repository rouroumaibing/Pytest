"""Error-branch coverage for :class:`testkit.k8s.client.K8sClient`.

Each CRUD helper wraps its API call in ``try/except`` and re-raises a
:class:`~testkit.exceptions.K8sError`. The happy paths are covered by
``test_k8s.py`` / ``test_k8s_full.py``; this module drives the ``except``
branch of every helper by making the backing API class itself raise.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Callable
from unittest import mock

import pytest
from testkit.exceptions import K8sError
from testkit.k8s import K8sClient


@pytest.fixture
def fake_k8s(monkeypatch: pytest.MonkeyPatch) -> mock.MagicMock:
    """Inject a fully mocked ``kubernetes`` module into ``sys.modules``."""
    k8s = types.ModuleType("kubernetes")
    config = mock.MagicMock()
    config.load_kube_config_from_dict.return_value = mock.MagicMock()
    client = mock.MagicMock()
    client.ApiClient.return_value = mock.MagicMock()
    k8s.config = config
    k8s.client = client
    k8s.stream = mock.MagicMock()
    k8s.stream.stream = mock.MagicMock(return_value="out")
    monkeypatch.setitem(sys.modules, "kubernetes", k8s)
    return k8s


@pytest.fixture
def client(fake_k8s: mock.MagicMock) -> K8sClient:
    return K8sClient({"apiVersion": "v1"})


def _apps_calls(c: K8sClient) -> list[Callable[[], object]]:
    return [
        lambda: c.list_deployments(),
        lambda: c.list_deployments("ns"),
        lambda: c.get_deployment("ns", "n"),
        lambda: c.create_deployment("ns", {}),
        lambda: c.delete_deployment("ns", "n"),
        lambda: c.list_statefulsets(),
        lambda: c.list_statefulsets("ns"),
        lambda: c.get_statefulset("ns", "n"),
        lambda: c.create_statefulset("ns", {}),
        lambda: c.delete_statefulset("ns", "n"),
        lambda: c.list_daemonsets(),
        lambda: c.list_daemonsets("ns"),
        lambda: c.get_daemonset("ns", "n"),
        lambda: c.create_daemonset("ns", {}),
        lambda: c.delete_daemonset("ns", "n"),
    ]


def _core_calls(c: K8sClient) -> list[Callable[[], object]]:
    return [
        lambda: c.list_pods(),
        lambda: c.list_pods("ns"),
        lambda: c.get_pod("ns", "n"),
        lambda: c.create_pod("ns", {}),
        lambda: c.delete_pod("ns", "n"),
        lambda: c.list_configmaps(),
        lambda: c.list_configmaps("ns"),
        lambda: c.get_configmap("ns", "n"),
        lambda: c.create_configmap("ns", {}),
        lambda: c.delete_configmap("ns", "n"),
        lambda: c.list_secrets(),
        lambda: c.list_secrets("ns"),
        lambda: c.get_secret("ns", "n"),
        lambda: c.create_secret("ns", {}),
        lambda: c.delete_secret("ns", "n"),
        lambda: c.list_namespaces(),
        lambda: c.get_namespace("default"),
        lambda: c.create_namespace({}),
        lambda: c.delete_namespace("default"),
    ]


def _networking_calls(c: K8sClient) -> list[Callable[[], object]]:
    return [
        lambda: c.list_ingresses(),
        lambda: c.list_ingresses("ns"),
        lambda: c.get_ingress("ns", "n"),
        lambda: c.create_ingress("ns", {}),
        lambda: c.delete_ingress("ns", "n"),
    ]


def test_apps_error_branches(client: K8sClient, fake_k8s: mock.MagicMock) -> None:
    fake_k8s.client.AppsV1Api.side_effect = RuntimeError("apps boom")
    for call in _apps_calls(client):
        with pytest.raises(K8sError):
            call()


def test_core_error_branches(client: K8sClient, fake_k8s: mock.MagicMock) -> None:
    fake_k8s.client.CoreV1Api.side_effect = RuntimeError("core boom")
    for call in _core_calls(client):
        with pytest.raises(K8sError):
            call()


def test_networking_error_branches(client: K8sClient, fake_k8s: mock.MagicMock) -> None:
    fake_k8s.client.NetworkingV1Api.side_effect = RuntimeError("net boom")
    for call in _networking_calls(client):
        with pytest.raises(K8sError):
            call()


def test_exec_error_branch(client: K8sClient, fake_k8s: mock.MagicMock) -> None:
    fake_k8s.stream.stream.side_effect = RuntimeError("exec boom")
    with pytest.raises(K8sError):
        client.exec_in_pod("ns", "pod", "ls")


def test_exec_error_carries_status(client: K8sClient, fake_k8s: mock.MagicMock) -> None:
    exc = RuntimeError("forbidden")
    exc.status = types.SimpleNamespace(reason="Forbidden", status=403)
    fake_k8s.stream.stream.side_effect = exc
    with pytest.raises(K8sError) as exc_info:
        client.exec_in_pod("ns", "pod", ["ls", "-l"], container="main", timeout=5)
    assert exc_info.value.context.get("status_code") == 403
    assert exc_info.value.context.get("reason") == "Forbidden"


def test_wrap_extracts_status(client: K8sClient, fake_k8s: mock.MagicMock) -> None:
    exc = RuntimeError("not found")
    exc.status = types.SimpleNamespace(reason="NotFound", status=404)
    fake_k8s.client.AppsV1Api.return_value.list_namespaced_deployment.side_effect = exc
    with pytest.raises(K8sError) as exc_info:
        client.list_deployments("ns")
    assert exc_info.value.context.get("status_code") == 404
    assert exc_info.value.context.get("reason") == "NotFound"
