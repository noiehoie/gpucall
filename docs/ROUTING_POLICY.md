# Routing Policy

gpucall is a deterministic governance router, not a Modal-only proxy.

The caller normally sends task intent, mode, and input references. The gateway
selects a recipe and tuple chain from policy, recipe constraints, request
weight, tuple capabilities, and observed tuple health.

Public callers must not set `recipe` or `requested_tuple`. Those fields are
for operator/debug flows only and are rejected by the public task endpoints
unless `GPUCALL_ALLOW_CALLER_ROUTING=1` is explicitly enabled. GPU selection is
not a caller-facing concept; GPU, region, price, stock, model, and engine belong
to the gateway catalogs.

## Deterministic Selection

1. Filter recipes by task, mode, MIME type, declared DataRef bytes, and token
   budget.
2. Choose the smallest capable matching recipe. Heavy requests are allowed to
   move to larger recipes instead of pinning all traffic to the largest GPU.
3. Estimate required model length from inline bytes, DataRef bytes, max output
   tokens, and the policy tokenizer safety multiplier.
4. Filter tuples whose `max_model_len`, `vram_gb`, modes, and data
   classification cannot satisfy that request.
5. Apply deterministic cost policy. Budget fields are optional; when no explicit
   budget is present, high-cost tuples are not auto-selected once their
   estimated cold-start + runtime + idle + standing endpoint cost exceeds the
   policy threshold.
6. Rank the remaining provider list with `ObservedRegistry`.

## Input Contract Preservation

Requests that combine `messages` with `inline_inputs` or `input_refs` are rejected in v2.0. This prevents provider adapters from silently dropping one input class while preserving another. Text and DataRef inputs may be combined only on provider paths that declare both contracts and serialize both classes deterministically.

`vision` requires an image `DataRef` with a content type beginning with `image/`.
An execution tuple must declare the `image` input contract to receive vision
routes. A text-only worker that hex-encodes image bytes is not a vision tuple.

Recipe system prompts are gateway policy transforms. The compiled plan exposes an audit-safe `system_prompt_transform` with the recipe, source field, byte count, and hash so callers can see that the provider-facing messages were governed by recipe policy.

Long-form `infer` traffic is split by deterministic recipe capacity. The standard profile includes `text-infer-large` at 65K, `text-infer-exlarge` at 131K, and `text-infer-ultralong` at 524K model length. These recipes require a provider that declares a matching context window; if they disappear from authenticated `/readyz/details`, the gateway is misconfigured rather than merely capacity constrained.

`vision` workers must not pass gateway system prompts as user image prompts. System prompts are governance transforms and may be audited, but the model-facing vision question must come from the caller's inline prompt or non-system chat messages.

## Production Provider Hygiene

Production auto-selected recipes do not name tuples. They become runnable
only when at least one configured production provider satisfies the recipe's
declared requirements.

- `local-echo` is smoke-only.
- fixed-response RunPod endpoints such as `Hello World` are smoke-only and must
  be named accordingly.
- placeholder RunPod Flash or Hyperstack workers that do not run a real model
  must not be eligible for production auto-routing.
- Modal text vLLM workers are not eligible for vision routing unless replaced by a real multimodal worker that preserves image semantics.

Adding RunPod, Hyperstack, or another vendor-backed surface to production
routing is valid only after the execution tuple describes a real worker model
and its worker returns real model output for the same TaskRequest contract.

`model:` is treated as the production-readiness declaration for external GPU
workers. A tuple without `model:` is considered provisionable/testable but not a
production inference target.

## Cost Metadata

Tuple YAML must describe billing mechanics that affect routing:

- `scaledown_window_seconds`: billed idle/runtime window after useful work.
- `min_billable_seconds`: minimum billable execution unit.
- `billing_granularity_seconds`: billing rounding interval.
- `standing_cost_per_second` and `standing_cost_window_seconds`: always-on or
  standby workers that accrue cost independent of a single request.
- `endpoint_cost_per_second` and `endpoint_cost_window_seconds`: endpoint or
  fixed service cost that must be budgeted with the route.

Official sources used for the current defaults:

- Modal billing: no minimum usage-time increments; autoscaler defaults to scale
  to zero and `scaledown_window` defaults to 60 seconds. Idle containers can
  still be billed while they are kept warm.
  https://modal.com/docs/guide/billing
  https://modal.com/docs/guide/cold-start
- RunPod Serverless: workers are billed from start until stop, rounded to the
  nearest second; idle timeout duration is part of compute cost and defaults to
  5 seconds. Flex idle state is not billed; Active workers run continuously.
  https://docs.runpod.io/serverless/pricing
  https://docs.runpod.io/serverless/workers/overview
- Hyperstack: public pricing states billing cycles are accurate to the minute.
  https://www.hyperstack.cloud/

Run:

```bash
gpucall cost-audit
gpucall cost-audit --live
```

The static report lists configured billing metadata and missing fields. The live
report queries Modal billing/app state, RunPod endpoint health, and Hyperstack
VM inventory when credentials and provider tools are available.

RunPod warm workers are treated as standing endpoint cost. Static tuple config
may describe the intended endpoint runtime under `provider_params.endpoint_runtime`.
If that declaration sets `workersMin`, `workers_min`, `workersStandby`, or
`workers_standby` above zero, config validation requires standing-cost metadata
and an explicit `provider_params.cost_approval` record. The live cost audit also
queries the RunPod endpoint inventory and blocks production launch when the live
endpoint has unapproved warm workers, even if the tuple YAML forgot to declare
them. If RunPod credentials are configured, the live audit also checks account
endpoints that are not declared in gpucall config and blocks any unmanaged
endpoint with standing workers. `workersMax` alone is not standing cost; it is
a capacity ceiling.

For startup-time live catalog gating, set `GPUCALL_LIVE_CATALOG_ON_STARTUP=1`.
Tuples blocked by live catalog evidence are opened in the observed registry
before routing, so a workload that normally used an unavailable VM tuple can
deterministically fall through to another eligible production tuple such as a
managed endpoint.

Live catalog evidence is not free-form text. It is reduced to deterministic
observations: price, stock, endpoint, credential, and contract. Unavailable stock
blocks the tuple for routing. A live price overrides configured
`cost_per_second` only when the provider observation contains a parseable
per-second or hourly price; otherwise the configured price remains the routing
price.

## Caller Notification

When a request routes to a remote worker, the API returns:

```text
X-GPUCall-Warning: remote_worker_cold_start_possible
X-GPUCall-Timeout-Seconds: 600
X-GPUCall-Lease-TTL-Seconds: 900
X-GPUCall-Min-Client-Timeout-Seconds: 600
```

When the worker must fetch DataRefs directly from object storage, the warning
also includes:

```text
dataref_worker_fetch
```

This keeps the caller informed that the gateway has accepted the request and is
dispatching it to a remote worker where cold start latency may dominate.

The timeout boundary is explicit. gpucall is responsible for deterministic
routing, dispatch, fallback, cleanup, and provider wait behavior until
`X-GPUCall-Timeout-Seconds`. A caller that uses a shorter HTTP, SDK, reverse
proxy, or job-runner timeout than `X-GPUCall-Min-Client-Timeout-Seconds` has
chosen to abandon the accepted request early; that client-side abandonment is
outside the gateway SLA. Callers that cannot wait for the advertised sync
timeout should use `mode=async` and poll job state instead of lowering the
client timeout.

Provider temporary-unavailable codes are immediate failover signals. When a
tuple returns any `PROVIDER_*` temporary code declared by
`gpucall.domain.ProviderErrorCode`, the dispatcher records the observation,
runs remote cleanup/cancel for the handle, and advances to the next eligible
tuple in the already compiled deterministic chain. These codes include provider
resource exhaustion, endpoint capacity misses, provisioning stock misses, queue
saturation, worker initializing/throttling/unhealthy states, provider/job/poll
timeouts, cancellation, preemption, maintenance, upstream unavailability, rate
or quota limits, region unavailability, image/model loading delay, concurrency
limits, lease expiry, stale accepted jobs, and unclassified retryable provider
errors. Application code must not choose the alternate provider itself; the
gateway owns this failover.

The compiled tuple chain is not treated as live capacity. Before each tuple
start, runtime admission checks tuple concurrency, provider-family concurrency,
task/intent/mode workload-scope concurrency, provider-family cooldown, and
per-request fallback attempt caps. A provider temporary failure always suppresses
the failed tuple for a bounded cooldown so concurrent plans do not stampede
through the same failing route. Provider-family suppression is narrower: it is
used only for failures that describe account, control-plane, maintenance, rate,
quota, or other family-wide conditions as declared by
`ProviderErrorClass.suppress_provider_family`. Tuple-local capacity misses such
as `PROVIDER_RESOURCE_EXHAUSTED` and `PROVIDER_CAPACITY_UNAVAILABLE` do not
suppress the whole provider family by default, so another tuple in the same
family can still be tried when policy and per-request family-attempt caps allow
it. Codes with `fallback_eligible: false`, such as `PROVIDER_QUOTA_EXCEEDED` and
`PROVIDER_CANCELLED`, stop blind fallback and return a structured
`failure_artifact` instead. Admission rejection is a routing state, not an
application parse error.

In production-like deployments with `GPUCALL_DATABASE_URL=postgresql://...`,
admission leases and provider-family suppression state are stored in Postgres
beside jobs and idempotency records. This makes live capacity control shared
across gateway processes/containers. Without `GPUCALL_DATABASE_URL`, admission
falls back to in-process memory for local development and tests.

Operators and callers that need a live answer should not infer it from
`/readyz`. Use `/v2/readiness/intents/{intent}` or `gpucall readiness` to compare
static eligible tuples with live-ready tuples and to see suppressed families,
in-flight counts, recommended mode, and caller action.

## Controlled Runtime Preference

gpucall treats operator-controlled execution as a first-class execution surface.
This is not "the caller's local machine." A controlled runtime is an execution
endpoint the gpucall operator declares in `config/runtimes/*.yml`, ties to a
surface/worker tuple with `controlled_runtime_ref`, and validates before
production routing. Boundaries are explicit: `gateway_host`, `private_network`,
or `site_network`.

If a controlled runtime satisfies the recipe, model, engine, data
classification, and policy constraints, routing may prefer it over leased cloud
GPU capacity because that is the honest low-exposure path.

The built-in `local-openai-compatible` adapter is provider-neutral. It can point
at ds4-server, llama.cpp server, local vLLM, or another OpenAI-compatible chat
endpoint. It supports inline text/chat requests only. It intentionally rejects
`DataRef` inputs so the gateway does not download or forward object bytes. Use a
dedicated worker contract when DataRef fetching must happen inside an approved
local execution boundary.

The dedicated local worker contract is `local-dataref-openai-worker`. That
adapter does not fetch DataRefs in the gateway process. It forwards the
worker-readable plan and DataRef metadata to a separately running local worker
endpoint. The worker process fetches HTTP(S) DataRefs, validates declared byte
length and SHA256, rejects non-text inputs, calls its configured local
OpenAI-compatible `/v1/chat/completions` server, and returns a gpucall
`TupleResult`. This keeps the gateway data-byte-less while allowing controlled
local runtimes to handle large text inputs without leasing cloud GPU capacity.

The registration path for an existing ds4/OpenAI-compatible endpoint is:

```bash
gpucall runtime add-openai --name site-gpu-ds4 --endpoint http://site-gpu-01.internal:18181 --dataref-worker
gpucall runtime validate --name site-gpu-ds4
gpucall validate-config
```

Discovery may find candidates, but discovery alone never makes a runtime
production-routable. Operator declaration, config validation, and validation
evidence remain required.
