# gpucall Refactor Knowledge Base

Last verified: 2026-05-16 JST
Branch: `codex/rc-audit-product-static-conditional-go`
Current HEAD: `aa342ee rc: harden production canary gates`

This document is the durable working memory for the upcoming large refactor.
It intentionally separates four kinds of information:

- observed facts from the current codebase
- product/design decisions agreed during review
- refactor requirements derived from those decisions
- execution gates that must not be bypassed

Do not treat aspirational target architecture as already implemented code.
Do not treat current implementation facts as permission to preserve an
incorrect boundary.

## Execution Packet Index

Do not use this long knowledge base as the only Codex CLI instruction. For
implementation work, use the short execution packets first:

1. `docs/refactor/00_PRIME_DIRECTIVE.md`
2. `docs/refactor/01_README_V2_CLAIM_MATRIX.md`
3. one phase-specific packet:
   - `docs/refactor/02_PROTOCOL_ADMISSION.md`
   - `docs/refactor/03_PROVIDER_EGRESS.md`
   - `docs/refactor/04_RECIPE_CONTROL_PLANE.md`
   - `docs/refactor/05_STATE_DATAREF_AND_SECURITY.md`
   - `docs/refactor/06_RELEASE_GATES.md`
4. `docs/refactor/07_CODEX_CLI_EXECUTION_PROTOCOL.md` when writing prompts or
   reviewing Codex CLI progress.

Use this file as the detailed background map after the short packets are read.

## Indexed Source Snapshot

The line-level index is stored locally at:

```text
/Users/tamotsu/Projects/gpucall/.state/refactor_knowledge/gpucall_refactor_knowledge.sqlite
```

Indexed scope:

```text
files:        149
python files: 139
lines:        51,507
symbols:      2,650
```

The SQLite DB contains:

- `files(path, suffix, bytes, lines, sha256, indexed_at)`
- `lines(path, line_no, text)`
- `symbols(path, kind, name, lineno, end_lineno, parent)`
- `imports(path, module, name, lineno)`

Largest source files:

```text
3015 gpucall/cli.py
2220 tests/test_providers.py
1819 tests/test_app.py
1679 gpucall/recipe_admin.py
1267 sdk/python/gpucall_sdk/client.py
1226 gpucall/execution_surfaces/hyperstack_vm.py
1157 gpucall/worker_contracts/modal.py
1130 gpucall/app.py
1031 gpucall/execution_catalog.py
1014 tests/test_config.py
1003 gpucall/execution_surfaces/managed_endpoint.py
```

Runtime config scale:

```text
recipes:      27
tuples:       3233
models:       66
engines:      9
runtimes:     2
tenants:      1
object_store: false
```

Tuple adapter counts:

```text
hyperstack:                1465
runpod-vllm-serverless:     845
runpod-vllm-flashboot:      844
modal:                       77
local-ollama:                 1
echo:                         1
```

Recipe task counts and auto-select flag count:

```text
infer:       19
vision:       3
convert:      1
fine-tune:    1
split-infer:  1
train:        1
transcribe:   1
auto_select:  5 recipes, not a task
```

## High-Level Runtime Flow

Request path:

```text
FastAPI endpoint
  -> app middleware: body limit, auth, rate limit, metrics
  -> gateway-owned routing enforcement
  -> GovernanceCompiler.compile(TaskRequest)
  -> tenant budget reservation
  -> DataRef worker-readable conversion if needed
  -> Dispatcher.execute_sync / execute_stream / submit_async
  -> TupleAdapter.start / wait / stream
  -> output validation / artifact registration
  -> budget commit or release
  -> JSON/OpenAI-compatible response or failure artifact
```

Core ownership today:

- `gpucall/app.py`: FastAPI app factory, runtime construction, auth, rate limiting, idempotency, budget calls, DataRef conversion, API response mapping, metrics, health/readiness, OpenAI facade endpoint, object presign endpoints.
- `gpucall/app_helpers.py`: response helpers, failure artifacts, budget helper calls, DataRef presign conversion, idempotency helpers, public summaries, OpenAI error shape.
- `gpucall/domain.py`: Pydantic domain model hub for tasks, recipes, tuples, policies, DataRefs, artifacts, jobs, results, provider errors, tenant specs.
- `gpucall/config.py`: YAML loading, split surface/worker join, config validation, controlled runtime validation.
- `gpucall/compiler.py`: deterministic recipe selection, tuple eligibility/ranking, cost estimate, security gate, attestation material, compile artifact, governance hash.
- `gpucall/routing.py`: pure-ish route rejection predicates for production eligibility, tuple constraints, catalog/model/engine compatibility, warning tags.
- `gpucall/dispatcher.py`: tuple fallback execution, runtime admission, provider temporary failure handling, cleanup, async job lifecycle, output validation, artifact registration.
- `gpucall/admission.py`: per-tuple/provider/workload live admission and cooldown state, with in-memory and Postgres variants.
- `gpucall/sqlite_store.py` / `gpucall/postgres_store.py`: job and idempotency persistence.
- `gpucall/tenant.py`: tenant budget ledger with SQLite and Postgres variants.
- `gpucall/artifacts.py`: artifact registry with SQLite and Postgres variants.
- `gpucall/object_store.py`: S3/R2 presign implementation.
- `gpucall/worker_contracts/io.py`: worker-side DataRef fetch, SSRF hardening, SHA validation, S3/HTTP handling.
- `gpucall/local_dataref_worker.py`: local OpenAI-compatible DataRef worker app.
- `gpucall/execution/registry.py`: adapter registration and descriptors.
- `gpucall/execution/payloads.py`: plan-to-worker payload and tuple result normalization.
- `gpucall/execution_surfaces/*`: concrete provider/runtime adapters.
- `gpucall/cli.py`: large command router for serve/init/doctor/validate/runtime/admin/audit/launch/seed/smoke/security/catalog.
- `sdk/python/gpucall_sdk/client.py`: Python SDK sync/async client, upload/DataRef flow, OpenAI-like chat resources, typed errors.

## Current Important Boundaries

### HTTP Runtime Boundary

`gpucall/app.py` currently mixes several concerns that can be separated later:

- ASGI app construction and lifespan
- runtime dependency construction
- auth and tenant identity
- body size protection
- rate limiting and metrics
- idempotency reservation/completion/release
- budget reserve/commit/release
- DataRef conversion to worker-readable presigned references
- endpoint-specific response shaping
- OpenAI-compatible error shaping

This is the highest-value extraction target, but it must wait until live canary is complete.

### Compile/Route Boundary

`GovernanceCompiler.compile()` is deterministic and should remain the central routing authority.
It currently owns:

- recipe selection
- request validation against recipe
- tuple chain selection
- tuple ranking
- cost estimate
- security gate/attestation metadata
- governance hash and compile artifact

`gpucall/routing.py` already contains reusable predicates. A future refactor should move more pure route decisions there, but must preserve exact ordering:

```text
local_preference
route quality penalty
observed reliability tier
vram
max_model_len - required_model_len
cost_per_second
tuple name
```

### Execution Boundary

`Dispatcher` owns runtime execution semantics:

- admission acquire/release
- fallback attempts
- provider-family attempt caps
- provider temporary failure suppression
- remote cleanup
- output validation
- artifact registration
- async job state transitions

Future extraction targets:

- `FallbackExecutor`
- `OutputValidator`
- `ArtifactResultRegistrar`
- `AsyncJobRunner`
- `RemoteCleanupService`

### Persistence Boundary

The following modules implement parallel SQLite/Postgres behavior:

- `sqlite_store.py` / `postgres_store.py`
- `tenant.py`
- `artifacts.py`
- `admission.py`

There is real duplication. Do not collapse it until behavior is locked by tests around:

- idempotency pending/completed reservation
- release on failure
- first-writer semantics
- tenant reservation atomicity
- artifact latest compare-and-set
- Postgres startup smoke

### DataRef Boundary

There are three DataRef areas:

- gateway presign and tenant prefixing in `app.py`, `app_helpers.py`, `object_store.py`
- worker-readable request conversion in `app_helpers.py`
- worker fetch and validation in `worker_contracts/io.py` and `local_dataref_worker.py`

Hardening facts:

- HTTP(S) worker refs require `gateway_presigned=true`.
- HTTP(S) worker refs require allowlisted host.
- URI userinfo is rejected.
- redirects are disabled.
- private/loopback/link-local/multicast/reserved/unspecified resolved addresses are rejected.
- bytes must be non-negative.
- SHA-256 is verified for S3 and HTTP(S).
- ambient S3 credentials require explicit opt-in.

This boundary is security-critical and should not be refactored casually.

## Provider Adapter Surface

Registered adapter families are loaded through `gpucall/execution/registry.py`.
Provider/runtime implementations live primarily in:

- `execution_surfaces/local_runtime.py`
- `execution_surfaces/function_runtime.py`
- `execution_surfaces/managed_endpoint.py`
- `execution_surfaces/hyperstack_vm.py`
- `execution_surfaces/iaas_clouds.py`

Large adapter files are not just adapters; they also contain:

- provider catalog probing
- config validation findings
- health/rejection helpers
- billing guards
- request payload normalization
- provider-specific error mapping
- cleanup/reconcile logic

Future refactor should split provider modules by concern:

```text
adapter runtime
catalog/live inventory
config validation
billing/cost guard
error mapping
payload shaping
cleanup/reconcile
```

## CLI Surface

`gpucall/cli.py` is the single largest Python file at 3,015 lines.
It currently owns parser construction and many command implementations.

Commands include:

- `serve`
- `explain-config`
- `init`
- `doctor`
- `validate-config`
- `runtime`
- `seed-liveness`
- `smoke`
- `tuple-smoke`
- `jobs`
- `registry`
- `catalog`
- `execution-catalog`
- `validator-plan`
- `audit`
- `cost-audit`
- `tuple-audit`
- `cleanup-audit`
- `lease-reaper`
- `security`
- `openapi`
- `launch-check`
- `post-launch-report`
- `release-check`
- `production-acceptance`
- `readiness`
- `setup`
- `configure`
- `admin`

Already split:

- `gpucall/cli_commands/readiness.py`
- `gpucall/cli_commands/setup.py`

Future split should preserve command output exactly and move one command family at a time.

Likely extraction order:

```text
doctor/security
launch/release/post-launch
seed/smoke/tuple-smoke
catalog/execution-catalog/validator-plan
admin/runtime/configure
```

## SDK Boundary

There are two SDK surfaces:

- root shim: `gpucall_sdk/__init__.py`
- canonical package: `sdk/python/gpucall_sdk/`

`sdk/python/gpucall_sdk/client.py` currently owns:

- sync client
- async client
- upload/DataRef helpers
- v2 task helper
- OpenAI-like chat resources
- response extraction
- typed HTTP errors
- warning emission
- log redaction
- caller-side circuit breaker

Root shim exists so source-tree imports can resolve canonical SDK subpackage paths.
Do not remove it without source-tree import tests.

## Tests As Safety Net

Current test distribution:

```text
tests/test_app.py                  94 tests
tests/test_providers.py            59 tests
tests/test_compiler.py             58 tests
tests/test_recipe_admin.py         37 tests
tests/test_dispatcher.py           34 tests
tests/test_config.py               34 tests
tests/test_sdk.py                  30 tests
tests/test_p1_audit_regressions.py 18 tests
```

Known full suite at checkpoint:

```text
526 passed, 1 skipped
```

Refactor rule:

- Run focused tests after each extraction.
- Run full pytest before any checkpoint commit.
- Preserve `tests/fixtures/config/` isolation from mutable repo `config/`.

## Current Release State

Checkpoint status:

```text
Code RC Go / Production traffic No-Go
```

Static/local product validation and production canary gate hardening are
checkpointed.

Known checkpoint commits:

```text
9083e45 rc: checkpoint audit hardening static validation
aa342ee rc: harden production canary gates
```

Production traffic is still blocked by environment/config readiness:

- RunPod production tuples still contain `RUNPOD_ENDPOINT_ID_PLACEHOLDER`
- object store/DataRef live is not configured
- SDK sync/async/OpenAI facade have not succeeded on the same production tuple
- active local-only success is not Production Go evidence

Code-side production canary gate fixes are in `aa342ee`:

- `tuple-smoke` requires explicit `--budget-usd`
- zero-cost estimate smoke requires `--allow-zero-estimate`
- RunPod vLLM health 404 can fall back to OpenAI-compatible models preflight
- tuple promotion validation carries an explicit validation budget

Do not start structural refactor until live canary either passes or is explicitly deferred.

## Prime Directive

The major refactor is not a cosmetic line-count exercise. It must make gpucall
a clear, deterministic, auditable GPU governance router.

Top-level product character:

```text
An honest, elegant router/gateway with no wasteful or duplicate control logic.
```

Non-negotiable product principle:

```text
No inference in control decisions.
```

This means no LLM, heuristic guessing, prompt classification, implicit provider
preference, or hidden model selection may decide:

- request admission
- recipe selection
- provider, model, GPU, runtime, or tuple selection
- fallback order
- budget admission
- confidentiality classification
- price freshness acceptance
- validation readiness
- cleanup actions
- production promotion

All control decisions must be deterministic rule evaluation over explicit
inputs: request metadata, tenant policy, recipes, model catalog, engine catalog,
execution tuples, price evidence, validation evidence, runtime readiness,
budget ledger, idempotency state, provider observations, and object-store/DataRef
preconditions.

LLM execution is allowed only after deterministic routing has selected a
production tuple and delivered the worker payload to that execution surface.
Admin-side LLM assistance, if present for recipe proposal, is non-authoritative
and cannot activate production config without deterministic materialization,
validation evidence, launch checks, and deployment.

## Product Architecture North Star

User-level product abstraction:

```text
gpucall receives LLM/VLM requests from callers through an OpenAI-compatible
protocol boundary, then routes the work to appropriate governed GPU execution
surfaces according to processing intent, workload scale, confidentiality class,
budget, tenant policy, catalog evidence, and validation evidence.
```

Design agreement for the major refactor:

```text
"OpenAI full compatibility" is the ideal at the caller-facing entrance.
The essence of gpucall is safely converting that entrance into a deterministic
governance routing contract.
```

This must become a first-class refactor goal, not a cosmetic cleanup goal.

Current state:

- The core conversion path exists: `/v1/chat/completions` admits an OpenAI-like
  request, converts it into `TaskRequest`, and sends it through the compiler and
  dispatcher.
- The current facade is not full OpenAI compatibility. It is an
  OpenAI-compatible strict subset with fail-closed unsupported feature handling.
- Text-only chat content is admitted through the facade; image/file/DataRef
  production paths are still separate gpucall APIs.
- `model` semantics require stricter product policy because OpenAI treats it as
  caller model selection, while gpucall's core value is to keep model/provider/GPU
  choice out of caller code.

Target architecture:

```text
OpenAI wire contract
  -> protocol admission layer
     - official schema/version validation
     - compatibility classification
     - unsupported feature detection
     - content part and size classification
     - model alias policy
     - metadata/header extraction
     - deterministic rejection reasons
  -> governance routing contract
     - task / intent / mode
     - input kind, size, and DataRef requirements
     - confidentiality class
     - tenant budget context
     - requested capabilities
     - catalog and validation constraints
  -> compiler / dispatcher
```

Refactor implications:

- Extract `openai_facade` into an explicit protocol admission layer.
- Do not scatter OpenAI field interpretation across `app.py`, compiler, SDK, or
  provider adapters.
- Preserve fail-closed behavior for unsupported, unknown, ambiguous, or unsafe
  OpenAI features.
- Avoid making `model` a raw provider/model escape hatch. Treat it as
  `gpucall:auto`, an allowed tenant alias, or recorded request metadata unless a
  product decision explicitly allows more.
- Separate "wire compatibility" from "governance semantics" in tests.

## v2-to-v3 Compatibility Principle

Design agreement:

```text
Completing the v2 ideal must not mean turning gpucall into a narrow
OpenAI-chat-to-cloud-GPU proxy. The v2 ideal must be completed as a deterministic
governance gateway whose central abstraction is the governance routing contract.
```

Why this matters:

- v3 features do not fit inside a simple chat proxy abstraction.
- TEE attestation, sovereignty routing, external KMS key release, encrypted
  artifact lineage, split-learning activation refs, long-running lifecycle,
  remote resource identity, cleanup/reaper behavior, and audit hash chaining all
  attach naturally to the governance contract.
- If v2 is refactored around chat-only request/response mechanics, v3 becomes a
  retrofit instead of an extension.

Do not narrow these contracts during the refactor:

- `TaskRequest` must not become infer/chat-only.
- `TupleResult` must not become text-only.
- Provider adapters must not be reduced to synchronous request/response only.
- Artifact, split-learning, train, fine-tune, DataRef, lease, cleanup, tenant
  policy, and Postgres-backed multi-process state concepts must not be deleted as
  "unused" simplifications.
- `model` must not become a caller-controlled raw provider/model selector.

Preferred direction:

```text
Keep the v2 runtime path simple, but keep the contract broad enough for v3.
Move v3-facing concepts into explicit contracts, interfaces, and policy objects
instead of scattering them or deleting them.
```

## README v2 Claim Alignment

Design agreement:

```text
Treat README v2 statements as implementation contracts during the major
refactor. The refactor must make the correspondence between README claims,
code boundaries, CLI/API surfaces, and tests clearer, not weaker.
```

Current assessment at `aa342ee`:

```text
v2 architecture/control-plane/static product surface: roughly 75-80% implemented
v2 production traffic readiness: roughly 55-60% implemented
```

These percentages are operator assessments, not measured coverage metrics.
Do not use them as completion criteria. Use the traceability matrix and release
gates below instead.

Evidence:

- Active config validates and static launch-check is green.
- Current config exposes 27 recipes, 3233 tuples, 66 models, 9 engines, 2
  controlled runtimes, and 21 API routes.
- CLI surfaces exist for setup, configure, admin tenant operations, runtime
  registration, validation, launch checks, tuple smoke, security scanning,
  recipe admin, caller recipe draft, and migration.
- Deployment artifacts exist for Docker/Postgres, Helm, systemd, Prometheus, and
  Grafana.
- Static/local product surface exists, but production traffic remains No-Go until
  real production tuples, object store/DataRef, and same-tuple SDK/OpenAI facade
  live success are proven.

Refactor requirement:

- Make README v2 claims mechanically traceable to contracts, tests, commands, or
  explicit environment-gated blockers.
- Do not hide missing production prerequisites behind broad "Conditional Go"
  language.
- Keep real provider credentials, real endpoint ids, and object-store secrets out
  of the refactor scope.
- If those environment prerequisites are absent, the system must report
  deterministic Production No-Go blockers rather than pretending readiness.

Out of scope for structural refactor:

- Injecting real RunPod endpoint ids or provider credentials.
- Creating or committing object-store secrets.
- Requiring billable live canary success as a refactor-only gate.

In scope for structural refactor:

- Clearer production-readiness diagnostics.
- Tests proving placeholder endpoint ids, missing object store, missing
  credentials, and unvalidated tuples fail closed.
- Documentation and CLI output that separate Code/Static Go from Production
  traffic Go.

README v2 traceability matrix starter:

| README v2 claim | Required implementation anchor | Current status | Refactor requirement |
| --- | --- | --- | --- |
| OpenAI-compatible facade | protocol admission layer, `/v1/chat/completions`, OpenAI schema fixtures | partial strict subset | Separate wire compatibility from governance semantics; fail closed on unsupported features. |
| 100% deterministic routing | compiler, routing predicates, dispatcher fallback, tenant policy, validation evidence | substantially implemented | Preserve no-inference control path; make rejection/fallback reasons mechanically testable. |
| Gateway runtime owns policy/audit/validation/cleanup | app services, compiler, dispatcher, admission, artifact registry, launch checks | implemented but mixed | Extract boundaries without changing behavior. |
| Caller-side helper | `gpucall-recipe-draft` package and SDK distribution | implemented | Keep deterministic and payload-sanitizing; no provider/model choice. |
| Administrator-side helper | `gpucall-recipe-admin`, materialize/review/promote/watch workflows | implemented but large | Split by workflow; keep activation behind validation and explicit gates. |
| Four-catalog routing | recipe/model/engine/tuple catalogs and config validation | implemented | Make catalog compatibility decisions easier to audit. |
| Validation evidence before production | tuple promotion, tuple-smoke, launch-check, validation artifacts | implemented with environment blockers | Keep billable/live validation explicit and budget-gated. |
| DataRef/object store | presign endpoints, SDK upload, worker-readable refs, worker fetch hardening | code implemented, live env missing | Preserve fail-closed missing object store diagnostics. |
| Controlled runtimes | runtime registration and local adapters | implemented | Keep local success separate from production cloud GPU evidence. |
| Provider failures and fallback | `ProviderErrorCode`, dispatcher fallback, admission cooldowns | implemented | Keep canonical errors; move provider-specific mapping to egress boundary. |
| Postgres-backed multi-process state | jobs, idempotency, tenant ledger, artifact registry, admission | implemented after audit hardening | Preserve behavior parity with SQLite fallback. |
| External migration package | onboarding docs and migration CLI | present | Ensure README claims point to deterministic commands/tests or explicit backlog. |
| v2.1/v3-facing contracts | artifact, train, fine-tune, split-infer, attestation/key-release fields | control-plane contracts present | Do not delete as unused while simplifying v2 runtime. |

## Provider Egress North Star

The exit-side counterpart to protocol admission:

```text
governance routing contract
  -> provider egress layer
  -> provider-specific execution contract
  -> canonical result / canonical error / cleanup evidence
```

Design agreement for the major refactor:

```text
Provider APIs are execution devices, not decision makers.
The compiler and dispatcher decide routing, fallback, budget, policy, and
admission. Provider adapters lower an already-compiled plan into one provider's
wire protocol, then return normalized evidence.
```

Target egress contract:

```text
CompiledPlan / governance contract
  -> provider egress admission
     - tuple is currently executable
     - budget, lease, concurrency, cooldown, and validation evidence are satisfied
     - credential, endpoint, object store, DataRef, and worker prerequisites exist
  -> provider adapter
     - provider payload lowering
     - start / wait / stream / cancel / cleanup
     - provider-specific health, preflight, and inventory
     - provider error mapping
  -> canonical output
     - TupleResult
     - TupleError
     - ProviderErrorCode
     - RemoteHandle / cleanup evidence
     - cost, usage, latency, and audit evidence
```

Adapter responsibility rules:

- Provider adapters may build provider payloads, call provider APIs, normalize
  provider responses, map provider failures, and cancel/cleanup remote work.
- Provider adapters must not decide routing, silently swap models, perform their
  own provider fallback, reinterpret tenant policy, bypass budget policy, invent
  DataRef access paths, or collapse provider errors into generic exceptions.

Current state assessment:

```text
overall provider-egress ideal: roughly 60-65% implemented
core dispatcher owns routing/fallback/admission/cleanup: roughly 75-80%
provider error/result normalization: roughly 65-70%
provider-specific behavior containment: roughly 50-60%
provider module maintainability and separation: roughly 40-50%
```

Evidence:

- `TupleAdapter` already gives the core a common start/wait/cancel/stream shape.
- `Dispatcher` owns tuple-chain traversal, admission, fallback, cleanup,
  registry observation, and output/artifact validation.
- `ProviderErrorCode` gives a canonical provider temporary-unavailability
  vocabulary.
- `execution/registry.py` already carries `endpoint_contract`, `output_contract`,
  and `stream_contract` descriptors.
- Provider modules are still too large and responsibility-heavy:
  `managed_endpoint.py`, `function_runtime.py`, `hyperstack_vm.py`, and
  `worker_contracts/modal.py` mix runtime calls, payload lowering, catalog/live
  validation, inventory, cleanup, cost or contract checks, and error mapping.

Current live-egress failure diagnosis:

```text
The current Production Go failures are not evidence that the provider-egress
abstraction cannot support multiple providers. They are primarily config/env
readiness failures: the active route does not reach a real production cloud GPU
tuple, forced RunPod smoke points at placeholder endpoint ids, and object-store
DataRef prerequisites are absent.
```

Confirmed facts at `aa342ee`:

- A normal text infer compile selects `local-author-ollama`; the compiled chain
  contains no RunPod tuples for that request.
- `config/workers` still contains many `RUNPOD_ENDPOINT_ID_PLACEHOLDER` targets.
- `config/object_store.yml` is absent, so presign/DataRef live canary cannot run.
- Local tuple success is not production cloud GPU evidence.
- The system is correctly failing closed instead of pretending these missing
  provider prerequisites are valid production execution.

Refactor caution:

- Do not misdiagnose the current production blocker as "provider abstraction is
  impossible" or as a reason to collapse provider-specific logic back into core.
- Preserve fail-closed behavior for missing endpoint ids, missing credentials,
  missing object store, and unvalidated production tuples.
- Improve observability and separation so future operators can distinguish:
  route did not include provider, provider config missing, provider preflight
  failed, provider capacity unavailable, DataRef storage missing, and provider
  output invalid.

Refactor implications:

- Make `provider egress layer` a first-class boundary, symmetrical with
  `protocol admission layer`.
- Split provider code by responsibility where practical:
  runtime adapter, payload lowering, response normalization, error mapping,
  live catalog/preflight, cleanup/reaper, and config validation.
- Keep routing and policy decisions in compiler/dispatcher, not adapters.
- Preserve current fail-closed behavior and existing provider-specific tests
  during extraction.

## Recipe Creation Control Plane

Recipe creation is in scope for the major refactor, but only as control-plane
boundary clarification. It must not become "smart automatic recipe generation"
inside the gateway runtime.

Caller-side boundary:

- `gpucall-recipe-draft` may create sanitized intake, preflight metadata,
  deterministic local drafts, quality feedback, comparisons, submissions, and
  status checks.
- It must not call an LLM.
- It must not choose provider, model, GPU, runtime, tuple, fallback order, or
  production activation.
- It must not transmit raw confidential payloads when sanitized metadata is
  sufficient.

Administrator-side boundary:

- `gpucall-recipe-admin` may materialize, review, promote, validate, watch inbox
  submissions, process quality feedback, and optionally produce administrator
  recipe-authoring proposals.
- Materialization, review, promotion, activation, and production routing gates
  must remain deterministic.
- Admin-side LLM authoring, if used, is proposal generation only. It is not
  production config and cannot bypass deterministic materialization,
  validation evidence, launch checks, or explicit administrator approval.

Required recipe pipeline shape:

```text
sanitized caller intake
  -> deterministic canonical recipe materialization
  -> admin review
  -> tuple candidate / execution contract derivation
  -> validation artifact
  -> launch check
  -> explicit production activation
```

Refactor implications:

- Split `recipe_admin.py` by workflow: parser/entrypoint, inbox index,
  materialization, quality feedback, review, promotion, authoring proposal, and
  automation/watch loops.
- Keep recipe authoring outside gateway runtime routing.
- Preserve guarded recipe writes and contract-narrowing checks.
- Preserve fail-closed behavior for unvalidated tuples, unsafe `auto_select`,
  missing validation budget, missing credentials, and missing endpoint ids.
- Make every stage auditable by artifact path, request id, SHA-256, validation
  evidence, and activation decision.

## First Refactor Targets After Production Decision

1. `gpucall/cli.py`
   - Largest command file.
   - Lowest runtime-risk if command families are moved with output tests.
   - Best early line reduction.

2. `gpucall/app.py`
   - Extract endpoint services only after endpoint tests and live smoke are stable.
   - Must preserve idempotency/budget/DataRef error behavior exactly.

3. Persistence interfaces
   - Introduce explicit protocols for idempotency, jobs, tenant ledger, artifact registry.
   - Collapse duplicated SQLite/Postgres control flow only after behavior parity tests.

4. Provider modules
   - Split adapter runtime from catalog/cost/validation helpers.
   - Avoid changing provider behavior during first pass.

5. SDK client
   - Split upload/task/OpenAI/error/circuit resources.
   - Preserve public API and typed exception contracts.

## Non-Negotiable Behavior To Preserve

- No inference in gateway runtime control decisions.
- Gateway runtime routing remains deterministic.
- Caller-controlled `recipe` / `requested_tuple` is disabled unless explicitly allowed.
- Unknown workload fails closed.
- No hosted AI fallback.
- OpenAI `model` must not become a raw caller-controlled provider/model selector.
- DataRef bodies do not cross gateway except through object-store presign workflow.
- DataRef SHA-256 validation remains mandatory where policy requires it.
- Provider temporary failures produce structured failure artifacts and fallback behavior.
- Tenant budget reservation is atomic and released on terminal failure.
- Idempotency first-writer reservation prevents duplicate execution.
- Artifact latest pointer remains true compare-and-set.
- Postgres mode uses Postgres for jobs, idempotency, tenant ledger, artifact registry, and admission.
- Caller-side recipe intake remains deterministic and sanitized.
- Admin-side LLM recipe authoring, if used, remains non-authoritative proposal only.
- CLI and API outputs remain stable unless tests are updated with explicit product decision.
