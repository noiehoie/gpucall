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

## External Canary Discipline

`news-system` is the first representative external canary, not a
product-specific contract owner. It is useful because its GPU workload shape is
broad. It must not become a hidden gpucall requirement.

## Onboarding Reality Check

The following exchange is a product-level constraint, not a casual observation:

```text
Q: Did it take 7h 55m 33s of agent work before the news-system pipeline could
   complete against gpucall without errors?
A: Yes.

Q: Is it normal for a first-time GitHub user to need 7h 55m 33s to install
   gpucall and adapt an existing system to it?
A: No.
```

Implication:

- A successful external canary after hours of expert agent intervention is not
  proof of good onboarding.
- gpucall must make the first-install and external-system adaptation path
  short, deterministic, observable, and self-diagnosing.
- The operator journey must surface missing recipes, unsuitable workload class,
  provider readiness, object-store gaps, SDK/version mismatch, budget admission,
  low-quality success, and next actions without requiring a senior engineer to
  read logs for hours.
- Any refactor that improves internals but leaves first-time adoption dependent
  on prolonged manual diagnosis is incomplete.

Normalized work classes:

- A: gpucall runtime plus baseline recipe/catalog defects discovered during the
  news-system canary. Treat the known A set as fixed after the audit/product
  hardening work.
- B: external-system adaptation work that becomes visible only when a real
  caller tries to migrate.
- C: the deterministic onboarding / workload contract kit. C must compress B
  from hand-driven agent work into a mostly deterministic product workflow.

The C validation baseline is not the current news-system production tree. It is
the pre-gpucall worktree on macmini at:

- Reference baseline: `/Users/admin/Developer/news-system-pre-gpucall`
- C onboarding sandbox: `/Users/admin/Developer/news-system-c-onboarding`
- Baseline commit: `73cbbd1` (`fix: unload Ollama model after pipeline on
  fallback path`)

These trees must be used to test whether C can onboard a first-time external
system without relying on the already-modified production checkout.

When a `news-system` canary exposes a failure, classify and fix it through
generic gpucall contracts before touching runtime logic:

1. caller request / SDK / OpenAI-compatible usage
2. recipe YAML
3. tuple / worker / surface YAML
4. policy YAML
5. validation evidence / launch gates
6. gateway or provider adapter code only if the contract layer cannot explain it

Before accepting any change, answer:

- Does this preserve deterministic routing and fail-closed behavior?
- Does this avoid `news-system`-specific names, newspaper concepts, file paths,
  OCR shortcuts, or caller-specific branches?
- Is the fix expressed as a reusable contract rather than a one-off exception?
- Which non-`news-system` external caller class benefits from this change?
- If no other caller benefits, should the change stay in the caller-side
  integration instead of gpucall?

Product ambition matters. Canary pressure must sharpen gpucall into a generally
useful GPU governance router capable of a stable, lightweight v3 with TEE,
sovereignty, KMS, and encrypted artifact guarantees. It must not distort the
gateway into a `news-system` adapter.

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
