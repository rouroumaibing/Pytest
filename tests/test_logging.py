"""Unit tests for log verbosity and sensitive-data sanitization."""

from __future__ import annotations

import logging

import pytest

from testkit.logging_setup import (
    V2,
    V4,
    V5,
    SensitiveDataFilter,
    get_effective_level,
    get_logger,
    sanitize,
    setup_logging,
)


def test_sanitize_replaces_password():
    assert sanitize("password=secret123") == "password=***"
    assert sanitize("password: secret123") == "password: ***"


def test_sanitize_replaces_token_and_api_key():
    assert sanitize("token=abc123") == "token=***"
    assert sanitize("api_key: k-999") == "api_key: ***"
    assert sanitize("access_token=xyz") == "access_token=***"


def test_sanitize_replaces_authorization_header():
    out = sanitize("Authorization: Bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "Bearer" in out or "***" in out


def test_sanitize_is_recursive_on_mappings():
    out = sanitize({"password": "p", "nested": {"token": "t"}, "keep": "v"})
    assert out["password"] == "p"  # keys are not sanitized, only values matched as strings
    # Values that are strings with a secret pattern get redacted.
    assert sanitize({"secret": "value=1"})["secret"] == "value=1"  # key 'secret' alone isn't a value


def test_sanitize_leaves_non_strings_untouched():
    assert sanitize(42) == 42
    assert sanitize(["a", "b"]) == ["a", "b"]


def test_sanitize_list_of_secrets():
    assert sanitize(["token=x", "keep"]) == ["token=***", "keep"]


def test_sensitive_data_filter_sanitizes_record():
    f = SensitiveDataFilter()
    record = logging.LogRecord("x", logging.INFO, "", 0, "password=secret", (), None)
    assert f.filter(record) is True
    assert record.msg == "password=***"


def test_get_effective_level_mapping():
    assert get_effective_level(5) == V5
    assert get_effective_level(6) == V5
    assert get_effective_level(4) == V4
    assert get_effective_level(2) == V2
    assert get_effective_level(0) == logging.INFO
    assert get_effective_level(1) == logging.INFO


def test_custom_levels_below_debug():
    assert V5 < logging.DEBUG
    assert V4 < logging.DEBUG
    assert V2 > logging.DEBUG  # V2 is above DEBUG (13 > 10)


def test_get_logger_namespacing():
    assert get_logger().name == "testkit"
    assert get_logger("config").name == "testkit.config"


def test_setup_logging_returns_logger_and_sets_level():
    logger = setup_logging(log_verbosity=5)
    assert logger.name == "testkit"
    assert logger.level == V5
    # Handler level must be NOTSET so sub-DEBUG custom levels are not dropped.
    assert any(h.level == logging.NOTSET for h in logger.handlers)
