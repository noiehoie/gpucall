# Controlled Runtimes

Controlled Runtime is gpucall's name for an operator-approved private execution
endpoint. It is not the caller's laptop by default, and it is not discovered by
guessing. The gpucall operator must declare the runtime, validate it, and then
promote the resulting tuple through the normal validation evidence path.

## Boundaries

- `gateway_host`: runtime runs on the same host as the gateway.
- `private_network`: runtime is reachable from the gateway over LAN, VPN,
  Tailscale, or another operator-managed private network.
- `site_network`: runtime is inside an operator-managed site or on-prem network.

## Config Shape

Controlled runtimes live in `config/runtimes/*.yml`.

```yaml
name: macstudio-ds4
kind: controlled_runtime
runtime_boundary: private_network
network_scope: tailscale
operator_controlled: true
endpoint: http://macstudio.tailnet:18181
adapter: local-dataref-openai-worker
model: deepseek-v4-flash
max_model_len: 1000000
input_contracts: [text, chat_messages, data_refs]
max_data_classification: restricted
trust_profile:
  security_tier: local
  sovereign_jurisdiction: jp
  dedicated_gpu: true
  requires_attestation: false
  supports_key_release: false
  allows_worker_s3_credentials: false
routing:
  enabled: true
  preference: prefer_when_eligible
  allowed_tasks: [infer]
  allowed_modes: [async]
  require_validation_evidence: true
health:
  check_url: http://macstudio.tailnet:18181/healthz
  timeout_seconds: 2
  failure_policy: disable_runtime
discovery:
  source: manual
  last_verified_at: null
```

The execution tuple references this record with `controlled_runtime_ref`.

## CLI

Register an existing OpenAI-compatible endpoint:

```bash
gpucall runtime add-openai \
  --name macstudio-ds4 \
  --endpoint http://macstudio.tailnet:18181 \
  --dataref-worker
```

Validate its health from the gateway host:

```bash
gpucall runtime validate --name macstudio-ds4
gpucall validate-config
```

`runtime add-openai` writes three files:

- `config/runtimes/<name>.yml`
- `config/surfaces/<name>.yml`
- `config/workers/<name>.yml`

The command does not install ds4. It registers and validates an endpoint the
operator already controls. Managed ds4 installation can be added later as a
separate install-plan workflow without changing routing semantics.
