# Deployment Manifests

`deploy/` contains operator-owned deployment manifests for environments that do not use Docker Compose.
The repository Docker Compose profile starts a dedicated Postgres service and
uses it for gateway jobs and idempotency by default. SQLite is a dev/test
fallback for runs where `GPUCALL_DATABASE_URL` is unset.

- `deploy/systemd/`: single-host service unit and environment template.
- `deploy/helm/gpucall/`: Kubernetes chart for the gateway process.
- `deploy/postgres/`: Postgres schema seed for the `GPUCALL_DATABASE_URL` job/idempotency runtime backend.
- `deploy/prometheus/` and `deploy/grafana/`: observability files for `/metrics/prometheus`.

## Netcup / Bare Metal Production Operations

For bare-metal or VPS deployments like Netcup, bind the gateway to the
Tailscale address or another private interface. Do not bind the gateway to
`0.0.0.0`.

```bash
TAILSCALE_IP=<your-tailscale-ip>
gpucall serve --config-dir /etc/gpucall --host "$TAILSCALE_IP" --port 18088
```

For Docker Compose deployments, publish the container port on the Tailscale
address instead of localhost or every interface. The production container should
also set `GPUCALL_GIT_COMMIT` so operators can verify the running revision
without relying on a git worktree inside the deployment directory.

```yaml
services:
  gpucall:
    ports:
      - "${TAILSCALE_IP}:18088:8080"
    environment:
      GPUCALL_GIT_COMMIT: "${GPUCALL_GIT_COMMIT}"
```

Run launch checks with an explicit config directory and the private gateway URL:

```bash
gpucall launch-check --profile static --config-dir /etc/gpucall
gpucall launch-check \
  --profile production \
  --config-dir /etc/gpucall \
  --url "http://${TAILSCALE_IP}:18088" \
  --output-json "$XDG_STATE_HOME/gpucall/launch/production-launch-check.json"
```

`launch-check` keeps stdout bounded by default and writes the full report to
`$XDG_STATE_HOME/gpucall/launch/launch-check.json`. Use `--json` only when an
automation path intentionally consumes the full report from stdout.
