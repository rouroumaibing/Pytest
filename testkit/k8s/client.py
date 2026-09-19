"""Thin wrapper over the official ``kubernetes`` client for direct cluster access.

This complements the HTTP management-plane :class:`~testkit.http.client.HTTPClient`
by talking *directly* to the cluster API server via a kubeconfig (not through a
REST management plane). ``kubernetes`` is imported lazily so the rest of the
framework (and ``import testkit``) works without the optional dependency.

The client exposes CRUD helpers for the common core/apps/networking resources
and an ``exec_in_pod`` helper built on ``kubernetes.stream``. Every failure is
wrapped in :class:`~testkit.exceptions.K8sError`.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any

from testkit.exceptions import K8sError
from testkit.logging_setup import get_logger

logger = get_logger("k8s")


def _require_kubernetes() -> Any:
    """Import ``kubernetes`` on demand; raise a clear error if missing."""
    try:
        import kubernetes  # type: ignore[import-not-found]  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without dep
        raise K8sError(
            "the 'kubernetes' package is required for K8sClient; "
            "install it with 'pip install testkit' (kubernetes>=28) or "
            "'pip install testkit[k8s]'",
            original_exception=exc,
        ) from exc
    return kubernetes


class K8sClient:
    """Direct Kubernetes API-server client (kubeconfig auth).

    Parameters
    ----------
    kubeconfig_dict:
        A kubeconfig mapping (the parsed YAML content), passed to
        ``load_kube_config_from_dict``.
    """

    def __init__(self, kubeconfig_dict: dict[str, Any]) -> None:
        kubernetes = _require_kubernetes()
        try:
            configuration = kubernetes.config.load_kube_config_from_dict(kubeconfig_dict)
        except Exception as exc:  # noqa: BLE001
            raise K8sError("failed to load kubeconfig", original_exception=exc) from exc
        self._api_client = kubernetes.client.ApiClient(configuration)
        self._kubernetes = kubernetes

    @classmethod
    def from_kubeconfig(cls, kubeconfig_dict: dict[str, Any]) -> K8sClient:
        """Construct a client from a kubeconfig mapping."""
        return cls(kubeconfig_dict)

    # -- connection lifecycle -------------------------------------------------

    def close(self) -> None:
        """Release the underlying API client and its connection pool."""
        try:
            self._api_client.close()
        except Exception as exc:  # noqa: BLE001
            raise K8sError("failed to close k8s client", original_exception=exc) from exc
        logger.v2("k8s client closed")

    def __enter__(self) -> K8sClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- api accessors --------------------------------------------------------

    def _core(self) -> Any:  # noqa: ANN401
        return self._kubernetes.client.CoreV1Api(self._api_client)

    def _apps(self) -> Any:  # noqa: ANN401
        return self._kubernetes.client.AppsV1Api(self._api_client)

    def _networking(self) -> Any:  # noqa: ANN401
        return self._kubernetes.client.NetworkingV1Api(self._api_client)

    @staticmethod
    def _wrap(exc: Exception, action: str, **ctx: object) -> K8sError:
        status = getattr(exc, "status", None)
        reason = getattr(status, "reason", None)
        code = getattr(status, "status", None)
        ctx.setdefault("reason", reason)
        if code is not None:
            ctx.setdefault("status_code", code)
        return K8sError(f"k8s {action} failed", original_exception=exc, **ctx)

    # -- apps: Deployment / StatefulSet / DaemonSet ---------------------------

    def list_deployments(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._apps().list_namespaced_deployment(namespace)
            return self._apps().list_deployment_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list deployments", namespace=namespace) from exc

    def get_deployment(self, namespace: str, name: str) -> Any:
        try:
            return self._apps().read_namespaced_deployment(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get deployment", namespace=namespace, name=name) from exc

    def create_deployment(self, namespace: str, body: object) -> Any:
        try:
            return self._apps().create_namespaced_deployment(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create deployment", namespace=namespace) from exc

    def delete_deployment(self, namespace: str, name: str) -> Any:
        try:
            return self._apps().delete_namespaced_deployment(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete deployment", namespace=namespace, name=name) from exc

    def list_statefulsets(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._apps().list_namespaced_stateful_set(namespace)
            return self._apps().list_stateful_set_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list statefulsets", namespace=namespace) from exc

    def get_statefulset(self, namespace: str, name: str) -> Any:
        try:
            return self._apps().read_namespaced_stateful_set(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get statefulset", namespace=namespace, name=name) from exc

    def create_statefulset(self, namespace: str, body: object) -> Any:
        try:
            return self._apps().create_namespaced_stateful_set(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create statefulset", namespace=namespace) from exc

    def delete_statefulset(self, namespace: str, name: str) -> Any:
        try:
            return self._apps().delete_namespaced_stateful_set(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete statefulset", namespace=namespace, name=name) from exc

    def list_daemonsets(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._apps().list_namespaced_daemon_set(namespace)
            return self._apps().list_daemon_set_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list daemonsets", namespace=namespace) from exc

    def get_daemonset(self, namespace: str, name: str) -> Any:
        try:
            return self._apps().read_namespaced_daemon_set(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get daemonset", namespace=namespace, name=name) from exc

    def create_daemonset(self, namespace: str, body: object) -> Any:
        try:
            return self._apps().create_namespaced_daemon_set(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create daemonset", namespace=namespace) from exc

    def delete_daemonset(self, namespace: str, name: str) -> Any:
        try:
            return self._apps().delete_namespaced_daemon_set(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete daemonset", namespace=namespace, name=name) from exc

    # -- core: Pod / ConfigMap / Secret / Namespace ---------------------------

    def list_pods(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._core().list_namespaced_pod(namespace)
            return self._core().list_pod_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list pods", namespace=namespace) from exc

    def get_pod(self, namespace: str, name: str) -> Any:
        try:
            return self._core().read_namespaced_pod(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get pod", namespace=namespace, name=name) from exc

    def create_pod(self, namespace: str, body: object) -> Any:
        try:
            return self._core().create_namespaced_pod(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create pod", namespace=namespace) from exc

    def delete_pod(self, namespace: str, name: str) -> Any:
        try:
            return self._core().delete_namespaced_pod(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete pod", namespace=namespace, name=name) from exc

    def list_configmaps(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._core().list_namespaced_config_map(namespace)
            return self._core().list_config_map_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list configmaps", namespace=namespace) from exc

    def get_configmap(self, namespace: str, name: str) -> Any:
        try:
            return self._core().read_namespaced_config_map(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get configmap", namespace=namespace, name=name) from exc

    def create_configmap(self, namespace: str, body: object) -> Any:
        try:
            return self._core().create_namespaced_config_map(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create configmap", namespace=namespace) from exc

    def delete_configmap(self, namespace: str, name: str) -> Any:
        try:
            return self._core().delete_namespaced_config_map(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete configmap", namespace=namespace, name=name) from exc

    def list_secrets(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._core().list_namespaced_secret(namespace)
            return self._core().list_secret_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list secrets", namespace=namespace) from exc

    def get_secret(self, namespace: str, name: str) -> Any:
        try:
            return self._core().read_namespaced_secret(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get secret", namespace=namespace, name=name) from exc

    def create_secret(self, namespace: str, body: object) -> Any:
        try:
            return self._core().create_namespaced_secret(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create secret", namespace=namespace) from exc

    def delete_secret(self, namespace: str, name: str) -> Any:
        try:
            return self._core().delete_namespaced_secret(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete secret", namespace=namespace, name=name) from exc

    def list_namespaces(self) -> Any:
        try:
            return self._core().list_namespace()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list namespaces") from exc

    def get_namespace(self, name: str) -> Any:
        try:
            return self._core().read_namespace(name)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get namespace", name=name) from exc

    def create_namespace(self, body: object) -> Any:
        try:
            return self._core().create_namespace(body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create namespace") from exc

    def delete_namespace(self, name: str) -> Any:
        try:
            return self._core().delete_namespace(name)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete namespace", name=name) from exc

    # -- networking: Ingress --------------------------------------------------

    def list_ingresses(self, namespace: str | None = None) -> Any:
        try:
            if namespace:
                return self._networking().list_namespaced_ingress(namespace)
            return self._networking().list_ingress_for_all_namespaces()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "list ingresses", namespace=namespace) from exc

    def get_ingress(self, namespace: str, name: str) -> Any:
        try:
            return self._networking().read_namespaced_ingress(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "get ingress", namespace=namespace, name=name) from exc

    def create_ingress(self, namespace: str, body: object) -> Any:
        try:
            return self._networking().create_namespaced_ingress(namespace, body)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "create ingress", namespace=namespace) from exc

    def delete_ingress(self, namespace: str, name: str) -> Any:
        try:
            return self._networking().delete_namespaced_ingress(name, namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(exc, "delete ingress", namespace=namespace, name=name) from exc

    # -- exec -----------------------------------------------------------------

    def exec_in_pod(
        self,
        namespace: str,
        pod_name: str,
        command: list[str] | str,
        container: str | None = None,
        timeout: float | None = None,
    ) -> str:
        """Run a command inside a pod and return its combined output.

        Parameters
        ----------
        namespace:
            Pod namespace.
        pod_name:
            Pod name.
        command:
            Command and arguments (list) or a shell string.
        container:
            Optional target container name.
        timeout:
            Optional exec timeout in seconds.

        Returns
        -------
        str
            Combined stdout/stderr of the command.
        """
        kubernetes = self._kubernetes
        try:
            stream = kubernetes.stream.stream
        except AttributeError:  # pragma: no cover - old client layout
            from kubernetes.stream import stream  # type: ignore  # noqa: PLC0415

        api = self._core()
        try:
            output = stream(
                api.connect_get_namespaced_pod_exec,
                pod_name,
                namespace,
                command=command,
                container=container,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                timeout_seconds=int(timeout) if timeout is not None else None,
                _preload_content=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap(
                exc, "exec in pod", namespace=namespace, pod=pod_name, command=command
            ) from exc
        logger.v4("k8s exec pod=%s/%s -> %r", namespace, pod_name, output)
        return str(output)
