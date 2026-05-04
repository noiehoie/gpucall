# Routing Policy

gpucall is a deterministic governance router, not a Modal-only proxy.

The caller normally sends task intent, mode, and input references. The gateway
selects a recipe and provider chain from policy, recipe constraints, request
weight, provider capabilities, and observed provider health.

Public callers must not set `recipe`, `requested_provider`, or `requested_gpu`.
Those fields are reserved for admin/debug flows and are rejected by the public
task endpoints unless `GPUCALL_ALLOW_CALLER_ROUTING=1` is explicitly enabled.

## Deterministic Selection

1. Filter recipes by task, mode, MIME type, declared DataRef bytes, and token
   budget.
2. Choose the smallest capable matching recipe. Heavy requests are allowed to
   move to larger recipes instead of pinning all traffic to the largest GPU.
3. Estimate required model length from inline bytes, DataRef bytes, max output
   tokens, and the policy tokenizer safety multiplier.
4. Filter providers whose `max_model_len`, `vram_gb`, modes, and data
   classification cannot satisfy that request.
5. Rank the remaining provider list with `ObservedRegistry`.

## Input Contract Preservation

Requests that combine `messages` with `inline_inputs` or `input_refs` are rejected in v2.0. This prevents provider adapters from silently dropping one input class while preserving another. Text and DataRef inputs may be combined only on provider paths that declare both contracts and serialize both classes deterministically.

`vision` requires an image `DataRef` with a content type beginning with `image/`. A provider must declare the `image` input contract to receive vision routes. A text-only worker that hex-encodes image bytes is not a vision provider.

Recipe system prompts are gateway policy transforms. The compiled plan exposes an audit-safe `system_prompt_transform` with the recipe, source field, byte count, and hash so callers can see that the provider-facing messages were governed by recipe policy.

## Production Provider Hygiene

Production auto-selected recipes do not name providers. They become runnable
only when at least one configured production provider satisfies the recipe's
declared requirements.

- `local-echo` is smoke-only.
- fixed-response RunPod endpoints such as `Hello World` are smoke-only and must
  be named accordingly.
- placeholder RunPod Flash or Hyperstack workers that do not run a real model
  must not be eligible for production auto-routing.
- Modal text vLLM workers are not eligible for vision routing unless replaced by a real multimodal worker that preserves image semantics.

Adding RunPod, Hyperstack, or another provider to production routing is valid
only after its ProviderSpec describes a real worker model and its worker returns
real model output for the same TaskRequest contract.

`model:` is treated as the production-readiness declaration for external GPU
workers. A provider without `model:` is considered provisionable/testable but
not a production inference target.

## Caller Notification

When a request routes to a remote worker, the API returns:

```text
X-GPUCall-Warning: remote_worker_cold_start_possible
```

When the worker must fetch DataRefs directly from object storage, the warning
also includes:

```text
dataref_worker_fetch
```

This keeps the caller informed that the gateway has accepted the request and is
dispatching it to a remote worker where cold start latency may dominate.
