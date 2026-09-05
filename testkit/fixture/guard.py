"""Concurrent fixture guard: cross-process coordination for shared fixtures.

A JSON state file plus a :class:`filelock.FileLock` coordinate multiple
processes (e.g. ``pytest-xdist`` workers). The first caller becomes the
*creator* and builds the shared resource; every other caller waits and
reuses it. A creation exceeding the timeout threshold is treated as a crash
and can be taken over.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from filelock import FileLock

from testkit.exceptions import FixtureError
from testkit.logging_setup import get_logger

logger = get_logger("fixture")

_STATE_CREATING = "creating"
_STATE_CREATED = "created"
_STATE_FAILED = "failed"


class ConcurrentFixtureGuard:
    """Coordinates creation of a single shared fixture across processes.

    Parameters
    ----------
    state_path:
        Path to the JSON state file.
    timeout:
        Seconds after which an unfinished ``creating`` state is considered a
        crash and can be taken over.
    poll_interval:
        Seconds between readiness polls in :meth:`wait_until_created`.
    exclusive_lock_path:
        Optional path for the exclusive (critical-section) lock. Defaults to
        ``<state_path>.exclusive.lock``.
    """

    def __init__(
        self,
        state_path: str | Path,
        timeout: float = 300.0,
        poll_interval: float = 1.0,
        exclusive_lock_path: str | Path | None = None,
    ) -> None:
        self._state_path = Path(state_path)
        self._timeout = timeout
        self._poll_interval = poll_interval
        self._lock = FileLock(str(self._state_path) + ".lock")
        self._exclusive_lock = FileLock(
            str(exclusive_lock_path or (str(self._state_path) + ".exclusive.lock"))
        )
        self._process_id = uuid.uuid4().hex

    # -- state file helpers ---------------------------------------------------

    def _read(self) -> dict[str, Any] | None:
        if not self._state_path.exists():
            return None
        with self._state_path.open("r", encoding="utf-8") as fh:
            return cast(dict[str, Any], json.load(fh))

    def _write(self, state: dict[str, Any]) -> None:
        tmp = self._state_path.with_suffix(self._state_path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, self._state_path)

    def _delete(self) -> None:
        if self._state_path.exists():
            self._state_path.unlink()

    # -- core API -------------------------------------------------------------

    def should_create(self, metadata: dict[str, Any] | None = None) -> bool:
        """Decide whether this process should create the shared fixture.

        The first caller returns ``True`` and writes a ``creating`` state;
        subsequent callers return ``False``. A stale ``creating`` state older
        than the timeout is treated as a crash and taken over.

        Returns
        -------
        bool
            ``True`` if this process is now the creator.
        """
        metadata = metadata or {}
        with self._lock:
            state = self._read()
            now = time.time()

            if state is None or state.get("state") == _STATE_FAILED:
                self._write(
                    {
                        "state": _STATE_CREATING,
                        "creator_id": self._process_id,
                        "created_at": now,
                        "metadata": metadata,
                    }
                )
                logger.v2("fixture creation claimed by %s", self._process_id)
                return True

            if state.get("state") == _STATE_CREATED:
                return False

            # state == creating
            created_at = float(state.get("created_at", now))
            if now - created_at > self._timeout:
                logger.v2("fixture creation timed out (%.1fs); taking over", now - created_at)
                self._write(
                    {
                        "state": _STATE_CREATING,
                        "creator_id": self._process_id,
                        "created_at": now,
                        "metadata": metadata,
                    }
                )
                return True
            return False

    def mark_created(self, metadata: dict[str, Any] | None = None) -> None:
        """Mark the fixture as successfully created (creator only)."""
        metadata = metadata or {}
        with self._lock:
            state = self._read()
            if state is None or state.get("creator_id") != self._process_id:
                raise FixtureError("only the creator can mark created", pid=self._process_id)
            state["state"] = _STATE_CREATED
            state["metadata"] = metadata
            self._write(state)
        logger.v2("fixture marked created by %s", self._process_id)

    def mark_failed(self, metadata: dict[str, Any] | None = None) -> None:
        """Mark creation as failed (creator only).

        The state file is removed so a waiting peer may claim creation.
        """
        metadata = metadata or {}
        with self._lock:
            state = self._read()
            if state is None or state.get("creator_id") != self._process_id:
                raise FixtureError("only the creator can mark failed", pid=self._process_id)
            state["state"] = _STATE_FAILED
            state["metadata"] = metadata
            self._write(state)
        logger.v2("fixture creation failed by %s", self._process_id)

    def is_creator(self) -> bool:
        """Return whether this process created the fixture."""
        state = self._read()
        return bool(state and state.get("creator_id") == self._process_id)

    def wait_until_created(
        self,
        check_fn: Callable[[], bool],
        timeout: float,
        poll_interval: float | None = None,
    ) -> bool:
        """Poll a user-defined readiness check until it succeeds or times out.

        ``check_fn`` is *not* a state-file check — it is an arbitrary
        callable such as ``lambda: api.get_cluster_status() == "Available"``.
        A ``failed`` state (creator gave up) aborts the wait immediately.

        Returns
        -------
        bool
            ``True`` if ``check_fn`` became truthy before the timeout.
        """
        interval = poll_interval or self._poll_interval
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self._read()
            if state is not None and state.get("state") == _STATE_FAILED:
                raise FixtureError(
                    "fixture creator reported failure", metadata=state.get("metadata")
                )
            try:
                if check_fn():
                    return True
            except Exception:
                # Readiness probes may raise transiently; keep polling.
                pass
            time.sleep(interval)
        return False

    def cleanup(self) -> bool:
        """Remove the state file (creator only).

        Returns
        -------
        bool
            ``True`` if this process was the creator and removed the file.
        """
        with self._lock:
            if self.is_creator():
                self._delete()
                logger.v2("fixture state cleaned by creator %s", self._process_id)
                return True
        return False

    # -- exclusive critical section ------------------------------------------

    def exclusive(self) -> FileLock:
        """Context manager serializing a critical section across processes."""
        return self._exclusive_lock
