"""Extended coverage tests for the Kubernetes direct client (client.py).

These tests exercise :class:`testkit.k8s.client.K8sClient` against a fully
faked ``kubernetes`` module injected into ``sys.modules`` so no real cluster
(or the optional ``kubernetes`` dependency) is required. Style follows
``tests/test_k8s.py`` (pytest + ``from unittest import mock``).
"""

from __future__ import annotations

import sys
import types
from unittest import mock

import pytest
from testkit.exceptions import K8sError
from testkit.k8s import K8sClient


@pytest.fixture
def fake_k8s(monkeypatch):
    """Inject a fully mocked ``kubernetes`` module into ``sys.modules``.

    ``K8sClient.__init__`` calls ``import kubernetes`` lazily via
    ``_require_kubernetes()``; putting a fake module object in ``sys.modules``
    satisfies that import without touching a real cluster or dependency.
    """
    k8s = types.ModuleType("kubernetes")
    config = mock.MagicMock()
    config.load_kube_config_from_dict.return_value = mock.MagicMock()
    client = mock.MagicMock()
    client.ApiClient.return_value = mock.MagicMock()
    k8s.config = config
    k8s.client = client
    k8s.stream = mock.MagicMock()
    k8s.stream.stream = mock.MagicMock(return_value="exec-out")
    monkeypatch.setitem(sys.modules, "kubernetes", k8s)
    return k8s


def _make_client():
    return K8sClient({"apiVersion": "v1"})


# -- construction / lifecycle -------------------------------------------------


def test_full_from_kubeconfig(fake_k8s):
    client = K8sClient.from_kubeconfig({"apiVersion": "v1"})
    assert isinstance(client, K8sClient)
    assert client._api_client is not None
    fake_k8s.config.load_kube_config_from_dict.assert_called_once_with({"apiVersion": "v1"})


def test_full_close(fake_k8s):
    client = _make_client()
    client.close()
    client._api_client.close.assert_called_once()


def test_full_close_raises_wrapped(fake_k8s):
    client = _make_client()
    client._api_client.close.side_effect = RuntimeError("socket borked")
    with pytest.raises(K8sError) as exc_info:
        client.close()
    assert exc_info.value.original_exception is not None


def test_full_context_manager(fake_k8s):
    with K8sClient({"apiVersion": "v1"}) as entered:
        assert isinstance(entered, K8sClient)
    # __exit__ must have closed the underlying api client.
    entered._api_client.close.assert_called_once()


# -- CRUD routing (both namespace branches) -----------------------------------
#
# Each tuple: (method, args, api-class-attr, api-method, expected-call-args).

_CRUD = [
    # Deployments
    ("list_deployments", (None,), "AppsV1Api", "list_deployment_for_all_namespaces", ()),
    ("list_deployments", ("ns",), "AppsV1Api", "list_namespaced_deployment", ("ns",)),
    ("get_deployment", ("ns", "d"), "AppsV1Api", "read_namespaced_deployment", ("d", "ns")),
    (
        "create_deployment",
        ("ns", {"kind": "Deployment"}),
        "AppsV1Api",
        "create_namespaced_deployment",
        ("ns", {"kind": "Deployment"}),
    ),
    ("delete_deployment", ("ns", "d"), "AppsV1Api", "delete_namespaced_deployment", ("d", "ns")),
    # StatefulSets
    ("list_statefulsets", (None,), "AppsV1Api", "list_stateful_set_for_all_namespaces", ()),
    ("list_statefulsets", ("ns",), "AppsV1Api", "list_namespaced_stateful_set", ("ns",)),
    ("get_statefulset", ("ns", "s"), "AppsV1Api", "read_namespaced_stateful_set", ("s", "ns")),
    (
        "create_statefulset",
        ("ns", {"kind": "StatefulSet"}),
        "AppsV1Api",
        "create_namespaced_stateful_set",
        ("ns", {"kind": "StatefulSet"}),
    ),
    ("delete_statefulset", ("ns", "s"), "AppsV1Api", "delete_namespaced_stateful_set", ("s", "ns")),
    # DaemonSets
    ("list_daemonsets", (None,), "AppsV1Api", "list_daemon_set_for_all_namespaces", ()),
    ("list_daemonsets", ("ns",), "AppsV1Api", "list_namespaced_daemon_set", ("ns",)),
    ("get_daemonset", ("ns", "ds"), "AppsV1Api", "read_namespaced_daemon_set", ("ds", "ns")),
    (
        "create_daemonset",
        ("ns", {"kind": "DaemonSet"}),
        "AppsV1Api",
        "create_namespaced_daemon_set",
        ("ns", {"kind": "DaemonSet"}),
    ),
    ("delete_daemonset", ("ns", "ds"), "AppsV1Api", "delete_namespaced_daemon_set", ("ds", "ns")),
    # Pods
    ("list_pods", (None,), "CoreV1Api", "list_pod_for_all_namespaces", ()),
    ("list_pods", ("ns",), "CoreV1Api", "list_namespaced_pod", ("ns",)),
    ("get_pod", ("ns", "p"), "CoreV1Api", "read_namespaced_pod", ("p", "ns")),
    (
        "create_pod",
        ("ns", {"kind": "Pod"}),
        "CoreV1Api",
        "create_namespaced_pod",
        ("ns", {"kind": "Pod"}),
    ),
    ("delete_pod", ("ns", "p"), "CoreV1Api", "delete_namespaced_pod", ("p", "ns")),
    # Services
    ("list_services", (None,), "CoreV1Api", "list_service_for_all_namespaces", ()),
    ("list_services", ("ns",), "CoreV1Api", "list_namespaced_service", ("ns",)),
    ("get_service", ("ns", "svc"), "CoreV1Api", "read_namespaced_service", ("svc", "ns")),
    (
        "create_service",
        ("ns", {"kind": "Service"}),
        "CoreV1Api",
        "create_namespaced_service",
        ("ns", {"kind": "Service"}),
    ),
    ("delete_service", ("ns", "svc"), "CoreV1Api", "delete_namespaced_service", ("svc", "ns")),
    # ConfigMaps
    ("list_configmaps", (None,), "CoreV1Api", "list_config_map_for_all_namespaces", ()),
    ("list_configmaps", ("ns",), "CoreV1Api", "list_namespaced_config_map", ("ns",)),
    ("get_configmap", ("ns", "c"), "CoreV1Api", "read_namespaced_config_map", ("c", "ns")),
    (
        "create_configmap",
        ("ns", {"kind": "ConfigMap"}),
        "CoreV1Api",
        "create_namespaced_config_map",
        ("ns", {"kind": "ConfigMap"}),
    ),
    ("delete_configmap", ("ns", "c"), "CoreV1Api", "delete_namespaced_config_map", ("c", "ns")),
    # Secrets
    ("list_secrets", (None,), "CoreV1Api", "list_secret_for_all_namespaces", ()),
    ("list_secrets", ("ns",), "CoreV1Api", "list_namespaced_secret", ("ns",)),
    ("get_secret", ("ns", "s"), "CoreV1Api", "read_namespaced_secret", ("s", "ns")),
    (
        "create_secret",
        ("ns", {"kind": "Secret"}),
        "CoreV1Api",
        "create_namespaced_secret",
        ("ns", {"kind": "Secret"}),
    ),
    ("delete_secret", ("ns", "s"), "CoreV1Api", "delete_namespaced_secret", ("s", "ns")),
    # Namespaces
    ("list_namespaces", (), "CoreV1Api", "list_namespace", ()),
    ("get_namespace", ("n",), "CoreV1Api", "read_namespace", ("n",)),
    (
        "create_namespace",
        ({"kind": "Namespace"},),
        "CoreV1Api",
        "create_namespace",
        ({"kind": "Namespace"},),
    ),
    ("delete_namespace", ("n",), "CoreV1Api", "delete_namespace", ("n",)),
    # Ingresses
    ("list_ingresses", (None,), "NetworkingV1Api", "list_ingress_for_all_namespaces", ()),
    ("list_ingresses", ("ns",), "NetworkingV1Api", "list_namespaced_ingress", ("ns",)),
    ("get_ingress", ("ns", "i"), "NetworkingV1Api", "read_namespaced_ingress", ("i", "ns")),
    (
        "create_ingress",
        ("ns", {"kind": "Ingress"}),
        "NetworkingV1Api",
        "create_namespaced_ingress",
        ("ns", {"kind": "Ingress"}),
    ),
    ("delete_ingress", ("ns", "i"), "NetworkingV1Api", "delete_namespaced_ingress", ("i", "ns")),
]


@pytest.mark.parametrize(("method", "args", "api_attr", "api_method", "call_args"), _CRUD)
def test_full_crud_routing(fake_k8s, method, args, api_attr, api_method, call_args):
    client = _make_client()
    getattr(client, method)(*args)
    api_ret = getattr(fake_k8s.client, api_attr).return_value
    getattr(api_ret, api_method).assert_called_once_with(*call_args)


# -- exec_in_pod --------------------------------------------------------------


def test_full_exec_list_command(fake_k8s):
    client = _make_client()
    out = client.exec_in_pod("ns", "pod", ["echo", "hi"])
    assert out == "exec-out"
    fake_k8s.stream.stream.assert_called_once()
    _, kwargs = fake_k8s.stream.stream.call_args
    assert kwargs["command"] == ["echo", "hi"]
    assert kwargs["container"] is None
    assert kwargs["timeout_seconds"] is None


def test_full_exec_str_command(fake_k8s):
    client = _make_client()
    client.exec_in_pod("ns", "pod", "echo hi")
    _, kwargs = fake_k8s.stream.stream.call_args
    assert kwargs["command"] == "echo hi"


def test_full_exec_with_container(fake_k8s):
    client = _make_client()
    client.exec_in_pod("ns", "pod", ["sh"], container="c1")
    _, kwargs = fake_k8s.stream.stream.call_args
    assert kwargs["container"] == "c1"


def test_full_exec_with_timeout(fake_k8s):
    client = _make_client()
    client.exec_in_pod("ns", "pod", ["sh"], timeout=3.5)
    _, kwargs = fake_k8s.stream.stream.call_args
    # timeout is coerced to an int for the API call.
    assert kwargs["timeout_seconds"] == 3


def test_full_exec_raises_with_status(fake_k8s):
    class _Status:
        reason = "Forbidden"
        status = 403

    class _ExecExc(Exception):
        status = _Status()

    fake_k8s.stream.stream.side_effect = _ExecExc("denied")
    client = _make_client()
    with pytest.raises(K8sError) as exc_info:
        client.exec_in_pod("ns", "pod", ["sh"])
    # The status info is preserved in the error context (see bug note).
    assert exc_info.value.context.get("status_code") == 403
    assert exc_info.value.context.get("reason") == "Forbidden"


# -- _wrap error helper -------------------------------------------------------


def test_full_wrap_preserves_status(fake_k8s):
    class _Status:
        reason = "NotFound"
        status = 404

    class _ApiExc(Exception):
        status = _Status()

    err = K8sClient._wrap(_ApiExc("boom"), "list deployments", namespace="ns")
    assert isinstance(err, K8sError)
    # ``_wrap`` records reason/status_code in the ``context`` dict, and the
    # base class mirrors every context entry onto a direct attribute.
    assert err.context.get("reason") == "NotFound"
    assert err.context.get("status_code") == 404
    assert err.context.get("namespace") == "ns"
    assert err.reason == "NotFound"
    assert err.status_code == 404


def test_full_wrap_without_status(fake_k8s):
    err = K8sClient._wrap(Exception("plain"), "get pod", namespace="ns", name="p")
    assert isinstance(err, K8sError)
    assert err.context.get("reason") is None
    assert err.context.get("status_code") is None
    assert err.context.get("name") == "p"


# -- error paths --------------------------------------------------------------


def test_full_load_kubeconfig_error_wrapped(fake_k8s):
    fake_k8s.config.load_kube_config_from_dict.side_effect = Exception("bad kubeconfig")
    with pytest.raises(K8sError) as exc_info:
        K8sClient({"apiVersion": "v1"})
    assert exc_info.value.original_exception is not None


def test_full_api_call_error_wrapped(fake_k8s):
    fake_k8s.client.CoreV1Api.return_value.list_namespaced_pod.side_effect = Exception("api down")
    client = _make_client()
    with pytest.raises(K8sError) as exc_info:
        client.list_pods("ns")
    assert exc_info.value.original_exception is not None
