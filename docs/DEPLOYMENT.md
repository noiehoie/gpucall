# Deployment Manifests

`deploy/` contains operator-owned deployment manifests for environments that do not use Docker Compose.

- `deploy/systemd/`: single-host service unit and environment template.
- `deploy/helm/gpucall/`: Kubernetes chart for the gateway process.
- `deploy/postgres/`: Postgres schema seed for the `GPUCALL_DATABASE_URL` job/idempotency runtime backend.
- `deploy/prometheus/` and `deploy/grafana/`: observability files for `/metrics/prometheus`.

SQLite remains the default v2.0 state backend. Set `GPUCALL_DATABASE_URL=postgresql://...` to use Postgres for jobs and idempotency entries.
