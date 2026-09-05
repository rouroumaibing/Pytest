# Fixture Guard

Coordinate creation of a single shared fixture across processes. The first
caller becomes the *creator*; everyone else waits and reuses. A stale
`creating` state past the timeout is treated as a crash and taken over.

```python
from testkit import ConcurrentFixtureGuard

guard = ConcurrentFixtureGuard("/tmp/shared-state.json", timeout=300)

if guard.should_create():
    try:
        # this process builds the expensive shared resource
        build_shared_resource()
        guard.mark_created()
    except Exception:
        guard.mark_failed()
        raise
else:
    # wait for the creator to finish
    ready = guard.wait_until_created(check_fn=lambda: resource_is_available(), timeout=300)
    assert ready

# ... every worker now uses the shared resource ...
guard.cleanup()  # creator only; removes the state file
```

## The auto-registered fixture

```python
def test_shared(testkit_fixture_guard):
    guard = testkit_fixture_guard("clusters", timeout=300)
    if guard.should_create():
        build_shared_resource()
        guard.mark_created()
```

## Methods

| Method | Purpose |
|--------|---------|
| `should_create()` | Claim creation; first caller wins |
| `mark_created()` | Mark the fixture ready (creator only) |
| `mark_failed()` | Give up; lets a waiting peer take over |
| `wait_until_created(check_fn, timeout)` | Poll a readiness predicate |
| `is_creator()` | Whether this process created it |
| `cleanup()` | Remove the state file (creator only) |
