# Changelog

## 2.0.9

- Improved `launch-check` output: added human-readable summary as default, `--json` for stdout compatibility, and `--output-json` for explicit report copies.
- Added Netcup / Bare Metal production operations documentation for Tailscale bind and explicit configuration directories.

## 2.0.0

- Added Data-plane-less task gateway for `infer` and `vision`.
- Added XDG config/state/cache layout.
- Added interactive `gpucall configure`.
- Added policy/recipe/provider YAML validation.
- Added Python and TypeScript SDKs.
- Added S3-compatible presigned PUT/GET object store support.
- Added SQLite WAL job persistence.
- Added immutable audit JSONL hash chain with redaction and file locking.
- Added Docker Compose deployment.
- Added smoke, doctor, audit, jobs, registry, security, and liveness seed CLI commands.
