You are running Phase 2 of the gpucall large refactor.

Run in yolo mode. Do not ask the user for approval. Do not pause for
questions. Do not edit dirty `config/` files.

Read these first, in this exact order:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/02_PROTOCOL_ADMISSION.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`

Use `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only as a lookup map if you
need deeper background. Do not rely on the long knowledge file alone.

Phase target:

Make the OpenAI-facing entrance honest and explicit:

```text
OpenAI wire contract
  -> protocol admission decision
  -> governance routing contract (`TaskRequest` or a narrowly named successor)
  -> compiler / dispatcher
```

This phase is not about provider execution, production live readiness, object
store setup, RunPod endpoint ids, recipe authoring, or config cleanup.

Before editing code, print a pre-edit report with:

- files read
- current OpenAI facade entrypoints
- non-negotiables from the docs above
- exact boundary you will edit
- exact boundary you will not edit
- focused tests you will run

Non-negotiables:

- The gateway runtime must not infer route, intent, task, model, provider,
  budget, confidentiality, fallback order, or capabilities from prompt text.
- `model` must not become a raw provider/model selector. Treat it only as
  `gpucall:auto`, an approved alias if already implemented, or metadata.
- Unsupported, unknown, ambiguous, unsafe, image/file/base64, or not-yet-modeled
  OpenAI features must fail closed with deterministic errors.
- Provider adapters must not inspect OpenAI wire fields.
- Compiler/dispatcher must receive governance data, not raw OpenAI semantics.
- Preserve existing successful text-only `/v1/chat/completions` behavior unless
  a focused test proves it is unsafe.
- Preserve Phase 1 release-gate behavior: do not claim Production traffic Go
  without same-tuple live evidence plus object-store/DataRef evidence.

Expected implementation direction:

- Inspect `gpucall/openai_facade/`, `gpucall/app.py`, `gpucall/domain.py`, and
  the existing OpenAI facade tests.
- Keep the actual admission logic inside `gpucall/openai_facade/`.
- If useful, introduce a small first-class admission classification/report that
  records fields admitted, transformed, rejected, ignored, and metadata-only.
- Make the `/v1/chat/completions` route consume only the admission result and
  pass the resulting governance request onward.
- Keep raw OpenAI payloads out of compiler, dispatcher, and provider adapters.
- Add focused tests that prove:
  - text-only OpenAI chat still maps to deterministic governance request data
  - unsupported content parts fail closed
  - unknown/fail-closed OpenAI fields fail closed
  - `model` is metadata/alias policy, not provider selection
  - stream options and conflicting token limits stay deterministic
  - admission classification/report is stable enough for audit/debugging

Use multi-ai-code before implementation if available. Bound auxiliary AI
processes; if they fail, hang, or drift outside this phase, stop them and
continue with deterministic code inspection and tests. Use multi-ai-review
before final if available, with the same bounded behavior.

Do not edit:

- `config/recipes/*`
- `config/surfaces/*`
- `config/workers/*`
- `config/.modal.toml`
- provider adapters
- dispatcher fallback policy
- DataRef worker
- tenant budget ledger
- artifact registry
- recipe admin/materialization
- v3 TEE/sovereign/split-learning contracts

Required verification:

```bash
uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
uv run pytest tests/test_openai_facade_admission.py tests/test_openai_contract.py tests/test_openai_sdk_oracle.py -q
uv run pytest tests/test_app.py -q -k 'openai or chat_completions' --maxfail=10
uv run pytest tests/test_production_acceptance.py tests/test_launch_reporting.py -q
```

If a named test does not exist or the expression selects zero tests, report that
exact fact and substitute the closest existing deterministic test after finding
it with `rg`.

Final report must include:

- changed files
- behavior preserved
- behavior intentionally changed
- exact command outputs
- whether full pytest was run
- remaining blockers
- confirmation that dirty `config/` files were not touched
- `git status --short`

Do not commit unless the parent session explicitly commits after reviewing the
phase result.
