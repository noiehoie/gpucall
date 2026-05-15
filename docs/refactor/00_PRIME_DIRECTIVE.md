# gpucall Refactor Prime Directive

Read this file before every refactor phase. If a narrower packet conflicts with
this file, stop and resolve the conflict before editing code.

## Product Ideal

gpucall is an OpenAI-compatible GPU governance gateway, not a clever model
router and not a generic proxy.

Caller-facing ideal:

```text
OpenAI compatibility at the entrance.
```

Core product essence:

```text
Safely convert caller requests into a deterministic governance routing contract.
```

Exit-side ideal:

```text
Provider APIs are execution devices, not decision makers.
```

Tone and engineering character:

```text
An honest, elegant router/gateway with no wasteful or duplicate control logic.
```

## Non-Negotiable Principle

```text
No inference in control decisions.
```

No LLM, heuristic guessing, prompt classification, implicit provider preference,
or hidden model selection may decide:

- request admission
- recipe selection
- provider, model, GPU, runtime, or tuple selection
- fallback order
- budget admission
- confidentiality classification
- price freshness acceptance
- validation readiness
- cleanup actions
- production promotion

Control decisions must be deterministic rule evaluation over explicit evidence:
request metadata, tenant policy, recipes, model catalog, engine catalog,
execution tuples, price evidence, validation evidence, runtime readiness, budget
ledger, idempotency state, provider observations, and object-store/DataRef
preconditions.

LLM execution is allowed only after deterministic routing has selected a
production tuple and delivered the worker payload. Admin-side LLM recipe
authoring, if present, is proposal generation only and cannot activate
production config.

## Required Architecture Direction

Entrance:

```text
OpenAI wire contract
  -> protocol admission layer
  -> governance routing contract
  -> compiler / dispatcher
```

Exit:

```text
governance routing contract
  -> provider egress layer
  -> provider-specific execution contract
  -> canonical result / canonical error / cleanup evidence
```

Recipe creation:

```text
sanitized caller intake
  -> deterministic canonical recipe materialization
  -> admin review
  -> tuple candidate / execution contract derivation
  -> validation artifact
  -> launch check
  -> explicit production activation
```

## Do Not Break

- Gateway runtime routing remains deterministic.
- Unknown workloads fail closed.
- No hosted AI fallback.
- OpenAI `model` must not become a raw caller-controlled provider/model selector.
- Caller-controlled `recipe` / `requested_tuple` stays disabled unless explicitly allowed.
- DataRef bodies do not cross the gateway except through object-store presign workflow.
- Provider adapters do not decide routing, policy, fallback, budget, or tenant semantics.
- Postgres mode keeps jobs, idempotency, tenant ledger, artifact registry, and admission in Postgres.
- v3-facing contracts are not deleted as "unused" simplifications.

## Required Work Protocol

Before each phase, report:

- read refactor packets
- non-negotiables relevant to the phase
- boundaries to edit
- boundaries not to edit
- focused tests to run

After each phase, report:

- files changed
- behavior preserved or intentionally changed
- focused test output
- full test status or reason it was not run
