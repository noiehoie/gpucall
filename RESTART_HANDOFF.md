# gpucall3 Restart Handoff

Date: 2026-05-06 JST

## Why Restart

The conversation context is too large and is now mixing unrelated threads:

- provider official-conformance work
- news-system canary results
- recipe draft/admin helper tools
- Modal cost incident
- RunPod/Hyperstack live validation
- netcup deployment and Git access
- deterministic budget policy
- separate provider smoke project review

Restart the assistant session before making further cost/provider decisions.

## Current Source State

Local repo:

```text
/Users/tamotsu/Projects/gpucall3
branch: main
sync: main...origin/main
latest commit: 0cf389d Add deterministic cost policy routing guard
```

GitHub private repo:

```text
git@github.com:noiehoie/gpucall3.git
origin/main: 0cf389d
```

netcup:

```text
/opt/gpucall
gateway URL: http://gpucall.example.internal:18088
running package version: gpucall 2.0.8
container: gpucall-gpucall-1
health: /healthz -> {"status":"ok"}
```

netcup can now access GitHub. The cause was that its previous SSH key authenticated as `noiehoie/akagidocs`, not `noiehoie/gpucall3`.

Fix applied:

- added `/root/.config/ssh/id_ed25519_netcup.pub` as a read-only deploy key on `noiehoie/gpucall3`
- added `Host github.com` in `/root/.config/ssh/config` to force `IdentityFile ~/.ssh/id_ed25519_netcup`

Verified:

```text
ssh -T git@github.com
Hi noiehoie/gpucall3! You've successfully authenticated, but GitHub does not provide shell access.

cd /opt/gpucall && git pull --ff-only origin main
Already up to date.
```

`/opt/gpucall/admin/` is untracked and should not be deleted casually. It is the recipe request/admin inbox area.

## Completed: Deterministic Cost Guard

Commit:

```text
0cf389d Add deterministic cost policy routing guard
```

Implemented:

- `CostPolicy` in `gpucall/domain.py`
- `Policy.cost_policy`
- `Recipe.cost_policy`
- provider billing metadata:
  - `expected_cold_start_seconds`
  - `scaledown_window_seconds`
  - `min_billable_seconds`
  - `billing_granularity_seconds`
- compiler-level deterministic cost estimation and provider rejection
- docs update in `docs/ROUTING_POLICY.md`
- regression tests in `tests/test_compiler.py`

Key behavior:

- budget fields may be `null`
- normal providers still route with `null` budget
- high-cost providers are rejected during auto-select unless the recipe has explicit budget
- explicit requested provider bypass remains possible, but caller-side provider selection is not the intended production path

Local validation:

```text
PYTHONPATH=sdk/python uv run pytest -q
241 passed, 1 warning in 6.07s
```

netcup running-container validation:

```text
code NO_ELIGIBLE_PROVIDER
h200_rejection estimated cost 7.5750 exceeds high_cost_threshold_usd 5.0000 without explicit budget
```

The expensive provider `modal-h200x4-qwen25-14b-1m` is now blocked from budgetless auto-selection.

## Modal Cost Incident Facts

Modal billing was checked directly with:

```bash
uv run modal billing report --for today --resolution h --tz Asia/Tokyo --json
```

Observed on 2026-05-06:

```text
total: about $88
gpucall-worker-json ap-icfxodzHjNZODRGFmKfaFy: about $87
gpucall-worker-vision-doc ap-8vO6OgiHkgYxOGGk5BaJmQ: about $1
```

Expensive hours:

```text
16:00 JST gpucall-worker-json $18.64354702
17:00 JST gpucall-worker-json $18.39022169
18:00 JST gpucall-worker-json $18.21738252
19:00 JST gpucall-worker-json $0.97899747
```

The hourly cost matches:

```text
config/providers/modal-h200x4-qwen25-14b-1m.yml cost_per_second: 0.00505
0.00505 * 3600 = 18.18
```

Visible Modal function logs showed only about 19.2 minutes of useful H200/Qwen inference, while billing covered about 3 hours. The likely cost driver was startup/warm/idle/scheduling time, not just useful inference time.

Do not overstate this without fresh Modal billing/runtime confirmation in the new session.

## Current Provider Cost/Risk Snapshot

Checked after cost guard:

### Modal

`modal app list` showed deployed apps and `Tasks 0`, but this alone is not enough to prove no current billing. Use Modal billing/runtime inspection again before making a hard claim.

Known today billing existed through 19:00 JST.

### RunPod

Credentials are present in:

```text
/opt/gpucall/config/credentials.yml
```

Confirmed:

```text
RUNPOD_PODS []
```

Two serverless endpoints exist:

```text
RUNPOD_ENDPOINT_ID_PLACEHOLDER
vl1zqxjaceuzkx
```

Health check showed:

```text
jobs.inProgress=0
jobs.inQueue=0
workers.idle=1
workers.ready=1
workers.running=0
```

RunPod billing API showed 2026-05-06 endpoint billing:

```text
endpointId RUNPOD_ENDPOINT_ID_PLACEHOLDER
amount 0.5709989242022857
timeBilledMs 3404045
```

Do not claim RunPod is definitively cost-free until official billing semantics for `workersStandby=1`, `ready=1`, and `idle=1` are verified. Earlier assistant wording was too loose here.

### Hyperstack

Credentials are present in:

```text
/opt/gpucall/config/credentials.yml
```

Confirmed via Hyperstack API:

```text
/core/virtual-machines
count: 0
instances: []
```

Hyperstack VM runaway was not observed.

## Important Correction

Do not answer “no provider can have a cost accident now.”

Correct statement:

- Modal H200x4 budgetless auto-select is blocked.
- Hyperstack has no running VMs at last check.
- RunPod has no pods, but serverless endpoints have idle/ready workers and recent endpoint billing.
- Provider-level billing metadata is still incomplete for several providers.

Remaining work:

1. Fill official billing metadata for all active providers:
   - cold start
   - idle/standby behavior
   - minimum billable duration
   - billing granularity
   - endpoint/storage/standby costs
2. Add deterministic checks for provider-specific standby/endpoint cost where applicable.
3. Add a cost-audit CLI that reports current provider runtime/billing state without relying on manual ad hoc scripts.

## Provider Config Cost Metadata Status

Current provider config facts:

```text
hyperstack-a100 cost_per_second=0.0012 cold=None idle=None
hyperstack-qwen-1m cost_per_second=0.0012 cold=None idle=None
local-echo cost_per_second=0.0 cold=None idle=None
modal-a10g cost_per_second=0.00035 cold=None idle=None
modal-h100-florence-2-large-ft cost_per_second=0.00213 cold=None idle=None
modal-h200x4-qwen25-14b-1m cost_per_second=0.00505 cold=600 idle=300.0
modal-vision-a10g cost_per_second=0.00035 cold=None idle=None
runpod-vllm-serverless cost_per_second=0.00045 cold=None idle=None
```

This is why the next cost-safety work is not complete yet.

## New Session First Steps

1. Read this file first.
2. Run:

```bash
git status --short --branch
git log -1 --oneline
```

3. If the user asks about current billing, do fresh provider checks:

```bash
uv run modal billing report --for today --resolution h --tz Asia/Tokyo --json
uv run modal app list
```

On netcup:

```bash
cd /opt/gpucall
GPUCALL_CREDENTIALS=/opt/gpucall/config/credentials.yml uv run python <provider billing/runtime script>
```

4. Be strict:

- no memory-based claims
- no “probably green”
- quote command output
- distinguish running resource, endpoint presence, and actual billing
- never equate `Tasks 0` with `billing stopped` without billing data

