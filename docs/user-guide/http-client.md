# HTTP Client

A thin wrapper over `requests.Session` with pluggable auth, dual-layer token
refresh, and per-request header overrides.

```python
from testkit import HTTPClient, TokenAuth

auth = TokenAuth("access-token", expires_at=1234567890, buffer_time=60)
client = HTTPClient("https://api.example.com/v1", auth=auth, timeout=30)

# raise_for_status + 404 -> ResourceNotFoundError
r = client.get("/clusters", raise_for_status=True)
client.post("/clusters", json={"name": "demo"})
client.put("/clusters/demo", json={"name": "renamed"})
client.delete("/clusters/demo")
```

## Token refresh

Refresh happens **proactively** (expiry + buffer) and **passively** (on 401):

```python
client.get("/secure")  # auto-refreshes if the token is about to expire
```

## Multipart upload

```python
client._request_with_files("POST", "/upload", file="/tmp/report.tar.gz")
client._request_with_files("POST", "/upload", file=("report.txt", open("report.txt", "rb")))
```

## Auth strategies

| Strategy | Purpose |
|----------|---------|
| `TokenAuth` | Bearer token with `expires_at` + `buffer_time` |
| `CookieAuth` | Cookie key/value pairs |
| `ApiKeyAuth` | Custom API-key header |
| `CustomAuth` | User-supplied `get_headers` / `refresh` callables |

## Exceptions

- `HttpTimeoutError` — request timed out
- `NetworkError` — transport-level failure (DNS, reset)
- `ResourceNotFoundError` — 404 (carries `resource_type` / `resource_id`)
- `HTTPError` — any other non-2xx status
