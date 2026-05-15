You are running Phase 3 of the gpucall large refactor.

Run in yolo mode. Do not ask the user for approval. Do not pause for
questions. Do not edit dirty `config/` files.

Read these first, in this exact order:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/03_PROVIDER_EGRESS.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`

Use `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only as a lookup map if you
need deeper background. Do not rely on the long knowledge file alone.

Phase target:

Make the provider-facing exit honest and explicit:

```text
CompiledPlan / governance routing contract
  -> provider egress admission
  -> provider-specific execution contract
  -> canonical TupleResult / TupleError / cleanup evidence
```

This phase is not about OpenAI protocol admission, recipe authoring, object
store setup, RunPod endpoint ids, production live readiness, config cleanup, or
v3 TEE implementation.

Before editing code, print a pre-edit report with:

- files read
- current provider egress entrypoints
- non-negotiables from the docs above
- exact boundary you will edit
- exact boundary you will not edit
- focused tests you will run

Non-negotiables:

- Provider APIs are execution devices, not decision makers.
- Provider adapters may lower payloads, call provider APIs, normalize responses,
  map provider failures, cancel/cleanup remote work, and perform provider-specific
  health/preflight/inventory checks.
- Provider adapters must not decide routing, fallback order, tenant policy,
  budget policy, confidentiality policy, validation readiness, or DataRef access
  paths.
- Dispatcher/provider egress must fail closed with canonical `TupleError` /
  `ProviderErrorCode` and cleanup evidence when execution cannot proceed.
- Do not silently collapse provider errors into generic exceptions.
- Do not claim Production traffic Go without same-tuple live success evidence
  plus object-store/DataRef evidence.
- Preserve Phase 1 release-gate semantics and Phase 2 protocol-admission
  behavior.

Expected implementation direction:

- Inspect `gpucall/dispatcher.py`, `gpucall/execution/base.py`,
  `gpucall/execution_surfaces/`, `gpucall/provider_errors.py`, and existing
  provider/dispatcher tests.
- Look first for duplicated provider egress admission / attempt orchestration in
  sync and stream paths.
- If practical, introduce a small first-class egress attempt/report object or
  helper that records tuple, provider family, workload scope, admission outcome,
  start decision, cleanup status, and canonical error classification.
- Keep route selection and fallback policy in dispatcher/admission, not provider
  adapters.
- Keep provider-specific HTTP/API details inside execution surface modules.
- Keep behavior changes minimal unless a focused test proves existing behavior
  violates this phase's non-negotiables.
- Add focused tests that prove:
  - provider adapters receive only `CompiledPlan`, not OpenAI wire payloads
  - egress admission decisions are audited deterministically
  - provider temporary failures keep canonical `ProviderErrorCode` mapping
  - cleanup is attempted and audited after provider failure/timeout
  - non-fallback-eligible provider errors do not silently fall through
  - sync and stream paths share the same egress invariants where applicable

Use multi-ai-code before implementation if available. Bound auxiliary AI
processes; if they fail, hang, or drift outside this phase, stop them and
continue with deterministic code inspection and tests. Use multi-ai-review
before final if available, with the same bounded behavior.

Do not edit:

- `config/recipes/*`
- `config/surfaces/*`
- `config/workers/*`
- `config/.modal.toml`
- OpenAI facade/protocol admission unless a provider-egress test proves a hard
  coupling bug
- recipe admin/materialization
- tenant budget ledger
- artifact registry persistence
- DataRef worker fetch policy
- v3 TEE/sovereign/split-learning contracts

Required verification:

```bash
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_dispatcher.py -q
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_providers.py tests/test_tuple_catalog.py -q --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_app.py -q -k 'provider or tuple or cleanup or openai' --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_production_acceptance.py tests/test_launch_reporting.py -q
```

If a named test does not exist or the expression selects zero tests, report that
exact fact and substitute the closest existing deterministic test after finding
it with `rg`.

Final report must include:

- changed files
- provider responsibility moved or made explicit
- behavior preserved
- behavior intentionally changed
- exact command outputs
- whether full pytest was run
- live provider testing status: success, skipped, or environment-gated
- remaining blockers
- confirmation that dirty `config/` files were not touched
- `git status --short`

Do not commit unless the parent session explicitly commits after reviewing the
phase result.
