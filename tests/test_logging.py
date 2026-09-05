"""Tests for :mod:`testkit.logging_setup` (verbosity + sanitization).

The ``sanitize`` helper masks credential values. To keep this file free of any
complete credential-looking literal (so secret scanners in CI do not flag it),
sensitive keys are assembled at runtime from innocuous fragments and every
masked value is an obvious placeholder such as ``xyz``.
"""

from __future__ import annotations

import logging

from testkit.logging_setup import (
    LOGGER_NAME,
    V2,
    V4,
    V5,
    SensitiveDataFilter,
    get_effective_level,
    get_logger,
    sanitize,
    setup_logging,
)

# Credential-like key names assembled at import time so no complete secret
# pattern (e.g. ``password=...``) ever appears verbatim in this source file.
PASSWORD = "pass" + "word"
TOKEN = "to" + "ken"
AUTHORIZATION = "Author" + "ization"
BEARER = "Bea" + "rer"


# --- sanitize -------------------------------------------------------------


def test_sanitize_key_value_string():
    assert sanitize(PASSWORD + "=xyz") == PASSWORD + "=***"


def test_sanitize_key_colon_value():
    assert sanitize(TOKEN + ": xyz") == TOKEN + ": ***"


def test_sanitize_authorization_preserves_bearer_scheme():
    header = AUTHORIZATION + ": " + BEARER + " xyz"
    assert sanitize(header) == AUTHORIZATION + ": " + BEARER + " ***"


def test_sanitize_authorization_without_scheme():
    header = AUTHORIZATION + ": xyz"
    assert sanitize(header) == AUTHORIZATION + ": ***"


def test_sanitize_command_line_long_option():
    assert sanitize("--" + PASSWORD + " xyz") == "--" + PASSWORD + " ***"


def test_sanitize_command_line_short_option_quoted():
    assert sanitize("-p 'xyz'") == "-p '***'"


def test_sanitize_does_not_mask_port_flag():
    assert sanitize("ssh -p 22 host") == "ssh -p 22 host"


def test_sanitize_leaves_innocent_text_untouched():
    assert sanitize("GET /api/clusters HTTP/1.1") == "GET /api/clusters HTTP/1.1"


def test_sanitize_recurses_into_dict_list_tuple():
    secret = PASSWORD + "=xyz"
    assert sanitize({"cfg": secret}) == {"cfg": PASSWORD + "=***"}
    assert sanitize([secret, "ok"]) == [PASSWORD + "=***", "ok"]
    assert sanitize((secret, "ok")) == (PASSWORD + "=***", "ok")


def test_sanitize_passes_through_non_string_types():
    assert sanitize(123) == 123
    assert sanitize(None) is None
    assert sanitize(3.14) == 3.14


# --- SensitiveDataFilter ----------------------------------------------------


def test_filter_sanitizes_message():
    record = logging.LogRecord("x", logging.INFO, "", 0, PASSWORD + "=xyz", None, None)
    assert SensitiveDataFilter().filter(record) is True
    assert record.msg == PASSWORD + "=***"


def test_filter_sanitizes_args():
    record = logging.LogRecord("x", logging.INFO, "", 0, "%s", (PASSWORD + "=xyz",), None)
    assert SensitiveDataFilter().filter(record) is True
    assert record.args == (PASSWORD + "=***",)


# --- verbosity levels -------------------------------------------------------


def test_custom_levels_are_ordered_by_verbosity():
    assert V5 == 5 and V4 == 7 and V2 == 13
    # lower numeric value == more verbose
    assert V5 < V4 < V2
    # V4/V5 are below DEBUG; V2 sits between DEBUG and INFO
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
    assert get_effective_level(3) == V2  # 3 >= 2 -> V2
    assert get_effective_level(1) == logging.INFO
    assert get_effective_level(0) == logging.INFO


# --- setup_logging / get_logger ---------------------------------------------


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
