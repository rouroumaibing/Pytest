"""Unit tests for :class:`HTTPClient` (``requests`` mocked, no real HTTP)."""

from __future__ import annotations

from io import BytesIO
from unittest import mock

import pytest
import requests
from testkit import HTTPClient
from testkit.exceptions import (
    HTTPError,
    HttpTimeoutError,
    NetworkError,
    ResourceNotFoundError,
)
from testkit.http.auth import AuthStrategy
from testkit.http.client import _merge_headers


class FakeAuth(AuthStrategy):
    """Controllable :class:`AuthStrategy` for request tests."""

    def __init__(self, token="x", expired=False, refresh_on_401=True):
        self._token = token
        self._expired = expired
        self._refresh_on_401 = refresh_on_401
        self.header_calls = 0
        self.refresh_calls = 0
        self._headers = {"Authorization": f"Bearer {token}"}

    def get_headers(self):
        self.header_calls += 1
        return dict(self._headers)

    def is_expired(self):
        return self._expired

    def refresh(self):
        self.refresh_calls += 1
        self._headers = {"Authorization": "Bearer refreshed"}

    def should_refresh_on_401(self, response):
        return self._refresh_on_401


def _resp(status_code=200, text="", json_data=None):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.text = text
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("no json body")
    return resp


def _client(auth=None, session=None):
    if session is None:
        session = mock.MagicMock(spec=requests.Session)
    return HTTPClient("https://api.example.com", auth=auth, session=session)


# -- convenience verbs -------------------------------------------------------


def test_convenience_verbs_use_correct_method():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)

    client.get("/a")
    client.post("/b", json={"k": "v"})
    client.put("/c", json={"k": "v"})
    client.patch("/d", json={"k": "v"})
    client.delete("/e")

    methods = [c.kwargs["method"] for c in session.request.call_args_list]
    assert methods == ["GET", "POST", "PUT", "PATCH", "DELETE"]


# -- auth header re-fetching -------------------------------------------------


def test_auth_headers_re_fetched_every_request():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    auth = FakeAuth()
    client = _client(auth=auth, session=session)

    client.get("/a")
    client.get("/b")
    # get_headers called once per request (not expired, no refresh).
    assert auth.header_calls == 2
    session.request.assert_called()


def test_proactive_refresh_when_expired():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    auth = FakeAuth(expired=True)
    client = _client(auth=auth, session=session)

    client.get("/a")
    assert auth.is_expired() is True
    assert auth.refresh_calls == 1
    # get_headers called initially, then re-fetched after refresh.
    assert auth.header_calls == 2
    # The refreshed header is what was ultimately sent.
    sent_headers = session.request.call_args.kwargs["headers"]
    assert sent_headers["Authorization"] == "Bearer refreshed"


def test_passive_401_refresh_and_retry():
    session = mock.MagicMock(spec=requests.Session)
    session.request.side_effect = [_resp(status_code=401), _resp(status_code=200)]
    auth = FakeAuth(refresh_on_401=True)
    client = _client(auth=auth, session=session)

    resp = client.get("/a")
    assert resp.status_code == 200
    assert auth.refresh_calls == 1
    # Two sends: original 401 then the retried request.
    assert session.request.call_count == 2
    # The retry used the refreshed header.
    retry_headers = session.request.call_args_list[1].kwargs["headers"]
    assert retry_headers["Authorization"] == "Bearer refreshed"


def test_401_with_refresh_disabled_does_not_retry():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp(status_code=401)
    auth = FakeAuth(refresh_on_401=False)
    client = HTTPClient("https://api.example.com", auth=auth, session=session, retry_on_401=False)

    client.get("/a", raise_for_status=False)
    assert auth.refresh_calls == 0
    assert session.request.call_count == 1


# -- raise_for_status --------------------------------------------------------


def test_raise_for_status_404_raises_resource_not_found():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp(status_code=404, json_data={"detail": "nope"})
    client = _client(session=session)

    with pytest.raises(ResourceNotFoundError) as exc_info:
        client.get("/missing", raise_for_status=True, resource_type="widget", resource_id=42)
    assert exc_info.value.context["status_code"] == 404
    assert exc_info.value.context["response_data"] == {"detail": "nope"}
    assert exc_info.value.context["resource_type"] == "widget"
    assert exc_info.value.context["resource_id"] == 42


def test_raise_for_status_404_with_non_json_body():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp(status_code=404, text="plain text")
    client = _client(session=session)

    with pytest.raises(ResourceNotFoundError) as exc_info:
        client.get("/missing", raise_for_status=True)
    assert exc_info.value.context["response_data"] == "plain text"


def test_raise_for_status_other_4xx_raises_http_error():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp(status_code=500, json_data={"err": "boom"})
    client = _client(session=session)

    with pytest.raises(HTTPError) as exc_info:
        client.get("/x", raise_for_status=True)
    assert exc_info.value.context["status_code"] == 500
    assert exc_info.value.context["response_data"] == {"err": "boom"}


def test_no_raise_when_raise_for_status_false():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp(status_code=500)
    client = _client(session=session)
    resp = client.get("/x", raise_for_status=False)
    assert resp.status_code == 500  # no exception


# -- _send exception mapping -------------------------------------------------


def test_send_maps_timeout():
    session = mock.MagicMock(spec=requests.Session)
    session.request.side_effect = requests.exceptions.Timeout("timed out")
    client = _client(session=session)
    with pytest.raises(HttpTimeoutError):
        client.get("/a")


def test_send_maps_connection_error():
    session = mock.MagicMock(spec=requests.Session)
    session.request.side_effect = requests.exceptions.ConnectionError("refused")
    client = _client(session=session)
    with pytest.raises(NetworkError):
        client.get("/a")


def test_send_maps_generic_request_exception():
    session = mock.MagicMock(spec=requests.Session)
    session.request.side_effect = requests.exceptions.RequestException("boom")
    client = _client(session=session)
    with pytest.raises(HTTPError):
        client.get("/a")


# -- _merge_headers ----------------------------------------------------------


def test_merge_headers_case_insensitive_override():
    merged = _merge_headers(
        {"X-Foo": "1", "Content-Type": "a"},
        {"x-foo": "2", "content-type": "b"},
    )
    # Later map wins regardless of casing; original casing of the winner kept.
    # Later map wins; the earlier key's casing is dropped on case-insensitive
    # override (only one key survives per lower-cased name).
    assert merged == {"x-foo": "2", "content-type": "b"}


def test_merge_headers_skips_none():
    merged = _merge_headers(None, {"A": "1"}, None, {"A": "2"})
    assert merged == {"A": "2"}


# -- base_url_override / extra_headers ---------------------------------------


def test_base_url_override_changes_url():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)
    client.get("/path", base_url_override="https://other.example.com")
    called_url = session.request.call_args.kwargs["url"]
    assert called_url == "https://other.example.com/path"


def test_extra_headers_merged_without_polluting_session():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    auth = FakeAuth()
    client = _client(auth=auth, session=session)
    client.get("/a", extra_headers={"X-Custom": "yes"})

    sent_headers = session.request.call_args.kwargs["headers"]
    assert sent_headers["X-Custom"] == "yes"
    assert sent_headers["Authorization"] == "Bearer x"


def test_json_body_passed_through():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)
    client.post("/a", json={"k": "v"})
    assert session.request.call_args.kwargs["json"] == {"k": "v"}


# -- _request_with_files -----------------------------------------------------


def test_request_with_files_string_path_opens_and_closes(tmp_path):
    payload = tmp_path / "data.txt"
    payload.write_text("hello", encoding="utf-8")
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)

    client._request_with_files("POST", "/upload", file=str(payload))
    kwargs = session.request.call_args.kwargs
    assert kwargs["method"] == "POST"
    files = kwargs["files"]
    assert "file" in files
    # The opened file object was closed in the finally block.
    assert files["file"][1].closed is True


def test_request_with_files_tuple():
    content = BytesIO(b"csv,data")
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)

    client._request_with_files("POST", "/upload", file=("report.csv", content, "text/csv"))
    files = session.request.call_args.kwargs["files"]
    assert files == {"file": ("report.csv", content, "text/csv")}
    assert content.closed is True  # closed by finally


def test_request_with_files_dict():
    content = BytesIO(b"x")
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)

    client._request_with_files("POST", "/upload", file={"upload": ("f.txt", content)})
    files = session.request.call_args.kwargs["files"]
    assert files == {"upload": ("f.txt", content)}


def test_request_with_files_generic():
    generic = [("a", "b")]  # neither str/tuple/dict with str first -> passthrough
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)

    client._request_with_files("POST", "/upload", file=generic)
    assert session.request.call_args.kwargs["files"] is generic


def test_request_with_files_strips_content_type():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)

    client._request_with_files(
        "POST",
        "/upload",
        file=("f.bin", BytesIO(b"x")),
        extra_headers={"Content-Type": "application/json"},
    )
    sent_headers = session.request.call_args.kwargs["headers"]
    lowered = {k.lower() for k in sent_headers}
    assert "content-type" not in lowered  # stripped so requests sets boundary


# -- ported from test_http_client.py (unique behaviours) --


def test_url_construction():
    client = HTTPClient("https://api.example.com/v1")
    assert client._url("/clusters") == "https://api.example.com/v1/clusters"
    assert client._url("clusters") == "https://api.example.com/v1/clusters"


def test_request_with_files_uses_base_url_override():
    session = mock.MagicMock(spec=requests.Session)
    session.request.return_value = _resp()
    client = _client(session=session)
    client._request_with_files(
        "POST", "/upload", file=("x.txt", BytesIO(b"data")), base_url_override="http://ext"
    )
    url = session.request.call_args.kwargs["url"]
    assert url == "http://ext/upload"


def test_merge_headers_preserves_independent_keys():
    merged = _merge_headers({"A": "1", "B": "2"}, {"C": "3"})
    assert merged == {"A": "1", "B": "2", "C": "3"}
