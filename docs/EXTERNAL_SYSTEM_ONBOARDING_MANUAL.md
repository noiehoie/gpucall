# External System Onboarding Manual

This manual defines the repeatable path for making any external system accept
gpucall without rediscovering the same migration problems for each product.

The goal is not merely "make an API call succeed." The goal is:

- application code stops choosing providers, GPUs, models, tuples, and fallback
  order;
- unsupported workloads produce deterministic intake for administrators instead
  of ad hoc direct-provider fallbacks;
- direct hosted-AI fallback is disabled by default in production;
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

External systems do not need to clone the gpucall gateway repository. The
caller-side helper is distributed as the SDK wheel:

```bash
uv tool install https://github.com/noiehoie/gpucall/releases/download/v2.0.8/gpucall_sdk-2.0.8-py3-none-any.whl
gpucall-recipe-draft --help
```

`gpucall-recipe-draft` and `gpucall_sdk` come from this caller SDK wheel.
`gpucall-migrate` is optional and belongs to the gateway package; if it is not
already available, use deterministic `rg` inventory instead of installing the
gateway package.

This manual is intentionally strict. `Conditional Go` is not an allowed final
state. If live canary is skipped, required preflight intake is only generated but
not submitted, direct hosted-AI fallback remains enabled by default, or DataRef
rules are not enforced, the result is `No-Go`.

Do not use HTTP 500 as a proxy for a missing recipe or missing provider. If a
recipe is absent, the gateway should return `422 NO_AUTO_SELECTABLE_RECIPE`; if
no eligible production tuple exists, it should return `503 NO_ELIGIBLE_TUPLE`.
An opaque `500 Internal Server Error` is a gateway-side internal error until a
gateway operator or gateway logs prove a more specific cause.

For image and file workflows, DataRef support is not optional. A system with
vision or file inputs is not considered migrated until those inputs have a
production path through `/v2/tasks/*` with `input_refs` or an explicitly
documented gpucall SDK DataRef path. OpenAI-facade base64 image/file payloads
are dev-only experiments and do not satisfy production onboarding.

When implementing DataRef manually, use the live gateway OpenAPI schema as the
source of truth. The v2 upload handshake is:

1. compute the file byte length and SHA-256 digest;
2. `POST /v2/objects/presign-put` with `name`, `bytes`, `sha256`, and
   `content_type`;
3. `PUT` the exact bytes to the returned `upload_url`;
4. pass the returned `data_ref` object unchanged in `input_refs`.

`input_refs` is a list of DataRef objects. It is not a list of strings, and
there is no caller request field named `data_ref` for task input. A DataRef uses
the `uri` field:

```json
{
  "uri": "s3://bucket/key",
  "sha256": "<64 hex chars>",
  "bytes": 1234,
  "content_type": "image/png"
}
```

Do not use obsolete examples such as `/v2/upload/presign` or
`{"data_ref": "..."}` for task input.

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
- small non-confidential inline text or `DataRef`
- execution preferences such as max tokens or timeout

They should not send:

- `requested_tuple`
- provider names
- GPU names
- direct provider model names as routing selectors
- fallback order
- direct hosted-AI fallback after gpucall is configured
- cleanup decisions
- production promotion decisions

## The Repeatable Onboarding Flow

### 1. Prepare gateway details

The gpucall administrator controls caller-facing gateway API key delivery.
External systems do not perform unauthorized self-registration, scrape keys
from the gateway repository, or reuse provider credentials. See
[GATEWAY_API_KEYS.md](GATEWAY_API_KEYS.md) for the operator procedure.

There are exactly two valid key-delivery routes:

1. **Operator-issued secret**: the administrator creates a tenant-scoped
   gateway key and passes it through the organization's approved secret channel.
2. **Operator-authorized trusted bootstrap**: the administrator explicitly
   enables trusted bootstrap for a CIDR or host allowlist, and the trusted
   internal system requests its own tenant-scoped key from the gateway once.

Trusted bootstrap is not caller-controlled registration. It is an administrator
policy that delegates delivery to machines inside the configured trust scope.

Give the external system only the integration facts it needs:

```bash
export GPUCALL_BASE_URL="https://gpucall-gateway.example.internal"
export GPUCALL_API_KEY="<real token>"
export GPUCALL_RECIPE_INBOX="admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox"
```

Rules:

- `GPUCALL_API_KEY` must be a gateway key issued for this external system or
  tenant;
- provider credentials from gpucall `credentials.yml` are not gateway API keys
  and must never be handed to callers;
- if trusted bootstrap is enabled and the operator instructs the system to use
  it, a trusted internal system may obtain its own key from
  `POST /v2/bootstrap/tenant-key`;
- trusted bootstrap response handling is strict: `200` means store the returned
  key, `403` means the system is outside the trusted scope, `409` means a key
  already exists and must not be reprinted, and `500` is a gateway-side
  internal error rather than a caller-side condition;
- administrators can inspect or enable this mode with
  `gpucall admin automation-status` and `gpucall admin automation-configure`;
- never use `GPUCALL_API_KEY=dummy` for integration;
- never call `/v2/bootstrap/tenant-key` unless the operator explicitly selected
  trusted bootstrap for this system;
- never commit the token;
- never print the token in completion reports;
- missing `GPUCALL_BASE_URL` or `GPUCALL_API_KEY` must fail closed in the
  application wrapper;
- if live integration tests cannot run because credentials are absent, the final
  status is `No-Go: live canary skipped`.

### 2. Inventory all model and GPU paths

Run `gpucall-migrate` if available:

```bash
gpucall-migrate assess /path/to/project --source example-caller-app
gpucall-migrate report /path/to/project --source example-caller-app
```

If `gpucall-migrate` is unavailable, do not clone or install the gateway package
to obtain it. Continue with the `rg` scan below and the report template in this
manual.

Also run deterministic source search:

```bash
rg -n "OpenAI|AsyncOpenAI|Anthropic|AsyncAnthropic|Gemini|GenerativeModel|call_llm|chat.completions|messages|model=|provider|gpu|requested_tuple|recipe|gpucall|DataRef|presign|vllm|transformers" /path/to/project
```

The inventory must classify every path:

| Class | Meaning | Action |
| :--- | :--- | :--- |
| `already-gpucall` | already routes through gpucall | verify payload and errors |
| `direct-hosted-api` | OpenAI / Anthropic / Gemini / hosted API | migrate behind gpucall, or keep only as dev/test opt-in that is disabled by default and unavailable in production |
| `direct-provider-gpu` | direct GPU provider or worker path | migrate behind gpucall or mark out of scope |
| `local-only` | local model path | keep if intentional |
| `unknown-workload` | no installed gpucall recipe likely fits | submit preflight before production |

Do not write "intentionally retained" for a production direct hosted-AI fallback.
The only acceptable retained hosted-AI path is a local development or test path
behind an explicit opt-in flag, with tests proving production fails closed.

### 3. Submit preflight before production

Preflight is not smoke testing. It does not start GPU resources. It gives the
administrator a sanitized statement of the workload class before traffic reaches
production.

Use terms precisely:

- `submitted`: intake reached the approved local or remote inbox and the path or
  request id is recorded.
- `generated-only`: a command, script, or JSON artifact exists, but no approved
  inbox received it.

Generated-only preflight is not enough for `Go`.

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

Production default must be fail-closed:

- missing `GPUCALL_BASE_URL` or `GPUCALL_API_KEY` raises a configuration error;
- gpucall governance failures do not trigger hosted-AI fallback;
- direct OpenAI / Anthropic / Gemini fallback is disabled by default;
- if fallback is retained for local development or tests, it requires an
  explicit opt-in environment variable and is rejected or ignored in production
  mode.

Images and files must use DataRef / presigned upload in the production path.
Confidential inputs and text above the configured small-text inline limit must
also use DataRef / presigned upload. Inline input is for small
non-confidential text only. If a required DataRef helper cannot be implemented,
the path must remain unmigrated, fail closed with a clear `DataRef required`
error, and the final status must be `No-Go`.

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

Correct manual DataRef upload payload:

```json
POST /v2/objects/presign-put
{
  "name": "page.png",
  "bytes": 2000000,
  "sha256": "<64 hex chars>",
  "content_type": "image/png"
}
```

The response contains `upload_url`, `method`, and `data_ref`. Upload the bytes
to `upload_url`, then submit the task with the returned object:

```json
{
  "task": "vision",
  "mode": "sync",
  "input_refs": [
    {
      "uri": "s3://bucket/gpucall/tenants/example/page.png",
      "sha256": "<64 hex chars>",
      "bytes": 2000000,
      "content_type": "image/png"
    }
  ],
  "max_tokens": 512
}
```

Do not transform the returned DataRef into a string. Keep `uri`, `sha256`,
`bytes`, `content_type`, and any other returned DataRef fields intact.

Correct OpenAI facade setup:

```python
from openai import OpenAI

client = OpenAI(
    base_url=f"{base_url.rstrip('/')}/v1",
    api_key=api_key,
)
```

Use the OpenAI-compatible facade for simple text/chat compatibility only. Do not
use it for production vision or file workflows. For images and files, use
`/v2/tasks/*` with `input_refs` or an explicitly documented gpucall SDK DataRef
path. A base64 `data:image/...` OpenAI-facade request is dev-only and cannot be
reported as a migrated production path.

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
| `500 Internal Server Error` | gateway-side internal error unless proven otherwise | record endpoint/status/body and report `No-Go`; do not speculate about recipe materialization, tuple activation, or provider absence |
| timeout during cold start | caller did not wait long enough or should use async | do not open provider circuit by default |
| malformed output / business validator failure after `200 OK` | quality feedback, not necessarily routing failure | submit `gpucall-recipe-draft quality` |
| provider 5xx without governance code | provider/runtime failure | retry/circuit according to local policy |
| gpucall not configured | application not ready for gpucall production | fail closed; do not call hosted AI by default |

For any gateway 5xx, completion reports must separate verified facts from
unknowns. Include HTTP status, response body if any, endpoint, request class,
whether bootstrap/auth/presign/preflight succeeded, and whether secrets were
redacted. Do not write "likely recipe missing", "probably tuple not activated",
or similar root-cause speculation unless the gateway logs or operator response
prove it. If unverified, write `root_cause=unverified`.

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
- `input_refs` is a list of DataRef objects with a `uri` field;
- manual upload uses `/v2/objects/presign-put` with `name`, `bytes`, `sha256`,
  and `content_type`;
- large input uses DataRef / presigned upload;
- image, file, confidential input, and over-limit text use DataRef or fail
  closed;
- every image/file caller path has a DataRef production path;
- OpenAI-facade base64 image/file payloads are rejected or dev-only opt-in;
- missing gpucall configuration fails closed;
- direct hosted-AI fallback is disabled by default;
- dev/test fallback, if retained, requires explicit opt-in and cannot run in
  production mode;
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

If canary receives a gateway 500, stop the canary fanout. Do not continue to
bulk workloads and do not retry with direct hosted-AI fallback. Record `No-Go:
gateway internal error` and hand the verified facts to the gpucall operator.

If available:

```bash
gpucall-migrate canary /path/to/project \
  --source example-caller-app \
  --command "<smallest representative command>"
```

Live canary is mandatory for `Go`. If gateway credentials or network access are
missing, the correct final status is `No-Go: live canary skipped`. Local tests
can prove implementation progress, but they cannot prove production readiness.

### 10. Go / No-Go checklist

Go only when:

- all direct hosted-AI production fallbacks are removed or disabled by default;
- any retained hosted-AI fallback is dev/test-only, explicit opt-in, and blocked
  in production;
- all unknown workloads have submitted preflight intake with request id or inbox
  path;
- no application payload contains `requested_tuple`;
- no application payload contains provider or GPU selectors;
- image and file workflows have DataRef production paths;
- confidential input and over-limit text use DataRef or fail closed;
- integration tests pass;
- live canary succeeded against the configured gpucall gateway or produced only
  expected governance failures after the request reached the gateway;
- cold-start timeout behavior is classified correctly;
- logs are redacted;
- the completion report includes command outputs.

No-Go when:

- direct hosted AI fallback remains enabled by default;
- direct hosted AI fallback is used when gpucall config is absent;
- unknown workloads are sent to gpucall without preflight;
- preflight is generated-only rather than submitted;
- live canary is skipped;
- caller code still chooses GPU/provider/model as route control;
- any image/file workflow lacks a DataRef production path;
- images or files are sent through OpenAI-facade base64 inline payloads as the
  only production implementation;
- confidential input or over-limit text is sent inline without DataRef
  enforcement;
- API key or presigned URL appears in logs;
- timeout increments provider circuit counters by default;
- tests only cover the happy path.

Do not report `Conditional Go`. If any Go condition is missing, report `No-Go`
and list the exact blockers.

## Final Report Template

```text
1. Inventory
   - total LLM/Vision/GPU paths:
   - already gpucall:
   - migrated:
   - direct hosted-AI fallbacks disabled by default:
   - dev/test-only direct fallbacks, if any:
   - unknown workloads submitted as preflight:
   - unknown workloads generated-only:

2. Changed files

3. Preflight submissions
   - submitted local inbox paths:
   - submitted remote inbox paths:
   - generated-only commands:

4. Tests
   - command:
   - output:

5. Canary
   - command:
   - live gateway reached: yes/no
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

7. DataRef enforcement
   - inline text byte limit:
   - image/file production paths use DataRef:
   - OpenAI-facade base64 image/file is absent or dev-only:
   - confidential/over-limit text uses DataRef or fails closed:

8. Remaining risk

9. Go / No-Go
   - Go is allowed only if all strict conditions are met.
   - Conditional Go is forbidden.
```
