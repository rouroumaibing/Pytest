# Case Dependencies

Skip a test case automatically when the case it depends on did not pass. The
`testkit_depends_on` marker is registered by the testkit plugin - no conftest
needed - and a dependency that failed, skipped, or has not run yet causes
a skip, never a failure of the dependent case. This keeps related cases off a
broken scene so the fault site stays intact for debugging.

```python
import pytest


@pytest.mark.testkit_depends_on("test_create_cluster")
def test_scale_cluster(): ...
```

## Rules

| Dependency outcome | Effect on the dependent test |
|--------------------|------------------------------|
| `passed` | Runs normally |
| `failed` | Skipped, reason names the dependency |
| `skipped` | Skipped (cascades down the chain) |
| no result yet | Skipped (conservative) |

- Dependencies are evaluated at *setup* of the dependent test, so the
  dependency must be collected before the dependent test - define it earlier
  in the file, or in an earlier module.
- A test skipped by this marker is itself recorded as skipped, so a chain of
  the cascade propagates through the whole dependency chain.
- The original failure signal stays on the original case only; the mechanism
  never turns a dependency problem into a failure of the dependent test.
- Each name is either a full nodeid (`test_mod.py::test_create_cluster`) or a
  bare test function name.
- Prefer bare test names: a full nodeid must match exactly, and nodeids depend
  on the inner run's rootdir (projects setting an in-project `--basetemp`, or
  otherwise relocating the rootdir, change the prefix). Bare names match by
  nodeid suffix and stay robust; use full nodeids only to disambiguate
  parametrized cases.
- **Dependency-name resolution is authoritative against the collected tests.**
  A name that matches no collected test - a typo, or a bare name pointing at a
  parametrized variant (whose nodeid carries a `[...]` suffix and so does not
  end with the bare name) - makes the dependent skip silently. The plugin
  emits a warning in single-process runs so the broken name is visible; use a
  full nodeid (`test_mod.py::test_create_cluster[param]`) to target a variant.

## Running under pytest-xdist

The outcome registry is per-process. Under `pytest-xdist`, dependent tests
**must** share an xdist group and be collected after the dependency:

```python
@pytest.mark.xdist_group("cluster")
@pytest.mark.testkit_depends_on("test_create_cluster")
def test_scale_cluster(): ...
```

```bash
pytest --dist loadgroup -n 4
```

Without a matching group the two may run on different workers, the
dependency's outcome is invisible to the dependent, and the dependent is
skipped silently (a cross-worker warning is emitted to surface the
misconfiguration). On resource-constrained environments, run serially
(`-n 1`) or group cases sharing scarce resources with `--dist loadgroup`,
allocating the resources themselves through the Resource Pool.
