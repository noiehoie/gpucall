# Migrations

v2.0 uses lightweight local state:

- Jobs: SQLite WAL at `$XDG_STATE_HOME/gpucall/state.db`.
- Observed registry and circuit breakers: SQLite WAL at `$XDG_STATE_HOME/gpucall/registry.db`.
- Audit: JSONL hash chain at `$XDG_STATE_HOME/gpucall/audit/trail.jsonl`.

The registry loader migrates legacy `registry.jsonl` observations into `registry.db` when the SQLite database is empty.

Postgres migrations are intentionally deferred to v2.1.
