# gpucall External SaaS v1 Operations

gpucall v1 SaaS operation requires tenant-scoped authentication, deterministic cost caps, live provider validation, cleanup audits, and reproducible release artifacts.

## Tenant Contract

Tenant limits live in `$GPUCALL_CONFIG_DIR/tenants/*.yml`:

```yaml
name: default
requests_per_minute: 120
daily_budget_usd: 25.0
monthly_budget_usd: 500.0
max_request_estimated_cost_usd: 10.0
object_prefix: default
```

Tenant API keys do not belong in YAML. Issue and hand off caller-facing
gateway keys with the procedure in [GATEWAY_API_KEYS.md](GATEWAY_API_KEYS.md).
Configure tenant keys through credentials or the environment:

```yaml
providers:
  auth:
    tenant_keys: "tenant-a:key-a,tenant-b:key-b"
```

or:

```bash
GPUCALL_TENANT_API_KEYS='tenant-a:key-a,tenant-b:key-b'
```

Legacy `auth.api_keys` and `GPUCALL_API_KEYS` remain supported and map to the `default` tenant.

## Tenant Gates

Before provider execution, the gateway:

- compiles the deterministic plan
- reads `attestations.cost_estimate.budget_reservation_usd`
- checks `max_request_estimated_cost_usd`
- checks daily and monthly projected spend
- records accepted request-budget reservation in `$GPUCALL_STATE_DIR/tenant_usage.db`

`estimated_cost_usd` remains visible for audit and includes separately declared
fixed or warm-capacity cost. Tenant spend gates use `budget_reservation_usd` so
shared fixed capacity is not charged once per caller request.

Rejected requests return `402 TENANT_BUDGET_EXCEEDED` before provider execution.

Object uploads are tenant-prefixed as `gpucall/tenants/<tenant>/...` unless the tenant config overrides `object_prefix`.

## Admin CLI

```bash
gpucall admin status
gpucall admin tenant-list
gpucall admin tenant-create --name tenant-a --daily-budget-usd 25 --monthly-budget-usd 500
gpucall admin tenant-key-create --name tenant-a
gpucall admin tenant-key-list
gpucall admin automation-status
gpucall admin automation-configure --handoff-mode handoff_file
gpucall admin tenant-onboard \
  --name tenant-b \
  --gateway-url https://gpucall-gateway.example.internal \
  --recipe-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --format env \
  --output /secure/handoff/tenant-b.gpucall.env
gpucall admin tenant-onboard-batch \
  --manifest systems.yml \
  --gateway-url https://gpucall-gateway.example.internal \
  --recipe-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --format env \
  --output /secure/handoff
gpucall admin tenant-usage
```

`tenant-create` writes quota metadata only. `tenant-key-create` generates a
caller-facing gateway API key, stores it in credentials, and prints the secret
once for handoff. `tenant-onboard` creates tenant metadata when needed,
generates the key, and writes a `0600` handoff file for internal automation
only when `admin.yml` sets `api_key_handoff_mode: handoff_file`.
Use `automation-configure` rather than editing `admin.yml` directly; it keeps
the automation mode and bootstrap allowlist in the validated schema.
`tenant-onboard-batch` does the same for a manifest of systems and validates
the full batch before issuing keys, so parallel migrations cannot accidentally
share one tenant or overwrite a handoff file.
For small trusted internal networks, `api_key_handoff_mode: trusted_bootstrap`
allows callers inside configured CIDRs/hosts to request their own tenant key
from `POST /v2/bootstrap/tenant-key`; the gateway still enforces one tenant/key
per system name and refuses to reprint existing keys.
`tenant-key-list` prints fingerprints only.

## Release Gate

For every release candidate:

```bash
gpucall validate-config
gpucall security scan-secrets
gpucall cost-audit
gpucall cleanup-audit
gpucall launch-check --profile static
gpucall release-check --output-dir "$XDG_STATE_HOME/gpucall/release"
```

Production promotion additionally requires:

```bash
gpucall launch-check --profile production --config-dir config --url http://127.0.0.1:18088
gpucall audit verify
```

`release-check` writes:

- `openapi.json`
- `release-manifest.json`

The manifest records commit, config hash, policy version, providers, recipes, tenants, and static launch blockers.

## Operational Alerts

Alert on:

- launch-check `go:false`
- cleanup-audit `ok:false`
- cost-audit live provider access failure
- tenant budget rejection spike
- 5xx spike by route template
- provider fallback spike
- RunPod queue growth
- Modal billing anomaly
- Hyperstack VM count greater than zero outside an active lease

## v1 Support Boundary

SaaS v1 supports `infer` and `vision` over sync, async, and stream routes. Unsupported workloads fail closed with structured governance errors. Provider capacity failures are reported separately from gateway failures.
