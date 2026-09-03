"""HTTP client package."""

from testkit.http.auth import (
    ApiKeyAuth,
    AuthStrategy,
    CookieAuth,
    CustomAuth,
    TokenAuth,
)
from testkit.http.client import HTTPClient

__all__ = [
    "HTTPClient",
    "AuthStrategy",
    "TokenAuth",
    "CookieAuth",
    "ApiKeyAuth",
    "CustomAuth",
]
