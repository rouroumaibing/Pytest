"""Unit tests for the HTTP client (HTTPClient)."""

from __future__ import annotations

from unittest import mock

import pytest
import requests

from testkit import HTTPClient, TokenAuth
from testkit.exceptions import HTTPError, ResourceNotFoundError
from testkit.http.client import _merge_headers


# -- header merge ------------------------------------------------------------

def test_merge_headers_later_overrides_earlier():
    merged = _merge_headers({"Authorization": "Bearer old", "X-A": "1"}, {"authorization": "Bearer new"})
    assert merged["authorization"] == "Bearer new"
    assert "Authorization" not in merged  # old key removed (case-insensitive)


def test_merge_headers_preserves_independent_keys():
    merged = _merge_headers({"A": "1", "B": "2"}, {"C": "3"})
    assert merged == {"A": "1", "B": "2", "C": "3"}


def test_merge_headers_handles_none():
    assert _merge_headers(None, {"A": "1"}) == {"A": "1"}
    assert _merge_headers(None, None) == {}


# -- client ----------------------------------------------------------------

def _response(status=200, body=None, text="{}"):
    resp = mock.MagicMock(spec=requests.Response)
    resp.status_code = status
    resp.text = text
    resp.json.return_value = body if body is not None else {}
    return resp


def test_url_construction():
    client = HTTPClient("https://api.example.com/v1")
    assert client._url("/clusters") == "https://api.example.com/v1/clusters"
    assert client._url("clusters") == "https://api.example.com/v1/clusters"


def test_url_base_override():
    client = HTTPClient("https://api.example.com/v1")
    assert client._url("/health", base_url_override="https://ext.io") == "https://ext.io/health"


def test_request_fetches_credentials_fresh():
    counter = {"n": 0}

    class _Auth(TokenAuth):
        def get_headers(self):
            counter["n"] += 1
            return {"Authorization": f"Bearer tok-{counter['n']}"}

    client = HTTPClient("http://localhost", auth=_Auth("x"))
    client._session.request = mock.MagicMock(return_value=_response())
    client.get("/a")
    client.get("/b")
    # Credentials re-fetched on every request.
    assert counter["n"] == 2


def test_proactive_refresh_before_request():
    class _Auth(TokenAuth):
        def __init__(self):
            super().__init__("old")
            self._expired = True
            self.refreshed = False

        def is_expired(self):
            return self._expired

        def refresh(self):
            self.refreshed = True
            self._access_token = "new"

    auth = _Auth()
    client = HTTPClient("http://localhost", auth=auth)
    client._session.request = mock.MagicMock(return_value=_response())
    client.get("/a")
    assert auth.refreshed is True
    # Request used refreshed credentials.
    headers = client._session.request.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer new"


def test_passive_refresh_on_401():
    class _Auth(TokenAuth):
        def __init__(self):
            super().__init__("old")
            self.refreshed = False

        def refresh(self):
            self.refreshed = True
            self._access_token = "new"

    auth = _Auth()
    client = HTTPClient("http://localhost", auth=auth)
    responses = iter([_response(status=401), _response(status=200)])
    client._session.request = mock.MagicMock(side_effect=lambda **kw: next(responses))
    resp = client.get("/a")
    assert auth.refreshed is True
    assert resp.status_code == 200


def test_404_raises_resource_not_found():
    client = HTTPClient("http://localhost")
    client._session.request = mock.MagicMock(return_value=_response(status=404, body={"e": "missing"}))
    with pytest.raises(ResourceNotFoundError) as exc_info:
        client.get("/clusters/c-9", raise_for_status=True, resource_type="cluster", resource_id="c-9")
    assert exc_info.value.context["resource_type"] == "cluster"
    assert exc_info.value.context["resource_id"] == "c-9"


def test_other_status_raises_http_error():
    client = HTTPClient("http://localhost")
    client._session.request = mock.MagicMock(return_value=_response(status=500))
    with pytest.raises(HTTPError) as exc_info:
        client.get("/a", raise_for_status=True)
    assert exc_info.value.context["status_code"] == 500


def test_timeout_maps_to_http_error():
    client = HTTPClient("http://localhost")
    client._session.request = mock.MagicMock(side_effect=requests.exceptions.Timeout("slow"))
    with pytest.raises(HTTPError):
        client.get("/a")


def test_connection_error_maps_to_http_error():
    client = HTTPClient("http://localhost")
    client._session.request = mock.MagicMock(side_effect=requests.exceptions.ConnectionError("refused"))
    with pytest.raises(HTTPError):
        client.get("/a")


def test_request_with_files_strips_content_type():
    calls = {}

    def fake_request(**kw):
        calls.update(kw)
        return _response()

    client = HTTPClient("http://localhost", auth=TokenAuth("tok"))
    client._session.request = fake_request
    client._request_with_files(
        "POST",
        "/upload",
        file=("x.txt", b"data"),
        extra_headers={"Content-Type": "application/octet-stream", "X-Extra": "1"},
    )
    headers = {k.lower(): v for k, v in calls["headers"].items()}
    assert "content-type" not in headers  # stripped so requests sets boundary
    assert "x-extra" in headers
    assert "files" in calls
    assert "data" in calls


def test_request_with_files_uses_base_url_override():
    client = HTTPClient("http://localhost")
    client._session.request = mock.MagicMock(return_value=_response())
    client._request_with_files(
        "POST", "/upload", file=("x.txt", b"data"), base_url_override="http://ext"
    )
    url = client._session.request.call_args.kwargs["url"]
    assert url == "http://ext/upload"


def test_convenience_verbs_delegate():
    client = HTTPClient("http://localhost")
    client._session.request = mock.MagicMock(return_value=_response())
    client.get("/a")
    client.post("/a")
    client.put("/a")
    client.patch("/a")
    client.delete("/a")
    methods = [c.kwargs["method"] for c in client._session.request.call_args_list]
    assert methods == ["GET", "POST", "PUT", "PATCH", "DELETE"]
