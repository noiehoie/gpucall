# gpucall v2.0 Launch Status

## Current Status

MVP technical core is implemented and externally provider-smoked.

## Verified External Providers

- Modal A10G sync: passed.
- Modal A10G stream heartbeat: passed.
- RunPod Serverless sync: passed with endpoint `RUNPOD_ENDPOINT_ID_PLACEHOLDER`.
- RunPod Flash sync: passed after `flash deploy`; endpoint `https://api.runpod.ai/v2/RUNPOD_ENDPOINT_ID_PLACEHOLDER/runsync`.
- Hyperstack VM lifecycle: passed with `default-NORWAY-1` / `n3-RTX-A4000x1`.
- Hyperstack A100 default: blocked by provider stock shortage for `A100-80G-PCIe`.

## Cleanup Verification

- Hyperstack `gpucall-managed-*` VM residual check: none remaining after smoke.
- Audit hash chain verification after provider smokes: valid.

## Commands

```bash
gpucall validate-config
gpucall doctor
gpucall launch-check
gpucall provider-smoke modal-a10g --recipe smoke-text-small --mode sync
gpucall provider-smoke modal-a10g --recipe smoke-text-small --mode stream
gpucall provider-smoke runpod-serverless --recipe smoke-text-small --mode sync
gpucall provider-smoke runpod-flash --recipe smoke-text-small --mode sync
gpucall provider-smoke hyperstack-a100 --recipe smoke-text-small --mode sync
gpucall post-launch-report
```

## Deferred by Design

- Public package publishing.
- Docker image publishing.
- Release tag creation.
- Launch announcement.
- 24-48h hypercare observation window.
- Postgres, Helm, systemd, chaos tests, penetration-style tests.
