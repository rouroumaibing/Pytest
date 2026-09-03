"""Unified exception hierarchy for the framework.

The hierarchy follows a single root :class:`TestKitError` with one subclass
per framework module. Every exception carries structured *context* (domain
data such as ``status_code``, ``resource_id``, ``command``) that is
automatically appended to the human-readable message, and preserves the
original exception via ``original_exception`` so the full traceback chain is
never lost.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional


class TestKitError(Exception):
    """Base class for every framework exception.

    Parameters
    ----------
    message:
        Human-readable description of the failure.
    original_exception:
        The underlying exception, if any, to preserve the exception chain.
    context:
        Arbitrary keyword arguments of domain data. Each ``key=value`` pair is
        appended to the message as ``key=value`` for quick diagnosis.
    """

    def __init__(
        self,
        message: str,
        original_exception: Optional[BaseException] = None,
        **context: Any,
    ) -> None:
        self.message = message
        self.original_exception = original_exception
        self.context: dict[str, Any] = dict(context)
        super().__init__(self._format())

    def _format(self) -> str:
        parts = [self.message]
        if self.context:
            rendered = ", ".join(
                f"{key}={value!r}" for key, value in self.context.items()
            )
            parts.append(f"[{rendered}]")
        return " ".join(parts)

    def __str__(self) -> str:
        return self._format()

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self._format()!r})"


class ConfigError(TestKitError):
    """Raised for configuration loading, merging or validation failures."""


class PoolError(TestKitError):
    """Raised for resource-pool allocation/release/persistence failures."""


class SSHError(TestKitError):
    """Raised for SSH connection or command-execution failures."""

    def __init__(
        self,
        message: str,
        command: Optional[str] = None,
        exit_code: Optional[int] = None,
        stderr: Optional[str] = None,
        original_exception: Optional[BaseException] = None,
        **context: Any,
    ) -> None:
        if command is not None:
            context.setdefault("command", command)
        if exit_code is not None:
            context.setdefault("exit_code", exit_code)
        if stderr is not None:
            context.setdefault("stderr", stderr)
        super().__init__(message, original_exception=original_exception, **context)


class HTTPError(TestKitError):
    """Raised for HTTP request failures.

    Carries ``status_code`` and the parsed ``response_data`` (when available)
    as structured context.
    """

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_data: Any = None,
        original_exception: Optional[BaseException] = None,
        **context: Any,
    ) -> None:
        if status_code is not None:
            context.setdefault("status_code", status_code)
        if response_data is not None:
            context.setdefault("response_data", response_data)
        super().__init__(message, original_exception=original_exception, **context)


class ResourceNotFoundError(HTTPError):
    """Raised when an API responds with 404 for a known resource.

    Automatically carries ``resource_type`` and ``resource_id`` so failures can
    be correlated to the specific missing resource.
    """

    def __init__(
        self,
        resource_type: str,
        resource_id: Any,
        status_code: int = 404,
        response_data: Any = None,
        original_exception: Optional[BaseException] = None,
        **context: Any,
    ) -> None:
        message = f"resource not found: {resource_type}={resource_id!r}"
        super().__init__(
            message,
            status_code=status_code,
            response_data=response_data,
            original_exception=original_exception,
            resource_type=resource_type,
            resource_id=resource_id,
            **context,
        )


class FixtureError(TestKitError):
    """Raised for concurrent-fixture coordination failures."""


class CleanupError(TestKitError):
    """Raised for resource-cleanup failures that survived retries."""


class PipelineError(TestKitError):
    """Raised for multi-stage pipeline orchestration failures."""
