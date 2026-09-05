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

[0.1.0]: https://github.com/example/testkit/releases/tag/v0.1.0
