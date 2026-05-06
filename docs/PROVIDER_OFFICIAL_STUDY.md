# Provider Official Study Notes

This file is the source-of-truth study note for provider adapter work. Do not treat
"calling a documented URL" as official conformance. A gpucall provider is official
only when its request shape, runtime setup, lifecycle, health, timeout, cleanup,
and validation behavior match the provider's official documentation, SDK, or
official repository.

## RunPod

Primary sources:

- RunPod worker-vLLM docs: https://docs.runpod.io/serverless/vllm/get-started
- RunPod worker-vLLM environment variables: https://docs.runpod.io/serverless/vllm/environment-variables
- RunPod worker-vLLM official repository: https://github.com/runpod-workers/worker-vllm
- RunPod Serverless endpoint operations: https://docs.runpod.io/serverless/endpoints/get-started
- RunPod OpenAI compatibility: https://docs.runpod.io/serverless/vllm/openai-compatibility

Conformance requirements:

- The production LLM path must be the official worker-vLLM contract unless a
  different official RunPod contract is explicitly marked non-production until
  live validation passes.
- OpenAI-compatible worker-vLLM calls use the endpoint base
  `https://api.runpod.ai/v2/<endpoint_id>/openai/v1` and standard OpenAI request
  shapes. The request `model` must match the deployed model name or the served
  model override exposed by the endpoint.
- Native Serverless queue operations are distinct from OpenAI-compatible direct
  routes. `/run`, `/runsync`, `/status`, `/cancel`, `/stream`, `/retry`,
  `/purge-queue`, and `/health` are the official endpoint operations. Do not mix
  queue response shapes with OpenAI response shapes.
- worker-vLLM deployment configuration is part of the provider contract:
  `MODEL_NAME`, `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`,
  `OPENAI_SERVED_MODEL_NAME_OVERRIDE`, `CUSTOM_CHAT_TEMPLATE` when needed,
  streaming batch settings, and `MAX_CONCURRENCY` must be recorded or validated
  before production routing.
- The official worker-vLLM image does not become a gpucall DataRef worker merely
  because it accepts text. If DataRef fetch is not implemented inside that
  official worker contract, the provider must reject DataRef plans and let the
  router fall back.
- Large-model timeout, worker initialization, model cache, and endpoint health
  are official operational concerns. A provider is not "good" only because one
  OpenAI request succeeded.

Implications for gpucall:

- `runpod-vllm-serverless` can be production-eligible only for the exact endpoint,
  model, worker image/env, input contracts, and recipe tuple that passed billable
  validation.
- `runpod-vllm-flashboot` remains non-production unless its official SDK contract,
  resource lifecycle, cleanup, timeout, and cost behavior pass billable validation.
- `doctor` and `launch-check` should surface endpoint health and worker-vLLM
  deployment-contract gaps separately from router/compiler gaps.

## Hyperstack

Primary sources:

- Infrahub API overview: https://portal.hyperstack.cloud/knowledge/api-documentation
- Infrahub API authentication: https://infrahub-doc.nexgencloud.com/docs/api-reference/authentication
- Hyperstack Ubuntu image names: https://portal.hyperstack.cloud/knowledge/what-ubuntu-images-does-hyperstack-offer
- Hyperstack SSH access: https://portal.hyperstack.cloud/knowledge/how-do-i-enable-ssh-access-to-my-virtual-machine
- Hyperstack VM billing status: https://portal.hyperstack.cloud/knowledge/how-do-i-check-the-billing-status-of-my-virtual-machines

Conformance requirements:

- The API base is `https://infrahub-api.nexgencloud.com/v1`, authenticated with
  the documented `api_key` header.
- VM creation must use official API catalog values for `environment_name`,
  `image_name`, `flavor_name`, and `key_name`. UI labels or remembered aliases
  are not acceptable production config.
- For Ubuntu images, `image_name` must be the official API Image Name in the
  Hyperstack image table.
- SSH access is a security-rule lifecycle operation. A production adapter must
  create the documented TCP/22 ingress rule only for the configured caller CIDR,
  not open `0.0.0.0/0`, and it must clean up the rule when the VM is destroyed.
- VM states and billing state must be checked through the official VM listing or
  detail APIs. Delete success is not enough unless the adapter can prove no
  gpucall-managed billable VM remains.
- Worker bootstrap over SSH is gpucall-owned glue. It may be necessary for MVP,
  but it is not a Hyperstack official inference contract. It must therefore be
  treated as a verified bootstrap layer, not as provider-native inference.

Implications for gpucall:

- Hyperstack provider YAML must fail validation when image/flavor/environment are
  not found in the live official catalog.
- Hyperstack live validation must record VM id, image, flavor, environment,
  security-rule id, deletion result, and post-delete/list confirmation.
- Hyperstack production routing must not rely on stale image names or UI labels.

## Modal

Primary sources:

- Modal apps and deployments: https://modal.com/docs/guide/apps
- Modal deployed function invocation: https://modal.com/docs/guide/trigger-deployed-functions
- Modal autoscaling: https://modal.com/docs/guide/scale
- Modal images: https://modal.com/docs/reference/modal.Image
- Modal GPU guide: https://modal.com/docs/guide/gpu

Conformance requirements:

- Production calls should invoke deployed Modal Functions through the Modal SDK
  when the gateway runs Python. Ephemeral `app.run()` is a development fallback,
  not a production default.
- Deployed apps persist until stopped. Functions scale independently. By default,
  no containers run when idle, which means scale-to-zero cold start is expected
  behavior.
- Cold-start mitigation must use Modal's official autoscaler controls:
  `min_containers`, `buffer_containers`, `scaledown_window`, or
  `Function.update_autoscaler()`. Caller-side timeout increases are only a
  workaround.
- Modal image construction must use official `modal.Image` APIs such as
  `from_registry`, `pip_install`, `uv_pip_install`, `add_local_python_source`,
  volumes, and secrets. Build-time and runtime state must be explicit.
- Secrets should be provided through Modal secrets or environment injection, not
  YAML plaintext.
- Function timeout, GPU type/count, image, mounted cache/volume, and deployed app
  name/function name are part of the provider contract.

Implications for gpucall:

- `modal-h200x4-qwen25-14b-1m` must encode cold-start expectations and an
  official warm-pool strategy instead of pushing the burden to callers.
- gpucall's Modal worker exposes official autoscaler knobs through deployment
  environment variables:
  - `GPUCALL_MODAL_A10G_MIN_CONTAINERS`
  - `GPUCALL_MODAL_H200X4_MIN_CONTAINERS`
  - `GPUCALL_MODAL_VISION_H100_MIN_CONTAINERS`
  - matching `*_SCALEDOWN_WINDOW` variables
- Vision providers must prove that the deployed function and model actually match
  the declared recipe capability; an HTTP 200 short-answer VQA response is not
  sufficient validation for document understanding.
- Modal live validation must bind the artifact to app name, function name, GPU,
  model, image/dependency contract, timeout, output contract, and quality category.

## Cross-Provider Rule

Provider implementation work must proceed in this order:

1. Read official docs/repositories.
2. Write the provider contract in config/schema terms.
3. Implement only what the official contract supports.
4. Reject unsupported plans explicitly.
5. Validate with billable live artifacts before production routing.
6. Make launch checks fail closed when official-contract evidence is missing.

## Implementation Lesson: 2026-05-06

Provider adapters must be implemented from official documentation, official SDKs,
or official repositories before live testing. A working smoke path is not
evidence of official conformance. For Hyperstack specifically, VM create payloads
must be validated through the official SDK/OpenAPI `CreateInstancesPayload`
contract, and provider error bodies must be redacted and preserved. Discarding
provider 400 bodies or inventing unofficial post-create lifecycle steps wastes
billable validation time and hides the actual provider-side failure reason.
