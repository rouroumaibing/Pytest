# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] - 2026-09-04

### Added

- Generic, business-agnostic test framework for systems with a REST API
  management plane and an SSH node plane.
- `HTTPClient` + pluggable `AuthStrategy` (Token / Cookie / ApiKey / Custom)
  with dual-layer token refresh and `_request_with_files` multipart upload.
- `SSHExecutor` with direct and jump-host (`direct-tcpip`) modes and
  `uname -m` architecture caching.
- `ResourcePool` with YAML persistence, `FileLock`, retries, batch allocation,
  and a strict `free` / `allocated` status model.
- `ConcurrentFixtureGuard` for cross-process shared-fixture coordination.
- `ResourceCleanup` with LIFO ordering, retries, and `skip_cleanup_on_failure`.
- `ConfigRegistry`, `WaitHelper`, `Pipeline`, and `BaseModel` / `Builder`.
- Unified exception hierarchy with `HttpTimeoutError`, `NetworkError`, and
  `ResourceNotFoundError` subclasses.
- `--v=N`-style verbosity logging with sensitive-data sanitization (headers and
  bare command-line secrets).
- Auto-registered pytest plugin: `--testkit-*` CLI options and `testkit_*`
  fixtures, with automatic test-failure detection.
- `py.typed` (PEP 561) and a single-source version in `testkit/_version.py`.
- MkDocs Material documentation site.
- CI (Python ≥ 3.10 × Linux / Windows / macOS) and Trusted Publisher (OIDC)
  release workflow.

## [Unreleased]

### Added

- `K8sClient`: thin direct-cluster client (kubeconfig auth) over the official
  `kubernetes` library, with CRUD helpers for Deployment / StatefulSet /
  DaemonSet / Pod / Service / Ingress / ConfigMap / Secret / Namespace and
  `exec_in_pod` (§19). `kubernetes` is a required dependency, imported lazily.
- `to_mib` quantity helper normalizing K8s quantity strings to MiB (§22).
- `SSHExecutor` enhancements: `scp_via_jump` (chunked SFTP), `scp_via_double_jump`
  (three-hop transfer), `find_package`, `file_exist`, `verify`, auto-reconnect
  with one retry, `keepalive` (default 60s), configurable `host_key_policy`
  (`reject` / `warn` / `auto`), and a `raw` client property (§20).
- `parallel_map` ordered parallel utility with `on_error="collect" | "raise"`
  and an `error_handler` hook (§21).
- Logging runtime controls: `RotatingFileHandler` support plus `set_level`,
  `set_verbosity`, idempotent `add_file_handler` / `remove_file_handler`, and
  `get_logging_info`; `paramiko` protocol logs stay at WARNING except at V5 (§23).
- `ConfigRegistry.generate_template` / `dump_template` producing a `dynamic.yaml`-style
  template from registered Pydantic models, filterable by used fixtures (§24).
- `ResourceCleanup.remove(name)` and `pending_count` for state queries (§25).
- `TokenAuth` now constructible with `access_token=None`; the token is fetched
  lazily on the first `get_headers()` call (§26).
- `WaitHelper.wait_until_deleted` treating `ResourceNotFoundError` / 404 as deleted (§27).
- `K8sClient` / `to_mib` are now documented: a `Kubernetes Client` row in both
  READMEs, a `docs/user-guide/k8s-client.md` page covering connection, the CRUD
  helpers, `exec_in_pod`, error context and quantity normalization, and a
  matching API Reference section (§19, §22).

### Fixed

- Framework exceptions mirror every `context` entry onto a direct attribute, so
  `err.status_code` / `err.resource_id` / `err.exit_code` read as expected
  alongside `err.context`. This also makes `WaitHelper.wait_until_deleted`
  honour the "equivalent 404 signal" case for framework errors (§27).
- `parallel_map` no longer drops successful `None` results — only failed or
  skipped slots are omitted (§21).
- `mypy` type-checking is now environment-independent: the `kubernetes` import
  is silenced by a per-module override instead of an inline
  `type: ignore[import-not-found]`, which became an `unused-ignore` error
  wherever `kubernetes` actually was installed (as in CI).
- `K8sClient`'s missing-dependency error no longer advertises a non-existent
  `testkit[k8s]` extra, and the `k8s` module docstrings now describe
  `kubernetes` the way the rest of the project does — a required dependency
  that is imported lazily (§19).

[0.1.0]: https://github.com/example/testkit/releases/tag/v0.1.0

