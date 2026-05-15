# Codex CLI Phase 0: Refactor Preflight

You are working in `/Users/tamotsu/Projects/gpucall` on branch
`codex/rc-audit-product-static-conditional-go`.

This is not a one-shot refactor. Do not attempt broad implementation in this
phase. Phase 0 is a preflight and execution-plan phase whose purpose is to prove
you understand the refactor packet system and to choose the first bounded
implementation phase.

## Read First

Read these files, in this order:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md`
4. `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md`

Also inspect `git status --short` before making any conclusion. Existing dirty
`config/` files and `config/.modal.toml` must not be reverted, staged, or
modified unless explicitly requested.

## Non-Negotiables

- No inference in gateway runtime control decisions.
- Preserve deterministic routing and fail-closed behavior.
- Do not turn OpenAI `model` into a raw caller-controlled provider/model
  selector.
- Provider adapters must not decide routing, fallback, tenant policy, or budget.
- Caller-side recipe intake remains deterministic and sanitized.
- Admin-side LLM recipe authoring, if used, remains non-authoritative proposal
  only.
- Do not delete v3-facing contracts as "unused" simplifications.
- Do not claim Production traffic Go without live production tuple and
  object-store evidence.

## Phase 0 Tasks

1. Report exactly which refactor docs you read.
2. Summarize the product ideal in no more than 10 bullets.
3. List the major implementation phases you recommend, in order.
4. For each phase, list:
   - primary packet to read
   - primary code boundary
   - likely files to edit
   - focused tests to run
   - checkpoint condition
5. Identify the safest first implementation phase.
6. Identify what must not be touched in that first phase.
7. Do not edit production code in Phase 0.
8. Do not create a commit in Phase 0.

## Final Output Format

Return:

```text
判定: Phase 0 complete / blocked

Read evidence:
...

Product ideal:
...

Recommended phase order:
...

Safest first phase:
...

Do not touch in first phase:
...

Focused tests for first phase:
...

Next prompt:
<a complete prompt for Codex CLI Phase 1>
```
