# Kubernetes Client

Direct access to a cluster API server through a kubeconfig — complementary to the
[HTTP Client](http-client.md), which talks to a REST management plane instead of
the cluster itself. Every call is wrapped: failures surface as `K8sError` carrying
the namespace, name, reason and status code on `err.context`.

`kubernetes` is a required dependency, but it is imported lazily. That means
`import testkit` and the rest of the framework keep working where the dependency
is unavailable (a slim image, an offline host) — only constructing a `K8sClient`
fails, with an install hint.

## Connecting

```python
import yaml

from testkit import K8sClient

with open("kubeconfig.yaml") as fh:
    kubeconfig = yaml.safe_load(fh)

with K8sClient(kubeconfig) as k8s:  # or K8sClient.from_kubeconfig(kubeconfig)
    pods = k8s.list_pods("default")
```

The kubeconfig mapping is passed to `load_kube_config_from_dict`. `K8sClient` is a
context manager; `close()` releases the underlying API client and its connection
pool. A kubeconfig that cannot be loaded raises `K8sError`.

## Resources

| Resource | Methods |
|----------|---------|
| Deployment | `list_deployments` · `get_deployment` · `create_deployment` · `delete_deployment` |
| StatefulSet | `list_statefulsets` · `get_statefulset` · `create_statefulset` · `delete_statefulset` |
| DaemonSet | `list_daemonsets` · `get_daemonset` · `create_daemonset` · `delete_daemonset` |
| Pod | `list_pods` · `get_pod` · `create_pod` · `delete_pod` |
| Service | `list_services` · `get_service` · `create_service` · `delete_service` |
| ConfigMap | `list_configmaps` · `get_configmap` · `create_configmap` · `delete_configmap` |
| Secret | `list_secrets` · `get_secret` · `create_secret` · `delete_secret` |
| Namespace | `list_namespaces` · `get_namespace` · `create_namespace` · `delete_namespace` |
| Ingress | `list_ingresses` · `get_ingress` · `create_ingress` · `delete_ingress` |

Argument conventions:

```python
k8s.list_deployments()  # every namespace
k8s.list_deployments("default")  # one namespace
k8s.get_deployment("default", "web")
k8s.create_deployment("default", body)
k8s.delete_deployment("default", "web")

k8s.get_namespace("default")  # namespaces are cluster-scoped: name only
k8s.create_namespace(body)
```

`list_*` takes an optional `namespace`; omit it to span all namespaces. `get_*` and
`delete_*` take `(namespace, name)`, except the cluster-scoped `Namespace` helpers,
which take `name` only. `create_*` takes `(namespace, body)`, except
`create_namespace(body)`.

## Exec inside a pod

```python
out = k8s.exec_in_pod("default", "web-0", ["cat", "/etc/hostname"])
out = k8s.exec_in_pod("default", "web-0", "df -h", container="sidecar", timeout=30)
```

`command` is either a list of arguments or a shell string; `container` targets a
specific container and `timeout` bounds the exec in seconds. Standard output and
standard error are combined and returned as one string.

## Error handling

```python
from testkit import K8sError

try:
    k8s.get_deployment("default", "missing")
except K8sError as err:
    assert err.context.get("status_code") == 404
    assert err.context.get("reason") == "NotFound"
    assert err.context["namespace"] == "default"
    assert err.context["name"] == "missing"
```

`reason` and `status_code` are present when the API server reports them. Exec
failures add `pod` and `command` to the same context, and — like every
[framework exception](../api-reference.md#exceptions) — each context entry is
mirrored onto a direct attribute, so `err.status_code` works as well.

## Quantity normalization

```python
from testkit import to_mib

to_mib("2Gi")  # 2048
to_mib("2048Mi")  # 2048
to_mib("500")  # 0 — bare numbers are byte counts
to_mib(None)  # 0
```

`to_mib` is dependency-free — it works without the `kubernetes` package.
