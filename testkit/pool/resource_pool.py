"""Generic resource pool backed by YAML persistence.

Resources are plain ``dict`` objects with no predefined fields — the pool
does not assume anything about the business shape of a resource. The only
framework-reserved key is ``status``, whose value must be exactly ``"free"``
or ``"allocated"``.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml
from filelock import FileLock

from testkit.exceptions import PoolError
from testkit.logging_setup import get_logger

logger = get_logger("pool")

# CRITICAL: the only two legal status values.
FREE = "free"
ALLOCATED = "allocated"
_STATUS_VALUES = (FREE, ALLOCATED)


def _validate_status(resource: dict[str, Any]) -> None:
    status = resource.get("status")
    if status not in _STATUS_VALUES:
        raise PoolError(
            "illegal resource status; only 'free' and 'allocated' are allowed",
            resource=resource,
            status=status,
        )


class ResourcePool:
    """A cross-process safe pool of generic resources.

    State changes are written back to the YAML file immediately under a
    :class:`filelock.FileLock`, and every acquire/release reloads the latest
    state from disk first (no in-memory cache), so the pool is safe to share
    across multiple ``pytest-xdist`` workers.

    Parameters
    ----------
    path:
        Path to the YAML persistence file.
    retries:
        Default retry count for :meth:`acquire` when insufficient resources
        are free.
    interval:
        Default wait (seconds) between acquire retries.
    id_field:
        Name of the key used to identify a resource (default ``"id"``). The
        pool otherwise places no constraint on resource field names.
    """

    def __init__(
        self,
        path: str | Path,
        retries: int = 3,
        interval: float = 1.0,
        id_field: str = "id",
    ) -> None:
        self._path = Path(path)
        self._retries = retries
        self._interval = interval
        self._id_field = id_field
        self._lock = FileLock(str(self._path) + ".lock")

    # -- persistence ---------------------------------------------------------

    def _read(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        with self._path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if data is None:
            return []
        if not isinstance(data, list):
            raise PoolError("pool file must contain a list", path=str(self._path))
        for item in data:
            if not isinstance(item, dict):
                raise PoolError("pool entries must be mappings", path=str(self._path))
            _validate_status(item)
        return data

    def _write(self, resources: list[dict[str, Any]]) -> None:
        # Atomic write: dump to a temp file then replace, so a crash mid-write
        # cannot leave a truncated pool file behind.
        tmp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            yaml.safe_dump(resources, fh, sort_keys=False, allow_unicode=True)
        os.replace(tmp_path, self._path)

    # -- public API ----------------------------------------------------------

    def add(self, resource: dict[str, Any]) -> None:
        """Add a new resource in the ``free`` state and persist it."""
        entry = dict(resource)
        entry["status"] = FREE
        with self._lock:
            resources = self._read()
            ids = {r.get(self._id_field) for r in resources}
            if entry.get(self._id_field) in ids:
                raise PoolError(
                    "duplicate resource id",
                    resource_id=entry.get(self._id_field),
                )
            resources.append(entry)
            self._write(resources)
        logger.v2("added resource id=%r", entry.get(self._id_field))

    def acquire(
        self,
        count: int = 1,
        predicate: Callable[[dict[str, Any]], bool] | None = None,
        retries: int | None = None,
        interval: float | None = None,
    ) -> list[dict[str, Any]]:
        """Allocate up to *count* free resources, retrying if needed.

        Parameters
        ----------
        count:
            Number of resources to allocate.
        predicate:
            Optional filter applied to candidate (free) resources.
        retries:
            Retry attempts, defaulting to the pool-wide value.
        interval:
            Wait between retries, defaulting to the pool-wide value.

        Returns
        -------
        list[dict]
            The allocated resources (status now ``"allocated"``). May be
            shorter than *count* if retries are exhausted.
        """
        attempts = self._retries if retries is None else retries
        delay = self._interval if interval is None else interval

        for attempt in range(attempts + 1):
            with self._lock:
                resources = self._read()
                free = [r for r in resources if r.get("status") == FREE]
                if predicate is not None:
                    free = [r for r in free if predicate(r)]
                allocated = free[:count]
                if len(allocated) == count or attempt == attempts:
                    if allocated:
                        for r in allocated:
                            r["status"] = ALLOCATED
                        self._write(resources)
                        logger.v2(
                            "allocated %d resource(s) on attempt %d",
                            len(allocated),
                            attempt + 1,
                        )
                    return allocated
            time.sleep(delay)

        return []

    def release(self, resources: list[dict[str, Any]]) -> None:
        """Return previously allocated resources to the ``free`` state."""
        if not resources:
            return
        ids = {r.get(self._id_field) for r in resources}
        with self._lock:
            current = self._read()
            for r in current:
                if r.get(self._id_field) in ids:
                    r["status"] = FREE
            self._write(current)
        logger.v2("released %d resource(s)", len(ids))

    def release_all(self) -> None:
        """Return every resource to the ``free`` state."""
        with self._lock:
            current = self._read()
            for r in current:
                r["status"] = FREE
            self._write(current)
        logger.v2("released all resources")

    def remove(self, resource_id: Any) -> None:
        """Remove a resource from the pool entirely."""
        with self._lock:
            current = self._read()
            remaining = [r for r in current if r.get(self._id_field) != resource_id]
            if len(remaining) == len(current):
                raise PoolError("resource not found", resource_id=resource_id)
            self._write(remaining)
        logger.v2("removed resource id=%r", resource_id)

    def list(self) -> list[dict[str, Any]]:
        """Return a snapshot of all resources (fresh from disk)."""
        with self._lock:
            return self._read()
