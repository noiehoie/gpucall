# External System Adaptation Prompt

This is the compact public prompt for small migrations. For new or high-risk
integrations, prefer the fuller onboarding package:

- [EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md](EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md)
- [EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md](EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md)

Paste the following block into the coding agent that owns the external system.
Replace every placeholder before sending it.

```text
You are the implementation agent for this repository. Migrate this system's
LLM / Vision / GPU inference paths to gpucall v2.0.

Read the public onboarding docs first:

- https://raw.githubusercontent.com/noiehoie/gpucall/v2.0.8/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md
- https://raw.githubusercontent.com/noiehoie/gpucall/v2.0.8/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md

Do not clone, install, modify, or vendor the gpucall gateway repository. Your
worktree is this external system only. If helper tools are missing, install only
the caller SDK helper wheel or use an operator-provided wheel:

  uv tool install <gpucall-sdk-wheel-url>

Operator-provided values:

  system_name=<system-name>
  GPUCALL_BASE_URL=<gpucall-base-url>
  GPUCALL_API_KEY=<gpucall-api-key-or-use-trusted-bootstrap>
  GPUCALL_RECIPE_INBOX=<approved-local-or-ssh-inbox>

`GPUCALL_API_KEY` is a tenant-scoped gpucall gateway key. It is not a provider
API key. Never scrape gpucall config files or provider credentials.

There are exactly two valid key routes:

1. The operator provides a real gateway API key. Store it in this system's
   secret environment and never print it.
2. The operator sets `GPUCALL_API_KEY=<use-trusted-bootstrap>`. Only then, call
   `POST $GPUCALL_BASE_URL/v2/bootstrap/tenant-key` once with
   `{"system_name":"<system-name>"}`. Handle responses strictly:
   - 200: store the returned key in this system's secret environment;
   - 403: No-Go, trusted bootstrap is not enabled for this client;
   - 409: No-Go, a key already exists and must be rotated or handed off by the
     operator;
   - 500: No-Go, gateway-side internal error. Do not guess the cause.

Rules:

- Read actual code before claims. Use `rg` first.
- Inventory every LLM / Vision / GPU path with file, line, function, current
  SDK/provider, model string, input size, timeout, sync/async behavior, and
  whether it already goes through gpucall.
- Keep local-only paths local when they are intentionally local and do not cross
  the hosted-AI boundary.
- The external system must not choose provider, GPU, model, tuple, fallback
  chain, or internal gpucall recipe.
- The external system sends workload intent, task, mode, input, DataRef, and
  execution preferences only.
- Do not use legacy `/infer`.
- Use `/v2/tasks/sync`, `/v2/tasks/async`, `/v2/tasks/stream`, or the documented
  OpenAI-compatible facade for small text chat only.
- Do not use the OpenAI facade for production image/file transport.
- Direct OpenAI/Anthropic/Gemini fallback is disabled by default in production.
  If retained for local development, it must require an explicit dev-only opt-in
  and production must ignore or reject that opt-in.
- Quality failures after gpucall returns `200 OK` are not a reason to fall back
  to hosted AI. Submit quality feedback instead.

Workload intents should be abstract and reusable. Prefer names such as:

- summarize_text
- rank_text_items
- translate_text
- extract_json
- chat_answer
- caption_image
- answer_question_about_image
- understand_document_image
- transcribe_audio
- summarize_audio
- summarize_video

Do not invent system-specific intent names such as `topic_ranking` or
`editorial_stance` when an abstract intent describes the work.

Preflight:

- Unknown or new workloads must submit sanitized preflight intake to
  `GPUCALL_RECIPE_INBOX` before Go.
- A generated command or local JSON file is `generated-only`, not `submitted`.
- `submitted` means the approved local or SSH inbox received the intake and the
  report includes the path or request id.
- Do not submit raw prompts, document text, image bytes, DataRef URIs,
  presigned URLs, API keys, provider output, or secrets.

DataRef:

- Inline input is for small non-confidential text only.
- Images, files, confidential input, and over-limit text must use DataRef or
  fail closed.
- Manual upload uses:
  `POST /v2/objects/presign-put` with `name`, `bytes`, `sha256`,
  `content_type`, then `PUT` to the returned `upload_url`, then pass the
  returned `data_ref` object unchanged in `input_refs`.
- `input_refs` is a list of DataRef objects with a `uri` field. It is not a
  string list, and task payloads do not have a top-level `data_ref` field.
- Never log API keys, prompt bodies, DataRef URIs, or presigned URLs.

Mode and timeout:

- Small interactive text can use sync.
- Long-context, batch/long-running, high-cold-start, image/file, and large
  DataRef workloads should use async or must have a timeout budget that honestly
  covers cold start and queueing.
- Do not encode provider-specific cold-start assumptions in application code.
  Submit size, intent, mode preference, and timeout preference; gpucall's
  catalog and recipes decide the execution surface.

Required tests:

- payload never contains `recipe`, `requested_tuple`, provider, GPU, or
  provider model selectors;
- missing API key fails clearly;
- direct hosted-AI fallback is disabled by default;
- image/file and over-limit text paths use DataRef or fail closed;
- secrets, prompts, DataRef URIs, and presigned URLs are redacted;
- 422 `NO_AUTO_SELECTABLE_RECIPE` and 503 `NO_ELIGIBLE_TUPLE` do not open a
  provider circuit breaker;
- gateway 500 is reported as verified gateway-side failure, not guessed as
  missing recipe or missing tuple.

Live canary is required for Go:

- healthz and readyz;
- auth rejection without key;
- small text path;
- every transport class used by this integration: inline text, text DataRef,
  image/file DataRef, async polling, or stream as applicable.

If any required canary is skipped, report `No-Go`.

Final report:

1. Inventory table
2. Changed files
3. Preflight submissions: submitted paths/request ids, not just generated
   commands
4. Tests with real command output
5. Live canary with HTTP status counts and latency
6. Redaction check
7. Remaining direct-provider paths, if any
8. Remaining unknown workloads
9. Go / No-Go

Do not report `Conditional Go`. If a Go condition is missing, report `No-Go`
with explicit blockers. Never print the real API key.
```

## Common Failure Modes

- Cloning the gpucall gateway repository into the external system.
- Treating a public GitHub URL or release asset as environment-specific truth
  without checking it.
- Calling `/v2/bootstrap/tenant-key` when the operator did not explicitly set
  `<use-trusted-bootstrap>`.
- Calling old `/infer`.
- Sending `GPUCALL_API_KEY=dummy`.
- Sending `input_refs` as a string or object instead of a list of DataRef
  objects.
- Treating HTTP 500 as "recipe missing" without evidence.
- Marking generated preflight commands as submitted.
- Using OpenAI-facade base64 image transport in production.
- Retrying with direct hosted AI after gpucall returns a governance or quality
  failure.
