# Deployment Manifests

`deploy/` contains operator-owned deployment manifests for environments that do not use Docker Compose.
The repository Docker Compose profile starts a dedicated Postgres service and
uses it for gateway jobs and idempotency by default. SQLite is a dev/test
fallback for runs where `GPUCALL_DATABASE_URL` is unset.

- `deploy/systemd/`: single-host service unit and environment template.
- `deploy/helm/gpucall/`: Kubernetes chart for the gateway process.
- `deploy/postgres/`: Postgres schema seed for the `GPUCALL_DATABASE_URL` job/idempotency runtime backend.
- `deploy/prometheus/` and `deploy/grafana/`: observability files for `/metrics/prometheus`.
