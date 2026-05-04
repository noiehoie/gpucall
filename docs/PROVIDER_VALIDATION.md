# Provider Validation

External provider validation can create billable resources. Run these only after credentials, quotas, and cost guardrails are confirmed.

```bash
gpucall provider-smoke modal-a10g --recipe text-infer-standard --mode sync
gpucall provider-smoke modal-a10g --recipe text-infer-standard --mode stream
gpucall provider-smoke runpod-vllm-serverless --recipe text-infer-light --mode sync
gpucall provider-smoke runpod-vllm-flashboot --recipe text-infer-light --mode sync
gpucall provider-smoke local-ollama --recipe text-infer-standard --mode sync
gpucall provider-smoke hyperstack-a100 --recipe text-infer-standard --mode sync
```

RunPod production validation uses the official worker-vLLM OpenAI-compatible route:

```text
POST /v2/<endpoint_id>/openai/v1/chat/completions
```

Use `runpod-vllm-serverless` for the stable Serverless endpoint and `runpod-vllm-flashboot` for the FlashBoot candidate. Keep the older `runpod-flash` name only as a compatibility alias.

Do not declare `stream` for RunPod worker-vLLM providers in v2.0. Token streaming is intentionally unsupported until the RunPod worker path has a real incremental generation contract.

RunPod Serverless native queue validation uses `/runsync`, `/run`, `/status/{job_id}`, and `/cancel/{job_id}`. That path is distinct from worker-vLLM's OpenAI-compatible route.

Keep smoke/stub endpoints out of production auto-routing. If a provider returns a fixed value such as `Hello World`, name it accordingly, for example `runpod-serverless-smoke`, and validate it only through `gpucall provider-smoke` or a non-auto-selected smoke recipe such as `smoke-text-small`.

For external GPU providers, `model:` is the production-readiness declaration. Do not set it on smoke endpoints. Once set, the provider can become eligible for auto-routing if policy, recipe requirements, modes, VRAM, and context length all match.

Record for each provider:

- success/failure
- latency
- cleanup result
- cost observed on provider dashboard
- audit trail validity after execution
