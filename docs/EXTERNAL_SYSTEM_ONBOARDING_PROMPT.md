# Universal gpucall Onboarding Prompt

Use this prompt when asking a coding agent inside another product to make that
product accept gpucall. It is written to avoid the migration mistakes that make
gpucall adoption harder than it should be.

Public reference URLs for agents that are not running inside the gpucall
repository:

- Prompt: https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- Manual: https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
- Repository: https://github.com/noiehoie/gpucall3

Important boundary for external-system agents:

- Do not clone, install, modify, or vendor the gpucall gateway repository.
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
uv tool install https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl
gpucall-recipe-draft --help
```

Fill these values before handing the prompt to the external-system agent:

- `<system-name>`: stable name of the external system
- `<gpucall-base-url>`: gateway base URL
- `<gpucall-api-key>`: gateway API key issued by the gpucall administrator for
  this system or tenant
- `<admin-inbox>`: approved local or SSH inbox for sanitized recipe requests
- `<canary-command>`: smallest representative command for that system
- `<gpucall-sdk-wheel-url>`: caller SDK helper wheel URL; default is
  `https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl`

Strict acceptance rule:

- The external system must not self-register for a key, scrape keys from
  gpucall configuration, or reuse provider credentials. The operator supplies a
  gateway API key issued according to `docs/GATEWAY_API_KEYS.md`.
- Never print `<gpucall-api-key>` in reports. Report only
  `GPUCALL_API_KEY=<set>`.
- Do not report `Go` unless live gpucall canary succeeded against the configured
  gateway and all required preflight intake was actually submitted.
- Do not report `Conditional Go`. Use `No-Go` with explicit blockers.
- A generated preflight command is not a submitted preflight.
- A skipped canary is not a successful canary.
- Direct hosted-AI fallback is forbidden by default in production.
- If any image or file workflow exists, DataRef upload support is mandatory for
  `Go`. Inline image/file transport is not an acceptable migrated production
  path.
- Do not mark a vision/file path as `migrated` until it uses `/v2/tasks/*` with
  `input_refs` or an explicitly documented gpucall SDK DataRef path.

```text
You are the implementation agent for this repository. Your task is to migrate
this system's LLM / Vision / GPU inference paths to gpucall v2.0 with minimum
behavioral disruption and maximum determinism.

Before editing, read the current gpucall onboarding documents:

- https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md

If network access is unavailable and the gpucall repository is checked out
locally, read the same files from that checkout instead.

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
export GPUCALL_RECIPE_INBOX="<admin-inbox>"
```

`GPUCALL_API_KEY` is a caller-facing gateway API key. It is not a provider API
key. It must be loaded from the runtime environment or secret manager, never
hard-coded into source, tests, docs, or completion reports.

If the operator enabled trusted bootstrap and did not provide
`GPUCALL_API_KEY`, request one exactly once from the gateway:

```bash
curl -fsS -X POST "$GPUCALL_BASE_URL/v2/bootstrap/tenant-key" \
  -H 'content-type: application/json' \
  -d '{"system_name":"<system-name>"}'
```

Store the returned `handoff` values in this system's secret environment and do
not print the raw `api_key` in reports. If the endpoint returns `403`, this
machine is not in the trusted bootstrap scope. If it returns `409`, a key
already exists and the operator must rotate or provide the existing secret; do
not try to bypass this.

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

Do not migrate unknown workload by guessing a model. Generate a preflight intake
instead. If the intake cannot be submitted because the helper is unavailable,
report `preflight command generated only` and mark the integration `No-Go` until
submission is completed.

Use these intent labels when possible:

- `summarize_text`
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
- HTTP timeout during cold start: do not automatically count as provider
  failure. Prefer async mode or longer caller timeout for long cold starts.
- provider runtime 5xx with no gpucall governance code: retry/circuit behavior
  may be appropriate.

## Phase 5: Canary

Run the smallest representative pipeline path first. Do not fan out to every
job at once.

Live canary is required for `Go`. If `GPUCALL_BASE_URL`, `GPUCALL_API_KEY`, or
gateway access is unavailable, report `No-Go: live canary skipped`.

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

Never include real API keys, Authorization headers, prompt bodies, image bytes,
presigned URLs, or DataRef URIs in the report.
```
