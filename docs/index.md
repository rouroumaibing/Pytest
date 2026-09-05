# testkit

A **generic, business-agnostic** Python test framework for systems with a REST
API **management plane** and an SSH **node plane**. Every module is usable
standalone — there are no preset concepts like `cluster`, `node`, or
`organization` anywhere in the package.

```bash
pip install testkit
```

## Feature overview

| Module | Responsibility |
|--------|----------------|
| `HTTPClient` + `AuthStrategy` | REST client with pluggable auth, dual-layer token refresh, multipart upload |
| `SSHExecutor` | Command execution over direct or jump-host (`direct-tcpip`) connections |
| `ResourcePool` | Cross-process resource pool (YAML + `FileLock`, retries, batch allocation) |
| `ConcurrentFixtureGuard` | One worker builds a shared fixture, others wait and reuse |
| `ResourceCleanup` | LIFO teardown with retries and `skip_cleanup_on_failure` |
| `ConfigRegistry` | Multi-environment YAML merge + `${VAR}` placeholders + Pydantic |
| `WaitHelper` | Generic polling with exception tolerance and token refresh |
| `Pipeline` | Serial, resumable multi-stage orchestration |
| Logging | `--v=N`-style verbosity with sensitive-data sanitization |

## Batteries-included pytest integration

Installing `testkit` auto-registers a pytest plugin — no `conftest.py` needed:

```python
def test_uses_fixtures(testkit_cleanup, testkit_wait_helper):
    testkit_cleanup.register(lambda: print("cleaned up"))
    assert testkit_wait_helper.until(lambda: True)
```

```text
--testkit-verbosity 2
--testkit-env staging
--testkit-resume-from deploy
--testkit-skip-cleanup-on-failure
```

## Next steps

- [Quickstart](quickstart.md) — install → a working test in ~30 lines
- [User Guide](user-guide/http-client.md) — one page per core module
- [API Reference](api-reference.md) — auto-generated from docstrings
- [Cookbook](cookbook.md) — common patterns
