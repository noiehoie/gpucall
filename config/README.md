# config/ — active gateway configuration

This directory is the four-catalog configuration the gateway routes from:

| Directory | Contents | Authored how |
| --- | --- | --- |
| `recipes/` | workload contracts (task, intent, context budget, output budget, modes, validation policy) | hand-written, reviewed |
| `models/`, `engines/` | model and engine capability catalogs | hand-written, reviewed |
| `tuples/` | execution tuple groups the router selects from | hand-written, reviewed |
| `surfaces/`, `workers/` | **generated** per-GPU execution surface and worker definitions (~3,200 files) | `scripts/materialize_provider_catalog.py` from the provider price/spec matrices in `candidate_sources/` |
| `tenants/`, `accounts/` | tenant quotas and provider account references (no secrets) | hand-written |

## Why the generated catalog is committed

`gpucall validate-config`, `launch-check`, and the routing tests are hermetic:
they validate the exact catalog that ships. Regenerating at install time would
make validation results depend on generator execution. The trade-off is a large
but mechanical tree; it is marked `linguist-generated` so diffs collapse.

Do not hand-edit `surfaces/` or `workers/`. Change the source matrices in
`candidate_sources/` and re-run:

```bash
uv run python scripts/materialize_provider_catalog.py
uv run gpucall validate-config --config-dir config
```

## Placeholders

RunPod worker files carry `RUNPOD_ENDPOINT_ID_PLACEHOLDER` until an operator
provisions an endpoint (via `gpucall setup` or the Panopticon supply
provisioning plan). Tuples pointing at placeholder targets are rejected from
production routing by `validate-config` and the launch gates — fail-closed by
design, so an unconfigured provider can never receive traffic.

Secrets never live here: credentials belong to the credential store
(`credentials.yml` is gitignored; see `docs/GATEWAY_API_KEYS.md`).
