# Provider Egress Packet

Read this with `00_PRIME_DIRECTIVE.md` and
`01_README_V2_CLAIM_MATRIX.md` before editing provider adapters, dispatcher
execution, live catalog code, provider errors, cleanup, or tuple smoke.

## Target Boundary

```text
CompiledPlan / governance contract
  -> provider egress admission
     - tuple is currently executable
     - budget, lease, concurrency, cooldown, and validation evidence are satisfied
     - credential, endpoint, object store, DataRef, and worker prerequisites exist
  -> provider adapter
     - provider payload lowering
     - start / wait / stream / cancel / cleanup
     - provider-specific health, preflight, and inventory
     - provider error mapping
  -> canonical output
     - TupleResult
     - TupleError
     - ProviderErrorCode
     - RemoteHandle / cleanup evidence
     - cost, usage, latency, and audit evidence
```

## Responsibility Rule

Provider APIs are execution devices, not decision makers.

Provider adapters may:

- lower a compiled plan into provider payloads
- call provider APIs
- normalize provider responses
- map provider failures
- cancel or cleanup remote work
- perform provider-specific health, preflight, and inventory checks

Provider adapters must not:

- decide routing
- silently swap models
- perform their own provider fallback
- reinterpret tenant policy
- bypass budget policy
- invent DataRef access paths
- collapse provider errors into generic exceptions

## Current Diagnosis

Current Production Go failures are primarily config/environment readiness
failures, not proof that provider abstraction is impossible:

- active route selects local runtime for normal text infer
- forced RunPod smoke points at placeholder endpoint ids
- object store/DataRef prerequisites are absent
- local tuple success is not production cloud GPU evidence

The correct behavior is fail-closed with clear No-Go blockers.

## Refactor Direction

Split provider code by responsibility where practical:

- runtime adapter
- payload lowering
- response normalization
- error mapping
- live catalog/preflight
- cleanup/reaper
- config validation
- billing/cost guard

Keep routing and policy in compiler/dispatcher/admission, not adapters.

## Completion Evidence

A phase touching provider egress must report:

- which provider responsibility moved
- unchanged routing/fallback behavior
- canonical error/result mapping preserved
- cleanup behavior preserved
- focused provider tests run
- whether live provider success was tested, skipped, or environment-gated
