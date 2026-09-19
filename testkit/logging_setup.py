"""Log verbosity control and sensitive-data sanitization.

The design is inspired by Kubernetes' ``--v=N`` flag: a single integer
``log_verbosity`` is the only control point. Three custom log levels are
registered below ``logging.DEBUG``:

* ``V2`` (13) — API summary (method, path, status)
* ``V4`` (7)  — full response body
* ``V5`` (5)  — trace-level detail

``V4``/``V5`` sit numerically *below* ``DEBUG`` (10) and ``V2`` sits between
``DEBUG`` and ``INFO``. The pytest handler is therefore configured with level
``NOTSET`` (0) so it never performs secondary filtering that would discard the
sub-DEBUG levels.
"""

from __future__ import annotations

import logging
import os
import re
from logging.handlers import RotatingFileHandler
from typing import Any, cast

# Custom verbosity levels (lower numeric value = more verbose).
V5 = 5  # trace
V4 = 7  # full response body
V2 = 13  # api summary

logging.addLevelName(V5, "V5")
logging.addLevelName(V4, "V4")
logging.addLevelName(V2, "V2")

LOGGER_NAME = "testkit"


class VerboseLogger(logging.Logger):
    """A :class:`logging.Logger` with extra ``v2``/``v4``/``v5`` verbosity methods."""

    def v2(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(V2):
            self._log(V2, msg, args, **kwargs)

    def v4(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(V4):
            self._log(V4, msg, args, **kwargs)

    def v5(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(V5):
            self._log(V5, msg, args, **kwargs)


# Register the subclass so every logger returned by ``logging.getLogger``
# (including pytest's own loggers) exposes the verbosity helpers.
logging.setLoggerClass(VerboseLogger)


_SENSITIVE_KEYS = (
    "password",
    "passwd",
    "pwd",
    "token",
    "access_token",
    "refresh_token",
    "secret",
    "api_key",
    "apikey",
    "access_key",
    # NOTE: "authorization" is intentionally absent here — it is handled by the
    # dedicated header pattern below, which understands the optional
    # ``Bearer``/``Basic``/``token`` scheme prefix. Including it here would make
    # the generic ``key=value`` pattern swallow only the scheme word (e.g.
    # ``Bearer``) and leak the actual credential that follows.
)

# Patterns matching ``<key> = <value>`` / ``<key>: <value>`` and
# ``Authorization: Bearer <value>``-style headers.
_VALUE_PATTERNS = [
    re.compile(
        rf"(?P<pre>\b(?:{'|'.join(_SENSITIVE_KEYS)})\b[\"']?\s*[:=]\s*[\"']?)(?P<val>[^\"'\s,}}]+)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<pre>(?:authorization|proxy-authorization)\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?)(?P<val>[^\s,]+)",
        re.IGNORECASE,
    ),
    # Command-line option forms: ``--password 'x'``, ``--token=x``,
    # ``sshpass -p 'secret'``. Covers bare secrets in command text, not just
    # the ``key=value`` form. The short ``-p`` form only matches a quoted
    # value so it never clobbers innocuous ``-p <port>``.
    re.compile(
        r"(?P<pre>(?:--(?:password|passwd|pwd|token|secret|api[-_]?key|access[-_]?key)[\s=]+['\"]?|-p\s*['\"]))(?P<val>[^'\"\s]+)",
        re.IGNORECASE,
    ),
]


def sanitize(text: Any) -> Any:
    """Replace sensitive values in *text* with ``***``.

    Strings are pattern-replaced; mappings are sanitized recursively on their
    string representation of values; other types are returned unchanged.
    """
    if isinstance(text, str):
        result = text
        for pattern in _VALUE_PATTERNS:
            result = pattern.sub(lambda m: m.group("pre") + "***", result)
        return result
    if isinstance(text, dict):
        return {k: sanitize(v) for k, v in text.items()}
    if isinstance(text, (list, tuple)):
        return type(text)(sanitize(v) for v in text)
    return text


class SensitiveDataFilter(logging.Filter):
    """A logging filter that sanitizes sensitive data before emission."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize(record.msg)
        if record.args:
            record.args = tuple(sanitize(a) for a in record.args)
        return True


_configured = False


def get_effective_level(verbosity: int) -> int:
    """Map a ``log_verbosity`` integer to a concrete logging level.

    Higher verbosity selects lower (more verbose) logging levels.
    """
    if verbosity >= 5:
        return V5
    if verbosity >= 4:
        return V4
    if verbosity >= 2:
        return V2
    return logging.INFO


def setup_logging(
    log_verbosity: int = 0,
    level: int | None = None,
    fmt: str | None = None,
) -> VerboseLogger:
    """Configure the framework logger once.

    Parameters
    ----------
    log_verbosity:
        Single control point. ``0`` disables verbosity logging, ``2`` enables
        ``V2``, ``4`` enables ``V4``, ``5`` enables ``V5``.
    level:
        Explicit override for the base level (defaults to ``get_effective_level``).
    fmt:
        Optional log format string.

    Returns
    -------
    VerboseLogger
        The configured ``testkit`` logger.
    """
    global _configured

    logger = cast(VerboseLogger, logging.getLogger(LOGGER_NAME))
    logger.setLevel(get_effective_level(log_verbosity) if level is None else level)
    logger.propagate = True

    if not logger.handlers and not _configured:
        handler = logging.StreamHandler()
        # CRITICAL: NOTSET so the handler never drops the sub-DEBUG custom
        # levels V2/V4/V5 through secondary filtering.
        handler.setLevel(logging.NOTSET)
        handler.setFormatter(
            logging.Formatter(fmt or "%(asctime)s %(levelname)s %(name)s: %(message)s")
        )
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)

    _configured = True
    return logger


def get_logger(name: str | None = None) -> VerboseLogger:
    """Return a child logger of the framework logger."""
    return cast(
        VerboseLogger,
        logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME),
    )


# Module-level logger used by the runtime-adjustment API below.
logger = get_logger()


# -- runtime adjustment API (framework logger) -------------------------------


def _framework_logger() -> VerboseLogger:
    return cast(VerboseLogger, logging.getLogger(LOGGER_NAME))


def _suppress_paramiko(effective_level: int) -> None:
    """Keep ``paramiko`` protocol logs quiet except at maximum verbosity (V5).

    paramiko logs connection/protocol detail at DEBUG/INFO. We pin its logger
    to WARNING unless the framework is at V5, where we open it up so the trace
    is visible instead of flooding normal runs.
    """
    paramiko_logger = logging.getLogger("paramiko")
    if effective_level <= V5:
        paramiko_logger.setLevel(V5)
    else:
        paramiko_logger.setLevel(logging.WARNING)


def set_level(level: str | int) -> None:
    """Set the framework logger level by name (``"DEBUG"``) or integer.

    Raises
    ------
    ValueError
        If *level* is neither a known level name nor an integer.
    """
    if isinstance(level, str):
        resolved = logging.getLevelName(level.upper())
        if not isinstance(resolved, int):
            raise ValueError(f"unknown log level: {level!r}")
    elif isinstance(level, int):
        resolved = level
    else:
        raise ValueError(f"level must be str or int, got {type(level).__name__}")
    _framework_logger().setLevel(resolved)
    _suppress_paramiko(resolved)
    logger.v2("log level set to %s", logging.getLevelName(resolved))


def set_verbosity(verbosity: int) -> None:
    """Map a 0-5 verbosity to the ``--v=N`` scheme and apply it.

    Equivalent to calling :func:`setup_logging` with the matching verbosity,
    but without re-adding handlers.
    """
    level = get_effective_level(verbosity)
    _framework_logger().setLevel(level)
    _suppress_paramiko(level)
    logger.v2("log verbosity set to %s -> level %s", verbosity, logging.getLevelName(level))


def add_file_handler(
    filename: str | os.PathLike[str],
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> None:
    """Attach an idempotent rotating file handler to the framework logger.

    The handler writes rotated logs (default 10 MiB, 5 backups) and sanitizes
    sensitive data, exactly like the console handler. Re-adding the same file
    is a no-op.
    """
    fw_logger = _framework_logger()
    target = os.path.abspath(os.fspath(filename))
    for handler in fw_logger.handlers:
        if (
            isinstance(handler, RotatingFileHandler)
            and os.path.abspath(handler.baseFilename) == target
        ):
            logger.v2("file handler already attached: %s", target)
            return
    handler = RotatingFileHandler(target, maxBytes=max_bytes, backupCount=backup_count)
    # NOTSET so sub-DEBUG verbosity levels are never secondarily filtered.
    handler.setLevel(logging.NOTSET)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(SensitiveDataFilter())
    fw_logger.addHandler(handler)
    logger.v2(
        "file handler attached: %s (%dMiB x%d)", target, max_bytes // 1024 // 1024, backup_count
    )


def remove_file_handler(filename: str | os.PathLike[str]) -> bool:
    """Remove a previously attached rotating file handler (idempotent).

    Returns ``True`` if a matching handler was removed, ``False`` otherwise.
    """
    fw_logger = _framework_logger()
    target = os.path.abspath(os.fspath(filename))
    removed = False
    for handler in list(fw_logger.handlers):
        if (
            isinstance(handler, RotatingFileHandler)
            and os.path.abspath(handler.baseFilename) == target
        ):
            fw_logger.removeHandler(handler)
            handler.close()
            removed = True
            logger.v2("file handler removed: %s", target)
    return removed


def get_logging_info() -> dict[str, Any]:
    """Return a snapshot of the current logging configuration."""
    fw_logger = _framework_logger()
    return {
        "level": logging.getLevelName(fw_logger.level),
        "level_no": fw_logger.level,
        "handlers": [
            {
                "type": type(h).__name__,
                "level": logging.getLevelName(h.level),
                "filename": getattr(h, "baseFilename", None),
            }
            for h in fw_logger.handlers
        ],
    }
