# RunPod Serverless Catalog

RunPod Serverless is modeled as a `managed_endpoint` execution surface. The
catalog is intentionally wider than the active production endpoints. The source
of truth is `candidate_sources/runpod_serverless.yml`, which expands GPU, model,
and worker-family matrices into deterministic tuple candidates. Production
routing uses only tuples with endpoint configuration and live validation
evidence.

## Catalog Families

`runpod-vllm-*` candidates use RunPod's official Serverless worker-vLLM path.
They declare:

- `adapter: runpod-vllm-serverless`
- `engine_ref: runpod-vllm-openai`
- `endpoint_contract: openai-chat-completions`
- `output_contract: openai-chat-completions`
- official `runpod/worker-v1-vllm` image and required worker environment

`runpod-native-*` candidates use RunPod's generic Serverless queue API. They
declare:

- `adapter: runpod-serverless`
- `engine_ref: runpod-serverless-gpucall`
- `endpoint_contract: runpod-serverless`
- `output_contract: gpucall-tuple-result`
- a required custom gpucall-compatible worker image before promotion

## GPU Scope

Candidates use the RunPod documented Serverless GPU pool IDs:

- `AMPERE_16`
- `AMPERE_24`
- `ADA_24`
- `AMPERE_48`
- `ADA_48_PRO`
- `AMPERE_80`
- `ADA_80_PRO`
- `HOPPER_141`

The catalog does not invent undocumented pool IDs. New GPU types can be added
only after RunPod documents the corresponding Serverless endpoint GPU selector.

## Promotion

Candidate tuples must not enter production routing until all of these are true:

- endpoint id is configured in `target`
- official execution contract validates
- model and worker environment agree
- cost metadata is present
- live endpoint inventory has no unapproved warm workers
- billable tuple smoke passes for the exact recipe/resource/model/engine/contract tuple
- cleanup audit remains green

RunPod Serverless endpoints should default to scale-to-zero. A tuple that needs
warm workers must declare the runtime intent and standing spend explicitly:

```yaml
provider_params:
  endpoint_runtime:
    workersMin: 1
  cost_approval:
    standing_workers_approved: true
    approved_by: operator
    approved_at: "2026-01-01T00:00:00Z"
    reason: bounded scheduled production warm pool
standing_cost_per_second: 0.001
standing_cost_window_seconds: 3600
```

Without that approval, `validate-config` rejects declared warm workers, and
`cost-audit --live` / production `launch-check` reject live warm workers found
directly in the provider endpoint inventory. When RunPod credentials are
configured, live audit also rejects unmanaged RunPod endpoints with warm workers
even when no active gpucall tuple points at that endpoint.

Provider Panopticon remediation is policy-driven. The gateway can always exclude
blocked tuples from routing, but provider-side changes are generated as an
explicit plan before any mutation:

```yaml
panopticon_remediation:
  exclude_from_routing:
    mode: auto
  scale_workers_min_to_zero:
    mode: approval_required
  delete_endpoint:
    mode: approval_required
  delete_network_volume:
    mode: approval_required
```

`gpucall panopticon plan` emits a strict JSON remediation plan from the current
snapshot. `gpucall panopticon apply <plan.json>` is a dry-run unless `--yes` is
provided. In v2, the only supported provider mutation is RunPod Serverless
`workersMin -> 0`; endpoint and network-volume deletion can appear in the plan
but is not executed by apply.

Provider supply provisioning is a separate plan/apply surface. It creates the
missing provider-side supply for a reviewed tuple or tuple candidate; it does
not run generation smoke and it does not silently activate production routing.

```bash
gpucall panopticon provision-plan \
  --config-dir config \
  --tuple runpod-vllm-ampere48-qwen2-5-vl-7b-instruct \
  --output-json supply-plan.json

gpucall panopticon provision-apply supply-plan.json          # dry-run
gpucall panopticon provision-apply supply-plan.json --yes    # provider mutation
```

`provision-plan` accepts either `--tuple`, `--candidate`, or
`--review-json`. Review JSON is the `gpucall-recipe-admin review` output; when
no candidate is named, the first `tuple_candidate_matches` entry is selected.
If `--template-id` is supplied, the plan creates only the RunPod endpoint. If no
template id is supplied, the plan first creates a private RunPod Serverless
template from the tuple image and worker environment, then creates the endpoint
from that returned template id. The endpoint request defaults to `workersMin:
0`, `workersMax: 1`, `computeType: GPU`, and the RunPod REST `gpuTypeIds`
derived from the tuple GPU family. Warm workers are blocked unless the
`provider_supply_provisioning` policy explicitly permits them:

```yaml
provider_supply_provisioning:
  create_runpod_serverless_endpoint:
    mode: approval_required
    default_workers_min: 0
    default_workers_max: 1
    max_workers_max: 3
    allow_warm_workers: false
```

After a successful endpoint create, `provision-apply` returns a materialized
config patch for the worker `target`. Operators should apply that patch only
after `/health`, `/models`, Panopticon readiness, and validation evidence pass
for the exact tuple.

For OpenAI-compatible LLM traffic, prefer `runpod-vllm-*`. Use
`runpod-native-*` when a gpucall worker must fetch DataRefs, return
`gpucall-tuple-result`, or implement behavior that the official worker-vLLM
contract cannot provide.
