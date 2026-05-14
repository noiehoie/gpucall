# Control Plane Invariants

This document maps the external OpenAI-compatible contract to internal
deterministic control-plane invariants. Caller-specific incidents may seed
anonymous fixtures, but product code and public documentation must stay generic.

## Invariants

| ID | Invariant | Acceptance |
| --- | --- | --- |
| F1 | Semantic task/intent/mode/input/output contracts lower to a worker wire contract with deterministic transform evidence. | `production_acceptance` reports `F1`. |
| F2 | Static eligibility is separate from live executability. | `production_acceptance` reports `F2/F6`. |
| F3 | Candidate, smoke-only, placeholder, endpoint-missing, or unvalidated tuples cannot enter production routing. | `production_acceptance` reports `F3`. |
| F4 | Provider temporary failures become route state and suppress affected tuple/family choices for concurrent plans. | `production_acceptance` reports `F4`. |
| F5 | Fallback has bounded attempts, family breadth, and wall-clock budget. | `production_acceptance` reports `F5`. |
| F6 | Readiness explains current route state, not only process liveness. | `production_acceptance` reports `F2/F6`. |
| F7 | OpenAI-supported interactions are contract-tested; unsupported fields fail closed with OpenAI-shaped errors. | `production_acceptance` reports `F7`. |
| F8 | Failure artifacts expose stable code, retryability, caller action, and redaction guarantees without secrets. | `production_acceptance` reports `F8`. |
| F9 | Budget states are reserved, committed, released, and refunded as distinct lifecycle states. | `production_acceptance` reports `F9`. |
| F10 | Async jobs expose terminal and late-terminal states without losing billing or cancellation history. | `production_acceptance` reports `F10`. |

## Orthogonal State Axes

The gateway must not collapse independent states into one enum. Route decisions
combine these axes deterministically:

- provider runtime state
- tenant budget state
- workload admission state
- tuple quality state

For example, a provider can be `live_ready` while a tenant is `exhausted`, or a
provider family can be healthy while one tuple is quality-suppressed for strict
schema failures.

## Anonymous Replay Boundary

Synthetic replay fixtures may contain task, mode, input size, content type,
hash shape, schema shape, event timing class, and failure code. They must not
contain tenant names, private paths, provider endpoint ids, API keys, prompt
bodies, raw outputs, DataRef URIs, presigned URLs, or real image/document bytes.
