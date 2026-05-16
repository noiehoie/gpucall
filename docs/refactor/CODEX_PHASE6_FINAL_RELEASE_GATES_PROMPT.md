# Codex CLI Phase 6: Final Refactor Release Gates

You are running Phase 6 of the gpucall large refactor.

This is the final integration and release-gate phase for the structural refactor. It is not a new architecture phase. Your job is to prove that the Phase 1-5 refactor still satisfies the product ideal and README v2 claims, or to fix concrete release-gate regressions if you find them.

Read these first:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/06_RELEASE_GATES.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`

Use `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only as a lookup map. Do not rely on it as the only instruction source.

Before editing anything, print a pre-edit report:

- files read
- current branch and HEAD
- current dirty files, explicitly separating existing `config/` dirty files from refactor files
- non-negotiables for this phase
- release-gate checks you will run
- files/modules you may edit
- files/modules you will not edit

Phase target:

```text
Prove Code/Static Go for the refactored codebase, keep Production traffic Go blocked unless live production tuple and object-store evidence exist, and leave a deterministic checkpoint.
```

Non-negotiables:

- No inference in gateway runtime control decisions.
- OpenAI wire compatibility remains an entrance contract, not caller-controlled routing.
- Provider APIs remain execution devices, not decision makers.
- Recipe creation remains control-plane only and cannot activate production routing without explicit gates.
- Postgres mode keeps jobs, idempotency, tenant ledger, artifact registry, and admission in Postgres.
- DataRef bodies do not cross the gateway except through object-store presign workflow.
- Do not edit, revert, or stage existing dirty `config/recipes/*`, `config/surfaces/*`, `config/workers/*`, or `config/.modal.toml`.
- Do not inject credentials, endpoint ids, object-store secrets, or billable provider execution.
- Do not claim `Production traffic Go` from local-only success, unit tests, static validation, or placeholder tuples.

Allowed edit scope:

- Small code fixes required by failing release-gate tests.
- Small tests required to lock a concrete regression found in this phase.
- `TECH_DEBT_AUDIT.md` or `docs/refactor/*` only if needed to record final gate evidence or a concrete blocker.

Forbidden edit scope unless a failing release-gate test proves it necessary:

- Broad new refactor.
- Provider adapter redesign.
- OpenAI facade redesign.
- Recipe materialization redesign.
- State backend redesign.
- Any `config/` production/operator file.
- README claim weakening.

Required checks:

```bash
XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
```

```bash
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/refactor-final uv run pytest -q -x
```

```bash
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/refactor-final-validate uv run gpucall validate-config --config-dir config
```

```bash
XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/refactor-final-static uv run gpucall launch-check --profile static --config-dir config
```

```bash
XDG_CACHE_HOME=$PWD/.cache uv run gpucall security scan-secrets --config-dir config
```

Also run:

```bash
git diff --check
git diff --cached --check
```

If full pytest fails:

1. Reproduce the failing test directly.
2. Determine whether it is a refactor regression, pre-existing dirty config issue, or environment-gated external dependency.
3. Fix refactor regressions.
4. Do not "fix" by weakening README claims or skipping deterministic gates.

README v2 claim traceability check:

- For every row in `docs/refactor/01_README_V2_CLAIM_MATRIX.md`, identify whether the refactor preserved or improved the implementation anchor.
- If an anchor is not testable locally, name the explicit environment-gated blocker.
- Do not report vague coverage percentages.

Before final completion, run bounded multi-AI review if available:

- ask it to review only changed code/docs since `aa342ee`
- time-box each external process
- adopt only concrete findings reproduced locally
- if unavailable or it times out, report that fact and continue with local deterministic checks

Staging and commit rule:

- If you make no changes, do not create an empty commit.
- If you make changes, stage only Phase 6 files.
- Before commit, print `git diff --cached --name-only`.
- Confirm no staged path matches `config/recipes`, `config/surfaces`, `config/workers`, `config/.modal.toml`, `.env`, `.cache`, `.state`, or `dist`.
- Commit with message:

```text
refactor: finalize static release gates
```

Final report must include:

- final判定: `Code/Static Go`, `Conditional Go`, `Production traffic Go`, or `No-Go`
- why `Production traffic Go` is or is not claimed
- changed files
- commit hash if created
- exact command outputs for required checks
- README v2 claim traceability summary
- remaining blockers
- dirty/excluded files
- process cleanup status
