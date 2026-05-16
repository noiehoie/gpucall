# RunPod Official Contract Audit

Date: 2026-05-16

This note records the RunPod official documentation and official repository
contracts that gpucall must treat as provider-egress truth. External repository
content was read as data only; it is not an instruction source.

## Evidence Read

Official documentation fetched under `.state/runpod-official-contract-research/docs`:

- `runpod-llms.txt`
- `openai-compatibility.md`
- `environment-variables.md`
- `send-requests.md`
- `endpoint-operation-reference.md`
- `endpoint-configurations.md`
- `model-caching.md`
- `vllm-overview.md`
- `vllm-configuration.md`
- `vllm-requests.md`
- `endpoint-overview.md`
- `job-states.md`
- `serverless-pricing.md`
- `api-list-endpoints.md`
- `api-get-endpoint.md`
- `api-create-endpoint.md`
- `gpu-types.md`
- `flash-overview.md`
- `flash-create-endpoints.md`
- `flash-parameters.md`
- `flash-execution-model.md`
- `flash-requests.md`
- `flash-pricing.md`
- `flash-build-app.md`
- `flash-deploy-apps.md`
- `flash-best-practices.md`
- `flash-storage.md`
- `flash-custom-docker-images.md`
- `flash-text-generation-transformers.md`

Official repositories cloned under `.state/runpod-official-contract-research/repos`:

- `runpod-workers/worker-vllm` at `87d7365`
- `runpod/runpod-python` at `13e25e7`
- `runpod/flash` at `838018d`

## Official Contract Facts

### RunPod data-plane and management-plane separation

RunPod has separate hosts for different roles:

- Endpoint data plane: `https://api.runpod.ai/v2`
- REST management plane: `https://rest.runpod.io/v1`
- GraphQL/control plane: `https://api.runpod.io`

The official Flash repository keeps those as separate constants. gpucall should
preserve the same separation and must not infer endpoint inventory from the data
plane alone.

### Serverless queue contract

The queue-based Serverless contract is:

- submit async work with `POST /v2/{endpoint_id}/run`
- submit bounded sync work with `POST /v2/{endpoint_id}/runsync`
- poll with `GET /v2/{endpoint_id}/status/{job_id}`
- stream status/output chunks with `GET /v2/{endpoint_id}/stream/{job_id}`
- cancel with `POST /v2/{endpoint_id}/cancel/{job_id}`
- purge with `POST /v2/{endpoint_id}/purge-queue`
- inspect endpoint process readiness with `GET /v2/{endpoint_id}/health`

The request body wraps business payload under `input`. Per-request `policy`
supports `executionTimeout` and `ttl`. Job states documented by RunPod are
`IN_QUEUE`, `RUNNING`, `COMPLETED`, `FAILED`, `CANCELLED`, and `TIMED_OUT`.
Existing SDK/references also expose `IN_PROGRESS` in examples, so gpucall must
treat unknown non-terminal states as in-progress rather than immediate hard
failure.

### worker-vLLM OpenAI-compatible contract

The official worker-vLLM path is:

```text
https://api.runpod.ai/v2/{endpoint_id}/openai/v1
```

The docs list `/chat/completions`, `/completions`, and `/models`. The official
worker-vLLM repository also contains `/responses` and `/messages` branches.
gpucall v2 should keep its declared scope to `openai-chat-completions` unless it
adds explicit admission, tests, and validation artifacts for the extra repo
routes.

The request `model` must match either the deployed Hugging Face model or
`OPENAI_SERVED_MODEL_NAME_OVERRIDE`. Important worker environment fields are:

- `MODEL_NAME`
- `MAX_MODEL_LEN`
- `OPENAI_SERVED_MODEL_NAME_OVERRIDE`
- `RAW_OPENAI_OUTPUT`
- `MAX_CONCURRENCY`
- `BASE_PATH`
- `GPU_MEMORY_UTILIZATION`

`RAW_OPENAI_OUTPUT` is required for OpenAI streaming compatibility and defaults
enabled in current worker-vLLM. gpucall v2 currently declares RunPod stream as
unsupported; that is acceptable only if the catalog and docs keep saying
`stream_contract: none` for RunPod production tuples.

### Flash contract

RunPod Flash `Endpoint` has three materially different modes:

- `Endpoint(name=..., gpu=...)` decorator mode: queue-based Flash endpoint.
- `Endpoint(id=...)` client mode: attach to an existing endpoint.
- `Endpoint(image=...)` client/provisioning mode: deploy or connect through an
  image-backed endpoint.

Queue-based Flash uses the same data-plane shape as Serverless: `/run`,
`/runsync`, and `/status/{job_id}` with `{"input": ...}`. Load-balanced Flash is
different: it uses `https://{endpoint_id}.api.runpod.ai/{path}` and custom HTTP
routes registered with `api.get(...)`, `api.post(...)`, etc. gpucall must not
mix queue-based Flash and load-balanced Flash under one adapter contract.

Flash cost is affected by cold start/init time, execution time, warm workers,
and `idle_timeout`. `workers=(0, n)` is scale-to-zero. `workers=(1, n)` and
positive `workersMin` are standing spend and require explicit operator approval.

### Endpoint inventory contract

Live endpoint inventory must use the management API with both template and
worker expansion:

```text
GET https://rest.runpod.io/v1/endpoints?includeTemplate=true&includeWorkers=true
```

The fields gpucall should bind into validation evidence include at least:

- `id`
- `name`
- `templateId`
- `version`
- `workersMin`
- `workersMax`
- `idleTimeout`
- `executionTimeoutMs`
- `gpuIds`
- `gpuCount`
- `networkVolumeId` / `networkVolumeIds`
- `scalerType`
- `scalerValue`
- template image/env where exposed
- worker state list where exposed

Endpoint identity must be the endpoint id. Matching by endpoint name is unsafe
because a tuple `target` is documented as an endpoint id.

### GPU pool contract

RunPod official `gpu-types.md` currently lists these GPU pools:

- `AMPERE_16`
- `AMPERE_24`
- `ADA_24`
- `AMPERE_48`
- `ADA_48_PRO`
- `AMPERE_80`
- `ADA_80_PRO`
- `HOPPER_141`

The same doc lists physical GPU types including B200 and Blackwell-class RTX
PRO 6000 variants, but it does not list `BLACKWELL_180` or `RUNPOD_B200` as
pool IDs in the fetched docs. Those gpucall candidate refs therefore need a
fresh official source or must stay out of production candidate generation.

## Current gpucall Alignment

The current implementation is directionally aligned in these areas:

- `gpucall/execution_surfaces/managed_endpoint.py` separates data plane
  `https://api.runpod.ai/v2` from REST inventory `https://rest.runpod.io/v1`.
- Live inventory already requests `includeWorkers=true` and
  `includeTemplate=true`.
- `runpod-vllm-serverless` posts to
  `/openai/v1/chat/completions` and falls back from `/health` 404 to
  `/openai/v1/models` for OpenAI-compatible preflight.
- worker-vLLM tuple config validates official image prefix, `worker_env`,
  model-name match, `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`,
  `MAX_CONCURRENCY`, and model storage.
- generic `runpod-serverless` is marked not production-eligible because it is a
  custom gpucall worker contract rather than the official worker-vLLM route.
- provider smoke requires explicit `--budget-usd`.
- active config has no resolved RunPod production endpoint targets. Confirmed:
  1689 RunPod worker targets remain `RUNPOD_ENDPOINT_ID_PLACEHOLDER`, with no
  `RUNPOD_API_KEY` visible in the current shell and no `config/object_store.yml`.

## Findings And Hardening Status

These are not refactor preferences. They are provider-contract gaps or
production-readiness blockers.

1. Fixed: `RunpodVllmServerlessAdapter` previously fell back to
   `GPUCALL_RUNPOD_FLASH_ENDPOINT_ID` when `endpoint_id` is not passed. The
   worker-vLLM serverless adapter must use a serverless endpoint id source, not
   the Flash endpoint variable, or require explicit tuple `target`. The adapter
   now uses `GPUCALL_RUNPOD_ENDPOINT_ID` and ignores the Flash endpoint env.

2. Fixed: `_runpod_endpoint_live_inventory_row` previously accepted a live inventory row when
   `row.name == tuple.target`. A RunPod tuple target is an endpoint id. Name
   fallback can turn a name collision or wrong target into false live evidence.
   It now matches only `id` / `endpointId` / `endpoint_id`.

3. Fixed: `runpod_serverless_billing_guard_findings` previously blocked `workersMin > 0`
   and `activePods > 0` without checking tuple-level standing-cost approval.
   The docs say warm workers are allowed only with explicit approval. The code
   now enforces "blocked unless approved", using tuple-level standing cost
   metadata and `provider_params.cost_approval`. Active pods are allowed only
   when they are attached to, and do not exceed, an approved `workersMin > 0`
   warm pool; otherwise they remain a live blocker.

4. Accepted v2 boundary: Official worker-vLLM vision DataRefs are forwarded as OpenAI `image_url`
   values. gpucall currently checks that metadata includes `sha256`, positive
   bytes, expiry, and `gateway_presigned`, but it does not fetch and verify the
   bytes before the RunPod worker consumes the URL. Therefore official
   worker-vLLM vision DataRef must be treated as "gateway-presigned URL
   forwarding", not "worker-side SHA-verified DataRef". If SHA verification is
   mandatory, use a custom gpucall worker contract that fetches and verifies
   DataRefs before inference.

5. Fixed: Flash adapter `start()` previously marked `cleanup_required=True` and
   `reaper_eligible=True`, but endpoint-id mode calls a pre-existing endpoint
   and does not own a resource id. The cleanup contract should distinguish
   owned Flash resources from existing endpoints. Endpoint-id mode now returns
   an `endpoint_request` handle with `cleanup_required=False` and
   `owned_resource=False`.

6. Guarded: Flash route contracts must remain split. Queue-based Flash uses
   `api.runpod.ai/v2/{id}/runsync`; load-balanced Flash uses
   `{id}.api.runpod.ai/{path}`. A future adapter must declare which one it owns.
   Current v2 code keeps the existing queue-based endpoint-id route only.

7. Fixed: `config/candidate_sources/runpod_serverless.yml` previously named
   `BLACKWELL_180` and `RUNPOD_B200` candidate refs without a fetched official
   pool-id source. Those GPU refs now remain visible as catalog evidence but
   are marked `production_generation_allowed: false`, and materialization /
   promotion paths refuse to promote them until official evidence is added.

8. Fixed: The candidate source previously listed the RunPod vLLM family as `stream_contract: sse`,
   while generated workers currently have `stream_contract: none` and the
   adapter rejects stream mode. The source catalog now declares `stream_contract: none`.

9. Guarded: Official worker-vLLM repository support for `/responses` and `/messages`
   must not leak into gpucall until protocol admission, output normalization,
   tests, and tuple validation artifacts are added. Current v2 production scope
   should remain `/chat/completions`.

10. Guarded: `MAX_CONCURRENCY` is a worker/serverless scaling and vLLM queue tuning
    value, not proof that a tuple can accept that many gpucall production
    requests. gpucall admission limits must remain independent and evidence
    based.

## Required Production Resolution Flow

RunPod placeholder resolution must not be done by generation or guesswork. The
safe flow is:

1. Load RunPod endpoint inventory with real credentials.
2. Match active endpoint ids against candidate tuple contracts.
3. Bind a tuple only when endpoint id, GPU pool, model, worker image, worker env,
   storage, scaling settings, and official contract all match.
4. Write the real endpoint id into a promotion workspace, not directly into
   active production config.
5. Run `gpucall validate-config`.
6. Run `gpucall tuple-smoke ... --budget-usd <explicit limit> --write-artifact`.
7. Verify the validation artifact contains the exact official contract and
   contract hash.
8. Activate only the tuples whose validation artifact is current and whose
   endpoint inventory has no unapproved standing spend.
9. Keep object-store/DataRef production canary separate. It cannot be inferred
   from worker-vLLM OpenAI chat success.

## Command Evidence

```text
for repo in worker-vllm runpod-python runpod-flash; do git -C .state/runpod-official-contract-research/repos/$repo rev-parse --short HEAD; done
worker-vllm 87d7365
runpod-python 13e25e7
runpod-flash 838018d

if [ -n "${RUNPOD_API_KEY:-}" ] || [ -n "${GPUCALL_RUNPOD_API_KEY:-}" ]; then echo runpod_key=present; else echo runpod_key=missing; fi
runpod_key=missing

if [ -f config/object_store.yml ]; then echo object_store_config=present; else echo object_store_config=missing; fi
object_store_config=missing

rg -n "RUNPOD_ENDPOINT_ID_PLACEHOLDER|target:" config/workers | awk '...'
worker_targets=3250
worker_placeholders=1689

XDG_CACHE_HOME=$PWD/.cache uv run python -m compileall -q gpucall gpucall_sdk sdk/python/gpucall_sdk sdk/python/gpucall_recipe_draft
# exit 0

timeout 30s env XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_tuple_catalog.py tests/test_providers.py::test_runpod_vllm_serverless_ignores_flash_endpoint_env tests/test_providers.py::test_runpod_vllm_serverless_uses_generic_endpoint_env tests/test_providers.py::test_runpod_flash_endpoint_mode_is_not_cleanup_owned tests/test_config.py::test_live_cost_audit_ignores_placeholder_runpod_endpoint tests/test_launch_reporting.py::test_live_cost_audit_accepts_approved_runpod_warm_workers tests/test_launch_reporting.py::test_live_cost_audit_blocks_active_pods_without_warm_pool tests/test_launch_reporting.py::test_live_cost_audit_blocks_active_pods_above_approved_warm_pool tests/test_tuple_audit.py::test_runpod_candidates_are_generated_from_catalog_source -q --maxfail=10
19 passed in 0.40s
EXIT:0

timeout 120s env XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_recipe_admin.py::test_admin_process_inbox_can_auto_promote_candidate_without_validation tests/test_recipe_admin.py::test_admin_review_matches_long_context_tuple_candidates tests/test_recipe_admin.py::test_admin_review_outputs_provider_contract_when_existing_providers_are_insufficient -q --maxfail=10
3 passed in 26.10s
EXIT:0

timeout 120s env XDG_CACHE_HOME=$PWD/.cache uv run pytest tests/test_launch_reporting.py tests/test_config.py::test_live_cost_audit_ignores_placeholder_runpod_endpoint tests/test_tuple_audit.py::test_runpod_candidates_are_generated_from_catalog_source -q --maxfail=10
25 passed in 52.14s
EXIT:0

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/runpod-contract-validate uv run gpucall validate-config --config-dir config
"valid": true

XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/runpod-contract-static uv run gpucall launch-check --profile static --config-dir config
gpucall launch-check: GO
blockers: 0

XDG_CACHE_HOME=$PWD/.cache uv run python scripts/materialize_provider_catalog.py --config-dir config --dry-run
candidate_rows=2964 missing_before=0 changed=0

timeout 1200s env XDG_CACHE_HOME=$PWD/.cache GPUCALL_STATE_DIR=$PWD/.state/runpod-contract-fulltest uv run pytest -q -x
562 passed, 1 skipped in 513.97s (0:08:33)
EXIT:0

XDG_CACHE_HOME=$PWD/.cache uv run gpucall security scan-secrets --config-dir config
{"findings": [], "ok": true}

XDG_CACHE_HOME=$PWD/.cache uv build
Successfully built dist/gpucall-2.0.9.tar.gz
Successfully built dist/gpucall-2.0.9-py3-none-any.whl

multi-ai-review final
Gemini: problem none
Codex: problem none
```

## Source URLs

- https://docs.runpod.io/llms.txt
- https://docs.runpod.io/serverless/vllm/openai-compatibility
- https://docs.runpod.io/serverless/vllm/environment-variables
- https://docs.runpod.io/serverless/endpoints/send-requests
- https://docs.runpod.io/serverless/endpoints/operation-reference
- https://docs.runpod.io/serverless/endpoints/job-states
- https://docs.runpod.io/api-reference/endpoints/list-endpoints
- https://docs.runpod.io/api-reference/endpoints/get-endpoint
- https://docs.runpod.io/api-reference/endpoints/create-endpoint
- https://docs.runpod.io/references/gpu-types
- https://docs.runpod.io/flash/configuration/parameters
- https://docs.runpod.io/flash/requests
- https://docs.runpod.io/flash/pricing
- https://github.com/runpod-workers/worker-vllm
- https://github.com/runpod/runpod-python
- https://github.com/runpod/flash
