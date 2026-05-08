# Migrations

v2.0 uses lightweight local state by default:

- Jobs: SQLite WAL at `$XDG_STATE_HOME/gpucall/state.db`.
- Observed registry and circuit breakers: SQLite WAL at `$XDG_STATE_HOME/gpucall/registry.db`.
- Audit: JSONL hash chain at `$XDG_STATE_HOME/gpucall/audit/trail.jsonl`.

The registry loader migrates legacy `registry.jsonl` observations into `registry.db` when the SQLite database is empty.

For multi-node or database-managed deployments, set `GPUCALL_DATABASE_URL=postgresql://...`.
The gateway then stores jobs and idempotency entries in Postgres using the schema in
`deploy/postgres/001_init.sql`.
