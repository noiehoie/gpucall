# Codex CLI Execution Protocol

Use this packet when writing prompts for Codex CLI or reviewing Codex CLI
progress. It exists to prevent lost-in-the-middle failures.

## Read Order

Every refactor phase must read:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. exactly the phase packet relevant to the work
4. `docs/refactor/GPuCALL_REFACTOR_KNOWLEDGE.md` only for detailed background
   or code map lookup

Do not ask Codex CLI to rely on the full knowledge base alone.

## Phase Packets

- protocol admission: `02_PROTOCOL_ADMISSION.md`
- provider egress: `03_PROVIDER_EGRESS.md`
- recipe control plane: `04_RECIPE_CONTROL_PLANE.md`
- state/DataRef/security: `05_STATE_DATAREF_AND_SECURITY.md`
- release/checkpoint: `06_RELEASE_GATES.md`

## Mandatory Pre-Edit Report

Before editing code, Codex CLI must print:

- files read
- non-negotiables relevant to the phase
- target boundary
- files/modules it expects to edit
- files/modules it will not edit
- focused tests it will run

If it cannot produce this report, stop it before implementation.

## Phase Scope Rule

Each phase should have one primary boundary. Do not mix protocol admission,
provider egress, recipe control plane, persistence, and release diagnostics in
one patch unless there is a concrete failing test that requires it.

## Required Final Report

After implementation, Codex CLI must print:

- changed files
- behavior preserved
- behavior intentionally changed
- tests run with exact command output
- full-test status
- remaining blockers
- unstaged or excluded files

## Prompt Skeleton

```text
Read these first:
- docs/refactor/00_PRIME_DIRECTIVE.md
- docs/refactor/01_README_V2_CLAIM_MATRIX.md
- docs/refactor/<PHASE_PACKET>.md

Then inspect only the code needed for this phase.

Before editing, report:
1. files read
2. non-negotiables for this phase
3. boundary to edit
4. boundary not to edit
5. focused tests to run

Implement the phase with minimal behavior change. Preserve deterministic
routing, fail-closed behavior, DataRef security, idempotency, tenant budget
semantics, provider error normalization, and README v2 claim traceability.

After editing, run focused tests and report exact outputs. Do not claim
Production traffic Go without live production tuple and object-store evidence.
```
