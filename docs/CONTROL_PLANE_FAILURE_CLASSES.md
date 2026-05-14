# Control-Plane Failure Classes

This ledger defines product-level failure classes for gpucall control-plane
work. It intentionally avoids caller-specific logs and tenant vocabulary.

| ID | Failure class | Product issue | Acceptance criterion |
| --- | --- | --- | --- |
| F1 | Semantic contract vs wire contract | A caller's semantic request can be rejected because a worker declares a different transport shape. | The compiler can route compatible semantic input through declared deterministic transforms, and the plan records transform evidence. |
| F2 | Static eligibility vs live executability | Catalog compatibility can be mistaken for current ability to execute. | Readiness and routing distinguish configured, eligible, live-ready, suppressed, and in-flight states. |
| F3 | Catalog candidate vs production route | A YAML entry can be treated as production capacity before endpoint, validation, and policy evidence exist. | Candidate tuples never enter production routing without endpoint configuration and validation evidence. |
| F4 | Provider temporary failure as routing state | Provider capacity and transient failures can remain local to one request instead of suppressing bad choices for concurrent plans. | Retryable provider failures update tuple/family suppression and affect concurrent route compilation or admission. |
| F5 | Fallback storm | A request can walk too many failing tuples or provider families. | Fallback has bounded per-request attempts, per-family attempts, and total wall-clock budget. |
| F6 | Readiness does not mean executability | Process health can be reported as ready while no production route can run a workload. | Intent readiness reports eligible count, live-ready count, suppression, queue/admission state, recommended mode, and caller action. |
| F7 | Partial OpenAI compatibility | Hand-written facade behavior can silently diverge from the OpenAI contract. | Supported OpenAI fields are contract-tested against vendored/generated OpenAI schema or SDK-oracle tests; unsupported fields fail closed. |
| F8 | Responsibility boundary is unclear | Caller, gateway, and provider responsibilities can be blurred in error responses. | Failure artifacts expose failure kind, stable code, retryability, caller action, and redaction guarantee without provider secrets. |
| F9 | Budget lifecycle ambiguity | Reserved, committed, released, and refunded cost states can be conflated. | Non-executed temporary failures release reservations; executed work commits; late async outcomes remain auditable. |
| F10 | Async lifecycle ambiguity | Caller timeout, job timeout, completion, cancellation, and expiration can collapse into one error shape. | Async jobs expose explicit states, late completion remains queryable, cancellation is explicit, and billing state is visible. |

## Contamination Rule

The source incident is not a product requirement. Remediation work must convert
each observed problem into one of the failure classes above or add a new generic
failure class with an acceptance criterion.
