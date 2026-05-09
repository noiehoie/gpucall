# Universal gpucall Onboarding Prompt

Use this prompt when asking a coding agent inside another product to make that
product accept gpucall. It is written to avoid the migration mistakes that make
gpucall adoption harder than it should be.

Public reference URLs for agents that are not running inside the gpucall
repository:

- Prompt: https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- Manual: https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md
- Repository: https://github.com/noiehoie/gpucall3

Fill these values before handing the prompt to the external-system agent:

- `<system-name>`: stable name of the external system
- `<gpucall-base-url>`: gateway base URL
- `<admin-inbox>`: approved local or SSH inbox for sanitized recipe requests
- `<canary-command>`: smallest representative command for that system

```text
You are the implementation agent for this repository. Your task is to migrate
this system's LLM / Vision / GPU inference paths to gpucall v2.0 with minimum
behavioral disruption and maximum determinism.

Before editing, read the current gpucall onboarding documents:

- https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md

If network access is unavailable and the gpucall repository is checked out
locally, read the same files from that checkout instead.

You must read the actual code and configuration before making claims. Do not
guess from filenames.

## gpucall contract

gpucall is a deterministic GPU governance gateway.

Application code must not choose:

- provider
- GPU
- execution tuple
- fallback order
- internal gpucall recipe, unless this repository already has an explicit,
  reviewed integration contract requiring one

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

## Phase 1: Classify workloads

For each path, classify it as one of:

- already-gpucall
- direct-hosted-api
- direct-provider-gpu
- local-only
- unknown-workload

Do not migrate unknown workload by guessing a model. Generate a preflight intake
instead.

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
- one DataRef helper if large files or images are used
- unit tests around payload construction and error handling

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
- Large input should use presign PUT and `DataRef`.
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
- `NO_AUTO_SELECTABLE_RECIPE` is classified as recipe-intake-needed
- `NO_ELIGIBLE_TUPLE` is not treated as direct provider failure
- timeout is not counted as provider circuit failure by default
- large input uses DataRef / presigned upload
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

Never include real API keys, Authorization headers, prompt bodies, image bytes,
presigned URLs, or DataRef URIs in the report.
```
