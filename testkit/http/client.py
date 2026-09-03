"""REST API HTTP client with pluggable auth, dual-layer token refresh and
per-request header overrides.

Key behaviours:

* Credentials are re-fetched from the :class:`AuthStrategy` on every request
  (no cached copy from construction time).
* Proactive refresh: expiry is checked before sending, with a configurable
  buffer; passive refresh on ``401`` acts as a fallback.
* ``extra_headers`` override/append per request without polluting the
  underlying :class:`requests.Session`.
* A unified exception hierarchy distinguishes 404 / network / timeout /
  other status codes.
"""

from __future__ import annotations

from typing import Any, Optional

import requests

from testkit.exceptions import HTTPError, ResourceNotFoundError
from testkit.http.auth import AuthStrategy
from testkit.logging_setup import get_logger

logger = get_logger("http.client")


def _merge_headers(*header_maps: Optional[dict[str, str]]) -> dict[str, str]:
    """Merge header mappings case-insensitively; later maps override earlier."""
    merged: dict[str, str] = {}
    index: dict[str, str] = {}
    for mapping in header_maps:
        if not mapping:
            continue
        for key, value in mapping.items():
            lower = key.lower()
            if lower in index:
                del merged[index[lower]]
            merged[key] = value
            index[lower] = key
    return merged


class HTTPClient:
    """A thin, generic wrapper over :class:`requests.Session`.

    Parameters
    ----------
    base_url:
        Base URL for all requests (e.g. ``https://api.example.com/v1``).
    auth:
        Optional :class:`AuthStrategy`.
    session:
        Optional pre-configured :class:`requests.Session`.
    timeout:
        Default request timeout (seconds).
    verify:
        TLS certificate verification (default ``True``).
    retry_on_401:
        Whether to refresh credentials and retry once on ``401``.
    """

    def __init__(
        self,
        base_url: str,
        auth: Optional[AuthStrategy] = None,
        session: Optional[requests.Session] = None,
        timeout: float = 30.0,
        verify: bool = True,
        retry_on_401: bool = True,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.timeout = timeout
        self.verify = verify
        self._retry_on_401 = retry_on_401
        self._session = session or requests.Session()

    def _url(self, path: str, base_url_override: Optional[str] = None) -> str:
        base = (base_url_override or self.base_url).rstrip("/")
        return f"{base}/{path.lstrip('/')}"

    # -- core request ---------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json: Any = None,
        data: Any = None,
        headers: Optional[dict[str, str]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
        raise_for_status: bool = False,
        resource_type: Optional[str] = None,
        resource_id: Any = None,
        base_url_override: Optional[str] = None,
        **kwargs: Any,
    ) -> requests.Response:
        """Perform an HTTP request.

        Parameters
        ----------
        method:
            HTTP method (``GET``, ``POST``, ...).
        path:
            Path appended to the base URL.
        params:
            Query string parameters.
        json:
            JSON body (serialized automatically).
        data:
            Form-encoded body.
        headers:
            Base headers for this request.
        extra_headers:
            Per-request header override/append (does not pollute the session).
        timeout:
            Per-request timeout override.
        raise_for_status:
            When ``True``, non-2xx responses raise :class:`HTTPError` (404
            raises :class:`ResourceNotFoundError`).
        resource_type:
            Optional resource type used to enrich a 404 error.
        resource_id:
            Optional resource id used to enrich a 404 error.
        base_url_override:
            Override the base URL (e.g. to call an external service).

        Returns
        -------
        requests.Response
            The HTTP response (unless ``raise_for_status`` triggers).
        """
        url = self._url(path, base_url_override)

        # Real-time credential reading — never cache the auth headers.
        auth_headers = self.auth.get_headers() if self.auth else {}
        request_headers = _merge_headers(auth_headers, headers, extra_headers)

        # Proactive refresh: check expiry before sending.
        if self.auth is not None and self.auth.is_expired():
            logger.v2("credentials expired; refreshing before request")
            self.auth.refresh()
            request_headers = _merge_headers(self.auth.get_headers(), headers, extra_headers)

        send_kwargs: dict[str, Any] = dict(
            method=method.upper(),
            url=url,
            params=params,
            json=json,
            data=data,
            headers=request_headers,
            timeout=timeout or self.timeout,
            verify=self.verify,
        )
        send_kwargs.update(kwargs)

        response = self._send(send_kwargs, path)

        # Passive refresh on 401 as a fallback.
        if (
            response.status_code == 401
            and self._retry_on_401
            and self.auth is not None
            and self.auth.should_refresh_on_401(response)
        ):
            logger.v2("received 401; refreshing credentials and retrying once")
            self.auth.refresh()
            send_kwargs["headers"] = _merge_headers(
                self.auth.get_headers(), headers, extra_headers
            )
            response = self._send(send_kwargs, path)

        logger.v2("http %s %s -> %s", method.upper(), path, response.status_code)
        logger.v4("http response body: %s", response.text)

        if raise_for_status and response.status_code >= 400:
            self._raise_for_status(response, path, resource_type, resource_id)
        return response

    def _send(self, send_kwargs: dict[str, Any], path: str) -> requests.Response:
        try:
            return self._session.request(**send_kwargs)
        except requests.exceptions.Timeout as exc:
            raise HTTPError("request timed out", path=path, original_exception=exc) from exc
        except requests.exceptions.ConnectionError as exc:
            raise HTTPError("network error", path=path, original_exception=exc) from exc
        except requests.exceptions.RequestException as exc:
            raise HTTPError("request failed", path=path, original_exception=exc) from exc

    @staticmethod
    def _raise_for_status(
        response: requests.Response,
        path: str,
        resource_type: Optional[str],
        resource_id: Any,
    ) -> None:
        status = response.status_code
        response_data = None
        try:
            response_data = response.json()
        except ValueError:
            response_data = response.text

        if status == 404:
            raise ResourceNotFoundError(
                resource_type=resource_type or "resource",
                resource_id=resource_id if resource_id is not None else path,
                status_code=404,
                response_data=response_data,
            )
        raise HTTPError(
            f"HTTP {status}",
            status_code=status,
            response_data=response_data,
            path=path,
        )

    # -- convenience verbs ----------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        return self.request("DELETE", path, **kwargs)

    # -- multipart upload -----------------------------------------------------

    def _request_with_files(
        self,
        method: str,
        path: str,
        file: Any = None,
        data: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        base_url_override: Optional[str] = None,
    ) -> requests.Response:
        """Perform a ``multipart/form-data`` upload request.

        The ``Content-Type`` header is automatically stripped so that
        ``requests`` sets the multipart boundary itself.

        Parameters
        ----------
        method:
            HTTP method (typically ``POST`` or ``PUT``).
        path:
            Path appended to the base URL.
        file:
            Either a file path (str), a ``(filename, fileobj[, content_type])``
            tuple, or a ``requests`` ``files``-style mapping.
        data:
            Optional form fields sent alongside the file.
        extra_headers:
            Per-request header override/append.
        base_url_override:
            Override the base URL (e.g. to call an external service).

        Returns
        -------
        requests.Response
        """
        files: Any
        if isinstance(file, str):
            files = {"file": (file.rsplit("/", 1)[-1], open(file, "rb"))}
        elif isinstance(file, tuple) and file and isinstance(file[0], str):
            files = {"file": file}
        elif isinstance(file, dict):
            files = file
        else:
            files = file

        # Strip Content-Type (case-insensitively) so requests can set the
        # multipart boundary on its own.
        headers = _merge_headers(extra_headers)
        headers = {k: v for k, v in headers.items() if k.lower() != "content-type"}

        try:
            return self.request(
                method,
                path,
                files=files,
                data=data,
                extra_headers=headers,
                base_url_override=base_url_override,
            )
        finally:
            # Close any file object we opened.
            if isinstance(files, dict):
                for value in files.values():
                    if isinstance(value, tuple) and len(value) >= 2 and hasattr(value[1], "close"):
                        value[1].close()
