# Contributing to testkit

Thanks for helping out. This project keeps a small, focused scope: a generic,
business-agnostic test framework. Please read this before opening a PR.

## Getting started

```bash
# create and activate a fresh venv, then:
pip install -e ".[dev]"
pre-commit install
```

## Development loop

```bash
pytest                 # run the test suite
ruff check .           # lint
ruff format --check .  # formatting
mypy testkit/          # strict type checking
```

## Quality gates (all must pass before merge)

1. `ruff check .` and `ruff format --check .` — zero issues
2. `mypy testkit/` — zero errors (strict mode)
3. `pytest -n auto tests/` — all tests pass on Python ≥ 3.10
4. `coverage run -m pytest && coverage report` — coverage ≥ 85%

> Measure coverage with `coverage run -m pytest`, not `pytest --cov`. This
> package publishes a `pytest11` entry point, so `testkit` is imported during
> pytest start-up — *before* `pytest-cov` begins tracing. The plugin's own
> import-time lines (imports, class bodies, `def` lines) would then be reported
> as uncovered, understating coverage by ~25 points. `coverage run` starts
> tracing first, so the numbers are correct.

## Conventions

- Python ≥ 3.10, typed with `from __future__ import annotations`.
- No business concepts (`cluster`, `node`, `organization`) in the package —
  keep every module usable standalone.
- New public symbols must be exported from `testkit/__init__.py` and covered
  by a unit test.
- Update `CHANGELOG.md` under `[Unreleased]` for user-visible changes.

## Releases

Releases are cut by tagging `v*`. The publish workflow builds `sdist` + `wheel`,
runs `twine check`, and uploads via Trusted Publisher (OIDC) — no static tokens.
