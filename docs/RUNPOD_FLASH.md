# RunPod worker-vLLM and FlashBoot

RunPod is supported in gpucall through two official RunPod contracts:

1. RunPod Serverless worker-vLLM for stable OpenAI-compatible queue execution.
2. RunPod Flash SDK `@Endpoint` function execution for FlashBoot candidates.

The implementation deliberately follows those two documented contracts and does not invent a third transport.

## Provider Names

Use distinct provider names so operational state does not get confused:

- `runpod-vllm-serverless`: official `runpod/worker-v1-vllm` Serverless endpoint, FlashBoot not required.
- `runpod-vllm-flashboot`: official RunPod Flash SDK `@Endpoint` function backed by Transformers/Qwen.
- `runpod-flash`: deprecated compatibility alias. Do not use for new production config.

## v2.0 Contract

- Supported modes: `sync`, `async`
- Unsupported mode: `stream`
- Stable Serverless engine: official RunPod worker-vLLM
- Stable Serverless transport: `POST https://api.runpod.ai/v2/<endpoint_id>/openai/v1/chat/completions`
- FlashBoot engine: official RunPod Flash SDK `@Endpoint` function execution
- Data plane:
  - `runpod-vllm-serverless` accepts inline text under policy limit only. The official worker-vLLM image does not fetch gpucall `DataRef` inputs.
  - `runpod-vllm-flashboot` fetches text `DataRef` inputs inside the Flash worker with SHA256 verification and a 16 MiB worker-side limit.

Async jobs are still supported because gpucall's job FSM wraps bounded sync provider execution.

## Provider YAML

Stable Serverless endpoint:

```yaml
name: runpod-vllm-serverless
adapter: runpod-vllm-serverless
max_data_classification: confidential
gpu: AMPERE_16
vram_gb: 16
max_model_len: 8192
cost_per_second: 0.00045
modes: [sync, async]
endpoint: null
target: "<runpod endpoint id>"
image: runpod/worker-v1-vllm:v2.18.1
model: Qwen/Qwen2.5-1.5B-Instruct
provider_params:
  worker_env:
    MODEL_NAME: Qwen/Qwen2.5-1.5B-Instruct
    OPENAI_SERVED_MODEL_NAME_OVERRIDE: Qwen/Qwen2.5-1.5B-Instruct
    MAX_MODEL_LEN: "8192"
    GPU_MEMORY_UTILIZATION: "0.95"
    MAX_CONCURRENCY: "30"
```

FlashBoot candidate:

```yaml
name: runpod-vllm-flashboot
adapter: runpod-vllm-flashboot
max_data_classification: confidential
gpu: AMPERE_16
vram_gb: 16
max_model_len: 8192
cost_per_second: 0.00045
modes: [sync, async]
endpoint: null
target: ""
image: null
endpoint_contract: runpod-flash-sdk
output_contract: gpucall-provider-result
model: Qwen/Qwen2.5-1.5B-Instruct
```

`model:` is the production-readiness declaration for the stable worker-vLLM path. `runpod-vllm-flashboot` remains explicitly non-production-eligible until billable Flash SDK validation artifacts are present, because its contract is `runpod-flash-sdk`, not RunPod's OpenAI-compatible worker-vLLM route.

## FlashBoot Promotion Gate

Promote `runpod-vllm-flashboot` to production only after all of these pass with billable tests:

1. `workers=(0, 1)` Flash SDK endpoint cold request succeeds.
2. Warm request succeeds before `idle_timeout`.
3. Sync text request succeeds through Flash SDK function execution.
4. JSON `response_format` request succeeds.
5. Stream is either proven or omitted from `modes`; v2.0 defaults to omitted.
6. Ten consecutive smoke requests meet the success threshold.
7. Revival after 5 minutes idle succeeds.
8. Revival after 30 minutes idle succeeds.
9. Timeout, throttle, and queue failures return retryable provider errors and gpucall falls back deterministically.
10. Audit shows `provider=runpod-vllm-flashboot` for successful jobs and redacts prompt content.

## Deployment Notes

For `runpod-vllm-serverless`, use RunPod's official worker-vLLM image first. Set at minimum:

- `MODEL_NAME`
- `OPENAI_SERVED_MODEL_NAME_OVERRIDE`
- `MAX_MODEL_LEN`
- `GPU_MEMORY_UTILIZATION`
- `MAX_CONCURRENCY`

These values must be represented in `provider_params.worker_env` so gpucall can
validate the declared provider contract against the official worker-vLLM
deployment contract before production routing.

Use a container disk large enough for the image and model cache. Keep `workersMin=0` unless intentionally warming the endpoint with explicit cost approval.

For `runpod-vllm-flashboot`, gpucall uses `gpucall/providers/runpod_flash_worker.py` as the official Flash SDK function body. Keep that file self-contained because RunPod Flash serializes and executes the function remotely.

## Validation

```bash
gpucall provider-smoke runpod-vllm-serverless --recipe text-infer-light --mode sync
gpucall provider-smoke runpod-vllm-flashboot --recipe text-infer-light --mode sync
gpucall validate-config
gpucall doctor
```

Expected result:

- no fixed `Hello World`
- no `Processed plan ...` stub response
- non-empty LLM text
- audit trail records the actual RunPod provider
- `doctor.routing` shows the provider only when `model:` is declared and circuit breaker allows it

## Future Engine Boundary

vLLM is the first production engine. The adapter boundary intentionally sits above the worker engine so future RunPod workers can support SGLang, TGI, llama.cpp, or another OpenAI-compatible server without exposing engine choice to callers.
