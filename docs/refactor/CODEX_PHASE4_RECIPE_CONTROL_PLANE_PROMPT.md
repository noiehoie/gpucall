You are running Phase 4 of the gpucall large refactor.

Run in yolo mode. Do not ask the user for approval. Do not pause for
questions. Do not edit dirty `config/` files.

Read these first, in this exact order:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/04_RECIPE_CONTROL_PLANE.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`

Use `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only as a lookup map if you
need deeper background. Do not rely on the long knowledge file alone.

Phase target:

Make recipe creation and administration honest as a control-plane pipeline:

```text
sanitized caller intake
  -> deterministic canonical recipe materialization
  -> admin review
  -> tuple candidate / execution contract derivation
  -> validation artifact
  -> launch check
  -> explicit production activation
```

This phase is not about OpenAI protocol admission, provider egress,
state/DataRef hardening, live production endpoint credentials, object-store
setup, v3 TEE implementation, or active dirty config cleanup.

Before editing code, print a pre-edit report with:

- files read
- current recipe control-plane entrypoints
- non-negotiables from the docs above
- exact boundary you will edit
- exact boundary you will not edit
- focused tests you will run

Non-negotiables:

- `gpucall-recipe-draft` is caller-side only. It may create sanitized intake,
  preflight metadata, deterministic local drafts, low-quality-success feedback,
  comparisons, submissions, and status checks.
- `gpucall-recipe-draft` must not call an LLM, choose provider/model/GPU/runtime
  /tuple/fallback, activate production routing, or transmit raw confidential
  payloads when sanitized metadata is sufficient.
- `gpucall-recipe-admin` is administrator-side only. It may materialize intake,
  review candidates, process inboxes, promote tuple candidates, run validation
  only when explicitly budgeted, and activate production config only through
  explicit deterministic gates.
- Admin-side LLM recipe authoring, if touched, is proposal generation only and
  must not become authoritative production config.
- No inference in runtime control decisions.
- Unknown/unsafe recipe support must fail closed.
- Preserve guarded writes, contract-narrowing checks, accept-all/admin.yml
  gates, validation-budget requirements, unsafe `auto_select` fail-closed
  behavior, and missing credential/endpoint-id blockers.
- Do not claim Production traffic Go from recipe-control tests.

Expected implementation direction:

- Inspect `gpucall/recipe_admin.py`, `gpucall/recipe_materialize.py`,
  `gpucall/recipe_authoring.py`, `gpucall/recipe_request_index.py`,
  `gpucall/quality_feedback_index.py`, `gpucall/tuple_promotion.py`,
  `sdk/python/gpucall_recipe_draft/`, and existing tests.
- `gpucall/recipe_admin.py` is too large. Split it by workflow into small,
  named modules where practical:
  - CLI parser/entrypoint
  - inbox processing and status
  - materialization workflow
  - quality feedback workflow
  - review/reporting
  - promotion/validation orchestration
  - authoring proposal bridge
  - automation/watch loops
- Keep public imports and CLI behavior stable unless a focused test proves an
  existing behavior violates this phase's non-negotiables.
- If full splitting is too risky in one patch, extract the highest-value
  internal workflows first, but do not leave duplicated control logic behind.
- Prefer thin orchestration modules over clever abstraction.
- Keep deterministic facts and gates visible in report JSON; do not hide them
  behind free-form text.
- Add or update focused tests that prove:
  - caller helper cannot select provider/model/GPU/runtime/tuple/fallback
  - caller helper produces sanitized intake only
  - admin materialization remains deterministic
  - accept-all/admin.yml gates still guard writes
  - validation/promote paths require explicit budget when billable
  - existing tuple activation cannot bypass validation evidence
  - quality feedback processing does not materialize production recipes by
    itself
  - authoring proposal output cannot activate production routing

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
- provider execution surfaces or dispatcher egress
- tenant budget ledger
- artifact registry persistence
- DataRef worker fetch policy
- v3 TEE/sovereign/split-learning runtime contracts

Required verification:

```bash
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_recipe_admin.py -q --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest sdk/python/tests/test_recipe_draft.py -q --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_tuple_promotion.py tests/test_admin_api_keys.py tests/test_public_release_audit.py -q --maxfail=10
XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_app.py -q -k 'recipe or inbox or quality or handoff or setup' --maxfail=10
```

If a named test does not exist or the expression selects zero tests, report that
exact fact and substitute the closest existing deterministic test after finding
it with `rg`.

Final report must include:

- changed files
- files split or moved
- caller/admin/gateway boundary preserved
- whether LLM proposal code was touched
- deterministic gates preserved
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
