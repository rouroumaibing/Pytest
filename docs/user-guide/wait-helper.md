# WaitHelper

Poll a condition until satisfied or timed out, tolerating transient exceptions
and optionally refreshing credentials during long waits.

```python
from testkit import WaitHelper

# Wait for any truthy value
WaitHelper(timeout=60).until(lambda: api.get("/health").status_code == 200)

# Wait for a specific value
WaitHelper(timeout=120, interval=2).until(
    lambda: api.get("/clusters/c-1").json()["status"], expected="Available"
)

# Convenience alias
WaitHelper().until_true(lambda: check())
```

## Token refresh during the wait

```python
auth = TokenAuth("token", expires_at=time.time() + 600)
WaitHelper(refresh_fn=auth.refresh, refresh_interval=300).until(
    lambda: api.get("/jobs/1").json()["state"] == "done"
)
```

## Timeout

```python
from testkit import WaitTimeout

try:
    WaitHelper(timeout=5).until(lambda: False)
except WaitTimeout as err:
    print("last value:", err.last_value)
```
