# gpucall Recipe Draft Tool

`gpucall-recipe-draft` is an operator-assist tool for cases where a caller's workload does not match the recipes and providers currently installed in a gpucall gateway.

It is intentionally separate from the gateway runtime.

- The gateway remains deterministic and does not inspect prompt meaning.
- The caller-side tool collects sanitized failure metadata for gpucall administrators.
- The caller-side tool does not call an LLM.
- Any generated recipe or provider draft requires human review before production use.

## When To Use It

Use this tool before a new workload class reaches production, and after gpucall returns a structured governance failure such as:

- `NO_AUTO_SELECTABLE_RECIPE`
- `no eligible provider after policy, recipe, and circuit constraints`
- context length, media type, mode, or capability mismatch

Do not use it to bypass policy. If gpucall fails closed, the workload should not be forced through a weaker provider.

In normal operation, unknown workloads are handled as follows:

1. Before production, caller runs `preflight` for the planned workload metadata.
2. Caller submits the sanitized preflight intake to the gpucall administrator.
3. Administrator materializes or rejects the workload class.
4. If production still fails, gpucall returns `422 NO_AUTO_SELECTABLE_RECIPE` or `503 no eligible provider after policy, recipe, and circuit constraints`.
5. Caller runs post-failure `intake` and `compare` to distinguish workload drift from admin/provider/runtime failure.

The helper is designed to remove raw prompt bodies, message bodies, documents, media bytes, DataRef URIs, presigned URLs, and secrets. It produces an intake artifact for gpucall administrators. LLM-assisted recipe authoring belongs on the administrator side, after the administrator accepts the sanitized intake into an audited workflow.

## Two Phases

### Phase 0: Preflight Intake

Use preflight before sending a new workload class to production. This does not contact gpucall and does not inspect prompt bodies.

```bash
gpucall-recipe-draft preflight \
  --task vision \
  --mode sync \
  --intent understand_document_image \
  --content-type image/png \
  --bytes 2000000 \
  --required-model-len 9000 \
  --classification confidential \
  --output preflight-intake.json
```

Submit this intake before the first production run:

```bash
gpucall-recipe-draft submit \
  --intake preflight-intake.json \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --source news-system
```

### Phase 1: Post-Failure Intake

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

If a preflight was submitted but the production run still failed, compare the preflight with the actual failure intake:

```bash
gpucall-recipe-draft compare \
  --preflight preflight-intake.json \
  --failure failure-intake.json \
  --output drift-report.json
```

The report classifies the failure as:

- `preflight_matched_runtime_failure`: metadata matched; check admin status, provider availability, validation, or runtime failures.
- `workload_drift`: actual workload differed materially from preflight; submit updated intake.
- `metadata_drift`: lower-level metadata differed; review the caller preflight declaration.

### Phase 2: Local Draft Summary

This phase consumes only the sanitized intake JSON and does not call an LLM.

The draft command creates a deterministic review artifact. It is useful as a cover sheet for the administrator, not as production config.

```bash
gpucall-recipe-draft draft --input intake.json --output recipe-draft.json
```

The output is not production config. It is a review artifact for gpucall administrators.

### Submission

The caller-side helper can submit sanitized intake and draft artifacts to a file-based inbox. This is not a gpucall API. The inbox can be a shared directory, mounted object-store prefix, or any path synchronized by your own transport.

```bash
gpucall-recipe-draft submit \
  --intake intake.json \
  --draft recipe-draft.json \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --source news-system
```

The submitted bundle contains only the sanitized intake and optional draft. It does not contain raw prompt bodies, DataRef URIs, presigned URLs, or secrets.

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
4. If the organization has adopted an accept-all policy, administrator runs the gateway-side `gpucall-recipe-admin` helper.
5. Administrator writes canonical gpucall recipe/provider YAML.
6. Administrator runs `gpucall validate-config`, tests, provider validation, and `gpucall launch-check`.
7. Only reviewed and validated config is committed and deployed.

## Gateway-Side Admin Helper

The caller-side helper ships with the SDK. The administrator-side helper ships with the gateway package, not the SDK.

For low-friction operations, a gpucall administrator may choose an explicit accept-all policy for sanitized caller intake:

```bash
gpucall-recipe-admin materialize \
  --input intake.json \
  --output-dir config/recipes \
  --report materialization-report.json \
  --accept-all
```

`--accept-all` is required so accidental materialization fails closed. This command writes canonical recipe YAML for the current gpucall schema. It does not create a capable provider, does not edit policy, and does not deploy anything.

After materialization:

```bash
gpucall validate-config --config-dir config
gpucall launch-check --profile static --config-dir config
```

If validation reports that no provider satisfies the new recipe, the administrator must add or enable an appropriate provider before production use.

### Inbox Automation

For operators who choose to accept every submitted sanitized request, the admin helper can process the inbox without an API endpoint:

```bash
gpucall-recipe-admin process-inbox \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --output-dir config/recipes \
  --accept-all
```

Processed submissions are moved to `inbox/processed`, failed submissions to `inbox/failed`, and materialization reports to `inbox/reports`.

To poll continuously:

```bash
gpucall-recipe-admin watch \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --output-dir config/recipes \
  --accept-all \
  --interval-seconds 10
```

This still only writes recipe YAML. It does not deploy, does not edit provider specs, and does not bypass `validate-config` or launch checks.

To inspect a submitted request:

```bash
gpucall-recipe-admin status \
  --request-id rr-20260506T010203Z-abcdef123456 \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox
```
