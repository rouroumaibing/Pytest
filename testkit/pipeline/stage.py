"""Multi-stage pipeline orchestration.

Stages execute serially in registration order. Any failure causes subsequent
stages to be skipped. A ``resume_from`` checkpoint lets callers restart from a
specific stage while earlier stages are recorded as ``skipped`` (they do not
affect the final success judgement). The pipeline is stateless — no checkpoint
files are written; the caller supplies the recovery point explicitly.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from testkit.exceptions import PipelineError
from testkit.logging_setup import get_logger

logger = get_logger("pipeline")

PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"


@dataclass
class StageResult:
    """Outcome of a single stage."""

    name: str
    status: str
    error: Optional[BaseException] = None
    data: Any = None

    @property
    def passed(self) -> bool:
        return self.status == PASSED

    @property
    def failed(self) -> bool:
        return self.status == FAILED

    @property
    def skipped(self) -> bool:
        return self.status == SKIPPED


def _call_stage(fn: Callable[..., Any], context: Any) -> Any:
    """Invoke a stage function, passing ``context`` only if it accepts it."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        params = {}
    if params:
        return fn(context)
    return fn()


class Pipeline:
    """A serial, resumable sequence of stages.

    Parameters
    ----------
    name:
        Optional pipeline name (for logging).
    """

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name or "pipeline"
        self._stages: list[tuple[str, Callable[..., Any]]] = []
        self._results: list[StageResult] = []

    def add_stage(self, name: str, fn: Callable[..., Any]) -> "Pipeline":
        """Register a stage.

        Parameters
        ----------
        name:
            Unique stage name (used for ``resume_from``).
        fn:
            Stage function; zero-arg or accepting a single ``context`` arg.

        Returns
        -------
        Pipeline
            ``self``, to allow chaining.
        """
        if any(existing == name for existing, _ in self._stages):
            raise PipelineError("duplicate stage name", stage=name)
        self._stages.append((name, fn))
        return self

    def stage(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Decorator form of :meth:`add_stage`."""

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self.add_stage(name, fn)
            return fn

        return decorator

    def run(
        self,
        resume_from: Optional[str] = None,
        context: Any = None,
    ) -> list[StageResult]:
        """Execute stages serially.

        Parameters
        ----------
        resume_from:
            Stage name to start from; earlier stages are recorded as
            ``skipped``.
        context:
            Optional value passed to stage functions that accept it.

        Returns
        -------
        list[StageResult]
            One result per registered stage, in registration order.
        """
        names = [name for name, _ in self._stages]
        start_index = 0
        if resume_from is not None:
            if resume_from not in names:
                raise PipelineError("unknown resume_from stage", stage=resume_from)
            start_index = names.index(resume_from)

        results: list[StageResult] = []
        failed = False

        for index, (name, fn) in enumerate(self._stages):
            if index < start_index:
                results.append(StageResult(name=name, status=SKIPPED))
                logger.v2("pipeline stage skipped (resume): %s", name)
                continue
            if failed:
                results.append(StageResult(name=name, status=SKIPPED))
                logger.v2("pipeline stage skipped (previous failure): %s", name)
                continue

            logger.v2("pipeline stage started: %s", name)
            try:
                data = _call_stage(fn, context)
                results.append(StageResult(name=name, status=PASSED, data=data))
                logger.v2("pipeline stage passed: %s", name)
            except Exception as exc:  # noqa: BLE001 - record and skip the rest
                results.append(StageResult(name=name, status=FAILED, error=exc))
                failed = True
                logger.v2("pipeline stage failed: %s (%s)", name, exc)

        self._results = results
        return results

    @property
    def success(self) -> bool:
        """Whether the last run had no failed stages."""
        return bool(self._results) and not any(r.failed for r in self._results)
