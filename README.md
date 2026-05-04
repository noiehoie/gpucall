# gpucall v2.0

L7 governance gateway for leased GPU task execution. The v2.0 MVP is scoped to `infer` and `vision` only.

## Quickstart

```bash
gpucall init
gpucall configure
gpucall validate-config
gpucall doctor
gpucall launch-check --profile static
docker compose -p gpucall up -d --build
gpucall smoke
gpucall launch-check --profile production --url http://127.0.0.1:18088
gpucall audit verify
```

Production-like runtime layout follows XDG:

- Config: `$XDG_CONFIG_HOME/gpucall` or `~/.config/gpucall`
- State: `$XDG_STATE_HOME/gpucall` or `~/.local/state/gpucall`
- Cache: `$XDG_CACHE_HOME/gpucall` or `~/.cache/gpucall`

## MVP Scope

Supported:

- Tasks: `infer`, `vision`
- Modes: `sync`, `async`, `stream`
- Object store: S3-compatible API, including Cloudflare R2 via endpoint override
- Deployment: Docker Compose
- State: SQLite WAL for jobs, JSONL audit hash chain

Not supported in v2.0:

- `transcribe`
- `train`
- `convert`
- fine-tune
- multi-file batch orchestration
- Postgres
- Helm/systemd packaging
- chaos and penetration-style test suites

## Secrets

Secrets do not belong in YAML. Use `gpucall configure`, environment variables, or a deployment secret manager.

```bash
gpucall security scan-secrets
```

Provider YAML should contain resource shape and routing metadata only.

## Python SDK

```python
from gpucall_sdk import GPUCallClient

with GPUCallClient("http://127.0.0.1:18088") as client:
    print(client.infer(prompt="hello"))
```

Async polling is hidden by default:

```python
from gpucall_sdk import AsyncGPUCallClient

async with AsyncGPUCallClient("http://127.0.0.1:18088") as client:
    job = await client.infer(mode="async", prompt="hello")
```

Files are uploaded to the configured object store with presigned PUT and sent to the gateway as `DataRef`. The SDK is distributed as the separate `gpucall-sdk` package; the gateway wheel does not include the SDK package.

## TypeScript SDK

```ts
import { GPUCallClient } from "@gpucall/sdk";

const client = GPUCallClient.fromEnv("http://127.0.0.1:18088");
const result = await client.infer({ prompt: "hello" });
```

## External System Migration

When adapting another product or service to gpucall, use the one-shot migration prompt in [docs/EXTERNAL_SYSTEM_ADAPTATION_PROMPT.md](docs/EXTERNAL_SYSTEM_ADAPTATION_PROMPT.md). External systems should normally send only `task`, `mode`, and input data or `DataRef`; recipe and provider selection belong to the gateway.

## Routing

gpucall is a deterministic governance router, not a Modal-only proxy. Recipe and provider selection rules are documented in [docs/ROUTING_POLICY.md](docs/ROUTING_POLICY.md).
RunPod Flash production validation is documented in [docs/RUNPOD_FLASH.md](docs/RUNPOD_FLASH.md).

## Zero-Trust Contracts

Provider definitions declare a `trust_profile` separate from recipe compute requirements. Restricted workloads are routed only to dedicated GPU providers or approved security tiers such as `confidential_tee` with attestation support or `split_learning`; shared GPU providers are rejected before execution. Governance hashes are deterministic over the request, policy, recipe, provider contract, and worker-readable DataRef set, excluding runtime IDs.

Workers consume gateway-presigned HTTP(S) DataRefs by default. Ambient `s3://` worker credentials are disabled unless explicitly opted in for a non-default worker environment. Chained artifacts are recorded as encrypted `ArtifactManifest` entries in the append-only Artifact Registry; the gateway stores lineage, version, checksums, key ids, and attestation references, not plaintext artifact bytes.

## Object Lifecycle

For Cloudflare R2 or S3-compatible buckets, configure lifecycle expiration for the gpucall prefix. A conservative MVP setting is:

- Prefix: `gpucall/`
- Expire objects after: 1-7 days
- Keep public access disabled
- Limit API token permission to object read/write for the bucket

## Provider Failures

Provider outages, remote capacity exhaustion, authentication failures, and provider-side queueing are outside the gateway SLA. The gateway records retryability, opens circuit breakers, and moves through the deterministic fallback chain.

## Launch Checks

```bash
gpucall validate-config
gpucall doctor
gpucall launch-check --profile static
gpucall seed-liveness text-infer-standard --count 100
gpucall registry show
gpucall smoke
gpucall launch-check --profile production --url http://127.0.0.1:18088
gpucall audit verify
gpucall post-launch-report
```

Production launch checks require gateway auth, object-store credentials, a live gateway smoke result, and a provider-validation JSON artifact. Static launch checks remain available for local config validation.
