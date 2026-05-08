# Observability

MVP controls:

- `/metrics` returns gateway request counters, recent average latency, and provider registry scores.
- `gpucall registry show` prints provider success/latency/cost baselines.
- `gpucall audit tail` shows immutable audit events.
- Docker logs are the primary structured runtime log stream for MVP.

Suggested alert rules:

- `/readyz` is not ready for 2 consecutive checks.
- `gpucall audit verify` returns `valid: false`.
- Provider success rate drops below 0.95 after at least 20 samples.
- Any cleanup/reconciliation failure appears in audit events.
- Cost dashboard exceeds the configured provider quota.

Dashboard panels:

- Gateway request count by route/status.
- Average latency from `/metrics`.
- Provider success rate and p50 latency from `gpucall registry show`.
- Job state counts from `gpucall jobs`.
- Object store ready status.

Cost values in the registry are estimates derived from configured `cost_per_second`, live catalog price observations when available, and observed runtime. They are routing signals, not provider invoices. Reconcile them with provider billing dashboards during live validation and post-launch review.

Async jobs are durable for status/result storage, but in-flight execution is process-bound in v2.0. On gateway restart, pending/running jobs are expired rather than replayed with persisted input payloads.
