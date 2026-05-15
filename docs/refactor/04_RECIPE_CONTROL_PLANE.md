# Recipe Control Plane Packet

Read this with `00_PRIME_DIRECTIVE.md` and
`01_README_V2_CLAIM_MATRIX.md` before editing recipe draft, recipe admin,
materialization, review, promotion, inbox, quality feedback, or recipe authoring.

## Boundary

Recipe creation is in scope for the major refactor only as control-plane
boundary clarification. It must not become automatic LLM-driven production
routing.

## Caller-Side Helper

`gpucall-recipe-draft` may create:

- sanitized intake
- preflight metadata
- deterministic local drafts
- low-quality-success feedback
- preflight/failure comparisons
- recipe or quality submissions
- status checks

It must not:

- call an LLM
- choose provider, model, GPU, runtime, tuple, or fallback order
- activate production routing
- transmit raw confidential payloads when sanitized metadata is sufficient

## Administrator-Side Helper

`gpucall-recipe-admin` may:

- materialize caller intake
- review recipe candidates
- process recipe and quality inboxes
- promote tuple candidates
- run validation when explicitly budgeted
- activate production config only through explicit gates
- generate admin-side recipe authoring proposals

Admin-side LLM recipe authoring, if used, is non-authoritative proposal
generation only. It is not production config and cannot bypass deterministic
materialization, validation evidence, launch checks, or administrator approval.

## Required Pipeline

```text
sanitized caller intake
  -> deterministic canonical recipe materialization
  -> admin review
  -> tuple candidate / execution contract derivation
  -> validation artifact
  -> launch check
  -> explicit production activation
```

## Refactor Direction

Split `recipe_admin.py` by workflow:

- parser/entrypoint
- inbox index
- materialization
- quality feedback
- review
- promotion
- authoring proposal
- automation/watch loops

Preserve:

- guarded recipe writes
- contract-narrowing checks
- accept-all/admin.yml gates
- validation-budget requirements
- unsafe `auto_select` fail-closed behavior
- missing credential/endpoint-id blockers

## Completion Evidence

A phase touching recipe control plane must report:

- caller/admin/gateway boundary preserved
- whether LLM proposal code was touched
- deterministic gates preserved
- files split or moved
- focused tests for materialize/review/promote/inbox/quality
