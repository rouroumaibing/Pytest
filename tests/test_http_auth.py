"""Unit tests for pluggable HTTP authentication strategies."""

from __future__ import annotations

import time

import pytest

from testkit import (
    AuthStrategy,
    TokenAuth,
    CookieAuth,
    ApiKeyAuth,
    CustomAuth,
)
from testkit.exceptions import HTTPError


def test_token_auth_builds_bearer_header():
    auth = TokenAuth("abc123")
    assert auth.get_headers() == {"Authorization": "Bearer abc123"}


def test_token_auth_not_expired_without_expiry():
    auth = TokenAuth("abc")
    assert auth.is_expired() is False


def test_token_auth_proactive_expiry_with_buffer():
    far_future = time.time() + 1000
    near_future = time.time() + 10
    assert TokenAuth("t", expires_at=far_future, buffer_time=60).is_expired() is False
    assert TokenAuth("t", expires_at=near_future, buffer_time=60).is_expired() is True


def test_token_auth_refresh_uses_provider():
    provider = {"calls": 0}

    def _provider():
        provider["calls"] += 1
        return {"access_token": "new-token", "expires_at": time.time() + 1000}

    auth = TokenAuth("old", expires_at=time.time() + 1, buffer_time=60, token_provider=_provider)
    auth.refresh()
    assert auth.get_headers() == {"Authorization": "Bearer new-token"}
    assert provider["calls"] == 1


def test_token_auth_refresh_without_provider_raises():
    auth = TokenAuth("t", expires_at=time.time() + 1, buffer_time=60)
    with pytest.raises(HTTPError):
        auth.refresh()


def test_token_auth_refresh_requires_access_token():
    auth = TokenAuth("t", token_provider=lambda: {"refresh_token": "r"})
    with pytest.raises(HTTPError):
        auth.refresh()


def test_cookie_auth_from_mapping():
    auth = CookieAuth({"sid": "abc", "theme": "dark"})
    assert auth.get_headers() == {"Cookie": "sid=abc; theme=dark"}


def test_cookie_auth_from_raw_string():
    auth = CookieAuth("sid=abc; theme=dark")
    assert auth.get_headers() == {"Cookie": "sid=abc; theme=dark"}


def test_api_key_auth_default_header():
    auth = ApiKeyAuth("k-123")
    assert auth.get_headers() == {"X-API-Key": "k-123"}


def test_api_key_auth_custom_header():
    auth = ApiKeyAuth("k-123", header_name="Authorization")
    assert auth.get_headers() == {"Authorization": "k-123"}


def test_custom_auth_delegates_to_provider():
    auth = CustomAuth(lambda: {"X-Custom": "value"})
    assert auth.get_headers() == {"X-Custom": "value"}


def test_custom_auth_is_fresh_each_call():
    counter = {"n": 0}

    def _provider():
        counter["n"] += 1
        return {"X-N": str(counter["n"])}

    auth = CustomAuth(_provider)
    assert auth.get_headers() == {"X-N": "1"}
    assert auth.get_headers() == {"X-N": "2"}


def test_auth_strategy_is_abstract():
    with pytest.raises(TypeError):
        AuthStrategy()  # cannot instantiate abstract class
