# gpucall

[![CI](https://github.com/noiehoie/gpucall/actions/workflows/ci.yml/badge.svg)](https://github.com/noiehoie/gpucall/actions/workflows/ci.yml)
[![License: AGPL-3.0](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)
[![SDK License: Apache-2.0](https://img.shields.io/badge/SDK-Apache--2.0-green)](sdk/python/LICENSE)
[![Release](https://img.shields.io/github/v/release/noiehoie/gpucall)](https://github.com/noiehoie/gpucall/releases)

**A fail-closed governance gateway for rented GPU inference.** Applications
declare *what* they need (task, intent, budget); gpucall decides *where* it
runs — across Modal, RunPod, Hyperstack, or local runtimes — using only
deterministic rules over verifiable evidence: route validation results, price
freshness, provider readiness probes, and budget ledgers. No evidence, no
route. Ever.

[日本語版 README](README.ja.md)

```text
caller / AI agent                    gpucall gateway                        providers
─────────────────    ──────────────────────────────────────────────    ────────────────
POST /v2/tasks/sync   compile: recipe → tuple chain (deterministic)      Modal functions
  task: infer     →   gates:   route validation evidence fresh?      →   RunPod endpoints
  intent: rank         price evidence fresh? budget reserved?            Hyperstack VMs
  budget, inputs       provider readiness probed? fail closed if not     local (vLLM/Ollama)
                      audit:   attempt records, cost commit,
                               cleanup evidence, failure taxonomy
                                        ▲
                      Provider Panopticon (out-of-path monitor):
                      endpoint existence, health, queue depth,
                      price observations → freshness-bounded snapshots
```

## Why this exists

Two facts collide when a team decides its data cannot go to hosted AI APIs:

1. **Rented GPU capacity is cheap and everywhere** — serverless functions,
   managed endpoints, spot VMs, marketplaces — but every surface has its own
   lifecycle, pricing, failure modes, and cleanup obligations.
2. **The moment application code starts choosing GPUs**, strings like
   `modal-h100` leak into business logic, nobody can say which route was
   validated, what it cost, or whether the data was cleaned up afterwards.

Existing layers solve adjacent problems: LLM API gateways (LiteLLM, Portkey)
proxy to *existing* endpoints but do not own endpoint lifecycle; GPU
orchestrators (SkyPilot, dstack) provision compute but have no concept of
route validation evidence or fail-closed budget routing. gpucall owns the
layer between them: **it treats a (recipe × tuple × mode × provider) route as
untrusted until proven, and keeps proving it.**

Routing decisions never involve an LLM. Same inputs, same catalog, same
evidence → same route. Rejections come back as machine-readable failure
artifacts with a caller action and an owner.

## Status — read this before evaluating

- **v2 (infer / vision): production.** It runs the author's news-analysis
  pipeline daily — 90K-token ranking workloads, vision OCR of newspaper
  pages, JSON extraction — through Modal-backed routes. That pipeline is the
  permanent canary: every release passes its regression suite.
- **Single maintainer.** Extensively AI-assisted development with
  deterministic tests as the quality gate (1,000+ tests in CI). Review
  accordingly.
- **API is pre-1.0**; minor versions may break contracts (documented in
  release notes).
- Provider support: Modal is the recommended happy path. RunPod and
  Hyperstack adapters exist with non-generation probes and provisioning
  plans; local runtimes (Ollama / OpenAI-compatible / vLLM) are first-class.
- v2.5 agent-native surface (estimate, failure taxonomy, MCP server) shipped;
  training / artifact lifecycle (v3) is design-stage — see `tasks/*-plan.md`.

## Quickstart

Install the CLI (installs `uv` if missing; pin with `GPUCALL_REF=<ref> sh`):

```bash
curl -fsSL https://raw.githubusercontent.com/noiehoie/gpucall/main/install.sh | sh
```

Run the guided setup — the first run works with **zero cloud credentials**
(local trial), then walks you to the Modal happy path:

```bash
gpucall setup                       # interactive; recommends local trial first
gpucall setup starter-plan --profile local-trial --output gpucall.setup.yml
gpucall setup apply --file gpucall.setup.yml --yes
gpucall serve --config-dir ~/.config/gpucall --port 18088
```

Estimate before you spend (compiles the route, reserves nothing):

```bash
curl -s localhost:18088/v2/estimate -X POST -H 'content-type: application/json' \
  -d '{"task":"infer","mode":"sync","intent":"summarize_text",
       "inline_inputs":{"prompt":{"value":"...", "content_type":"text/plain"}}}'
```

Then execute the same request against `/v2/tasks/sync`. From Python:

```python
from gpucall_sdk import GPUCallClient

with GPUCallClient("http://127.0.0.1:18088") as client:
    print(client.estimate(prompt="hello", intent="summarize_text"))
    print(client.infer(prompt="hello", intent="summarize_text"))
```

The SDK ships separately (Apache-2.0):
[`gpucall_sdk-2.0.69-py3-none-any.whl`](https://github.com/noiehoie/gpucall/releases/download/v2.0.69/gpucall_sdk-2.0.69-py3-none-any.whl)
— caller applications never import the gateway package.

## Core concepts (five words each)

| Concept | Meaning |
| --- | --- |
| **recipe** | workload contract: intent, budgets, modes |
| **tuple** | one executable route: GPU × model × surface |
| **route validation evidence** | proof this exact route worked |
| **Provider Panopticon** | out-of-path provider readiness monitor |
| **fail closed** | unknown / stale / unpriced → reject |

The full grammar lives in [docs/PRODUCT_NORTH_STAR.md](docs/PRODUCT_NORTH_STAR.md)
and [docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md](docs/OOB_USER_EXPERIENCE_PRODUCT_SPEC.md).

## What makes it different

- **Evidence-gated routing.** A route enters production only with fresh
  provider evidence *and* accepted validation evidence for the exact recipe,
  tuple, mode, and config hash. Config drift invalidates evidence; the admin
  service re-validates automatically within an explicit budget.
- **Budgets are hard ceilings**, enforced before dispatch with atomic
  reserve / commit / release — not dashboards after the bill.
- **Cleanup is evidence**, not hope: ownership-tagged provider resources,
  cleanup manifests, lease reaping, and audit records
  (`gpucall cleanup-audit`).
- **Agents are first-class callers**: non-billable `POST /v2/estimate`,
  deterministic `GET /v2/failure-taxonomy` (every failure has a class, a
  retry semantic, an owner, a next action), async jobs with polling and
  cancellation, and an MCP stdio server (`gpucall-mcp`) — see
  [docs/AGENT_NATIVE_EXECUTION.md](docs/AGENT_NATIVE_EXECUTION.md).
- **Demand creates supply, governed.** Unknown workloads are not just
  rejected: callers submit sanitized intake (never raw prompts), the admin
  pipeline materializes a recipe draft, provisions or matches supply, runs
  billable validation inside a configured budget, and only then activates the
  route. Every step leaves an artifact.
- **Provider conformance is testable**: `gpucall provider-conformance` runs
  the same registry-level checks against all 13 built-in adapters that a
  future plugin would face.

## What it is not

- Not an LLM API gateway for hosted providers — use LiteLLM / Portkey if you
  are fronting OpenAI/Anthropic/Bedrock.
- Not a general GPU orchestrator or training scheduler — use SkyPilot /
  dstack for interactive clusters and sweeps.
- Not a model marketplace and not a hosted service. You bring provider
  accounts; gpucall governs how they get used.

## Repository layout

```text
gpucall/                 gateway, compiler, dispatcher, provider adapters, CLI
gpucall/config_templates shipped starter catalog (see config/README.md — generated)
sdk/python/              caller SDK + gpucall-recipe-draft (Apache-2.0)
config/                  active dev catalog: recipes, models, engines, tuples
docs/                    product spec, contracts, onboarding, runbooks
tests/                   1,000+ deterministic tests (no credentials required)
```

## Development

```bash
uv sync
uv run pytest                                   # full suite, hermetic
uv run gpucall validate-config --config-dir config
uv run gpucall security scan-secrets
uv run gpucall provider-conformance
```

Release gates are described in
[docs/PUBLIC_RELEASE_CHECKLIST.md](docs/PUBLIC_RELEASE_CHECKLIST.md).
Operational reports from real canary runs are committed under `tasks/` —
including the failures. See [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).

## License

- Gateway (this repository): [AGPL-3.0-only](LICENSE). Run it, modify it,
  ship it — network use counts as distribution, so a hosted derivative must
  publish its source.
- Python SDK (`sdk/python/`): [Apache-2.0](sdk/python/LICENSE), so caller
  applications take no copyleft obligation.
- Commercial licensing: open an issue.
