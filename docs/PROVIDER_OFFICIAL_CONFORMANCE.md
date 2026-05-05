# Provider Official Conformance

This file records the official provider contracts used by gpucall adapters. It is not a substitute for billable live validation; it is the source map for code-level conformance tests.

## Modal

- Module: `gpucall.providers.modal_adapter`
- Contract: deployed Modal function invocation
- Official sources:
  - https://modal.com/docs/reference/modal.Function
- Code mapping:
  - `modal.Function.from_name(app_name, function_name)`
  - `Function.spawn(...)` when available, falling back to `Function.remote(...)`
  - `FunctionCall.get(timeout=...)`
  - `Function.remote_gen(...)` for generator streaming

## RunPod Serverless

- Module: `gpucall.providers.runpod_serverless_adapter`
- Contract: queue-based Serverless endpoint operations
- Official sources:
  - https://docs.runpod.io/serverless/endpoints/send-requests
- Code mapping:
  - `POST https://api.runpod.ai/v2/<endpoint_id>/runsync`
  - `POST https://api.runpod.ai/v2/<endpoint_id>/run`
  - `GET https://api.runpod.ai/v2/<endpoint_id>/status/<job_id>`
  - `POST https://api.runpod.ai/v2/<endpoint_id>/cancel/<job_id>`
  - `Authorization: Bearer <RUNPOD_API_KEY>`

## RunPod worker-vLLM

- Modules:
  - `gpucall.providers.runpod_vllm_adapter`
  - `gpucall.providers.runpod_flash_adapter`
- Contract: RunPod worker-vLLM OpenAI-compatible endpoint
- Official sources:
  - https://docs.runpod.io/serverless/vllm/openai-compatibility
- Code mapping:
  - Base URL: `https://api.runpod.ai/v2/<endpoint_id>/openai/v1`
  - Chat route: `POST /chat/completions`
  - `Authorization: Bearer <RUNPOD_API_KEY>`
  - OpenAI-compatible `model`, `messages`, `stream`, `temperature`, `max_tokens`, and `response_format` payload fields

## RunPod FlashBoot

- Module: `gpucall.providers.runpod_flashboot_adapter`
- Contract: `runpod-flash` SDK / gpucall Flash worker path
- Production status: not production eligible until billable validation artifacts prove the SDK path and cleanup lifecycle.
- Code mapping:
  - Descriptor contract is `runpod-flash-sdk`, not `openai-chat-completions`.
  - Output contract is `gpucall-provider-result`.

## Hyperstack

- Module: `gpucall.providers.hyperstack_adapter`
- Contract: Infrahub REST API plus SSH into the provisioned VM
- Official sources:
  - https://portal.hyperstack.cloud/knowledge/api-documentation
  - https://infrahub-doc.nexgencloud.com/docs/api-reference/authentication
  - https://portal.hyperstack.cloud/knowledge/how-do-i-enable-ssh-access-to-my-virtual-machine
  - https://portal.hyperstack.cloud/knowledge/how-do-i-check-the-billing-status-of-my-virtual-machines
- Code mapping:
  - Base URL: `https://infrahub-api.nexgencloud.com/v1`
  - Authentication header: `api_key`
  - VM create/list/status/delete: `/core/virtual-machines`
  - SSH firewall rule: `/core/virtual-machines/<vm_id>/sg-rules`
  - Catalog validation: `/core/images`, `/core/flavors`, `/core/environments`

## Non-Negotiables

- A provider adapter descriptor must not claim an official OpenAI-compatible contract if its code path returns a gpucall-specific result contract.
- Compatibility shims may re-export classes, but built-in registration must happen in the concrete provider contract module.
- Production-eligible external adapters must carry official source URLs in their descriptor.
