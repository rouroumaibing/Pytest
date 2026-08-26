"""Sanitizer 与 RetryPolicy 单元测试。"""

from __future__ import annotations

import pytest

from atf.exceptions import ATFError
from atf.utils.retry import RetryPolicy, RetryExhaustedError
from atf.utils.sanitizer import Sanitizer


class TestSanitizer:
    def test_kv_forms(self):
        san = Sanitizer()
        assert san.mask_text("password=Admin@123 ok") == "password=*** ok"
        assert "Admin@123" not in san.mask_text("curl -u user --form token=abc123 http://x")
        assert "s3cret" not in san.mask_text("secret: s3cret")

    def test_json_form(self):
        san = Sanitizer()
        out = san.mask_text('{"token": "abc123", "name": "ok"}')
        assert "abc123" not in out
        assert '"name": "ok"' in out

    def test_bearer_basic_token_masked(self):
        san = Sanitizer()
        out = san.mask_text("Authorization: Bearer eyJhbGciOi.abc.def")
        assert "eyJhbGciOi" not in out
        out2 = san.mask_text("Proxy-Authorization: Basic dXNlcjpwd2Q=")
        assert "dXNlcjpwd2Q" not in out2

    def test_private_key_block(self):
        san = Sanitizer()
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        assert "MIIE" not in san.mask_text(pem)

    def test_extra_keywords(self):
        san = Sanitizer(extra_keywords=["tenant_passwd"])
        out = san.mask_text("tenant_passwd=hunter2")
        assert "hunter2" not in out

    def test_case_insensitive_keys(self):
        san = Sanitizer()
        assert "v1" not in san.mask_text("Api_Key: v1")

    def test_mask_mapping(self):
        san = Sanitizer()
        out = san.mask_mapping({
            "user": "bob",
            "password": "p",
            "nested": {"api_key": "k", "keep": 1},
            "items": [{"token": "t"}],
        })
        assert out["user"] == "bob"
        assert out["password"] == "***"
        assert out["nested"]["api_key"] == "***"
        assert out["nested"]["keep"] == 1
        assert out["items"][0]["token"] == "***"

    def test_plain_text_untouched(self):
        san = Sanitizer()
        assert san.mask_text("hello world") == "hello world"


class TestRetryPolicy:
    def test_no_retry_when_success(self):
        calls = []

        def fn():
            calls.append(1)
            return "ok"

        assert RetryPolicy(retries=3, interval=0).execute(fn) == "ok"
        assert len(calls) == 1

    def test_retry_on_exception_then_success(self):
        calls = []

        def fn():
            calls.append(1)
            if len(calls) < 3:
                raise ValueError("boom")
            return 42

        policy = RetryPolicy(retries=3, interval=0.01)
        assert policy.execute(fn, description="flaky") == 42
        assert len(calls) == 3

    def test_raise_after_exhaustion(self):
        def fn():
            raise RuntimeError("always")

        policy = RetryPolicy(retries=2, interval=0.01)
        with pytest.raises(RuntimeError, match="always"):
            policy.execute(fn)

    def test_non_matching_exception_not_retried(self):
        calls = []

        def fn():
            calls.append(1)
            raise KeyError("fatal")

        policy = RetryPolicy(retries=5, interval=0.01, exceptions=(ValueError,))
        with pytest.raises(KeyError):
            policy.execute(fn)
        assert len(calls) == 1

    def test_result_predicate_retry(self):
        calls = []

        def fn():
            calls.append(1)
            return None if len(calls) < 2 else "resource"

        policy = RetryPolicy(retries=3, interval=0.01)
        assert policy.execute(fn, is_retryable=lambda r: r is None) == "resource"
        assert len(calls) == 2

    def test_result_predicate_exhausted(self):
        policy = RetryPolicy(retries=1, interval=0.01)
        with pytest.raises(RetryExhaustedError):
            policy.execute(lambda: None, is_retryable=lambda r: r is None)

    def test_backoff_and_cap(self):
        policy = RetryPolicy(retries=5, interval=1.0, backoff=2.0, max_interval=3.0)
        assert policy._delay(1) == 1.0
        assert policy._delay(2) == 2.0
        assert policy._delay(3) == 3.0  # capped
        assert policy._delay(9) == 3.0

    def test_on_retry_callback(self):
        events = []
        policy = RetryPolicy(retries=1, interval=0.01, on_retry=lambda a, d, r: events.append((a, r)))
        calls = []

        def fn():
            calls.append(1)
            if len(calls) == 1:
                raise ValueError("x")

        policy.execute(fn)
        assert events and events[0][0] == 1

    def test_exhausted_is_atf_error(self):
        assert issubclass(RetryExhaustedError, ATFError)
