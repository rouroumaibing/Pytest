"""Pluggable authentication strategies for the HTTP client.

All strategies implement a unified interface so the client never knows which
mechanism is in use:

* :meth:`AuthStrategy.get_headers` — headers for the current request, fetched
  fresh every call (no cached credential copies).
* :meth:`AuthStrategy.is_expired` — proactive expiry check (with buffer).
* :meth:`AuthStrategy.refresh` — refresh credentials.
* :meth:`AuthStrategy.should_refresh_on_401` — passive refresh gate.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

from testkit.exceptions import HTTPError
from testkit.logging_setup import get_logger

logger = get_logger("http.auth")


class AuthStrategy(ABC):
    """Unified authentication strategy interface."""

    @abstractmethod
    def get_headers(self) -> dict[str, str]:
        """Return headers for the current request, fetched fresh every call."""

    def is_expired(self) -> bool:
        """Return ``True`` if credentials need proactive refresh."""
        return False

    def refresh(self) -> None:
        """Refresh credentials (proactive or passive).

        Default is a no-op; token-based strategies override it.
        """
        return None

    def should_refresh_on_401(self, response: Any) -> bool:
        """Whether a 401 response should trigger a refresh + retry."""
        return True


class TokenAuth(AuthStrategy):
    """Bearer-token authentication with proactive + passive refresh.

    Parameters
    ----------
    access_token:
        Initial access token.
    refresh_token:
        Optional refresh token.
    expires_at:
        Epoch seconds at which the access token expires (optional).
    buffer_time:
        Seconds of safety margin for the proactive expiry check.
    token_provider:
        Callable returning a mapping with new token fields
        (``access_token`` / ``refresh_token`` / ``expires_at``). Required for
        automatic refresh.
    """

    def __init__(
        self,
        access_token: str,
        refresh_token: str | None = None,
        expires_at: float | None = None,
        buffer_time: float = 60.0,
        token_provider: Callable[[], Mapping[str, Any]] | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._expires_at = expires_at
        self._buffer_time = buffer_time
        self._token_provider = token_provider

    def get_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._access_token}"}

    @property
    def expires_at(self) -> float | None:
        return self._expires_at

    def is_expired(self) -> bool:
        if self._expires_at is None:
            return False
        return time.time() + self._buffer_time >= self._expires_at

    def refresh(self) -> None:
        if self._token_provider is None:
            raise HTTPError(
                "token expired and no token_provider configured",
                status_code=401,
            )
        new = self._token_provider()
        if "access_token" not in new:
            raise HTTPError(
                "token_provider returned no access_token",
                status_code=401,
            )
        self._access_token = new["access_token"]
        if "refresh_token" in new:
            self._refresh_token = new["refresh_token"]
        if "expires_at" in new:
            self._expires_at = new["expires_at"]
        logger.v2("token refreshed proactively")


class CookieAuth(AuthStrategy):
    """Cookie-based authentication.

    Parameters
    ----------
    cookies:
        Cookie mapping (``name -> value``) or a raw ``Cookie`` header string.
    """

    def __init__(self, cookies: Mapping[str, str] | str) -> None:
        self._cookies = cookies

    def get_headers(self) -> dict[str, str]:
        if isinstance(self._cookies, str):
            return {"Cookie": self._cookies}
        return {"Cookie": "; ".join(f"{k}={v}" for k, v in self._cookies.items())}


class ApiKeyAuth(AuthStrategy):
    """API-key authentication via a configurable header.

    Parameters
    ----------
    api_key:
        The API key value.
    header_name:
        Header used to carry the key (default ``X-API-Key``).
    """

    def __init__(self, api_key: str, header_name: str = "X-API-Key") -> None:
        self._api_key = api_key
        self._header_name = header_name

    def get_headers(self) -> dict[str, str]:
        return {self._header_name: self._api_key}


class CustomAuth(AuthStrategy):
    """Arbitrary authentication via a user-supplied header provider.

    Parameters
    ----------
    provider:
        Callable returning the header mapping for the current request.
    """

    def __init__(self, provider: Callable[[], dict[str, str]]) -> None:
        self._provider = provider

    def get_headers(self) -> dict[str, str]:
        return self._provider()
