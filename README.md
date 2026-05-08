# gpucall v2.0

[日本語版 README](README.ja.md)

**gpucall is a gateway that keeps GPU / model / provider choice out of application code and enforces GPU execution with 100% deterministic routing from organizational policy and evidence.**

Sending business data to hosted AI APIs such as Gemini, GPT, or Claude is, for many organizations, still an external transfer of internal resources. The natural way to avoid that is to run LLMs, vLLM, or Transformers on GPU capacity under your own governance. Buying and operating GPUs directly, however, means large upfront spend, procurement lead time, operational burden, hardware failure handling, and idle capacity.

gpucall targets the practical middle path: **keep data and control inside the organization, but rent GPU compute from the cloud when needed**. Once cloud GPUs enter the picture, the organization still has to know which execution surface ran the job, whether the price was current, whether the tuple was validated, and whether the route satisfied policy. gpucall enforces those checks in the gateway instead of leaving them to ad hoc application-side decisions.

Behind an OpenAI-compatible facade, gpucall normalizes heterogeneous GPU execution surfaces such as Modal serverless functions, RunPod managed endpoints, Hyperstack VMs, and local runtimes. Application code sends only `task`, `mode`, input data, or `DataRef`s. gpucall joins recipe, model, engine, execution tuple, price freshness, validation evidence, and tenant policy, then routes only to production tuples that are allowed to run.

The v2.0 MVP production scope is `infer` and `vision`.

## What Need Does gpucall Serve?

When teams put LLM / Vision workloads into real business systems, the same problems keep appearing:

- **Internal data should not be sent to hosted AI APIs by default**: SaaS AI APIs are useful, but sending business data, customer data, unpublished documents, or internal analysis to an external API is itself a governance event.
- **Buying GPUs is too heavy**: Owned GPUs bring procurement cost, installation, operations, failure handling, idle capacity, and poor fit for bursty demand. Teams often want to rent cloud GPUs only when needed.
- **Application code starts choosing models, providers, and GPUs**: Strings such as `claude-haiku`, `gpt-4o`, or `modal-h100` get scattered across application code, undermining policy and cost control.
- **Hosted API gateways are not enough**: Tools such as LiteLLM and Portkey are strong at unifying hosted model providers, but they do not primarily own the lifecycle, validation, cleanup, billable smoke checks, and price freshness of GPU execution surfaces you rent yourself.
- **Kubernetes inference stacks assume too much**: Not every execution surface lives inside one Kubernetes cluster. In practice, serverless GPUs, managed endpoints, IaaS VMs, and local runtimes coexist.
- **Routes must not silently bypass policy when conditions are not met**: If no GPU is available, price data is stale, or only unvalidated execution targets exist, "just send it to another cheap model" can become a cost or information-governance incident.
- **New business requirements need a safe intake path**: When an application needs work the current configuration cannot handle, the answer should not be to send raw prompts or confidential files to an administrator. It should submit intent, and the operator should review, configure, validate, and promote support safely.

gpucall fills that gap. Existing applications call an OpenAI-compatible API or the gpucall SDK, while GPU / provider / model selection is pulled back into the gateway. Unknown workloads fail closed. The caller-side helper produces sanitized intake, and the administrator-side helper moves that intake through the recipe / tuple / validation pipeline.

## Core Selling Point: 100% Deterministic Routing

gpucall does not use an LLM for routing decisions.

Which recipe is selected, which tuple is considered, which provider comes next in fallback order, whether price freshness is acceptable for budget policy, whether validation evidence is production-ready, and whether tenant policy allows the route are all deterministic evaluations over catalog, policy, runtime evidence, and request metadata.

That means:

- The same input, catalog, policy, and live evidence produce the same routing decision.
- Operators can audit why one tuple was selected and why another tuple was rejected.
- LLM-based "smart model routing" or prompt classification does not enter the gateway runtime.
- Unknown, stale, unvalidated, and over-budget states fail closed.

gpucall is not a router that tries to look clever. It is a GPU governance router whose decisions are explainable, reproducible, and auditable.

## Product Shape

gpucall is a three-part product, not just a gateway binary:

- **Gateway runtime scripts**: deterministic request admission, recipe selection, tuple routing, policy enforcement, audit, validation gates, cleanup, and fail-closed execution.
- **Caller-side helper**: the SDK-distributed `gpucall-recipe-draft` tool. It lets external systems submit sanitized workload intent, preflight metadata, post-failure intake, and low-quality-success feedback without exposing raw content or choosing providers, GPUs, models, or tuples.
- **Administrator-side helper**: the gateway-distributed `gpucall-recipe-admin` tool. It reviews caller intake, materializes recipe intent, derives missing execution contracts, promotes candidate tuples through isolated config and billable validation, and only then allows production activation.

The responsibility boundary is part of the product contract: callers describe workload intent; administrators manage catalogs, tuples, validation evidence, and production promotion; the gateway executes only validated policy decisions.

## Why Not an Existing Router or Inference Stack?

gpucall is not trying to replace every LLM gateway, Kubernetes inference stack, or GPU provisioner. It occupies a narrower control-plane gap: policy-enforced execution across heterogeneous leased GPU surfaces where the gateway owns recipe selection, tuple routing, validation evidence, price freshness, cleanup, and audit.

Adjacent systems solve different layers:

| Category | Examples | What they are good at | gpucall boundary |
| :--- | :--- | :--- | :--- |
| LLM API gateways | [LiteLLM](https://docs.litellm.ai/), [Portkey AI Gateway](https://portkey.ai/docs/product/ai-gateway) | Unified API access to many hosted model providers, virtual keys, fallback, cost tracking, guardrails, and observability | gpucall manages leased GPU execution surfaces and production tuple promotion, not only hosted API provider selection |
| Model/provider marketplaces | [OpenRouter](https://openrouter.ai/docs/guides/routing/provider-selection) | Routing across model providers behind a SaaS API | gpucall is designed for operator-owned governance over recipes, tuples, validation artifacts, object-store DataRefs, and provider lifecycle |
| Kubernetes inference stacks | [llm-d](https://llm-d.ai/) and Kubernetes inference-gateway patterns | High-performance distributed inference inside Kubernetes, KV-cache-aware routing, prefill/decode separation, cluster-native operations | gpucall does not require all execution to live inside one Kubernetes cluster; it normalizes Modal functions, RunPod endpoints, Hyperstack VMs, and local runtimes under one governance contract |
| GPU provisioning tools | GPU cloud provisioners and cluster schedulers | Acquiring or scheduling GPU capacity | gpucall treats capacity as one input to deterministic routing, then adds recipe fit, model/engine compatibility, security policy, validation evidence, cost freshness, and cleanup/audit contracts |

The non-overlap is deliberate. gpucall's differentiated surface is the combination of:

- **Heterogeneous execution governance**: Modal serverless functions, RunPod managed endpoints, Hyperstack VMs, and local runtimes are represented as execution tuples rather than caller-selected providers.
- **Deterministic four-catalog routing**: recipe, model, engine, and execution tuple compatibility are evaluated without LLM-based routing.
- **Validation evidence before production**: tuples are promoted through review, endpoint configuration, billable validation, and activation gates instead of being trusted because a YAML entry exists.
- **Price freshness as policy input**: configured prices and live price evidence are separated; strict budget mode can fail closed on stale or unknown price data.
- **Data-plane-less caller integration**: external systems can submit `DataRef`s and sanitized recipe requests without giving the gateway raw payload bytes or provider choice.

## LLM Boundary

The gateway runtime is a deterministic governance runtime. It must not use an LLM to choose recipes, tuples, providers, GPUs, models, prices, stock state, fallback order, cleanup actions, or production promotion.

LLM inference is allowed only after deterministic routing has selected a production tuple and delivered the worker payload to the chosen execution surface. At that point, the provider worker may run vLLM, Transformers, worker-vLLM, or another declared model engine to process the caller's task.

The caller-side and administrator-side helpers are boundary tools. The caller-side helper remains deterministic and only prepares sanitized intake. If LLM-assisted recipe authoring is ever used, it belongs only in an audited administrator-side workflow over sanitized intake; production activation still requires deterministic materialization, validation evidence, launch checks, and deployment.

## Quickstart

```bash
gpucall init
gpucall configure
gpucall validate-config
gpucall doctor
gpucall tuple-audit
gpucall execution-catalog candidates --recipe text-infer-standard
gpucall lease-reaper
gpucall cost-audit
gpucall cleanup-audit
gpucall launch-check --profile static
gpucall release-check
docker compose -p gpucall up -d --build
gpucall smoke
gpucall cost-audit --live
gpucall cleanup-audit
gpucall launch-check --profile production --url http://127.0.0.1:18088
gpucall audit verify
```

Production-like runtime layout follows XDG:

- Config: `$XDG_CONFIG_HOME/gpucall` or `~/.config/gpucall`
- State: `$XDG_STATE_HOME/gpucall` or `~/.local/state/gpucall`
- Cache: `$XDG_CACHE_HOME/gpucall` or `~/.cache/gpucall`

## MVP Scope

Production-supported v2.0 tasks:

- Tasks: `infer`, `vision`
- Draft control-plane recipe contracts: `transcribe`, `convert`, `train`, `fine-tune`, `split-infer`
- Modes: `sync`, `async`, `stream`
- Object store: S3-compatible API, including Cloudflare R2 via endpoint override
- Deployment: Docker Compose
- State: SQLite WAL by default; Postgres job/idempotency backend with `GPUCALL_DATABASE_URL`
- Optional deployment manifests: Helm, systemd, Postgres DDL, Prometheus alerts, Grafana dashboard

Not production-supported in v2.0:

- high-confidential provider live connections for TEE/sovereign execution

## Secrets

Secrets do not belong in YAML. Use `gpucall configure`, environment variables, or a deployment secret manager.

```bash
gpucall security scan-secrets
```

Provider YAML should contain resource shape and routing metadata only.

## License

Copyright (c) 2026 Sugano Tamotsu. All rights reserved.

This repository is public for evaluation, integration review, and security discussion. It is not open source unless a separate written license says otherwise.

## SaaS v1 Operations

External SaaS operation uses tenant quota YAML plus credentials-managed tenant API keys. See [docs/SAAS_V1_OPERATIONS.md](docs/SAAS_V1_OPERATIONS.md).

## Python SDK

```python
from gpucall_sdk import GPUCallClient

with GPUCallClient("http://127.0.0.1:18088") as client:
    print(client.infer(prompt="hello"))
```

Async polling is hidden by default:

```python
from gpucall_sdk import AsyncGPUCallClient

async with AsyncGPUCallClient("http://127.0.0.1:18088") as client:
    job = await client.infer(mode="async", prompt="hello")
```

Files are uploaded to the configured object store with presigned PUT and sent to the gateway as `DataRef`. The SDK is distributed as the separate `gpucall-sdk` package; the gateway wheel does not include the SDK package.

## TypeScript SDK

```ts
import { GPUCallClient } from "@gpucall/sdk";

const client = GPUCallClient.fromEnv("http://127.0.0.1:18088");
const result = await client.infer({ prompt: "hello" });
```

## External System Migration

When adapting another product or service to gpucall, use the one-shot migration prompt in [docs/EXTERNAL_SYSTEM_ADAPTATION_PROMPT.md](docs/EXTERNAL_SYSTEM_ADAPTATION_PROMPT.md). External systems should normally send only `task`, `mode`, and input data or `DataRef`; recipe and provider selection belong to the gateway.

For productized migration, use the deterministic migration kit:

```bash
gpucall-migrate assess /path/to/project --source example-caller-app
gpucall-migrate preflight /path/to/project --source example-caller-app
gpucall-migrate canary /path/to/project --command "uv run python -m src.pipeline.main"
gpucall-migrate patch /path/to/project
gpucall-migrate onboard /path/to/project --source example-caller-app
```

The migration kit scans source files, classifies direct OpenAI/Anthropic paths,
detects caller-side routing selectors, generates sanitized preflight commands,
runs optional canaries, and writes JSON/Markdown reports under
`.gpucall-migration`. It is deterministic and does not call an LLM.

If a caller's workload is unknown to the installed recipe catalog and production tuples, gpucall fails closed instead of guessing or routing to a weaker model. If gpucall returns `200 OK` but the caller's own business validator rejects the output, treat it as low-quality success feedback. Use the SDK-distributed `gpucall-recipe-draft` helper to sanitize either case and submit a recipe intent request for gpucall administrators. See [docs/RECIPE_DRAFT_TOOL.md](docs/RECIPE_DRAFT_TOOL.md).

Unknown workloads return a structured governance error instead of being silently routed:

- `422 NO_AUTO_SELECTABLE_RECIPE`: no installed recipe honestly describes the request.
- `503 no eligible provider after policy, recipe, and circuit constraints`: a recipe exists, but no currently eligible provider can execute it.

The response includes a `failure_artifact` with redacted request metadata, rejection reasons, `caller_action`, and a redaction guarantee.

When this happens, run the independent helper:

```bash
gpucall-recipe-draft preflight --task vision --intent understand_document_image --content-type image/png --bytes 2000000 --output preflight-intake.json
gpucall-recipe-draft intake --error gpucall-error.json --intent <caller-intent> --output intake.json --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
gpucall-recipe-draft quality --task vision --intent understand_document_image --quality-failure-kind insufficient_ocr --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
gpucall-recipe-draft compare --preflight preflight-intake.json --failure intake.json --output drift-report.json
gpucall-recipe-draft draft --input intake.json --output recipe-draft.json
gpucall-recipe-draft submit --intake intake.json --draft recipe-draft.json --remote-inbox admin@gateway.example.internal:/opt/gpucall/state/recipe_requests/inbox
```

The caller-side helper is deterministic and does not call an LLM. It prepares sanitized intake and an optional local draft summary so gpucall administrators can decide whether the workload class should become a supported recipe. With `--inbox-dir` or `--remote-inbox`, the helper submits sanitized intake directly to the approved operator inbox. Remote submission uses SSH and does not call the gateway API. If the administrator adopts an accept-all policy, the gateway-side `gpucall-recipe-admin materialize --accept-all` helper can turn sanitized intake into canonical recipe YAML. Any draft or materialized recipe still requires `validate-config`, tests, launch checks, and deployment before subsequent requests can use it.

For fully file-based automation without adding a gateway API, administrators can run:

```bash
gpucall-recipe-admin watch --inbox-dir /path/to/inbox --output-dir config/recipes --accept-all
```

For a persistent operator host, the same route can be opened by config instead
of a per-command flag. This remains disabled by default:

```yaml
# config/admin.yml
recipe_inbox_auto_materialize: true
```

With that file present, `gpucall-recipe-admin watch` and `process-inbox` can
materialize sanitized caller submissions without `--accept-all`. This route only
writes reviewed recipe YAML and a static catalog-readiness report. Billable
smoke validation and production activation are separate explicit promotion
steps, because they can spend provider money or mutate active routing.

Inbox processing preserves the original submitted JSON as the audit source of
truth under `inbox/processed` or `inbox/failed`. It also maintains a SQLite WAL
index at `inbox/recipe_requests.db` with request id, source, task, intent,
status, file paths, SHA-256, and timestamps so operators can query request
history without treating the database as the canonical payload store.

The operator inbox and runtime readiness are queryable without running billable
validation:

```bash
gpucall-recipe-admin inbox list --inbox-dir /path/to/inbox
gpucall-recipe-admin inbox status --inbox-dir /path/to/inbox --request-id rr-...
gpucall-recipe-admin inbox materialize --inbox-dir /path/to/inbox --output-dir config/recipes --accept-all
gpucall-recipe-admin inbox readiness --inbox-dir /path/to/inbox --config-dir config
gpucall readiness --config-dir config --intent translate_text
```

## Routing

gpucall is a deterministic governance router, not a Modal-only proxy. Recipe and provider selection rules are documented in [docs/ROUTING_POLICY.md](docs/ROUTING_POLICY.md).
Capability catalog rules for recipe/model/engine/provider matching are documented in [docs/CAPABILITY_CATALOG.md](docs/CAPABILITY_CATALOG.md).
RunPod Flash production validation is documented in [docs/RUNPOD_FLASH.md](docs/RUNPOD_FLASH.md).
RunPod Serverless catalog expansion rules are documented in [docs/RUNPOD_SERVERLESS_CATALOG.md](docs/RUNPOD_SERVERLESS_CATALOG.md).

## Zero-Trust Contracts

Provider definitions declare a `trust_profile` separate from recipe compute requirements. Restricted workloads are routed only to dedicated GPU providers or approved security tiers such as `confidential_tee` with attestation support or `split_learning`; shared GPU providers are rejected before execution. Governance hashes are deterministic over the request, policy, recipe, provider contract, and worker-readable DataRef set, excluding runtime IDs.

Workers consume gateway-presigned HTTP(S) DataRefs by default. Ambient `s3://` worker credentials are disabled unless explicitly opted in for a non-default worker environment. Chained artifacts are recorded as encrypted `ArtifactManifest` entries in the append-only Artifact Registry; the gateway stores lineage, version, checksums, key ids, and attestation references, not plaintext artifact bytes.

Provider-independent v2.1 control-plane contracts are implemented for `train`, `fine-tune`, and `split-infer`: explicit artifact export versions, key-release requirements, attestation-bound execution gates, split-learning activation refs, and artifact manifest validation. Provider adapters for Azure/GCP sovereign TEE and split-learning workers remain separate implementation work.

## Object Lifecycle

For Cloudflare R2 or S3-compatible buckets, configure lifecycle expiration for the gpucall prefix. A conservative MVP setting is:

- Prefix: `gpucall/`
- Expire objects after: 1-7 days
- Keep public access disabled
- Limit API token permission to object read/write for the bucket

## Provider Failures

Provider outages, remote capacity exhaustion, authentication failures, and provider-side queueing are outside the gateway SLA. The gateway records retryability, opens circuit breakers, and moves through the deterministic fallback chain.

## Launch Checks

```bash
gpucall validate-config
gpucall doctor
gpucall tuple-audit
gpucall cost-audit
gpucall cleanup-audit
gpucall launch-check --profile static
gpucall seed-liveness text-infer-standard --count 100 --budget-usd 0.10
gpucall registry show
gpucall smoke
gpucall cost-audit --live
gpucall cleanup-audit
gpucall launch-check --profile production --url http://127.0.0.1:18088
gpucall audit verify
gpucall post-launch-report
```

Production launch checks require gateway auth, object-store credentials, a live gateway smoke result, complete provider cost metadata, live provider cost/resource audit access, cleanup audit success, and provider-validation JSON artifacts. Static launch checks remain available for local config validation.
