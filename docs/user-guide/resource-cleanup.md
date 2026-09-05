# Resource Cleanup

Collect teardown callbacks and run them **LIFO** at the end of a test, with
retries and failure isolation (one failure never blocks the rest).

```python
from testkit import ResourceCleanup

cleanup = ResourceCleanup(retry_count=3, retry_interval=1.0)

cleanup.register(lambda: delete_cluster("c-1"), name="cluster-c-1")
cleanup.register(lambda: delete_network("n-1"), name="network-n-1")

results = cleanup.cleanup()  # LIFO: network first, then cluster
for r in results:
    assert r.success, r.error
```

## Async deletion

Pass a `wait_fn` predicate to poll until a background deletion finishes:

```python
cleanup.register(
    lambda: api.delete_cluster("c-1"),
    wait_fn=lambda: api.get_cluster("c-1") is None,
    timeout=300,
)
```

## Skip on failure

```python
cleanup = ResourceCleanup(skip_cleanup_on_failure=True)
cleanup.cleanup(failed=True)  # no teardown runs; state preserved for debugging
```

## The auto-registered fixture

`testkit_cleanup` detects test failure automatically, so
`--testkit-skip-cleanup-on-failure` works with zero wiring:

```python
def test_something(testkit_cleanup):
    testkit_cleanup.register(lambda: print("cleaned up"))
    assert True
```
