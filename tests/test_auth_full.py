"""Unit tests for the authentication strategies in ``testkit.http.auth``."""

from __future__ import annotations

import time
from unittest import mock

import pytest
from testkit import ApiKeyAuth, AuthStrategy, CookieAuth, CustomAuth, TokenAuth
from testkit.exceptions import HTTPError


def test_auth_strategy_is_abstract():
    with pytest.raises(TypeError):
        AuthStrategy()  # get_headers is abstract


def test_auth_strategy_defaults():
    class _Concrete(AuthStrategy):
        def get_headers(self):
            return {"X": "1"}

    auth = _Concrete()
    assert auth.is_expired() is False
    assert auth.refresh() is None
    assert auth.should_refresh_on_401(object()) is True


# -- TokenAuth ---------------------------------------------------------------


def test_token_auth_with_explicit_token():
    auth = TokenAuth(access_token="abc123")
    assert auth.get_headers() == {"Authorization": "Bearer abc123"}


def test_token_auth_none_triggers_provider_lazily():
    provider = mock.MagicMock(return_value={"access_token": "tok"})
    auth = TokenAuth(token_provider=provider)
    # No network / provider call at construction time.
    assert provider.call_count == 0
    assert auth.get_headers() == {"Authorization": "Bearer tok"}
    # Fetched exactly once; subsequent calls are cached.
    assert auth.get_headers() == {"Authorization": "Bearer tok"}
    assert provider.call_count == 1


def test_token_auth_refresh_via_provider():
    provider = mock.MagicMock(
        return_value={
            "access_token": "new",
            "refresh_token": "r",
            "expires_at": time.time() + 1000,
        }
    )
    auth = TokenAuth(access_token="old", token_provider=provider)
    auth.refresh()
    assert auth.get_headers() == {"Authorization": "Bearer new"}
    assert provider.call_count == 1


def test_token_auth_refresh_without_provider_raises():
    auth = TokenAuth(access_token="old")
    with pytest.raises(HTTPError):
        auth.refresh()


def test_token_auth_none_without_provider_raises():
    auth = TokenAuth()
    with pytest.raises(HTTPError):
        auth.get_headers()


def test_token_auth_is_expired_none_never_expires():
    auth = TokenAuth(access_token="x")  # expires_at is None
    assert auth.is_expired() is False


def test_token_auth_is_expired_when_past():
    auth = TokenAuth(access_token="x", expires_at=time.time() - 100)
    assert auth.is_expired() is True


def test_token_auth_not_expired_when_far_future():
    auth = TokenAuth(access_token="x", expires_at=time.time() + 1_000_000)
    assert auth.is_expired() is False


def test_token_auth_should_refresh_on_401_default_true():
    auth = TokenAuth(access_token="x")
    assert auth.should_refresh_on_401(mock.MagicMock(status_code=401)) is True


def test_token_auth_expires_at_property():
    auth = TokenAuth(access_token="x", expires_at=123.0)
    assert auth.expires_at == 123.0


# -- ApiKeyAuth --------------------------------------------------------------


def test_api_key_auth_default_header():
    auth = ApiKeyAuth(api_key="secret")
    assert auth.get_headers() == {"X-API-Key": "secret"}


def test_api_key_auth_custom_header():
    auth = ApiKeyAuth(api_key="secret", header_name="Authorization")
    assert auth.get_headers() == {"Authorization": "secret"}


# -- CookieAuth --------------------------------------------------------------


def test_cookie_auth_from_mapping():
    auth = CookieAuth({"a": "1", "b": "2"})
    assert auth.get_headers() == {"Cookie": "a=1; b=2"}


def test_cookie_auth_from_string():
    auth = CookieAuth("session=xyz")
    assert auth.get_headers() == {"Cookie": "session=xyz"}


# -- CustomAuth --------------------------------------------------------------


def test_custom_auth_uses_provider():
    provider = mock.MagicMock(return_value={"X-Custom": "yes"})
    auth = CustomAuth(provider=provider)
    assert auth.get_headers() == {"X-Custom": "yes"}
    provider.assert_called_once()


# -- ported from test_http_auth.py / test_auth_lazy.py (unique behaviours) --


def test_token_auth_not_expired_without_expiry():
    auth = TokenAuth("abc")
    assert auth.is_expired() is False


def test_token_auth_proactive_expiry_with_buffer():
    far_future = time.time() + 1000
    near_future = time.time() + 10
    assert TokenAuth("t", expires_at=far_future, buffer_time=60).is_expired() is False
    assert TokenAuth("t", expires_at=near_future, buffer_time=60).is_expired() is True


def test_token_auth_refresh_requires_access_token():
    auth = TokenAuth("t", token_provider=lambda: {"refresh_token": "r"})
    with pytest.raises(HTTPError):
        auth.refresh()


def test_cookie_auth_from_raw_string():
    auth = CookieAuth("sid=abc; theme=dark")
    assert auth.get_headers() == {"Cookie": "sid=abc; theme=dark"}


def test_custom_auth_is_fresh_each_call():
    counter = {"n": 0}

    def _provider():
        counter["n"] += 1
        return {"X-N": str(counter["n"])}

    auth = CustomAuth(_provider)
    assert auth.get_headers() == {"X-N": "1"}
    assert auth.get_headers() == {"X-N": "2"}


def test_lazy_token_no_io_until_get_headers():
    provider = mock.MagicMock(return_value={"access_token": "t", "expires_at": 9999999999})
    auth = TokenAuth(access_token=None, token_provider=provider)
    provider.assert_not_called()  # no network IO at construction
    header = auth.get_headers()
    provider.assert_called_once()
    assert header == {"Authorization": "Bearer t"}
    # second call is cached, provider not invoked again
    auth.get_headers()
    provider.assert_called_once()
