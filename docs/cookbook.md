# Cookbook

Common patterns, copy-paste runnable.

## Poll until an async resource becomes ready

```python
from testkit import HTTPClient, TokenAuth, WaitHelper

client = HTTPClient("https://api.example.com/v1", auth=TokenAuth("token"))
client.post("/clusters", json={"name": "demo"})

WaitHelper(timeout=120, interval=2).until(
    lambda: client.get("/clusters/demo").json()["status"] == "Available"
)
```

## Keep credentials alive during a long wait

```python
auth = TokenAuth("token", expires_at=time.time() + 600)
client = HTTPClient("https://api.example.com/v1", auth=auth)

WaitHelper(refresh_fn=auth.refresh, refresh_interval=300).until(
    lambda: client.get("/jobs/1").json()["state"] == "done"
)
```

## Handle a 404 specifically

```python
from testkit import HTTPClient, ResourceNotFoundError

client = HTTPClient("https://api.example.com/v1")
try:
    client.get("/clusters/nope", raise_for_status=True, resource_type="cluster", resource_id="nope")
except ResourceNotFoundError as err:
    assert err.context["resource_id"] == "nope"
```

## Build a domain model from an API response

```python
from testkit import BaseModel, Builder


class Cluster(BaseModel):
    def __init__(self, id, status):
        self.id = id
        self.status = status


class ClusterBuilder(Builder):
    def build(self):
        return Cluster(self._fields["id"], self._fields["status"])


cluster = Cluster.builder().with_id("c-1").with_status("Active").build()
```

## Share a fixture across xdist workers

```python
def test_shared(testkit_fixture_guard):
    guard = testkit_fixture_guard("clusters", timeout=300)
    if guard.should_create():
        try:
            create_expensive_resource()
            guard.mark_created()
        except Exception:
            guard.mark_failed()
            raise
    # ... use the shared resource
```

## Preserve state on failure for debugging

```bash
pytest --testkit-skip-cleanup-on-failure
```

When a test fails, `testkit_cleanup` skips teardown so you can inspect the
leftover resources.
