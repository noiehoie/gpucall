# gpucall OOB Final Report (v2.0.69)

Date: 2026-07-02 JST
Decision: Go
Sanitization: no API keys, raw prompts, presigned URLs, DataRef URIs, or provider raw outputs.

## Scope

- Public release artifacts v2.0.68 and v2.0.69 (both cut, published, and
  deployed during this session)
- Clean-room install + local trial on netcup2 with a fresh HOME/XDG tree
- Production-configured netcup2 gateway upgraded v2.0.67 → v2.0.68 → v2.0.69
- Re-audit of the v2.0.67 Go decision ("trust nothing, re-audit"), which
  found and fixed three product defects (see below)

## Release artifact evidence

```text
gh release view v2.0.69 --repo noiehoie/gpucall --json assets
["gpucall-2.0.69-py3-none-any.whl","gpucall-2.0.69.tar.gz",
 "gpucall_sdk-2.0.69-py3-none-any.whl","gpucall_sdk-2.0.69.tar.gz","SHA256SUMS"]
```

Release gates (all green before each tag):

```text
uv run pytest                    -> 1008 passed, 1 skipped  (v2.0.69)
(sdk) uv run pytest              -> 65 passed
gpucall security scan-secrets    -> ok: true, 0 findings
gpucall validate-config          -> valid: true (32 recipes, 3233 tuples)
gpucall launch-check --profile static -> static_config_valid: True
gpucall release-check            -> go: True, code_static_go: True
scripts/check_product_contamination.sh -> ok
scripts/check_provider_parity.py -> ok
```

## Clean-room install evidence (netcup2, fresh HOME)

```text
GPUCALL_REF=v2.0.69 sh -c 'curl -fsSL .../v2.0.69/install.sh | sh'
gpucall 2.0.69
gpucall setup apply --file gpucall.setup.yml --yes
  [warn] no cloud providers configured
  [ok] validate-config: 32 recipes, 3233 tuples
  [ok] security scan-secrets: 0 findings
OOB readiness: local-trial-ready
```

Zero-credential first run stays bounded, does not hang, and does not pretend
cloud routing is ready.

## Re-audit findings (v2.0.67 Go re-examined)

The 2026-06-20 Go was real but did not survive twelve days of production:

1. `onboarding-ready` degraded to `onboarding-blocked` within an hour of every
   setup because nothing maintained the synthetic dry-run evidence
   (TTL 3600s). Fixed in v2.0.68 (watch-service refresh); verified live —
   netcup2 recovered to `onboarding-ready` and has held it across two
   upgrades and service restarts.
2. Light/vision route validation evidence was silently invalidated by a
   config change on 2026-06-29; four text routes and one vision route were
   unroutable for nine days. Repaired by re-validation; prevented in v2.0.69
   (automatic drift re-validation in the watch service).
3. The Modal worker capped Qwen2.5 contexts at 32768 while the catalog
   declared 131072, killing large production rank workloads as unclassified
   502s. Fixed in v2.0.69 (model-card YaRN configuration); worker redeployed
   through the consent-gated flow (plan hash `12d4aa43d17ed1f3`) and the rank
   route re-validated (`passed: true`; Modal logs show
   `Maximum concurrency for 131,072 tokens per request`).

## v2.5 Agent-Native surface shipped (v2.0.68)

- `POST /v2/estimate` (non-billable pre-execution estimate)
- `GET /v2/failure-taxonomy` (deterministic retry/failure taxonomy)
- `gpucall-mcp` stdio MCP tool server
- SDK `estimate()` sync/async
- `gpucall provider-conformance` (13/13 adapters pass)
- Unauthenticated access to the production gateway's new endpoints returns
  401 (fail-closed confirmed live).

## Caller evidence

See `tasks/news-system-canary-report.md`: caller tests 32/32, 5-intent canary
5/5 GO, production rank workload re-run on the fixed worker.

## Decision

Go, with the standing operational dependency that the Modal provider path
stays funded and routable. Remaining supply blockers (RunPod endpoint zero,
Hyperstack SSH prerequisites) are machine-classified with owners in
`tasks/provider-loop-report.md` and do not block the Modal happy path.
