# Tuple Validation

External provider validation can create billable resources. Run these only after credentials, quotas, and cost guardrails are confirmed.

```bash
gpucall tuple-smoke modal-a10g --recipe text-infer-standard --mode sync
gpucall tuple-smoke runpod-vllm-serverless --recipe text-infer-light --mode sync
gpucall tuple-smoke runpod-vllm-flashboot --recipe text-infer-light --mode sync
gpucall tuple-smoke local-ollama --recipe text-infer-standard --mode sync
gpucall tuple-smoke hyperstack-a100 --recipe text-infer-standard --mode sync
```

RunPod production validation uses the official worker-vLLM OpenAI-compatible route:

```text
POST /v2/<endpoint_id>/openai/v1/chat/completions
```

Use `runpod-vllm-serverless` for the stable Serverless endpoint and `runpod-vllm-flashboot` for the FlashBoot candidate.

Do not declare `stream` for RunPod worker-vLLM tuples in v2.0. Token streaming is intentionally unsupported until the RunPod worker path has a real incremental generation contract.

Modal stream tuples must call a deployed generator function through `Function.remote_gen(...)`. Post-hoc chunking of a completed response is not a stream contract.

RunPod Serverless native queue validation uses `/runsync`, `/run`, `/status/{job_id}`, and `/cancel/{job_id}`. That path is distinct from worker-vLLM's OpenAI-compatible route.

Keep smoke/stub endpoints out of production auto-routing. If a provider returns a fixed value such as `Hello World`, name it accordingly, for example `runpod-serverless-smoke`, and validate it only through `gpucall tuple-smoke` or a non-auto-selected smoke recipe such as `smoke-text-small`.

For external GPU tuples, `model:` is the production-readiness declaration. Do not set it on smoke endpoints. Once set, the provider can become eligible for auto-routing if policy, recipe requirements, modes, VRAM, and context length all match.

Record for each provider:

- success/failure
- latency
- cleanup result
- cost observed on provider dashboard
- audit trail validity after execution

Production `launch-check --profile production` requires one valid JSON artifact per required production tuple under `$XDG_STATE_HOME/gpucall/provider-validation/`. The tuple key is derived from account, execution surface, endpoint contract, output contract, stream contract, model ref, engine ref, and endpoint/lifecycle configuration state. Artifacts are bound to the current git HEAD and active config directory; stale artifacts do not satisfy the gate.

Required top-level fields:

```json
{
  "validation_schema_version": 1,
  "tuple": "modal-a10g",
  "recipe": "text-infer-standard",
  "mode": "sync",
  "passed": true,
  "started_at": "2026-05-07T00:00:00+00:00",
  "ended_at": "2026-05-07T00:01:00+00:00",
  "commit": "<current git HEAD>",
  "config_hash": "<active config hash>",
  "governance_hash": "<compiled plan governance hash>",
  "official_contract": {},
  "official_contract_hash": "<sha256 of canonical official_contract JSON>",
  "cleanup": {},
  "cost": {},
  "audit": {}
}
```

`official_contract` must include:

- `adapter`
- `account_ref`
- `execution_surface`
- `endpoint_contract`
- `expected_endpoint_contract`
- `output_contract`
- `expected_output_contract`
- `stream_contract`
- `expected_stream_contract`
- `official_sources`

The observed `endpoint_contract`, `output_contract`, and `stream_contract` must equal their expected values. `official_sources` must name the official provider documentation or official repository material used for the contract. `official_contract_hash` is the SHA-256 of the canonical JSON encoding of `official_contract` with sorted keys and compact separators.

`cleanup` must be an object. If cleanup is required, set `{"required": true, "completed": true, ...}` only after the provider-side resource is actually absent. `gpucall cleanup-audit` rejects missing cleanup objects and rejects artifacts where cleanup is required but not completed.

`cost` must be an object containing the estimated or observed billable resource cost. `audit` must be an object containing the related audit event identifiers.

During `launch-check --profile production`, a successful gateway smoke also satisfies live validation for the exact tuple it actually exercises. A retryable vendor capacity artifact, for example an `iaas_vm` lease attempt returning no stock before any VM is created, is reported as `capacity_unavailable_tuples` instead of being treated as a code or cleanup failure. It does not hide leaked resources; `cleanup-audit` must still return `ok: true`.

## Artifact and Split-Learning Worker Paths

Workers support governed artifact execution only when the worker environment has explicit artifact export capabilities:

- `GPUCALL_WORKER_ARTIFACT_DEK_HEX`: 32-byte AES-256 key encoded as hex. This must be released to the worker by tenant KMS/HYOK/BYOK or an attestation-bound mechanism; the gateway must not generate it.
- `GPUCALL_WORKER_ARTIFACT_BUCKET` plus optional `GPUCALL_WORKER_ARTIFACT_PREFIX`, or an explicit `GPUCALL_WORKER_ARTIFACT_URI`.

For `train` and `fine-tune`, the worker fetches DataRefs, encrypts a chained artifact bundle with AES-GCM, writes ciphertext directly to object storage, and returns an `artifact_manifest`. For `split-infer`, the worker fetches the activation ref, verifies the bytes through the DataRef path, and returns a deterministic split-learning acceptance result. Missing key material or object-store destination is a hard worker error, not a production success.

Hyperstack worker bootstrap uploads `input.json` and `worker.py` via SFTP. The shell command no longer embeds request JSON or worker source through heredocs.
