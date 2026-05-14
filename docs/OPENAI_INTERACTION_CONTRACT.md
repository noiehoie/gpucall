# OpenAI Interaction Contract

gpucall's OpenAI-compatible boundary is an external API contract. Internal
routing, tuple state, budget state, and provider retries are implementation
details that must preserve this boundary.

## Source Of Truth

- Vendored OpenAI schema: `third_party/openai/openapi.documented.yml`
- Generated contract snapshot: `gpucall/openai_contract/chat_completions.json`
- Runtime admission: `gpucall/openai_facade/chat_completions.py`

The v2 contract target is Chat Completions compatibility. Unsupported official
fields must fail closed with an OpenAI-shaped error. They must not be ignored,
silently downgraded, or used as gateway routing hints unless explicitly
classified.

## Required Interaction Cases

Acceptance must cover more than request field presence:

- `stream=true` with `response_format`
- stream usage reporting through `stream_options.include_usage`
- malformed or interrupted stream behavior
- `tools`, `tool_choice`, and `parallel_tool_calls`
- `n > 1` multiple choices and usage accounting
- `json_schema` strict and non-strict structured output
- unsupported fields returning stable OpenAI-compatible error shape
- official OpenAI Python SDK parsing requests, responses, and errors

## Error Shape

Unsupported or invalid OpenAI input must return a stable HTTP status and JSON
error shape understandable by OpenAI SDK clients. The failure may include a
gpucall failure artifact, but the public error must not expose provider secrets,
prompt bodies, raw model output, DataRef URIs, or presigned URLs.

## Streaming And Billing

If a stream fails after HTTP 200 and partial chunks, gpucall must define whether
usage is partial, unavailable, committed, released, or refunded. This is not a
provider detail; it is part of the caller contract.
