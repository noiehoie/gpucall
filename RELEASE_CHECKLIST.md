# Public-Safe Operator Release Checklist

This checklist contains only placeholder commands and public-safe operational
steps. Keep live endpoint IDs, private hostnames, and local audit transcripts
out of this file.

## Build

- [ ] `pytest -q`
- [ ] `npx -p typescript tsc -p sdk/typescript/tsconfig.json --noEmit`
- [ ] `docker compose -p gpucall up -d --build`
- [ ] `gpucall smoke`
- [ ] `gpucall audit verify`
- [ ] `gpucall security scan-secrets`
- [ ] `gpucall launch-check`
- [ ] `gpucall post-launch-report`

## External Provider Validation

Run only when credentials and cost guardrails are ready:

```bash
gpucall tuple-smoke modal-a10g --recipe text-infer-standard
gpucall tuple-smoke runpod-serverless --recipe text-infer-standard
gpucall tuple-smoke runpod-flash --recipe text-infer-standard
gpucall tuple-smoke local-ollama --recipe text-infer-standard
gpucall tuple-smoke hyperstack-a100 --recipe text-infer-standard
```

## Launch

- [ ] Freeze production config
- [ ] Rotate gateway API key
- [ ] Rotate R2 token after rehearsal
- [ ] Seed liveness cache
- [ ] Run first infer job
- [ ] Run first vision job
- [ ] Verify object store upload/download
- [ ] Verify audit chain
- [ ] Verify provider cleanup
- [ ] Watch jobs, audit, provider dashboards, and cost for 24-48 hours
- [ ] Archive `$XDG_STATE_HOME/gpucall/launch/launch-check.json`
- [ ] Archive `$XDG_STATE_HOME/gpucall/launch/post-launch-report.json`
