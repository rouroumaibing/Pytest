# Quickstart

Install, then write a working test in about 30 lines.

## 1. Install

```bash
pip install testkit
```

The pytest plugin is auto-registered, so `testkit_*` fixtures and
`--testkit-*` options are available immediately.

## 2. Hello world (REST + SSH)

```python
# test_hello.py
from testkit import HTTPClient, TokenAuth, SSHExecutor, ResourcePool


def test_hello(testkit_cleanup):
    # 1. REST management plane
    client = HTTPClient("https://api.example.com/v1", auth=TokenAuth("token"))
    r = client.get("/health", raise_for_status=True)
    assert r.status_code == 200

    # 2. SSH node plane
    with SSHExecutor("10.0.0.5", username="root", password="secret") as ssh:
        result = ssh.execute("uname -m")
        assert result.ok, result.stderr
        arch = result.stdout.strip()

    # 3. Resource pool shared across workers
    pool = ResourcePool("/tmp/resources.yaml")
    pool.add({"id": "r1"})
    allocated = pool.acquire(count=1)
    assert len(allocated) == 1

    # 4. Teardown — runs automatically (LIFO) at test end
    testkit_cleanup.register(lambda: pool.release(["r1"]))
```

## 3. Run it

```bash
pytest test_hello.py -v
pytest -n 4                                  # parallel (pytest-xdist)
pytest --testkit-verbosity 2                 # enable framework logging
```

## 4. What's included

- **No business concepts** — bind your own domain via `BaseModel`/`Builder`.
- **Auto failure detection** — `testkit_cleanup` knows if the test failed,
  enabling `--testkit-skip-cleanup-on-failure` with zero wiring.
- **Config from YAML** — see the [Config Registry](user-guide/config-registry.md)
  guide.
