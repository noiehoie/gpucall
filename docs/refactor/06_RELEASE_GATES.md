# Release Gates Packet

Read this with `00_PRIME_DIRECTIVE.md` before declaring any Go/No-Go result,
creating a checkpoint commit, or changing production-readiness diagnostics.

## Current Release State

Current checkpoint:

```text
aa342ee rc: harden production canary gates
```

Status:

```text
Code RC Go / Production traffic No-Go
```

Production traffic remains blocked by environment/config readiness:

- production RunPod tuples still need real endpoint ids and credentials
- object store/DataRef live is not configured
- SDK sync/async/OpenAI facade have not succeeded on the same production tuple
- local-only success is not production cloud GPU evidence

## Go Language

Use precise status language:

- `Code/Static Go`: static validation and tests pass.
- `Conditional Go`: local/static or packaging path passes but live production
  prerequisites are missing.
- `Production traffic Go`: same production tuple succeeds through required live
  paths and object-store/DataRef requirements are met.
- `No-Go`: code, config, test, or environment blocker prevents the claimed scope.

Do not hide missing production prerequisites behind broad `Conditional Go`.

## Out Of Scope For Structural Refactor

- injecting real RunPod endpoint ids
- adding provider credentials
- creating or committing object-store secrets
- requiring billable live canary success as a refactor-only gate
- reverting existing dirty operator `config/` changes without explicit approval

## In Scope For Structural Refactor

- clearer readiness diagnostics
- tests for placeholder endpoint ids
- tests for missing object store
- tests for missing credentials
- tests for unvalidated tuple fail-closed behavior
- CLI/API output that separates Code/Static Go from Production traffic Go

## Required Verification

For normal structural refactor checkpoints:

```text
uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
uv run pytest <focused tests> -q
```

Run full pytest before checkpoint commit unless explicitly deferred:

```text
GPUCALL_STATE_DIR=$PWD/.state/<name> uv run pytest -q -x
```

For production claims, also require relevant live canary evidence:

- gateway `/healthz`, `/readyz`, `/openapi.json`
- SDK sync success on production tuple
- SDK async success on same production tuple
- OpenAI facade success on same production tuple
- object store/DataRef live canary when file/image workflows are in scope
- provider tuple smoke with explicit `--budget-usd`

## Commit Rule

Before committing:

- list staged files
- confirm no secrets are staged
- confirm dirty `config/` files are intentionally included or excluded
- run focused tests
- report any full-test deferral
