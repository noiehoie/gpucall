# gpucall Recipe Draft Tool

`gpucall-recipe-draft` is an operator-assist tool for cases where a caller's workload does not match the recipes and providers currently installed in a gpucall gateway.

It is intentionally separate from the gateway runtime.

- The gateway remains deterministic and does not inspect prompt meaning.
- The caller-side tool collects sanitized failure metadata for gpucall administrators.
- The caller-side tool does not call an LLM.
- Any generated recipe or provider draft requires human review before production use.

## When To Use It

Use this tool when gpucall returns a structured governance failure such as:

- `NO_AUTO_SELECTABLE_RECIPE`
- `no eligible provider after policy, recipe, and circuit constraints`
- context length, media type, mode, or capability mismatch

Do not use it to bypass policy. If gpucall fails closed, the workload should not be forced through a weaker provider.

In normal operation, unknown workloads are handled as follows:

1. gpucall returns `422 NO_AUTO_SELECTABLE_RECIPE` when no installed recipe honestly describes the request.
2. gpucall returns `503 no eligible provider after policy, recipe, and circuit constraints` when a recipe exists but no eligible provider can execute it.
3. The caller runs this helper outside the gateway runtime.
4. The caller sends the sanitized intake and draft to the gpucall administrator through an approved operator channel.
5. The administrator decides whether the request should become a supported workload class.
6. If recipe authoring needs LLM assistance, the administrator runs that inside an audited gpucall admin workflow.
7. The administrator reviews the draft, writes canonical recipe/provider YAML, validates it, and deploys it for future runs.

The helper is designed to remove raw prompt bodies, message bodies, documents, media bytes, DataRef URIs, presigned URLs, and secrets. It produces an intake artifact for gpucall administrators. LLM-assisted recipe authoring belongs on the administrator side, after the administrator accepts the sanitized intake into an audited workflow.

## Two Phases

### Phase 1: Deterministic Intake

This phase does not use an LLM.

It reads a gpucall error payload and caller-supplied high-level intent, then writes an allowlisted JSON document. It removes prompt bodies, DataRef URIs, presigned URLs, API keys, and message content.

```bash
gpucall-recipe-draft intake \
  --error gpucall-error.json \
  --task vision \
  --intent understand_document_image \
  --business-need "画像の内容に関する質問に答えたい" \
  --classification confidential \
  --output intake.json
```

The output contains:

- sanitized task, mode, intent, and classification
- content types, byte sizes, and estimated context limits
- recipe rejection reasons
- redaction report

### Phase 2: Local Draft Summary

This phase consumes only the sanitized intake JSON and does not call an LLM.

The draft command creates a deterministic review artifact. It is useful as a cover sheet for the administrator, not as production config.

```bash
gpucall-recipe-draft draft --input intake.json --output recipe-draft.json
```

The output is not production config. It is a review artifact for gpucall administrators.

## Caller-Facing Intents

Callers should describe intent at a high level. They should not specify GPU names, provider names, model names, or gpucall-internal capability labels.

Examples:

- `summarize_text`
- `translate_text`
- `extract_json`
- `caption_image`
- `answer_question_about_image`
- `understand_document_image`
- `transcribe_audio`
- `summarize_audio`
- `summarize_video`

gpucall administrators map these intents to recipe and provider contracts.

## Security Rules

The intake phase is allowlist-based. It is designed to keep the following out of LLM prompts and operator tickets:

- prompt body
- message body
- document text
- media bytes
- DataRef URI
- presigned URL
- API key and authorization headers

For `restricted` workloads, use the intake artifact only, or use an approved local/closed LLM for draft assistance.

## Administrator Workflow

1. Caller runs `gpucall-recipe-draft intake` against the failure payload.
2. Caller sends `intake.json` and a business-level description to the gpucall administrator.
3. Administrator decides whether recipe authoring is appropriate.
4. If needed, administrator runs an audited admin-side recipe-authoring workflow.
5. Administrator writes canonical gpucall recipe/provider YAML.
6. Administrator runs `gpucall validate-config`, tests, provider validation, and `gpucall launch-check`.
7. Only reviewed and validated config is committed and deployed.
