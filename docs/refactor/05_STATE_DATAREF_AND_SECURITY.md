# State, DataRef, and Security Packet

Read this with `00_PRIME_DIRECTIVE.md` and
`01_README_V2_CLAIM_MATRIX.md` before editing persistence, idempotency, tenant
budget, artifact registry, admission leases, DataRef conversion, object store,
or worker fetch code.

## Persistence Boundary

Parallel SQLite/Postgres behavior exists in:

- `sqlite_store.py` / `postgres_store.py`
- `tenant.py`
- `artifacts.py`
- `admission.py`

Do not collapse duplication until behavior is locked by tests for:

- idempotency pending/completed reservation
- first-writer semantics
- release on failure
- tenant reservation atomicity
- artifact latest compare-and-set
- Postgres startup smoke
- admission lease and cooldown behavior

Postgres mode must keep jobs, idempotency, tenant ledger, artifact registry, and
admission state in Postgres. SQLite remains local dev/test fallback.

## DataRef Boundary

DataRef areas:

- gateway presign and tenant prefixing
- worker-readable request conversion
- worker fetch and validation
- object-store live configuration

Security facts to preserve:

- HTTP(S) worker refs require `gateway_presigned=true`.
- HTTP(S) worker refs require allowlisted host.
- URI userinfo is rejected.
- redirects are disabled.
- private, loopback, link-local, multicast, reserved, and unspecified resolved
  addresses are rejected.
- bytes must be non-negative.
- SHA-256 is verified for S3 and HTTP(S).
- ambient S3 credentials require explicit opt-in.
- DataRef bodies do not cross the gateway except through object-store presign
  workflow.

## Refactor Direction

- Introduce explicit persistence protocols only after tests exist.
- Collapse SQLite/Postgres duplication only where behavior is proven equivalent.
- Keep DataRef validation and SSRF hardening easy to audit.
- Keep object-store missing configuration as deterministic Production No-Go.
- Do not convert DataRef live readiness into a fake static success.

## Completion Evidence

A phase touching this area must report:

- persistence invariant preserved
- SQLite tests run
- Postgres tests or smoke status
- DataRef hardening tests run
- object-store live status as configured, missing, or skipped
- no secrets added or staged
