# Universal gpucall Onboarding Prompt

Use this prompt when asking a coding agent inside another product to make that
product accept gpucall. It is written to avoid the migration mistakes that make
gpucall adoption harder than it should be.

Public reference/template URLs for agents that are not running inside the
gpucall repository:

- Prompt: https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- Manual: https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
- Repository: https://github.com/noiehoie/gpucall

Important boundary for external-system agents:

- Do not clone, install, modify, or vendor the gpucall gateway repository.
- Treat the operator-provided prompt/handoff and the live gateway OpenAPI schema
  as authoritative for environment-specific facts. Public GitHub documents are
  generic references; they do not define the installed gateway URL, recipe
  inbox, API key policy, trusted bootstrap scope, or private SDK mirror.
- Read only the raw onboarding documents above unless the operator explicitly
  provides a local gpucall checkout for reference.
- Make code changes only in the external system being migrated.
- If `gpucall-migrate`, `gpucall-recipe-draft`, or `gpucall_sdk` are not
  already available in the external system's environment, do not fetch the
  gpucall gateway repository. Install only the caller SDK helper from the public
  wheel URL below, or use an operator-provided wheel. `gpucall-migrate` is
  optional and belongs to the gateway package; if it is unavailable, report that
  fact and continue without it.

Caller SDK helper wheel:

```bash
uv tool install https://github.com/noiehoie/gpucall/releases/download/v2.0.8/gpucall_sdk-2.0.8-py3-none-any.whl
gpucall-recipe-draft --help
```

Fill these values before handing the prompt to the external-system agent. Until
these placeholders are filled, this file is a template, not a complete
deployment instruction:

- `<system-name>`: stable name of the external system
- `<gpucall-base-url>`: gateway base URL
- `<gpucall-api-key>`: gateway API key issued by the gpucall administrator for
  this system or tenant, or the literal placeholder
  `<use-trusted-bootstrap>` when the administrator has explicitly enabled
  trusted bootstrap for this system
- `<admin-inbox>`: approved local or SSH inbox for sanitized recipe requests
- `<canary-command>`: smallest representative command for that system
- `<gpucall-sdk-wheel-url>`: caller SDK helper wheel URL; default is
  `https://github.com/noiehoie/gpucall/releases/download/v2.0.8/gpucall_sdk-2.0.8-py3-none-any.whl`, or an operator-hosted
  mirror for the installed environment

Strict acceptance rule:

- The external system must not perform unauthorized self-registration, scrape
  keys from gpucall configuration, or reuse provider credentials. API key setup
  has exactly two allowed routes:
  1. the operator supplies a gateway API key issued according to
     `docs/GATEWAY_API_KEYS.md`;
  2. the operator explicitly authorizes trusted bootstrap by setting
     `<gpucall-api-key>` to `<use-trusted-bootstrap>` and providing a reachable
     `<gpucall-base-url>`.
- Trusted bootstrap is operator-authorized key delivery, not caller-controlled
  registration. If the placeholder is anything other than
  `<use-trusted-bootstrap>`, do not call `/v2/bootstrap/tenant-key`.
- Never print `<gpucall-api-key>` in reports. Report only
  `GPUCALL_API_KEY=<set>`.
- Do not report `Go` unless live gpucall canary succeeded against the configured
  gateway and all required preflight intake was actually submitted.
- Do not report `Conditional Go`. Use `No-Go` with explicit blockers.
- A generated preflight command is not a submitted preflight.
- A skipped canary is not a successful canary.
- HTTP 500 is not a recipe-missing signal. Do not guess the root cause of a
  gateway 500. Record it as a gateway-side internal error and report `No-Go`.
- Direct hosted-AI fallback is forbidden by default in production.
- If any image or file workflow exists, DataRef upload support is mandatory for
  `Go`. Inline image/file transport is not an acceptable migrated production
  path.
- Do not mark a vision/file path as `migrated` until it uses `/v2/tasks/*` with
  `input_refs` or an explicitly documented gpucall SDK DataRef path.
- For DataRef wire format, prefer the live gateway OpenAPI schema over memory
  or old examples. The v2 presign endpoint is `/v2/objects/presign-put`, not
  `/v2/upload/presign`.

```text
You are the implementation agent for this repository. Your task is to migrate
this system's LLM / Vision / GPU inference paths to gpucall v2.0 with minimum
behavioral disruption and maximum determinism.

Before editing, read the operator-provided handoff values and the live gateway
OpenAPI schema. Use public onboarding documents only as generic references:

- https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- https://raw.githubusercontent.com/noiehoie/gpucall/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md

If the public URLs are unavailable, do not treat that alone as a failed
onboarding. Continue from the operator-provided handoff and
`$GPUCALL_BASE_URL/openapi.json`. If network access is unavailable and the
operator provided a local gpucall checkout for reference, read the same files
from that checkout instead.

Do not clone, install, modify, or vendor the gpucall gateway repository. Your
worktree is the external system only. If gpucall helper commands or SDK packages
are not already installed, install only the caller SDK helper from
`<gpucall-sdk-wheel-url>` or an operator-provided wheel. Do not fetch the
gateway repository to obtain tools. If the wheel cannot be installed, report
that blocker and proceed with the parts that can be completed from this
repository's source code.

You must read the actual code and configuration before making claims. Do not
guess from filenames.

## gpucall contract

gpucall is a deterministic GPU governance gateway.

Use these operator-provided values:

```bash
export GPUCALL_BASE_URL="<gpucall-base-url>"
export GPUCALL_API_KEY="<gpucall-api-key>"
export GPUCALL_RECIPE_INBOX="<recipe-request-admin-inbox>"
export GPUCALL_QUALITY_FEEDBACK_INBOX="<quality-feedback-admin-inbox>"
```

Validate the installed gateway before implementation:

```bash
curl -fsS "$GPUCALL_BASE_URL/healthz"
curl -fsS "$GPUCALL_BASE_URL/readyz"
curl -fsS "$GPUCALL_BASE_URL/openapi.json" -o /tmp/gpucall-openapi.json
```

`GPUCALL_API_KEY` is a caller-facing gateway API key. It is not a provider API
key. It must be loaded from the runtime environment or secret manager, never
hard-coded into source, tests, docs, or completion reports.

There are exactly two valid ways to obtain `GPUCALL_API_KEY`:

1. **Operator-issued secret**: `<gpucall-api-key>` is a real tenant-scoped
   gateway key. Store it in this system's secret environment and do not call
   `/v2/bootstrap/tenant-key`.
2. **Operator-authorized trusted bootstrap**: `<gpucall-api-key>` is exactly
   `<use-trusted-bootstrap>`. In that case, request one key exactly once from
   the gateway:

```bash
curl -fsS -X POST "$GPUCALL_BASE_URL/v2/bootstrap/tenant-key" \
  -H 'content-type: application/json' \
  -d '{"system_name":"<system-name>"}'
```

Store the returned `api_key` and `handoff` values in this system's secret
environment, then set `GPUCALL_API_KEY` from that secret. Do not print the raw
`api_key` in reports.

If `<gpucall-api-key>` is empty, `dummy`, unset, or any placeholder other than
`<use-trusted-bootstrap>`, do not bootstrap. Report `No-Go: gateway API key not
provided`.

Trusted bootstrap response handling:

- `200`: store the returned key and continue.
- `403`: this machine is not in the trusted bootstrap scope; report `No-Go`.
- `409`: a key already exists and gpucall will not reprint it; the operator
  must rotate or provide the existing secret. Do not try to bypass this.
- `500`: gateway-side internal error; report `No-Go` with verified facts only.

Application code must not choose:

- provider
- GPU
- execution tuple
- fallback order
- internal gpucall recipe, unless this repository already has an explicit,
  reviewed integration contract requiring one
- direct hosted-AI fallback after gpucall is configured

Application code sends workload intent and data only:

- `task`
- `mode`
- inline input, or `DataRef` / `input_refs`
- execution preferences such as max tokens or timeout

The gateway owns recipe selection, tuple routing, policy checks, price
freshness, validation evidence, fail-closed behavior, cleanup, and audit.

## Phase 0: Inventory before edits

Run a source scan and produce a table of every LLM / Vision / GPU call path.
Include:

- file path and line
- function/class
- current provider or SDK
- current model string, if any
- sync / async / stream behavior
- timeout behavior
- whether raw prompt, image, file bytes, URL, or DataRef is used
- whether the path already goes through gpucall
- whether the path is direct OpenAI / Anthropic / Gemini / provider SDK

Use deterministic search first:

```bash
rg -n "OpenAI|AsyncOpenAI|Anthropic|AsyncAnthropic|Gemini|GenerativeModel|call_llm|chat.completions|messages|model=|provider|gpu|requested_tuple|recipe|gpucall|DataRef|presign|vllm|transformers" .
```

If available, run:

```bash
gpucall-migrate assess . --source <system-name>
gpucall-migrate report . --source <system-name>
gpucall-migrate onboard . --source <system-name>
```

If `gpucall-migrate` is unavailable, do not install the gateway package to get
it. Treat it as optional and continue with `rg`-based inventory.

## Phase 1: Classify workloads

For each path, classify it as one of:

- already-gpucall
- direct-hosted-api
- direct-provider-gpu
- local-only
- unknown-workload

`local-only` means the work is intentionally handled by a local runtime under
the application's own control, such as an embedding model or a private local
OpenAI-compatible server. Keep those paths local when they do not cross the
hosted-AI boundary and do not need gpucall governance. Do not turn honest local
work into a remote GPU call just to increase gpucall usage.

Do not migrate unknown workload by guessing a model. Generate a preflight intake
instead. If the intake cannot be submitted because the helper is unavailable,
report `preflight command generated only` and mark the integration `No-Go` until
submission is completed.

Use these intent labels when possible:

- `summarize_text`
- `rank_text_items`
- `translate_text`
- `extract_json`
- `chat_answer`
- `caption_image`
- `answer_question_about_image`
- `understand_document_image`
- `transcribe_audio`
- `summarize_audio`
- `summarize_video`

## Phase 2: Preflight unknown or new workloads

Before production traffic reaches gpucall for a workload that is not already
known to be supported, submit sanitized intent.

Use these words precisely:

- `submitted`: the intake was written to the approved local or remote inbox and
  the path or request id is reported.
- `generated-only`: a command or JSON draft exists, but no approved inbox
  received it.

`generated-only` is useful progress, but it is not production-ready and must
produce `No-Go`.

Do not send raw prompt bodies, image bytes, document text, DataRef URIs,
presigned URLs, API keys, or provider output.

Example:

```bash
gpucall-recipe-draft preflight \
  --task infer \
  --mode sync \
  --intent translate_text \
  --business-need "translate customer-visible article text, 500-8000 chars" \
  --content-type text/plain \
  --bytes 8000 \
  --required-model-len 32768 \
  --remote-inbox <admin-inbox> \
  --source <system-name>
```

If the tool supports only `--context-budget-tokens` instead of
`--required-model-len`, use the available equivalent. Report the substitution.

## Phase 3: Implement the integration

Prefer the smallest stable adapter layer in this repository:

- one gpucall client wrapper
- one error-classification function
- one DataRef helper if the repository has any image workflow, file workflow,
  confidential-content workflow, or text above the explicit small-text inline
  limit
- unit tests around payload construction and error handling

Production default must be fail-closed:

- Missing `GPUCALL_BASE_URL` or `GPUCALL_API_KEY` must fail with a configuration
  error.
- Do not fall back to OpenAI, Anthropic, Gemini, or any hosted AI API because
  gpucall is missing or returns a governance error.
- If a direct hosted-AI fallback is retained for local development or tests, it
  must require an explicit opt-in such as `ALLOW_DIRECT_AI_FALLBACK_FOR_DEV=1`,
  and production mode must ignore or reject that opt-in.
- Do not use direct hosted-AI fallback for quality failures after gpucall returns
  `200 OK`, including JSON parse failures, empty output, schema failures, or
  business-validator rejection. Treat those as quality feedback and submit
  `gpucall-recipe-draft quality`.
- If the caller requires a specific JSON business schema, do not rely on prompt
  text plus `response_format={"type":"json_object"}`. Send
  `response_format={"type":"json_schema","json_schema":...}` with required
  fields. `json_object` guarantees only that the output is a JSON object.
- If `200 OK` / `output_validated=true` still fails caller-side schema
  validation, submit `gpucall-recipe-draft quality` with
  `--quality-failure-kind schema_mismatch`, `--response-format`, sanitized
  `--expected-json-schema`, sanitized `--observed-json-schema`, and success /
  failure counts. Submit it to `GPUCALL_QUALITY_FEEDBACK_INBOX`, not
  `GPUCALL_RECIPE_INBOX`. Do not include raw model output.
- Add tests proving hosted-AI fallback is disabled by default.

Correct request shape:

```json
{
  "task": "infer",
  "mode": "sync",
  "inline_inputs": {
    "prompt": {
      "value": "hello",
      "content_type": "text/plain"
    }
  },
  "max_tokens": 64
}
```

Rules:

- Use `/v2/tasks/sync`, `/v2/tasks/async`, or `/v2/tasks/stream`.
- Do not use legacy `/infer`.
- Do not send `requested_tuple`.
- Do not send provider or GPU selectors.
- Do not send model strings as routing selectors.
- Do not send OpenAI `messages` directly to `/v2/tasks/*`; convert to
  `inline_inputs.prompt.value` unless using the OpenAI-compatible facade.
- `input_refs` must be a list.
- `input_refs` items must be DataRef objects, not strings. A DataRef uses a
  `uri` field:

  ```json
  {"uri": "s3://bucket/key", "sha256": "<64 hex chars>", "bytes": 1234, "content_type": "image/png"}
  ```

- To create a DataRef for caller-uploaded bytes, call
  `POST /v2/objects/presign-put` with this exact request shape:

  ```json
  {
    "name": "input.png",
    "bytes": 1234,
    "sha256": "<64 hex chars>",
    "content_type": "image/png"
  }
  ```

  Upload the bytes with `PUT` to the returned `upload_url`, then pass the
  returned `data_ref` object unchanged in `input_refs`. Do not invent a
  `data_ref` string field and do not use `/v2/upload/presign`.
- Inline payload is allowed only for small non-confidential text. Define the
  inline byte limit in code and test it.
- Images and files must use presign PUT and `DataRef`. Do not implement
  production vision/file transport as OpenAI-facade base64 inline content.
- Confidential content and payloads above the inline text limit must use presign
  PUT and `DataRef`.
- If a required DataRef helper cannot be implemented in this turn, leave the
  path unmigrated, fail closed with `DataRef required`, and report `No-Go`.
- Do not log API keys, prompt bodies, presigned URLs, DataRef URIs, or provider
  raw output.

Mode and timeout rules:

- Small interactive text may use sync.
- Long-context, batch/long-running, high-cold-start, image/file, and large
  DataRef workloads should use async or must have a timeout budget that honestly
  covers cold start and queueing.
- Do not encode provider-specific cold-start assumptions in application code.
  Submit intent, size, mode preference, and timeout preference. The installed
  catalog and gateway recipes decide the execution surface.
- If a sync canary times out on a long-context or high-cold-start workload, do
  not retry by falling back to direct hosted AI. Convert that path to async or
  submit/update preflight so the gateway can materialize the correct recipe.

If using the OpenAI SDK facade, configure only:

```python
from openai import OpenAI

client = OpenAI(
    base_url=f"{GPUCALL_BASE_URL.rstrip('/')}/v1",
    api_key=GPUCALL_API_KEY,
)
```

Use `model="gpucall:chat"` or another documented gpucall facade model only as
the facade selector. Do not use provider model names for routing.

Do not use the OpenAI-compatible facade for vision or file workflows. Use
`/v2/tasks/*` with `input_refs` or an explicitly documented gpucall SDK DataRef
path. A base64 `data:image/...` OpenAI-facade request is a dev-only experiment,
not a migrated production path, and must produce `No-Go` if it is the only
vision implementation.

## Phase 4: Error behavior

Classify these correctly:

- `401`: authentication/configuration error. Do not retry blindly.
- `422 NO_AUTO_SELECTABLE_RECIPE`: workload not yet supported. Do not open a
  provider circuit breaker. Submit recipe-draft intake.
- `503 NO_ELIGIBLE_TUPLE`: recipe may exist, but no currently eligible tuple.
  Treat as governance/capacity state, not direct provider failure.
- `500 Internal Server Error`: gateway-side internal error. Do not infer
  "recipe not materialized", "tuple not activated", or "provider missing"
  unless the response body or gateway operator explicitly says so. Recipe
  absence should be reported by `422 NO_AUTO_SELECTABLE_RECIPE`; eligible tuple
  absence should be reported by `503 NO_ELIGIBLE_TUPLE`.
- HTTP timeout during cold start: do not automatically count as provider
  failure. Prefer async mode or longer caller timeout for long cold starts.
- provider runtime 5xx with no gpucall governance code: retry/circuit behavior
  may be appropriate.
- malformed output, empty output, schema failure, or business-validator failure
  after `200 OK`: do not fall back to direct hosted AI in production. Record a
  quality failure and submit `gpucall-recipe-draft quality`. For schema
  failures, include expected and observed schemas as metadata, never raw output.

For any gateway 5xx, the completion report must include only verified facts:
HTTP status, response body if available, endpoint, request class, whether
bootstrap/auth/presign/preflight succeeded, and whether the caller exposed any
secret. Do not write speculative root causes.

## Phase 5: Canary

Run the smallest representative pipeline path first. Do not fan out to every
job at once.

Live canary is required for `Go`. If `GPUCALL_BASE_URL`, `GPUCALL_API_KEY`, or
gateway access is unavailable, report `No-Go: live canary skipped`.

Canary must cover every transport class used by the integration:

- small inline text through the OpenAI-compatible facade or `/v2/tasks/sync`;
- over-limit/confidential text through presign PUT + `DataRef` + `/v2/tasks/sync`
  when such a path exists;
- image/file workflow through presign PUT + `DataRef` + `/v2/tasks/sync` when
  such a path exists.

If any required transport class is untested, report `No-Go`.

Record:

- number of gpucall calls
- HTTP status counts
- `NO_AUTO_SELECTABLE_RECIPE`
- `NO_ELIGIBLE_TUPLE`
- timeout count
- whether circuit breaker count changed
- latency: cold and warm if visible
- whether any raw prompt/secret appeared in logs

If available:

```bash
gpucall-migrate canary . --source <system-name> --command "<canary-command>"
```

## Phase 6: Required tests

Add or update tests proving:

- payload has `task` and `mode`
- payload does not contain `recipe`
- payload does not contain `requested_tuple`
- payload does not contain provider, GPU, or provider model selector
- `input_refs` is a list
- API key is required and never printed
- missing gpucall configuration fails closed
- direct hosted-AI fallback is disabled by default
- quality failures after gpucall `200 OK` do not trigger direct hosted-AI
  fallback in production
- dev/test fallback, if retained, requires explicit opt-in and is rejected or
  ignored in production mode
- `NO_AUTO_SELECTABLE_RECIPE` is classified as recipe-intake-needed
- `NO_ELIGIBLE_TUPLE` is not treated as direct provider failure
- timeout is not counted as provider circuit failure by default
- image, file, confidential input, and over-limit text use DataRef / presigned
  upload or fail closed
- every image/file caller path has a DataRef production path
- OpenAI-facade base64 image/file payloads are rejected or dev-only opt-in
- inline input above the configured byte limit is rejected
- raw prompt, presigned URL, DataRef URI, and Authorization header are redacted

## Completion report

Report only facts verified by commands.

Include:

- changed files
- inventory table
- preflight submissions created, including file path or remote inbox path
- tests run and exact output
- canary command and result
- remaining direct provider paths
- remaining unknown workloads
- secret/log redaction confirmation
- final Go / No-Go

Final status rules:

- `Go`: live canary succeeded; all required preflight intake was submitted;
  direct hosted-AI fallback is disabled by default; image/file workflows use
  DataRef production paths; DataRef rules are enforced; tests pass.
- `No-Go`: anything else.
- Do not use `Conditional Go`.
- Do not write `unknown workloads submitted` unless an approved inbox actually
  received them.
- Do not write "likely cause" for gateway 5xx. If root cause was not verified
  from gateway logs or an operator response, write `root_cause=unverified`.

Never include real API keys, Authorization headers, prompt bodies, image bytes,
presigned URLs, or DataRef URIs in the report.
```
