"""BaseClient 单元测试:基于本地 http.server,无需真实被测系统。"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import requests

from atf.exceptions import ApiError
from atf.http import (
    ApiHTTPStatusError,
    ApiKeyAuth,
    ApiTimeoutError,
    ApiTransportError,
    BaseClient,
    CookieAuth,
    TokenAuth,
)
from atf.utils.retry import RetryPolicy


class _Handler(BaseHTTPRequestHandler):
    """可编程应答:/status<N> 返回 N;/echo 回显请求;/flaky 前两次 503。

    认证校验:要求 Authorization: Bearer good-token,否则 401。
    """

    def log_message(self, *args):  # 静默测试服务器日志
        pass

    def _authed(self) -> bool:
        return self.headers.get("Authorization") == "Bearer good-token"

    def _reply(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authed():
            self._reply(401, {"error": "unauthorized"})
            return
        if self.path.startswith("/status"):
            code = int(self.path.removeprefix("/status"))
            self._reply(code, {"status": code})
        elif self.path == "/echo":
            self._reply(200, {"headers": dict(self.headers), "params": self.path})
        elif self.path == "/flaky":
            count = self.server.flaky_hits = getattr(self.server, "flaky_hits", 0) + 1
            if count <= 2:
                self._reply(503, {"retry": True})
            else:
                self._reply(200, {"attempt": count})
        elif self.path == "/slow":
            import time as _t

            _t.sleep(2.0)
            self._reply(200, {})
        else:
            self._reply(404, {"error": "nf"})

    def do_POST(self):
        if not self._authed():
            self._reply(401, {"error": "unauthorized"})
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        self._reply(201, {"created": payload})


@pytest.fixture(scope="module")
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.fixture
def client(server):
    c = BaseClient(
        server,
        auth=TokenAuth("good-token"),
        extra_headers={"X-From": "atf-test"},
        retry=RetryPolicy(retries=3, interval=0.05, backoff=1.0),
        timeout=5.0,
    )
    yield c
    c.close()


class TestRequests:
    def test_get_json(self, client):
        resp = client.get("/echo")
        assert resp.status_code == 200
        assert resp.json()["headers"]["X-From"] == "atf-test"

    def test_post_body(self, client):
        resp = client.post("/echo", json_body={"name": "n1"})
        assert resp.status_code == 201

    def test_auth_header_applied(self, client):
        resp = client.get("/echo")
        assert resp.json()["headers"]["Authorization"] == "Bearer good-token"

    def test_per_request_headers_override(self, client):
        resp = client.get("/echo", headers={"X-From": "override"})
        assert resp.json()["headers"]["X-From"] == "override"

    def test_convenience_methods(self, client):
        # PUT/DELETE 走 BaseHTTPRequestHandler 默认 501,验证透传不炸
        resp = client.put("/echo", raise_for_status=False)
        assert resp.status_code in (404, 501)
        resp = client.delete("/echo", raise_for_status=False)
        assert resp.status_code in (404, 501)


class TestErrors:
    def test_status_error_raised(self, client):
        with pytest.raises(ApiHTTPStatusError) as ei:
            client.get("/status500")
        assert ei.value.response.status_code == 500

    def test_status_error_can_be_disabled(self, client):
        resp = client.get("/status404", raise_for_status=False)
        assert resp.status_code == 404

    def test_transport_error_wrapped(self):
        bad = BaseClient("http://127.0.0.1:1", retry=RetryPolicy(retries=1, interval=0.05))
        with pytest.raises(ApiTransportError):
            bad.get("/x")
        bad.close()

    def test_timeout_wrapped(self, server):
        slow = BaseClient(server, auth=TokenAuth("good-token"),
                          timeout=0.2, retry=RetryPolicy(retries=0))
        with pytest.raises(ApiTimeoutError):
            slow.get("/slow")
        slow.close()

    def test_exceptions_share_atf_base(self):
        assert issubclass(ApiHTTPStatusError, ApiError)
        assert issubclass(ApiTransportError, ApiError)
        assert issubclass(ApiTimeoutError, ApiError)


class TestRetry:
    def test_retry_on_503_then_success(self, client):
        resp = client.get("/flaky")
        assert resp.status_code == 200
        assert resp.json()["attempt"] == 3  # 2 次 503 均被重试

    def test_non_retryable_status_not_retried(self, client):
        resp = client.request("GET", "/status404", raise_for_status=False)
        assert resp.status_code == 404  # 直接返回,无重试开销


class TestAuthRefresh:
    def test_401_triggers_refresh_and_retry(self, server):
        tokens = ["stale-token"]
        auth = TokenAuth(
            tokens[0],
            refresh_fn=lambda: tokens.append("good-token") or tokens[-1],
        )
        with BaseClient(server, auth=auth) as client:
            resp = client.get("/echo")
            assert resp.status_code == 200
            assert auth.token == "good-token"

    def test_401_without_refresh_raises(self, server):
        with BaseClient(server, auth=TokenAuth("stale-token")) as client:
            with pytest.raises(ApiHTTPStatusError) as ei:
                client.get("/echo")
            assert ei.value.response.status_code == 401


class TestAuthStrategies:
    def test_apikey_auth(self, server):
        with BaseClient(server, auth=ApiKeyAuth("k")) as client:
            # 测试服务器只认 Bearer → 401;这里只验证 header 被注入到请求中
            resp = client.get("/echo", raise_for_status=False)
            assert resp.status_code == 401
            assert resp.headers.get("X-Api-Key") is None  # resp headers, not req
        # 改用无认证 client 验证 header 注入:
        # 直接检查 session 上 header 是否存在
        session = requests.Session()
        ApiKeyAuth("k").apply(session)
        assert session.headers["X-Api-Key"] == "k"

    def test_cookie_auth(self):
        session = requests.Session()
        CookieAuth({"sid": "abc"}).apply(session)
        assert session.cookies.get("sid") == "abc"

    def test_custom_auth(self):
        session = requests.Session()

        def hook(s):
            s.headers["X-Sign"] = "sig"

        from atf.http import CustomAuth

        CustomAuth(hook).apply(session)
        assert session.headers["X-Sign"] == "sig"


class TestSummaryLogging:
    def test_summary_line_captured_by_caplog(self, client, caplog):
        """BaseClient 的 _log_summary 走 atf.http logger,应能被 caplog 捕获。"""
        import logging

        with caplog.at_level(logging.INFO, logger="atf.http"):
            resp = client.get("/echo")
        assert resp.status_code == 200
        http_records = [r for r in caplog.records if r.name == "atf.http"]
        assert http_records, "atf.http 摘要日志未被 caplog 捕获"
        assert any(r.levelno == logging.INFO for r in http_records)
