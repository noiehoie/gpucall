# gpucall Recipe Draft Tool

`gpucall-recipe-draft` is an operator-assist tool for cases where a caller's workload does not match the recipes and tuples currently installed in a gpucall gateway.

It is intentionally separate from the gateway runtime.

- The gateway remains deterministic and does not inspect prompt meaning.
- The caller-side tool collects sanitized failure metadata for gpucall administrators.
- The caller-side tool does not call an LLM.
- Any generated recipe intent draft requires human review before production use.
- LLM-assisted recipe authoring, if used, belongs only in an audited administrator-side workflow over sanitized intake. It is never part of gateway routing, tuple selection, live catalog evaluation, validation gates, cleanup, or production activation.

## Product Role

gpucall has three product components:

- the gateway runtime, which performs deterministic admission, recipe selection, tuple routing, policy, audit, cleanup, and validation-gated execution;
- the caller-side helper, `gpucall-recipe-draft`, which turns external workload intent and failure/quality metadata into sanitized intake bundles;
- the administrator-side helper, `gpucall-recipe-admin`, which reviews intake, materializes recipe intent, derives missing tuple contracts, and promotes only validated production tuples.

This document covers the caller-side helper and the handoff to the administrator-side helper. The caller-side helper must not choose providers, GPUs, models, engines, or tuples. It describes workload intent and context needs; catalog management and production promotion belong to administrators.

The boundary is strict: external callers may use human judgment or their own AI systems to decide what they want to request, but gpucall treats that as caller-side input. gpucall turns only sanitized metadata into deterministic review artifacts. The gateway never delegates governance decisions to an LLM.

## When To Use It

Use this tool before a new workload class reaches production, after gpucall returns a structured governance failure, and after a `200 OK` result that the caller's own business validator rejects.

- `NO_AUTO_SELECTABLE_RECIPE`
- `no eligible tuple after policy, recipe, and circuit constraints`
- context length, media type, mode, or capability mismatch
- low-quality success, where gpucall executed a recipe but the selected model or recipe did not satisfy the caller's declared capability

gpucall failure responses include a machine-readable `failure_artifact`. This artifact is the preferred input for operator workflows because it contains only redacted routing metadata:

- `failure_id`
- `failure_kind`
- `caller_action`
- `safe_request_summary`
- `capability_gap`
- `rejection_matrix`
- `redaction_guarantee`

It does not include prompt bodies, message content, DataRef URIs, presigned URLs, API keys, provider raw output, or provider secrets.

Do not use it to bypass policy. If gpucall fails closed, the workload should not be forced through a weaker provider.

In normal operation, unknown workloads are handled as follows:

1. Before production, caller runs `preflight` for the planned workload metadata.
2. Caller submits the sanitized preflight intake to the gpucall administrator.
3. Administrator materializes or rejects the workload class.
4. If production still fails, gpucall returns `422 NO_AUTO_SELECTABLE_RECIPE` or `503 no eligible tuple after policy, recipe, and circuit constraints`.
5. Caller runs post-failure `intake` and `compare` to distinguish workload drift from admin/catalog/runtime failure.
6. If gpucall returns `200 OK` but caller-side validation fails, caller runs `quality` and submits that sanitized feedback to the separate quality feedback inbox.

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
  --context-budget-tokens 9000 \
  --classification confidential \
  --output preflight-intake.json
```

Submit this intake before the first production run:

```bash
gpucall-recipe-draft submit \
  --intake preflight-intake.json \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --source example-caller-app
```

If the gpucall administrator exposes an SSH-only operator inbox, submit directly to it:

```bash
gpucall-recipe-draft submit \
  --intake preflight-intake.json \
  --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --source example-caller-app
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
  --output intake.json \
  --remote-quality-inbox admin@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox \
  --source example-caller-app
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

### Phase 1b: Low-Quality Success Feedback

This phase is for `200 OK` responses that gpucall considers successful but the caller's business validator rejects. The gateway must not inspect prompt meaning or media content, so the caller owns this quality judgment.

Do not pass raw output, prompt bodies, image bytes, DataRef URIs, or presigned URLs. Pass only metadata and the caller-side failure category.

```bash
gpucall-recipe-draft quality \
  --task vision \
  --mode sync \
  --intent understand_document_image \
  --expected-output headline_list \
  --content-type image/jpeg \
  --bytes 1136521 \
  --dimension 1200x2287 \
  --observed-recipe vision-image-standard \
  --reported-tuple modal-vision-a10g \
  --reported-tuple-model Salesforce/blip-vqa-base \
  --quality-failure-kind insufficient_ocr \
  --quality-failure-reason "short answer only; expected top headlines" \
  --remote-quality-inbox admin@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox \
  --source example-caller-app
```

This creates a `deterministic-quality-feedback-intake` submission for the separate quality feedback inbox. It is evidence for recipe or tuple quality review, not a recipe materialization request and not proof that gpucall routed incorrectly.

#### Quality Feedback Grammar For Structured Output

When gpucall returns `200 OK` and `output_validated=true`, `response_format:
json_object` means only that the returned value is a JSON object. It does not
mean that caller-specific fields are present. If the caller requires fields such
as `articles`, `headline_original`, or `rank`, the production request should use
`response_format: json_schema` with those fields marked as required.

If a caller used `json_object` and then rejected the result because the business
schema was wrong, submit quality feedback with structured-output metadata:

```bash
gpucall-recipe-draft quality \
  --task vision \
  --mode sync \
  --intent understand_document_image \
  --expected-output articles_json \
  --content-type image/jpeg \
  --bytes 1136521 \
  --observed-recipe vision-understand-document-image-draft \
  --reported-tuple modal-h100-qwen25-vl-3b \
  --output-validated true \
  --quality-failure-kind schema_mismatch \
  --observed-output-kind json_object_wrong_schema \
  --response-format json_object \
  --expected-json-schema expected-schema.json \
  --observed-json-schema observed-schema.json \
  --schema-success-count 5 \
  --schema-failure-count 16 \
  --remote-quality-inbox admin@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox \
  --source example-caller-app
```

`expected-schema.json` is the caller's desired JSON Schema. It may contain
property names, `type`, `required`, `properties`, `items`, numeric/string
limits, and composition keywords. Do not include examples, raw values, prompt
text, document text, or output excerpts.

`observed-schema.json` is the observed output shape, not the observed output. It
must contain keys and types only. For example, this is safe:

```json
{
  "type": "object",
  "required": ["contains_text", "dominant_color", "summary"],
  "properties": {
    "contains_text": {"type": "boolean"},
    "dominant_color": {"type": "string"},
    "summary": {"type": "string"}
  }
}
```

This feedback tells the administrator whether the correct action is caller-side
`json_schema`, recipe-level schema defaults, or stronger tuple validation. The
caller-side helper remains deterministic and does not inspect or forward raw
model output.

### Phase 2: Local Draft Summary

This phase consumes only the sanitized intake JSON and does not call an LLM.

The draft command creates a deterministic review artifact. It is useful as a cover sheet for the administrator, not as production config.

```bash
gpucall-recipe-draft draft --input intake.json --output recipe-draft.json
```

The output is not production config. It is a review artifact for gpucall administrators.

### Submission

The caller-side helper can submit sanitized intake and draft artifacts to a file-based inbox. This is not a gpucall API. The inbox can be a local shared directory, or an SSH-accessible directory controlled by the gpucall administrator.

Production deployments should not depend on a `root` submitter writing files
that a different unprivileged watcher later reads. The recommended shape is a
dedicated operator account or shared service group that owns the dropbox:

```bash
install -d -o gpucall -g gpucall-intake -m 2770 /opt/gpucall/state/recipe_requests/inbox
install -d -o gpucall -g gpucall-intake -m 2770 /opt/gpucall/state/quality_feedback/inbox
```

Caller SSH access should be scoped to that approved inbox path, preferably by a
forced command or deployment-specific SSH policy. The submitted payloads are
sanitized metadata only; API keys, prompt bodies, raw model output, DataRef
URIs, and presigned URLs must not be included. The SDK remote submitter writes
the final JSON atomically and makes it readable by the gateway watcher as a
compatibility guard for cross-user dropboxes, but a production installation
should still use service-account ownership, group permissions, default ACLs, or
an equivalent restricted intake mechanism instead of relying on `root`.

```bash
gpucall-recipe-draft submit \
  --intake intake.json \
  --draft recipe-draft.json \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --source example-caller-app
```

For remote submission:

```bash
gpucall-recipe-draft submit \
  --intake intake.json \
  --draft recipe-draft.json \
  --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox \
  --source example-caller-app
```

Remote submission uses SSH, creates the target directory if needed, writes a temporary file, and atomically renames it to `<request_id>.json`. The submitted bundle contains only the sanitized intake and optional draft. It does not contain raw prompt bodies, DataRef URIs, presigned URLs, or secrets.

For caller automation, `preflight` and `intake` accept `--inbox-dir`, `--remote-inbox`, and `--source`. `quality` accepts those legacy flags plus `--quality-inbox-dir` and `--remote-quality-inbox`; operators should route quality feedback to the separate quality feedback inbox. When an inbox flag is present, the helper writes the sanitized intake and submits it in one command.

After submitting, callers should poll the same approved inbox for a sanitized
status summary instead of waiting for a human copy/paste relay:

```bash
gpucall-recipe-draft status \
  --pipeline recipe \
  --request-id rr-20260510T104401Z-2add39e6d6d2 \
  --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox

gpucall-recipe-draft status \
  --pipeline quality \
  --request-id rr-20260511T124553Z-03157f894e6d \
  --remote-quality-inbox admin@gateway.example.internal:/opt/gpucall/state/quality_feedback/inbox
```

Convenience aliases are available:

```bash
gpucall-recipe-draft recipe-status --request-id rr-... --remote-inbox "$GPUCALL_RECIPE_INBOX"
gpucall-recipe-draft quality-status --request-id rr-... --remote-quality-inbox "$GPUCALL_QUALITY_FEEDBACK_INBOX"
```

The status output includes only processing state, decision, task, intent,
quality kind, safe blockers/warnings, and next actions. It does not return raw
prompt text, raw model output, DataRef URIs, presigned URLs, or API keys.

## Caller-Facing Intents

Callers should describe intent at a high level. They should not specify GPU names, provider names, model names, or gpucall-internal capability labels.

Examples:

- `summarize_text`
- `rank_text_items`
- `translate_text`
- `extract_json`
- `caption_image`
- `answer_question_about_image`
- `understand_document_image`
- `transcribe_audio`
- `summarize_audio`
- `summarize_video`

gpucall administrators map these intents to recipe intent and tuple contracts.

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
5. Administrator writes canonical gpucall recipe intent and production tuple YAML.
6. Administrator runs `gpucall validate-config`, tests, provider validation, and `gpucall launch-check`.
7. Only reviewed and validated config is committed and deployed.

## Gateway-Side Admin Helper

The caller-side helper ships with the SDK. The administrator-side helper ships with the gateway package, not the SDK.

Before materializing a submitted request, review it against the capability catalog:

```bash
gpucall-recipe-admin review \
  --input /path/to/gpucall-recipe-requests/inbox/rr-....json \
  --config-dir config
```

The review checks the sanitized request against recipe, model, engine, resource catalog, policy, and live validation evidence. If existing execution tuples are insufficient, the report includes a `required_execution_contract` describing the missing model/engine/resource/contract tuple that must be authored and validated.

For low-friction operations, a gpucall administrator may choose an explicit accept-all policy for sanitized caller intake:

```bash
gpucall-recipe-admin materialize \
  --input intake.json \
  --output-dir config/recipes \
  --config-dir config \
  --report materialization-report.json \
  --accept-all
```

`--accept-all` is required so accidental materialization fails closed. This command writes canonical recipe YAML for the current gpucall schema. With `--config-dir`, materialization consults the installed recipe/model/engine/tuple catalog before writing YAML. Long-context, batch or long-running workloads, and workloads whose satisfying tuple candidates declare `expected_cold_start_seconds` above the sync-safe threshold are materialized as async-only recipes. The caller's requested mode is treated as intake evidence, not as routing authority. The command does not create a capable provider, does not edit policy, and does not deploy anything.

When failure intake reports ultra or mega context requirements, the
materializer still writes a draft recipe contract instead of failing because no
current tuple can run it. Context budgets up to the fixed catalog tiers are
rounded to the next known tier; requirements above the current ultra tier are
rounded to the next power of two and marked as `scale: mega` in the
materialization report and required execution contract. These drafts remain
async-only, `auto_select: false`, and require administrator tuple authoring,
billable validation, and explicit activation before production routing.

After materialization:

```bash
gpucall validate-config --config-dir config
gpucall launch-check --profile static --config-dir config
```

If validation reports that no execution tuple satisfies the new recipe, the administrator must add or enable an appropriate tuple before production use.

### Inbox Automation

For operators who choose to accept every submitted sanitized request, the admin helper can process the inbox without an API endpoint:

```bash
gpucall-recipe-admin process-inbox \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --output-dir config/recipes \
  --config-dir config \
  --accept-all
```

Processed submissions are moved to `inbox/processed`, failed submissions to `inbox/failed`, and materialization reports to `inbox/reports`. The original JSON file remains the canonical submission record; it is not deleted after materialization. `process-inbox` and `watch` also maintain `inbox/recipe_requests.db`, a SQLite WAL index containing request id, source, task, intent, status, original/report/recipe paths, original SHA-256, and timestamps.

Quality feedback uses a separate inbox because it is evidence about an existing
recipe or tuple, not a recipe materialization request:

```bash
gpucall-recipe-admin process-quality-inbox \
  --inbox-dir /path/to/gpucall-quality-feedback/inbox \
  --config-dir config

gpucall-recipe-admin quality-inbox list \
  --inbox-dir /path/to/gpucall-quality-feedback/inbox
```

Processed feedback is moved to `inbox/processed`, failed feedback to
`inbox/failed`, and review reports to `inbox/reports`. The helper maintains
`inbox/quality_feedback.db` with feedback id, source, task, intent, quality
kind, observed tuple, status, paths, SHA-256, and timestamps. This route does
not write recipe YAML, does not activate tuples, and does not run billable
validation. Administrators use the report as evidence for recipe revision,
model upgrade, or tuple promotion decisions.

`--accept-all` is a one-shot operator decision. For a persistent operator host,
the same route can be opened explicitly in config:

```yaml
# config/admin.yml
recipe_inbox_auto_materialize: true
```

When this flag is absent or false, `process-inbox` and `watch` fail closed unless
`--accept-all` is present. When it is true, sanitized caller submissions can be
automatically reviewed and materialized into draft recipe YAML with a static
catalog-readiness report. Existing recipe names are linked in the report instead
of overwritten unless `--force` is explicit. Even with `--force`, materialization
refuses to narrow an existing recipe contract by reducing context budget, input
bytes, allowed modes, model capabilities, MIME prefixes, or data classification.
Use `--allow-contract-narrowing` only for a deliberate operator rollback after
reviewing the existing and proposed contract. This route does not run billable
validation and does not activate production routing. Use `gpucall-recipe-admin
promote` explicitly when a recipe should be elevated to production.

Operators who deliberately want full automation can open the escalation chain in
`config/admin.yml`:

```yaml
recipe_inbox_auto_materialize: true
recipe_inbox_auto_validate_existing_tuples: false
recipe_inbox_auto_activate_existing_validated_recipe: false
recipe_inbox_auto_promote_candidates: true
recipe_inbox_auto_billable_validation: true
recipe_inbox_auto_activate_validated: true
recipe_inbox_auto_require_auto_select_safe: true
recipe_inbox_auto_set_auto_select: false
recipe_inbox_auto_run_validate_config: true
recipe_inbox_auto_run_launch_check: false
recipe_inbox_promotion_work_dir: /opt/gpucall/state/recipe_requests/promotions
```

The chain is ordered and fail-closed:

- `recipe_inbox_auto_promote_candidates` requires `recipe_inbox_auto_materialize`.
- `recipe_inbox_auto_validate_existing_tuples` requires `recipe_inbox_auto_materialize`.
- `recipe_inbox_auto_activate_existing_validated_recipe` requires existing tuple validation.
- `recipe_inbox_auto_billable_validation` requires auto-promotion or existing tuple validation.
- `recipe_inbox_auto_activate_validated` requires billable validation.
- `recipe_inbox_auto_set_auto_select` requires an activation path.
- `recipe_inbox_auto_run_launch_check` requires validate-config automation.

When enabled, `process-inbox` and `watch` reuse the same review and promotion
pipeline as the manual commands. Candidate promotion materializes provider tuple,
surface, and worker YAML into an isolated promotion workspace. Billable
validation runs `gpucall tuple-smoke`. Activation copies only validated recipe
and tuple config into the active config directory. If no candidate tuple exists,
the report records `SKIPPED_NO_TUPLE_CANDIDATE` and does not invent a provider.
If an existing production tuple already satisfies the materialized recipe,
operators can separately allow existing-tuple validation and activation. That
path writes a production recipe only after matching live validation evidence is
present or after billable validation succeeds. It can set `auto_select: true`
only when `recipe_inbox_auto_set_auto_select` is enabled, and by default it
requires the auto-select shadowing review to be safe.

To poll continuously:

```bash
gpucall-recipe-admin watch \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox \
  --output-dir config/recipes \
  --config-dir config \
  --accept-all \
  --interval-seconds 10
```

This still only writes recipe YAML. It does not deploy, does not edit provider specs, and does not bypass `validate-config` or launch checks.

### Administrator LLM-assisted refinement

The deterministic materializer intentionally writes a conservative first draft.
When the first draft has validation errors, low-quality success evidence, or
operator review notes, an administrator can ask gpucall itself to produce a
proposal patch:

```bash
gpucall-recipe-admin author \
  --report /path/to/inbox/reports/rr-....report.json \
  --config-dir config \
  --output /path/to/inbox/reports/rr-....authoring.json
```

This is the only designed LLM-assisted part of the recipe lifecycle. The helper
builds a sanitized bundle from the materialization report, admin review,
readiness, validation attempts, and catalog summary, then runs the admin-only
`admin-author-recipe-draft` recipe. The output is a proposal artifact containing
a JSON Patch-style `patch`, a `validation_plan`, and `risk_notes`.

The command never writes production config. It rejects guarded patch targets
such as `/auto_select`, `/name`, and `/task`. Applying the proposal, validating
it, running billable smoke, and activating it remain deterministic
administrator-side steps.

For private operations, configure a controlled local OpenAI-compatible runtime
and keep the authoring recipe as an explicit admin recipe. The compiler prefers
eligible `local_runtime` tuples before remote GPU tuples, so recipe authoring can
dogfood gpucall without sending operator evidence to a hosted provider.

Until a DeepSeek V4 Flash ds4 host is available, operators can register a
smaller local Ollama model as the interim authoring runtime:

```bash
gpucall runtime add-ollama \
  --name local-author-ollama \
  --endpoint http://127.0.0.1:11434 \
  --model qwen2.5-32b:latest \
  --max-model-len 32768
gpucall runtime validate --name local-author-ollama
gpucall validate-config
```

This is an operator-controlled local fallback, not a replacement for ds4. When a
proper ds4 host is later available, register it with `gpucall runtime
add-openai --name local-ds4 --endpoint http://127.0.0.1:8000/v1 --model
deepseek-v4-flash` and keep the same `admin-author-recipe-draft` workflow.

To inspect a submitted request:

```bash
gpucall-recipe-admin status \
  --request-id rr-20260506T010203Z-abcdef123456 \
  --inbox-dir /path/to/gpucall-recipe-requests/inbox
```

Use `--index-db` with `process-inbox`, `watch`, or `status` only when the operator wants that index somewhere other than `inbox/recipe_requests.db`.
