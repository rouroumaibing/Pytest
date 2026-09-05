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
import re
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
