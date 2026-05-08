# gpucall Product Interface

gpucall v2.0 exposes three interface levels.

## Level 1: OpenAI-Compatible Facade

Use this for migration from existing OpenAI-compatible clients.

Endpoint:

```http
POST /v1/chat/completions
```

Example:

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.environ["GPUCALL_API_KEY"],
    base_url="https://gpucall-gateway.example.internal/v1",
)

response = client.chat.completions.create(
    model="gpucall:auto",
    messages=[{"role": "user", "content": "Return JSON: {\"answer\": 2}"}],
    response_format={"type": "json_object"},
)
```

The facade is intentionally thin. It is for lightweight text requests and
compatibility. Large inputs must use the gpucall SDK DataRef upload path.
The accepted model names in v2.0 are `gpucall:auto` and the compatibility
alias `gpucall:chat`; provider and recipe selection remain gateway-owned.

Structured output validation is returned in:

```http
X-GPUCall-Output-Validated: true
```

`stream: true`, tools, function calling, and Anthropic-compatible `/v1/messages`
are not part of the v2.0 MVP.

## Level 2: gpucall Python SDK

Use this for production gpucall integrations.

The SDK hides:

- inline vs DataRef routing
- presigned upload
- async polling
- structured output validation
- warning and error mapping

Example:

```python
from gpucall_sdk import GPUCallClient

client = GPUCallClient(
    "https://gpucall-gateway.example.internal",
    api_key=os.environ["GPUCALL_API_KEY"],
)

response = client.chat.completions.create(
    model="gpucall:auto",
    messages=[{"role": "user", "content": "Return exactly {\"answer\":2}"}],
    response_format={"type": "json_object"},
    parse_json=True,
)

assert response["output_validated"] is True
print(response["parsed"])
```

The SDK does not send provider or recipe. Governance, recipe selection,
tuple routing, fallback, lease cleanup, and audit remain gateway
responsibilities. Public task endpoints reject caller-controlled routing unless
the gateway is explicitly started with a debug override.

## Level 3: Low-Level Control API

Use `/v2/tasks/sync`, `/v2/tasks/async`, `/v2/tasks/stream`, `/v2/tasks/batch`, and object-store
presign endpoints for internal systems and advanced integrations.

This level exposes `TaskRequest`, `DataRef`, `response_format`, and job polling
directly. Public callers still must not set `recipe` or `requested_tuple`;
those fields are reserved for admin/debug flows. GPU and model selection are
management-side catalog decisions, not caller payload fields.

Do not combine `messages` with `inline_inputs` or `input_refs` in v2.0. Use one
chat message list or use inline/DataRef inputs. Vision requests must include an
image DataRef.
