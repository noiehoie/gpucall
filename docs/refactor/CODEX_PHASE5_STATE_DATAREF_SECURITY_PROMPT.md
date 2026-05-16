You are running Phase 5 of the gpucall large refactor.

Run in yolo mode. Do not ask the user for approval. Do not pause for
questions. Do not edit dirty `config/` files.

Read these first, in this exact order:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/05_STATE_DATAREF_AND_SECURITY.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`

Use `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only as a lookup map if you
need deeper background. Do not rely on the long knowledge file alone.

Phase target:

Make state, DataRef, and security boundaries easier to audit without weakening
any hardening added during the audit phase.

This phase is not about OpenAI protocol admission, provider egress,
recipe-control-plane workflow, live production endpoint credentials, object
store secret setup, dirty production config cleanup, or v3 TEE implementation.

Before editing code, print a pre-edit report with:

- files read
- current state/DataRef/security entrypoints
- non-negotiables from the docs above
- exact boundary you will edit
- exact boundary you will not edit
- focused tests you will run

Non-negotiables:

- No inference in runtime control decisions.
- Postgres mode must keep jobs, idempotency, tenant ledger, artifact registry,
  and admission state in Postgres.
- SQLite remains a local dev/test fallback only when `GPUCALL_DATABASE_URL` is
  unset.
- Do not collapse SQLite/Postgres duplication until behavior parity is locked by
  tests.
- Preserve idempotency pending/completed reservation, first-writer semantics,
  release-on-failure, and no overwrite of completed results.
- Preserve tenant budget atomic reservation semantics.
- Preserve artifact latest compare-and-set semantics.
- Preserve admission lease/cooldown behavior.
- DataRef bodies must not cross the gateway except through object-store presign
  workflow.
- HTTP(S) worker refs require `gateway_presigned=true`.
- HTTP(S) worker refs require allowlisted hosts.
- Reject URI userinfo, redirects, private/link-local/loopback/multicast/reserved
  /unspecified resolved addresses, and negative byte counts.
- Verify SHA-256 for both S3 and HTTP(S) refs.
- Ambient S3 worker credentials require explicit opt-in.
- Missing object-store configuration remains a deterministic Production No-Go,
  not a fake static success.
- Do not add or stage secrets.

Expected implementation direction:

- Inspect `gpucall/sqlite_store.py`, `gpucall/postgres_store.py`,
  `gpucall/tenant.py`, `gpucall/artifacts.py`, `gpucall/admission.py`,
  `gpucall/app.py`, `gpucall/app_helpers.py`, `gpucall/object_store.py`,
  `gpucall/worker_contracts/io.py`, `gpucall/local_dataref_worker.py`, and the
  focused tests before editing.
- If behavior is already correct but obscured, extract small named helpers or
  protocols that make invariants explicit and testable.
- Prefer deleting duplicated control flow only where tests prove behavior
  parity. Do not create clever abstractions around security-sensitive fetch
  logic.
- Keep DataRef validation logic boring, explicit, and easy to inspect.
- Keep object-store live checks honest: configured, missing, or skipped.
- Keep error messages deterministic and bounded.
- Do not claim Production traffic Go from static or unit tests.

Useful candidate improvements, subject to your pre-edit findings:

- Add explicit protocol/adapter boundaries for idempotency/job state only if the
  current app construction is mixing persistence selection with request logic.
- Factor repeated SQLite/Postgres idempotency reservation invariants into shared
  tests rather than shared unsafe implementation.
- Factor DataRef URL/IP/size/SHA validation helpers only if doing so reduces
  duplication without hiding checks.
- Add regression tests for any invariant you touch before or with the change.

Use multi-ai-code before implementation if available. Bound auxiliary AI
processes; if they fail, hang, or drift outside this phase, stop them and
continue with deterministic code inspection and tests. Use multi-ai-review
before final if available, with the same bounded behavior.

Do not edit:

- `config/recipes/*`
- `config/surfaces/*`
- `config/workers/*`
- `config/.modal.toml`
- OpenAI facade/protocol admission
- provider execution surfaces or dispatcher egress unless a failing
  state/DataRef/security test requires it
- recipe-control-plane workflow unless a failing state/DataRef/security test
  requires it
- live provider endpoint ids or credentials
- v3 TEE/sovereign/split-learning runtime contracts

Required verification:

```bash
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_p1_audit_regressions.py tests/test_worker_io.py -q --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_app.py -q -k 'idempotency or object or dataref or tenant or artifact or admission or presign' --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_providers.py tests/test_dispatcher.py -q --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run gpucall security scan-secrets --config-dir config
```

If a named test does not exist or the expression selects zero tests, report that
exact fact and substitute the closest existing deterministic test after finding
it with `rg`.

Final report must include:

- changed files
- state boundary preserved or intentionally changed
- DataRef/security boundary preserved or intentionally changed
- SQLite/Postgres parity evidence
- DataRef hardening evidence
- object-store live status: configured, missing, or skipped
- no secrets added or staged
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
