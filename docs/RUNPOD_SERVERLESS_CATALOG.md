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
- billable tuple smoke passes for the exact recipe/resource/model/engine/contract tuple
- cleanup audit remains green

For OpenAI-compatible LLM traffic, prefer `runpod-vllm-*`. Use
`runpod-native-*` when a gpucall worker must fetch DataRefs, return
`gpucall-tuple-result`, or implement behavior that the official worker-vLLM
contract cannot provide.
