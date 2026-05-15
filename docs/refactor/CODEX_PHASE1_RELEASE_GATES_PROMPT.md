# Codex CLI Phase 1: Release Gates / Production No-Go Diagnostics

Worktree: `/Users/tamotsu/Projects/gpucall`
Branch: `codex/rc-audit-product-static-conditional-go`

This is an implementation phase, but it is intentionally narrow. Improve
release/launch/production-readiness diagnostics only. Make `Code/Static Go` and
`Production traffic No-Go` mechanically distinct.

## Read First

Read these files in order:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/06_RELEASE_GATES.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`
5. `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only for lookup

Before editing, run `git status --short` and print:

- files read
- non-negotiables relevant to this phase
- target boundary
- files/modules expected to edit
- files/modules explicitly not edited
- focused tests to run

## Phase Target

Make release/launch/production-readiness diagnostics clear and deterministic.
Preserve fail-closed behavior for:

- placeholder RunPod endpoint ids
- missing object store/DataRef live config
- missing credentials
- unvalidated production tuples

Do not claim `Production traffic Go` without same production tuple live success
and object-store/DataRef evidence.

## Allowed Edit Boundary

Likely allowed files:

- `gpucall/cli.py` release/launch/production diagnostic helpers only
- `gpucall/production_acceptance.py` only if a production acceptance invariant
  output needs clearer No-Go classification
- `gpucall/acceptance_invariants.py` only if existing invariant wording is
  ambiguous
- focused tests under:
  - `tests/test_launch_check.py`
  - `tests/test_launch_reporting.py`
  - `tests/test_compiler.py`
  - `tests/test_execution_catalog.py`
  - closely related existing tests if the listed names differ

## Do Not Touch

- dirty `config/` files
- `config/.modal.toml`
- provider credentials, endpoint ids, object-store secrets
- compiler routing order
- dispatcher fallback/admission behavior
- OpenAI facade admission/model semantics
- provider adapters
- recipe admin workflows
- DataRef worker fetch/security code
- v3-facing contracts

## Required Workflow

Follow the project AGENTS workflow:

- Before implementation, run the required multi-AI planning/code workflow if
  available in this environment. If unavailable, state exactly why and continue
  with the narrow implementation.
- Before final completion, run multi-AI review if available. If unavailable,
  state exactly why and continue with deterministic tests.

Do not create a commit unless explicitly requested.

## Focused Tests

Run:

```bash
uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
uv run pytest tests/test_launch_check.py tests/test_launch_reporting.py tests/test_compiler.py::test_runpod_provider_with_placeholder_endpoint_is_not_auto_routed tests/test_execution_catalog.py::test_execution_catalog_treats_placeholder_targets_as_unconfigured -q
```

If any listed tests do not exist, inspect the current test suite with `rg` and
use the closest existing tests. Report the substitution explicitly.

Optional additional focused tests if touched:

```bash
uv run pytest tests/test_config.py::test_live_cost_audit_ignores_placeholder_runpod_endpoint tests/test_app.py::test_worker_readable_request_requires_object_store_for_s3_refs -q
```

## Final Report

Return:

```text
判定: Phase 1 complete / blocked

Pre-edit report:
...

Changed files:
...

Behavior preserved:
...

Behavior intentionally changed:
...

Command outputs:
...

Full-test status:
...

Remaining blockers:
...

Unstaged/excluded files:
...

Next recommended phase:
...

Next prompt:
...
```
