# README v2 Claim Matrix

Read this with `00_PRIME_DIRECTIVE.md` before any broad refactor. README v2
claims are product contracts. Refactoring must make the mapping from claim to
code, CLI/API, tests, and blockers clearer.

Percent estimates in the knowledge base are operator assessments, not measured
coverage metrics. Use this matrix and release gates instead.

| README v2 claim | Code/API anchor | Current status | Refactor obligation |
| --- | --- | --- | --- |
| OpenAI-compatible entrance | `/v1/chat/completions`, OpenAI schema fixtures, facade response/error shape | partial strict subset | Separate wire compatibility from governance semantics; fail closed on unsupported features. |
| GPU/model/provider choice out of application code | compiler, tenant policy, catalog routing, OpenAI `model` policy | substantially implemented | Prevent `model`, recipe, or tuple from becoming caller-controlled escape hatches. |
| 100% deterministic routing | `GovernanceCompiler`, `routing.py`, dispatcher fallback, validation evidence | substantially implemented | Preserve no-inference control path and make rejection/fallback reasons testable. |
| Gateway runtime owns policy/audit/validation/cleanup | `app.py`, compiler, dispatcher, admission, artifacts, launch checks | implemented but mixed | Extract boundaries without changing behavior. |
| Three-part product shape | gateway runtime, `gpucall-recipe-draft`, `gpucall-recipe-admin` | implemented | Keep caller/admin/gateway responsibilities separate. |
| Caller-side helper | `sdk/python/gpucall_recipe_draft` | implemented | Keep deterministic, sanitized, and unable to choose provider/model/GPU/tuple. |
| Administrator-side helper | `gpucall/recipe_admin.py`, materialize/review/promote/watch | implemented but too large | Split by workflow; keep production activation behind explicit deterministic gates. |
| Four-catalog routing | recipes, models, engines, execution tuples | implemented | Make compatibility decisions auditable and test-backed. |
| Validation evidence before production | tuple-smoke, tuple promotion, launch-check, validation artifacts | implemented with environment blockers | Keep validation explicit, budget-gated, and fail-closed when missing. |
| Price freshness as policy input | cost catalog, cost audit, strict budget checks | implemented | Preserve stale/unknown price fail-closed semantics. |
| DataRef/object store | presign endpoints, SDK upload, worker-readable refs, worker fetch hardening | code implemented, live env missing | Preserve missing object-store No-Go diagnostics and SHA validation. |
| Controlled runtimes | runtime registration, local adapters, local DataRef worker | implemented | Keep local success distinct from production cloud GPU evidence. |
| Provider failures/fallback | `ProviderErrorCode`, dispatcher fallback, admission cooldowns | implemented | Keep canonical errors and move provider-specific mapping behind egress boundary. |
| Postgres-backed state | jobs, idempotency, tenant ledger, artifact registry, admission | implemented after audit hardening | Preserve SQLite/Postgres behavior parity. |
| External migration package | onboarding docs, migration kit | present | Claims must point to deterministic commands/tests or explicit backlog. |
| v2.1/v3-facing contracts | artifact, train, fine-tune, split-infer, attestation/key-release fields | control-plane contracts present | Do not delete as unused while simplifying v2 runtime. |

## Refactor Rule

For any README v2 claim touched by a phase, update or preserve at least one of:

- code boundary
- CLI/API output
- test coverage
- explicit environment-gated blocker
- documentation pointer

If no anchor exists, record it as a blocker instead of silently weakening the
README claim.
