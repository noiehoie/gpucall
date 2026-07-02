# Agent-Native Execution Layer (v2.5)

gpucall treats AI agents as first-class callers. An agent must be able to
estimate, submit, poll, cancel, and classify failures without asking a human
and without knowing provider mechanics.

This document is the tracked contract for that surface.

## Surfaces

| Surface | Transport | Purpose |
| --- | --- | --- |
| `POST /v2/estimate` | HTTP | Non-billable pre-execution estimate: compiled route + cost, no budget reservation, no dispatch. |
| `POST /v2/tasks/sync` / `async` / `stream` | HTTP | Governed execution. |
| `GET /v2/jobs/{job_id}` / `POST /v2/jobs/{job_id}/cancel` | HTTP | Async progress polling and cancellation. |
| `GET /v2/failure-taxonomy` | HTTP | Deterministic failure/retry taxonomy with caller actions and owners. |
| `GET /readyz/details`, `GET /v2/readiness/intents/{intent}` | HTTP | Machine-readable readiness. |
| `gpucall-mcp` | MCP stdio | Tool interface for agent runtimes (Claude Code, Codex, Gemini CLI, custom agents). |
| `gpucall_sdk` `estimate()` / `infer()` / `vision()` / `poll_job()` | Python | Programmatic caller SDK. |

## Estimate Before Billable Work

`POST /v2/estimate` accepts the same `TaskRequest` grammar as `/v2/tasks/*`.
It compiles the governed route deterministically and returns:

- the selected recipe and tuple chain (`plan`)
- `estimated_cost_usd` and `budget_reservation_usd`
- the full `cost_estimate` breakdown (billing model, billable seconds, price freshness)
- `billable: false`, `budget_reserved: false`

Governance failures (no recipe, no tuple, policy denied, invalid contract) are
classified exactly like the billable path — the estimate is never weaker than
the real gate. Nothing is executed and no provider is contacted.

SDK: `client.estimate(prompt=..., intent=...)` on both `GPUCallClient` and
`AsyncGPUCallClient`.

## Failure And Retry Taxonomy

`GET /v2/failure-taxonomy` returns:

- `provider_errors`: every `ProviderErrorCode` with `retryable`,
  `fallback_eligible`, `caller_action`, and provider-family suppression flags.
- `governance_failures`: `no_recipe`, `no_tuple`, `policy_denied`,
  `input_contract`, `tenant_budget` with HTTP status,
  `retryable_without_change`, `caller_action`, and `owner`.
- `retry_semantics`: idempotency-key reuse rules and caller circuit-breaker
  scoping (`task:intent:mode:transport`, never process-global).

Agents use this to decide the next step after any failure without human relay.
Failure responses themselves carry a `failure_artifact` with
`failure_kind`, `caller_action`, `capability_gap`, and a redaction guarantee.

## MCP Tool Interface

`gpucall-mcp` is installed with the gpucall package. It is a deterministic thin
adapter: every tool maps 1:1 to a gateway endpoint; no routing, retry policy,
or budget decision happens inside the MCP layer.

Configuration (environment):

- `GPUCALL_GATEWAY_URL`: gateway base URL (required)
- `GPUCALL_API_KEY`: caller tenant key (optional; sent as a Bearer header, never echoed)
- `GPUCALL_MCP_TIMEOUT_SECONDS`: HTTP timeout, default 630 (covers cold-start-capable routes)

Registration example for Claude Code:

```bash
claude mcp add gpucall \
  --env GPUCALL_GATEWAY_URL=http://gateway.example.internal:18088 \
  --env GPUCALL_API_KEY=... \
  -- gpucall-mcp
```

Tools:

| Tool | Gateway endpoint |
| --- | --- |
| `gpucall_estimate` | `POST /v2/estimate` |
| `gpucall_submit_task` | `POST /v2/tasks/sync` or `/v2/tasks/async` |
| `gpucall_job_status` | `GET /v2/jobs/{job_id}` |
| `gpucall_cancel_job` | `POST /v2/jobs/{job_id}/cancel` |
| `gpucall_readiness` | `GET /readyz/details` |
| `gpucall_failure_taxonomy` | `GET /v2/failure-taxonomy` |

`mode=stream` is rejected over MCP; agents use sync or async.

Tool results wrap the gateway response as
`{"status_code": ..., "body": ...}` in a text content block, with
`isError: true` for HTTP >= 400 and for gateway-unreachable failures, which are
returned as bounded errors with a `caller_action`.

## Boundaries

- The MCP server and estimate endpoint make no control decisions. Recipe,
  tuple, provider, price, validation, and budget decisions stay inside the
  deterministic gateway.
- Secrets never cross the tool boundary: API keys, presigned URLs, DataRef
  URIs, and provider raw output do not appear in tool listings, tool errors,
  or logs.
- Unknown, stale, unvalidated, over-budget, or provider-ambiguous states keep
  failing closed; the agent surface only makes those states machine-readable.
