# Migrations

v2.0 uses Postgres for gateway jobs and idempotency in the repository Docker Compose profile.
The bundled Compose database is the production-like default. SQLite remains a
lightweight fallback for local development and tests when `GPUCALL_DATABASE_URL`
is unset.

- Jobs and idempotency: Postgres with `GPUCALL_DATABASE_URL`, seeded by `deploy/postgres/001_init.sql`.
- Local fallback jobs: SQLite WAL at `$XDG_STATE_HOME/gpucall/state.db`.
- Observed registry and circuit breakers: SQLite WAL at `$XDG_STATE_HOME/gpucall/registry.db`.
- Audit: JSONL hash chain at `$XDG_STATE_HOME/gpucall/audit/trail.jsonl`.

The registry loader migrates legacy `registry.jsonl` observations into `registry.db` when the SQLite database is empty.

For non-Compose deployments, set `GPUCALL_DATABASE_URL=postgresql://...` explicitly.
