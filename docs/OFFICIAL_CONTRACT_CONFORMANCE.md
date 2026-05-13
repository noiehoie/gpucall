# Official Contract Conformance

This file records the official execution contracts used by gpucall surfaces. It is not a substitute for billable live validation; it is the source map for code-level conformance tests.

## OpenAI Chat Completions Contract

- Vendored spec: `third_party/openai/openapi.documented.yml`
- Source URL: `https://app.stainless.com/api/spec/documented/openai/openapi.documented.yml`
- License: MIT, recorded in `third_party/openai/LICENSE`
- Generated gateway contract: `gpucall/openai_contract/chat_completions.json`
- Generated SDK contract: `sdk/python/gpucall_sdk/openai_contract/chat_completions.json`
- Regeneration command: `uv run python scripts/generate_openai_contract.py`

The generated contract is the source of truth for top-level Chat Completions
request field classification. Hand-written gateway validation may narrow
support, but it must not silently accept fields absent from the vendored OpenAI
spec.

## Modal

- Module: `gpucall.execution_surfaces.function_runtime`
- Contract: deployed Modal function invocation
- Official sources:
  - https://modal.com/docs/reference/modal.Function
- Code mapping:
  - `modal.Function.from_name(app_name, function_name)`
  - `Function.spawn(...)` when available, falling back to `Function.remote(...)`
  - `FunctionCall.get(timeout=...)`
  - `Function.remote_gen(...)` for generator streaming

## RunPod Serverless

- Module: `gpucall.execution_surfaces.managed_endpoint`
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

- Module: `gpucall.execution_surfaces.managed_endpoint`
- Contract: RunPod worker-vLLM OpenAI-compatible endpoint
- Official sources:
  - https://docs.runpod.io/serverless/vllm/openai-compatibility
- Code mapping:
  - Base URL: `https://api.runpod.ai/v2/<endpoint_id>/openai/v1`
  - Chat route: `POST /chat/completions`
  - `Authorization: Bearer <RUNPOD_API_KEY>`
  - OpenAI-compatible `model`, `messages`, `stream`, `temperature`, `max_tokens`, `top_p`, `stop`, `seed`, penalty, `response_format`, `tools`, `tool_choice`, `functions`, and `function_call` payload fields

## RunPod FlashBoot

- Module: `gpucall.execution_surfaces.function_runtime`
- Contract: `runpod-flash` SDK / gpucall Flash worker path
- Production status: not production eligible until billable validation artifacts prove the SDK path and cleanup lifecycle.
- Code mapping:
  - Descriptor contract is `runpod-flash-sdk`, not `openai-chat-completions`.
  - Output contract is `gpucall-tuple-result`.

## Hyperstack

- Module: `gpucall.execution_surfaces.iaas_vm`
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

## Local Ollama

- Module: `gpucall.execution_surfaces.local_runtime`
- Contract: Ollama native generate API
- Official sources:
  - https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-completion
- Code mapping:
  - `POST <base_url>/api/generate`
  - Request fields: `model`, `prompt`, `stream: false`
  - Response mapping uses the native Ollama `response` field.

## Azure Compute VM

- Module: `gpucall.execution_surfaces.iaas_vm`
- Contract: Azure SDK for Python Compute VM lifecycle.
- Production status: not production eligible until worker bootstrap and result retrieval are configured.
- Official sources:
  - https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.operations.virtualmachinesoperations
  - https://learn.microsoft.com/en-us/python/api/azure-identity/azure.identity.defaultazurecredential
- Code mapping:
  - `ComputeManagementClient(DefaultAzureCredential(), subscription_id)`
  - `virtual_machines.begin_create_or_update(resource_group, vm_name, parameters)`
  - `virtual_machines.begin_delete(resource_group, vm_name)`
  - Confidential VM intent is represented in the VM `security_profile`.

## GCP Confidential Space VM

- Module: `gpucall.execution_surfaces.iaas_vm`
- Contract: Google Cloud Compute Python `InstancesClient` VM lifecycle.
- Production status: not production eligible until worker bootstrap and result retrieval are configured.
- Official sources:
  - https://cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient
  - https://cloud.google.com/compute/docs/reference/rest/v1/instances/insert
- Code mapping:
  - `compute_v1.InstancesClient().insert(project=..., zone=..., instance_resource=...)`
  - `compute_v1.InstancesClient().delete(project=..., zone=..., instance=...)`
  - Confidential execution intent is represented in `confidential_instance_config`.

## Scaleway Instance

- Module: `gpucall.execution_surfaces.iaas_vm`
- Contract: Scaleway Instance REST API.
- Production status: not production eligible until worker bootstrap and result retrieval are configured.
- Official sources:
  - https://www.scaleway.com/en/developer-api/
  - https://www.scaleway.com/en/developers/api/instances/
- Code mapping:
  - Authentication header: `X-Auth-Token`
  - `POST https://api.scaleway.com/instance/v1/zones/<zone>/servers`
  - `DELETE https://api.scaleway.com/instance/v1/zones/<zone>/servers/<server_id>`

## OVHcloud Public Cloud Instance

- Module: `gpucall.execution_surfaces.iaas_vm`
- Contract: official `ovh` Python wrapper for Public Cloud instance lifecycle.
- Production status: not production eligible until worker bootstrap and result retrieval are configured.
- Official sources:
  - https://github.com/ovh/python-ovh
  - https://api.ovh.com/console/#/cloud/project/%7BserviceName%7D/instance#POST
  - https://api.ovh.com/console/#/cloud/project/%7BserviceName%7D/instance/%7BinstanceId%7D#DELETE
- Code mapping:
  - `ovh.Client(endpoint=..., application_key=..., application_secret=..., consumer_key=...)`
  - `client.post("/cloud/project/<service_name>/instance", ...)`
  - `client.delete("/cloud/project/<service_name>/instance/<instance_id>")`

## Non-Negotiables

- An execution-surface adapter descriptor must not claim an official OpenAI-compatible contract if its code path returns a gpucall-specific result contract.
- Built-in registration must happen in an execution-surface module, not a provider-name compatibility shim.
- Production-eligible external adapters must carry official source URLs in their descriptor.
- Lifecycle-only adapters must not be production-eligible for deterministic routing until they implement worker bootstrap, result retrieval, and billable live validation.
