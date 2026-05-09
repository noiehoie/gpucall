# External System Onboarding Manual

This manual defines the repeatable path for making any external system accept
gpucall without rediscovering the same migration problems for each product.

The goal is not merely "make an API call succeed." The goal is:

- application code stops choosing providers, GPUs, models, tuples, and fallback
  order;
- unsupported workloads produce deterministic intake for administrators instead
  of ad hoc direct-provider fallbacks;
- cold starts, capacity misses, and governance failures are classified correctly;
- no prompts, files, presigned URLs, DataRef URIs, or secrets leak into logs;
- the migration leaves behind tests and reports that the next operator can
  trust.

Use [EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md](EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md)
as the copy-paste prompt for the code agent working inside the external system.

The fastest happy path is:

```bash
gpucall-migrate onboard /path/to/project \
  --source example-caller-app \
  --command "<smallest representative command>"
```

The sections below define what that command should discover, what the
implementation agent must still verify, and when the integration is allowed to
move beyond canary.

## Product Contract

gpucall has three product components:

1. **Gateway runtime**: deterministic admission, recipe selection, tuple
   routing, policy, price freshness, validation evidence, cleanup, and audit.
2. **Caller-side helper**: `gpucall-recipe-draft`, which prepares sanitized
   intent and feedback bundles for unknown or under-supported workloads.
3. **Administrator-side helper**: `gpucall-recipe-admin`, which reviews intake,
   materializes recipe YAML, checks catalog fit, and promotes only validated
   tuples.

External systems should normally send only:

- `task`
- `mode`
- inline input or `DataRef`
- execution preferences such as max tokens or timeout

They should not send:

- `requested_tuple`
- provider names
- GPU names
- direct provider model names as routing selectors
- fallback order
- cleanup decisions
- production promotion decisions

## The Repeatable Onboarding Flow

### 1. Prepare gateway details

Give the external system only the public integration facts it needs:

```bash
export GPUCALL_BASE_URL="https://gpucall-gateway.example.internal"
export GPUCALL_API_KEY="<real token>"
export GPUCALL_RECIPE_INBOX="admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox"
```

Rules:

- never use `GPUCALL_API_KEY=dummy` for integration;
- never commit the token;
- never print the token in completion reports;
- skip live integration tests when the token is absent.

### 2. Inventory all model and GPU paths

Run `gpucall-migrate` if available:

```bash
gpucall-migrate assess /path/to/project --source example-caller-app
gpucall-migrate report /path/to/project --source example-caller-app
```

Also run deterministic source search:

```bash
rg -n "OpenAI|AsyncOpenAI|Anthropic|AsyncAnthropic|Gemini|GenerativeModel|call_llm|chat.completions|messages|model=|provider|gpu|requested_tuple|recipe|gpucall|DataRef|presign|vllm|transformers" /path/to/project
```

The inventory must classify every path:

| Class | Meaning | Action |
| :--- | :--- | :--- |
| `already-gpucall` | already routes through gpucall | verify payload and errors |
| `direct-hosted-api` | OpenAI / Anthropic / Gemini / hosted API | migrate or intentionally leave with documented reason |
| `direct-provider-gpu` | direct GPU provider or worker path | migrate behind gpucall or mark out of scope |
| `local-only` | local model path | keep if intentional |
| `unknown-workload` | no installed gpucall recipe likely fits | submit preflight before production |

### 3. Submit preflight before production

Preflight is not smoke testing. It does not start GPU resources. It gives the
administrator a sanitized statement of the workload class before traffic reaches
production.

Example for translation:

```bash
gpucall-recipe-draft preflight \
  --task infer \
  --mode sync \
  --intent translate_text \
  --business-need "translate customer-visible article text, 500-8000 chars" \
  --content-type text/plain \
  --bytes 8000 \
  --required-model-len 32768 \
  --remote-inbox "$GPUCALL_RECIPE_INBOX" \
  --source example-caller-app
```

Example for document vision:

```bash
gpucall-recipe-draft preflight \
  --task vision \
  --mode sync \
  --intent understand_document_image \
  --business-need "answer questions about page images without exposing raw images to hosted AI APIs" \
  --content-type image/png \
  --bytes 2000000 \
  --context-budget-tokens 9000 \
  --classification confidential \
  --remote-inbox "$GPUCALL_RECIPE_INBOX" \
  --source example-caller-app
```

Administrator-side materialization is separate:

```bash
gpucall-recipe-admin inbox list --inbox-dir /path/to/inbox
gpucall-recipe-admin inbox materialize --inbox-dir /path/to/inbox --output-dir config/recipes --accept-all
gpucall-recipe-admin inbox readiness --inbox-dir /path/to/inbox --config-dir config
```

Billable validation and production activation remain explicit promotion steps.
Do not hide provider spend inside recipe creation.

### 4. Implement the application adapter

Prefer a single local wrapper around gpucall. That wrapper should own:

- base URL and API key loading;
- endpoint selection: `/v2/tasks/sync`, `/v2/tasks/async`, `/v2/tasks/stream`;
- OpenAI-facade compatibility if the repository uses OpenAI SDK conventions;
- DataRef upload helpers;
- error classification;
- redaction.

The wrapper should not expose provider, GPU, tuple, or fallback selection to the
rest of the application.

Correct direct task payload:

```json
{
  "task": "infer",
  "mode": "sync",
  "inline_inputs": {
    "prompt": {
      "value": "Say ok only.",
      "content_type": "text/plain"
    }
  },
  "max_tokens": 8
}
```

Correct OpenAI facade setup:

```python
from openai import OpenAI

client = OpenAI(
    base_url=f"{base_url.rstrip('/')}/v1",
    api_key=api_key,
)
```

### 5. Decide sync, async, or stream

Use sync only when the caller timeout is comfortably above the gpucall plan's
expected runtime and cold-start range.

Use async when:

- cold start can exceed the caller's HTTP timeout;
- the workload is large or bursty;
- user-facing code can poll job status;
- retrying the whole request would duplicate expensive work.

Use stream when:

- caller UX benefits from incremental output;
- the worker contract supports streaming;
- the client handles SSE heartbeat lines.

The caller-side timeout boundary is explicit. If gpucall advertises or returns a
minimum timeout and the caller chooses a shorter timeout, that timeout is a
caller-side integration issue. It should not be counted as provider failure by
default.

### 6. Classify failures correctly

| Condition | Meaning | Correct response |
| :--- | :--- | :--- |
| `401` | missing or wrong API key | fail with configuration error |
| `422 NO_AUTO_SELECTABLE_RECIPE` | gateway cannot honestly describe this workload with installed recipes | submit `gpucall-recipe-draft intake` or preflight |
| `503 NO_ELIGIBLE_TUPLE` | recipe exists, but no eligible production tuple is currently available | treat as governance/capacity state, not direct provider failure |
| timeout during cold start | caller did not wait long enough or should use async | do not open provider circuit by default |
| malformed output / business validator failure after `200 OK` | quality feedback, not necessarily routing failure | submit `gpucall-recipe-draft quality` |
| provider 5xx without governance code | provider/runtime failure | retry/circuit according to local policy |

### 7. Redaction rules

Never log:

- `GPUCALL_API_KEY`
- Authorization header
- prompt body
- message body
- document text
- image bytes
- provider raw output
- DataRef URI
- presigned upload or download URL
- provider API key

Completion reports should write:

```text
GPUCALL_API_KEY=<set>
upload_url=<redacted>
data_ref=<redacted>
prompt=<redacted>
```

### 8. Required tests

Every integration should leave tests for:

- payload includes `task` and `mode`;
- payload excludes `recipe`;
- payload excludes `requested_tuple`;
- payload excludes provider, GPU, tuple, and provider model selectors;
- OpenAI messages are converted correctly when using `/v2/tasks/*`;
- `input_refs` is a list;
- large input uses DataRef / presigned upload;
- API key is required;
- API key and presigned URLs are redacted;
- `NO_AUTO_SELECTABLE_RECIPE` does not open a provider circuit;
- `NO_ELIGIBLE_TUPLE` does not open a provider circuit;
- timeout is not counted as provider failure by default;
- async completion without inline result is handled gracefully;
- stream heartbeat is not treated as an error.

### 9. Canary protocol

Do not switch the whole production pipeline at once.

1. Run one smallest representative path.
2. Record call count, status count, governance errors, timeouts, and latency.
3. Confirm no prompt, secret, DataRef URI, or presigned URL appears in logs.
4. Confirm circuit-breaker counters changed only for true provider failures.
5. Expand to the next workload class.

If available:

```bash
gpucall-migrate canary /path/to/project \
  --source example-caller-app \
  --command "<smallest representative command>"
```

### 10. Go / No-Go checklist

Go only when:

- all direct provider paths are either migrated or intentionally documented;
- all unknown workloads have preflight submissions;
- no application payload contains `requested_tuple`;
- no application payload contains provider or GPU selectors;
- integration tests pass;
- canary succeeded or produced only expected governance failures;
- cold-start timeout behavior is classified correctly;
- logs are redacted;
- the completion report includes command outputs.

No-Go when:

- direct hosted AI fallback remains undocumented;
- unknown workloads are sent to gpucall without preflight;
- caller code still chooses GPU/provider/model as route control;
- API key or presigned URL appears in logs;
- timeout increments provider circuit counters by default;
- tests only cover the happy path.

## Final Report Template

```text
1. Inventory
   - total LLM/Vision/GPU paths:
   - already gpucall:
   - migrated:
   - intentionally retained direct paths:
   - unknown workloads submitted as preflight:

2. Changed files

3. Preflight submissions
   - local files:
   - remote inbox paths:

4. Tests
   - command:
   - output:

5. Canary
   - command:
   - call count:
   - HTTP 200:
   - NO_AUTO_SELECTABLE_RECIPE:
   - NO_ELIGIBLE_TUPLE:
   - timeout:
   - circuit breaker delta:

6. Redaction
   - API key redacted:
   - prompt redacted:
   - presigned URLs redacted:
   - DataRef URIs redacted:

7. Remaining risk

8. Go / No-Go
```
