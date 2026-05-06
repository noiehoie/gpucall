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

Tenant API keys do not belong in YAML. Configure them through credentials or the environment:

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
- reads `attestations.cost_estimate.estimated_cost_usd`
- checks `max_request_estimated_cost_usd`
- checks daily and monthly projected spend
- records accepted estimated spend in `$GPUCALL_STATE_DIR/tenant_usage.db`

Rejected requests return `402 TENANT_BUDGET_EXCEEDED` before provider execution.

Object uploads are tenant-prefixed as `gpucall/tenants/<tenant>/...` unless the tenant config overrides `object_prefix`.

## Admin CLI

```bash
gpucall admin status
gpucall admin tenant-list
gpucall admin tenant-create --name tenant-a --daily-budget-usd 25 --monthly-budget-usd 500
gpucall admin tenant-usage
```

`tenant-create` writes quota metadata only. Add the tenant key in credentials or an environment variable.

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
gpucall launch-check --profile production --url http://127.0.0.1:18088
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
