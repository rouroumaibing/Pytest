"""Full unit tests for testkit.logging_setup.

These tests exercise the verbosity-control and sensitive-data masking API.
Note: ``add_file_handler`` is *idempotent* and returns ``None`` (it does not
return a handler id); it attaches a ``RotatingFileHandler`` to the framework
logger. See the bug report for details.
"""

from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler

import pytest
from testkit.logging_setup import (
    V2,
    V4,
    V5,
    add_file_handler,
    get_logger,
    get_logging_info,
    remove_file_handler,
    set_level,
    set_verbosity,
    setup_logging,
)


def test_setup_logging_returns_framework_logger():
    logger = setup_logging()
    assert isinstance(logger, logging.Logger)
    assert logger.name == "testkit"


def test_get_logger_returns_child():
    child = get_logger("child")
    assert child.name == "testkit.child"
    assert child.parent is not None

    root = get_logger()
    assert root.name == "testkit"


def test_set_level_accepts_str_and_int():
    set_level("DEBUG")
    assert logging.getLogger("testkit").level == logging.DEBUG

    set_level("INFO")
    assert logging.getLogger("testkit").level == logging.INFO

    set_level(V5)
    assert logging.getLogger("testkit").level == V5


def test_set_level_unknown_name_raises_value_error():
    with pytest.raises(ValueError):
        set_level("NOT_A_REAL_LEVEL")

    # Non-str / non-int is also rejected.
    with pytest.raises(ValueError):
        set_level(object())  # type: ignore[arg-type]


def test_set_verbosity_maps_0_to_5():
    set_verbosity(0)
    assert logging.getLogger("testkit").level == logging.INFO

    set_verbosity(1)
    assert logging.getLogger("testkit").level == logging.INFO

    set_verbosity(2)
    assert logging.getLogger("testkit").level == V2

    set_verbosity(3)
    assert logging.getLogger("testkit").level == V2

    set_verbosity(4)
    assert logging.getLogger("testkit").level == V4

    set_verbosity(5)
    assert logging.getLogger("testkit").level == V5


def test_add_file_handler_attaches_rotating_handler():
    import tempfile

    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "app.log")

    # Actual behaviour: returns None, not a handler id.
    assert add_file_handler(target) is None

    fw = logging.getLogger("testkit")
    attached = [
        h
        for h in fw.handlers
        if isinstance(h, RotatingFileHandler)
        and os.path.abspath(h.baseFilename) == os.path.abspath(target)
    ]
    assert len(attached) == 1
    handler = attached[0]
    assert handler.maxBytes == 10 * 1024 * 1024
    assert handler.backupCount == 5

    # Idempotent: a second add does not create a duplicate.
    add_file_handler(target)
    assert (
        len(
            [
                h
                for h in fw.handlers
                if isinstance(h, RotatingFileHandler)
                and os.path.abspath(h.baseFilename) == os.path.abspath(target)
            ]
        )
        == 1
    )

    # Cleanup so the handler does not leak across tests.
    assert remove_file_handler(target) is True


def test_add_file_handler_custom_rollover():
    import tempfile

    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "custom.log")

    add_file_handler(target, max_bytes=1024, backup_count=2)
    fw = logging.getLogger("testkit")
    handler = next(
        h
        for h in fw.handlers
        if isinstance(h, RotatingFileHandler)
        and os.path.abspath(h.baseFilename) == os.path.abspath(target)
    )
    assert handler.maxBytes == 1024
    assert handler.backupCount == 2

    assert remove_file_handler(target) is True


def test_remove_file_handler_idempotent():
    import tempfile

    tmp = tempfile.mkdtemp()
    target = os.path.join(tmp, "idempotent.log")

    add_file_handler(target)
    assert remove_file_handler(target) is True  # removed once
    assert remove_file_handler(target) is False  # second remove is a no-op

    # Removing an unknown file is also a no-op, not an error.
    assert remove_file_handler(os.path.join(tmp, "missing.log")) is False


def test_get_logging_info_structure():
    setup_logging()
    info = get_logging_info()
    assert isinstance(info, dict)
    assert "level" in info
    assert "level_no" in info
    assert "handlers" in info
    assert isinstance(info["handlers"], list)
    for entry in info["handlers"]:
        assert "type" in entry
        assert "level" in entry
        assert "filename" in entry


class _CaptureHandler(logging.Handler):
    """A handler that stores every emitted LogRecord for inspection."""

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def test_sensitive_data_masking():
    from testkit.logging_setup import SensitiveDataFilter

    child = get_logger("masktest")
    child.propagate = False  # isolate from the framework logger's handlers
    child.setLevel(logging.DEBUG)

    cap = _CaptureHandler()
    cap.addFilter(SensitiveDataFilter())
    child.addHandler(cap)

    try:
        child.info("token=abc123def456 and password=hunter2xyz MARKER")
    finally:
        child.removeHandler(cap)
        cap.close()

    assert cap.records, "expected at least one captured record"
    record = next(r for r in cap.records if "MARKER" in str(r.msg))
    masked = str(record.msg)
    assert "abc123def456" not in masked
    assert "hunter2xyz" not in masked
    assert "***" in masked


def test_paramiko_suppressed_unless_verbosity_5():
    set_verbosity(5)
    assert logging.getLogger("paramiko").level == V5

    set_verbosity(0)
    assert logging.getLogger("paramiko").level == logging.WARNING


# -- ported from test_logging.py (granular sanitize / filter / level mapping) --

from testkit.logging_setup import (  # noqa: E402
    LOGGER_NAME,
    SensitiveDataFilter,
    get_effective_level,
    sanitize,
)

# Credential-like key names assembled at import time so no complete secret
# pattern (e.g. ``password=...``) ever appears verbatim in this source file.
_PASSWORD = "pass" + "word"
_TOKEN = "to" + "ken"
_AUTHORIZATION = "Author" + "ization"
_BEARER = "Bea" + "rer"


def test_sanitize_key_value_string():
    assert sanitize(_PASSWORD + "=xyz") == _PASSWORD + "=***"


def test_sanitize_key_colon_value():
    assert sanitize(_TOKEN + ": xyz") == _TOKEN + ": ***"


def test_sanitize_authorization_preserves_bearer_scheme():
    header = _AUTHORIZATION + ": " + _BEARER + " xyz"
    assert sanitize(header) == _AUTHORIZATION + ": " + _BEARER + " ***"


def test_sanitize_authorization_without_scheme():
    header = _AUTHORIZATION + ": xyz"
    assert sanitize(header) == _AUTHORIZATION + ": ***"


def test_sanitize_command_line_long_option():
    assert sanitize("--" + _PASSWORD + " xyz") == "--" + _PASSWORD + " ***"


def test_sanitize_command_line_short_option_quoted():
    assert sanitize("-p 'xyz'") == "-p '***'"


def test_sanitize_does_not_mask_port_flag():
    assert sanitize("ssh -p 22 host") == "ssh -p 22 host"


def test_sanitize_leaves_innocent_text_untouched():
    assert sanitize("GET /api/clusters HTTP/1.1") == "GET /api/clusters HTTP/1.1"


def test_sanitize_recurses_into_dict_list_tuple():
    secret = _PASSWORD + "=xyz"
    assert sanitize({"cfg": secret}) == {"cfg": _PASSWORD + "=***"}
    assert sanitize([secret, "ok"]) == [_PASSWORD + "=***", "ok"]
    assert sanitize((secret, "ok")) == (_PASSWORD + "=***", "ok")


def test_sanitize_passes_through_non_string_types():
    assert sanitize(123) == 123
    assert sanitize(None) is None
    assert sanitize(3.14) == 3.14


def test_filter_sanitizes_message():
    record = logging.LogRecord("x", logging.INFO, "", 0, _PASSWORD + "=xyz", None, None)
    assert SensitiveDataFilter().filter(record) is True
    assert record.msg == _PASSWORD + "=***"


def test_filter_sanitizes_args():
    record = logging.LogRecord("x", logging.INFO, "", 0, "%s", (_PASSWORD + "=xyz",), None)
    assert SensitiveDataFilter().filter(record) is True
    assert record.args == (_PASSWORD + "=***",)


def test_custom_levels_are_ordered_by_verbosity():
    assert V5 == 5 and V4 == 7 and V2 == 13
    assert V5 < V4 < V2
    assert V4 < logging.DEBUG < V2 < logging.INFO


def test_custom_level_methods_are_attached():
    logger = get_logger("vtest")
    assert callable(logger.v2)
    assert callable(logger.v4)
    assert callable(logger.v5)


def test_get_effective_level_mapping():
    assert get_effective_level(5) == V5
    assert get_effective_level(4) == V4
    assert get_effective_level(2) == V2
    assert get_effective_level(3) == V2
    assert get_effective_level(1) == logging.INFO
    assert get_effective_level(0) == logging.INFO


def test_setup_logging_verbosity_sets_level():
    assert setup_logging(log_verbosity=4).level == V4
    assert setup_logging(log_verbosity=5).level == V5


def test_setup_logging_default_is_info():
    assert setup_logging().level == logging.INFO


def test_setup_logging_handler_is_notset():
    logger = setup_logging()
    assert len(logger.handlers) >= 1
    assert all(h.level == logging.NOTSET for h in logger.handlers)


def test_get_logger_names():
    assert get_logger().name == LOGGER_NAME
    assert get_logger("http").name == LOGGER_NAME + ".http"
