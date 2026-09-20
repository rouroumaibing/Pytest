"""Kubernetes direct-client utilities (requires ``kubernetes``, imported lazily)."""

from testkit.k8s.client import K8sClient
from testkit.k8s.quantities import to_mib

__all__ = ["K8sClient", "to_mib"]
