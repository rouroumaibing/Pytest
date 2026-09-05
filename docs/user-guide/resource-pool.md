# Resource Pool

A cross-process resource pool persisted to YAML with a `FileLock`, safe to
share across `pytest-xdist` workers. Status is strictly `free` / `allocated`.

```python
from testkit import ResourcePool

pool = ResourcePool("/tmp/resources.yaml")

# Seed resources (id + any extra fields you need)
pool.add({"id": "r1", "ip": "10.0.0.1"})
pool.add({"id": "r2", "ip": "10.0.0.2"})

# Batch-allocate free resources (retries + interval under the lock)
allocated = pool.acquire(count=1)
print(allocated)  # [{"id": "r1", "ip": "10.0.0.1", "status": "allocated"}]

# Return them to the free pool
pool.release(["r1"])
```

## Key behaviours

- **Pre-operation reload** — every operation re-reads the file from disk; no
  in-memory cache, so multiple processes never see stale state.
- **Strict status** — only `free` / `allocated` are valid; anything else raises
  `PoolError`.
- **Atomic writes** — state is written back on every mutation, guarded by
  `FileLock`.

## Integration with cleanup

```python
def test_needs_resource(testkit_cleanup):
    pool = ResourcePool("/tmp/resources.yaml")
    allocated = pool.acquire()
    testkit_cleanup.register(lambda: pool.release([r["id"] for r in allocated]))
```
