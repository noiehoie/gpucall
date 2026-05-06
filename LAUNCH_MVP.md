# gpucall MVP Launch Runbook

## Scope

- Tasks: `infer`, `vision`
- Modes: `sync`, `async`, `stream`
- Providers: Modal, RunPod Serverless, RunPod Flash, Hyperstack, Local fallback
- Object store: Cloudflare R2 through S3-compatible presigned URLs
- Deploy: Docker Compose on `/opt/gpucall`
- Config: `$XDG_CONFIG_HOME/gpucall`
- State: `$XDG_STATE_HOME/gpucall`
- Cache: `$XDG_CACHE_HOME/gpucall`

## Configure

```bash
gpucall configure
gpucall doctor
gpucall explain-config text-infer-standard --mode async
gpucall cost-audit
gpucall cleanup-audit
gpucall launch-check --profile static
gpucall release-check
```

Expected `doctor` signals before launch:

- `secrets.object_store: true`
- `secrets.runpod: true`
- `secrets.hyperstack: true` when Hyperstack is enabled
- `object_store.region: auto` for Cloudflare R2
- `state_dir` under `$XDG_STATE_HOME/gpucall`

## Preflight

```bash
cd /opt/gpucall
docker compose -p gpucall up -d --build
gpucall smoke
gpucall audit verify
gpucall cost-audit --live
gpucall cleanup-audit
gpucall launch-check --profile production --url http://127.0.0.1:18088
```

`gpucall smoke` checks:

- `/healthz`
- `/readyz`
- gateway auth rejection without a token
- explicit non-empty smoke execution through the gateway route
- Cloudflare R2 presigned PUT upload when object store is configured
- vision smoke only when object store can provide an image DataRef

Production auto-selected recipes must not include `local-echo` or smoke/stub providers. Stub endpoints such as a RunPod endpoint returning a fixed `Hello World` response must be named as smoke providers and referenced only by explicit smoke recipes.

Provider validation artifacts under `$XDG_STATE_HOME/gpucall/provider-validation/` must match the current commit and config hash, include official contract checks, and include cleanup, cost, and audit objects.

## Liveness Seed

```bash
gpucall seed-liveness text-infer-standard --count 100
gpucall audit verify
```

Use the seed to warm `ObservedRegistry` with initial latency and success observations before sending customer traffic.

## Runtime Checks

```bash
gpucall jobs --limit 20
gpucall audit tail --limit 20
curl -sS http://127.0.0.1:18088/readyz
```

Expected service binding:

- `127.0.0.1:18088 -> 8080`
- no public port exposure until a reverse proxy with explicit auth/routing is configured

## SDK Smoke

Python:

```python
from gpucall_sdk import GPUCallClient

with GPUCallClient("http://127.0.0.1:18088") as client:
    print(client.infer(prompt="hello"))
```

TypeScript:

```ts
import { GPUCallClient } from "gpucall-sdk";

const client = GPUCallClient.fromEnv("http://127.0.0.1:18088");
console.log(await client.infer({ prompt: "hello" }));
```

## Launch Gate

- `gpucall smoke` succeeds.
- `gpucall cost-audit` has complete billing metadata for every billable provider.
- `gpucall cost-audit --live` can read provider-side cost or resource state for every configured live provider.
- `gpucall cleanup-audit` returns `ok: true`.
- tenant governance is configured, and tenant keys live in credentials or environment variables, not YAML.
- `gpucall launch-check --profile production --url ...` returns `go: true`.
- `provider_live_validation.capacity_unavailable_adapters` is acceptable only for retryable external capacity failures with no leaked resources.
- `gpucall audit verify` returns `valid: true`.
- object store is `true` in `/readyz`.
- unauthenticated task requests return `401`.
- no state or audit files exist under `$XDG_CONFIG_HOME/gpucall`.
- async job status does not persist inline payloads or signed input refs.
- Docker Compose project is `gpucall`.
- container is healthy.
- provider credentials are stored in `credentials.yml` with `0600`.

## Post-Launch

- Watch `gpucall jobs` for failures.
- Watch `gpucall audit tail`.
- Generate `gpucall post-launch-report` after each launch rehearsal and at the end of hypercare.
- Track provider cost dashboards for 24-48 hours.
- Rotate gateway API key and R2 token after the first launch rehearsal.
