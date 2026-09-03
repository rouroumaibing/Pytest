"""Log verbosity control and sensitive-data sanitization.

The design is inspired by Kubernetes' ``--v=N`` flag: a single integer
``log_verbosity`` is the only control point. Three custom log levels are
registered below ``logging.DEBUG``:

* ``V2`` (13) — API summary (method, path, status)
* ``V4`` (7)  — full response body
* ``V5`` (5)  — trace-level detail

Because these levels are numerically *below* ``DEBUG`` (10), the pytest
handler must be configured with level ``NOTSET`` (0) so it never performs
secondary filtering that would otherwise discard them.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

# Custom verbosity levels (lower numeric value = more verbose).
V5 = 5  # trace
V4 = 7  # full response body
V2 = 13  # api summary

logging.addLevelName(V5, "V5")
logging.addLevelName(V4, "V4")
logging.addLevelName(V2, "V2")

LOGGER_NAME = "testkit"


def _logger_v(level: int, name: str):
    """Build a bound method that logs at a custom verbosity level."""

    def _log(self, msg: str, *args: Any, **kwargs: Any) -> None:
        if self.isEnabledFor(level):
            self._log(level, msg, args, **kwargs)

    _log.__name__ = name
    _log.__doc__ = f"Log at custom level {name} ({level})."
    return _log


# Attach v2/v4/v5 helpers onto the standard Logger class.
if not hasattr(logging.Logger, "v2"):
    logging.Logger.v2 = _logger_v(V2, "v2")  # type: ignore[attr-defined]
if not hasattr(logging.Logger, "v4"):
    logging.Logger.v4 = _logger_v(V4, "v4")  # type: ignore[attr-defined]
if not hasattr(logging.Logger, "v5"):
    logging.Logger.v5 = _logger_v(V5, "v5")  # type: ignore[attr-defined]


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
        r"(?P<pre>(?:authorization|proxy-authorization)\s*[:=]\s*)(?P<val>(?:bearer|basic|token)\s+)?[^\s,]+",
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
    level: Optional[int] = None,
    fmt: Optional[str] = None,
) -> logging.Logger:
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
    logging.Logger
        The configured ``testkit`` logger.
    """
    global _configured

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(get_effective_level(log_verbosity) if level is None else level)
    logger.propagate = True

    if not logger.handlers and not _configured:
        handler = logging.StreamHandler()
        # CRITICAL: NOTSET so the handler never drops the sub-DEBUG custom
        # levels V2/V4/V5 through secondary filtering.
        handler.setLevel(logging.NOTSET)
        handler.setFormatter(
            logging.Formatter(
                fmt or "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )
        handler.addFilter(SensitiveDataFilter())
        logger.addHandler(handler)

    _configured = True
    return logger


def get_logger(name: Optional[str] = None) -> logging.Logger:
    """Return a child logger of the framework logger."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
