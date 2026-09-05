# testkit

A generic, **business-agnostic** Python test framework for systems composed of a **REST API management plane** and an **SSH node plane**. Every module is independently usable — there are no preset business concepts (no "cluster", "node", or "organization") anywhere in the package.

[中文文档](README.zh-CN.md) · [设计文档](docs/DESIGN.md)

## Features

| Module | What it does |
|--------|--------------|
| `ResourcePool` | Cross-process-safe resource pool (YAML persistence + `FileLock`, retries, batch allocation) |
| `SSHExecutor` | SSH command execution — direct or jump-host (`direct-tcpip` tunnel), architecture detection |
| `HTTPClient` + `AuthStrategy` | REST client with pluggable auth, dual-layer token refresh, multipart upload |
| `ConcurrentFixtureGuard` | Cross-process coordination so only one worker builds a shared fixture |
| `ConfigRegistry` | YAML multi-env merge + `${VAR}` substitution + Pydantic validation |
| `ResourceCleanup` | LIFO teardown with retries and failure isolation |
| `WaitHelper` | Generic polling with exception tolerance and in-flight token refresh |
| `Pipeline` | Serial, resumable multi-stage orchestration |
| Exceptions / Logging | Unified exception hierarchy + k8s-style `--v=N` verbosity |

## Installation

Requires Python ≥ 3.10. To use `testkit` as an importable library from anywhere:

```bash
pip install -e .
```

> The editable install also registers the `pytest11` entry point, so any
> project that installs `testkit` automatically gains its fixtures and CLI
> options (no `conftest.py` needed). See the plugin tests below.

## Quick start

```python
from testkit import (
    HTTPClient,
    TokenAuth,
    SSHExecutor,
    ResourcePool,
    WaitHelper,
)

# 1. HTTP client with token auth (proactive + passive refresh)
client = HTTPClient("https://api.example.com/v1", auth=TokenAuth("your-token"))

# 2. SSH executor (direct connection)
with SSHExecutor("10.0.0.5", username="root", password="***") as ssh:
    result = ssh.execute("uname -m")
    print(result.stdout, result.exit_code)

# 3. Resource pool shared across pytest-xdist workers
pool = ResourcePool("/tmp/resources.yaml")
pool.add({"id": "r1"})
allocated = pool.acquire(count=1)

# 4. Poll until a condition is met
WaitHelper(timeout=60).until(lambda: client.get("/health").status_code == 200)
```

## Directory layout

```
testkit/
├── exceptions.py        # unified exception hierarchy
├── logging_setup.py     # --v=N verbosity + sensitive-data sanitization
├── config/              # ConfigRegistry (YAML + env + Pydantic)
├── pool/                # ResourcePool
├── ssh/                 # SSHExecutor / SSHResult
├── http/                # HTTPClient + auth strategies
├── fixture/             # ConcurrentFixtureGuard
├── cleanup/             # ResourceCleanup
├── utils/               # WaitHelper
├── model/               # Builder + BaseModel
└── pipeline/            # Pipeline / StageResult
```

## Running tests

```bash
pip install -e ".[dev]"   # runtime + dev deps, and the plugin entry point
pytest                    # full suite: library + examples + plugin (171 tests)
pytest -n auto            # concurrent (pytest-xdist)
```

### Coverage gate (85%)

The pytest plugin auto-registers through the `pytest11` entry point, which
imports `testkit` at startup — *before* coverage starts — so the plugin's
import-time code would otherwise show as uncovered and drag coverage down.
Measure library coverage honestly by disabling the plugin for the coverage run,
then test the plugin separately:

```bash
pytest -n auto tests/ --cov=testkit --cov-fail-under=85 -p no:testkit --ignore=tests/test_plugin.py
pytest tests/test_plugin.py    # plugin auto-registration, exercised via pytester
```

### Lint & type check

```bash
ruff check .
ruff format --check .
mypy testkit
```

## Cleaning up

Every run produces renewable temp artifacts (`__pycache__/`, `.pytest_cache/`, `.pytest_tmp/`, `*.egg-info/`, `.coverage`). They are already git-ignored, but you can wipe them from disk with one command:

```bash
./scripts/clean.sh            # remove all temp artifacts
./scripts/clean.sh --dry      # list what would be removed, without deleting
```

The script only matches explicit temp patterns and **explicitly skips `.git/` and `.workbuddy/`** — it never touches source or project data. Everything removed is regenerated on the next `pytest` / `pip install -e .` run.

## License

MIT
