# gpucall Product North Star

Updated: 2026-05-19 16:21 JST

This document is the project-level product memory for gpucall v2 and later.
It captures the direction that must remain visible across threads, planning
sessions, and implementation work.

## North Star

gpucall should turn GPU compute into electricity.

External systems and AI agents should not choose providers, GPUs, endpoint IDs,
pricing plans, worker templates, queue behavior, or fallback chains in
application code. They should declare workload intent, inputs, and explicit
constraints such as budget, latency, locality, or confidentiality. gpucall and
the operator policy supply the governance machinery: deterministic routing,
provider evidence, price evidence, validation evidence, data-sovereignty rules,
execution, cleanup, and audit.

The user should ask for computation, not infrastructure. A mature gpucall lets a
caller say "run this 70B inference" or "fine-tune this model on this corpus" and
receive the result, while gpucall chooses the provider, GPU class, price window,
data movement path, execution mode, cleanup path, and audit record.

The primary value is the computation result without infrastructure knowledge.
Governance evidence is the proof that the result was produced under the declared
constraints. Governance is not a replacement for the user-facing north star; it
is how gpucall makes that north star safe and auditable.

The working product metaphor is:

> GPU の Cloudflare: users and agents do not care which GPU node runs the work;
> gpucall chooses, verifies, routes, audits, and cleans up.

## What Electricity Means

The north star is not just easier provider selection. It implies these product
capabilities:

1. **Workload-declarative interface**: callers describe the job, model/data
   intent, mode, constraints, and budget. They do not ask for "one H100" or a
   RunPod endpoint. gpucall derives the required capability, context window,
   VRAM, execution surface, and cleanup contract.
2. **Checkpoint and artifact lifecycle**: for training and fine-tuning, cloud
   GPU work should be resumable, checkpointed, encrypted, synchronized back to
   the operator-controlled environment, and cleaned from cloud storage after the
   governed lifecycle completes.
3. **Cost prediction and hard ceilings**: gpucall should estimate cost and time
   before billable execution, enforce budget caps, stop loss behavior, and
   price freshness, and prevent runaway cloud GPU spend by design.
4. **Agent-native GPU procurement**: AI agents should be able to call gpucall
   without knowing provider mechanics. The important interface is structured,
   idempotent, machine-readable readiness, execution, failure, retry, budget,
   and operator-action output.

## Market Position

The market opening is not "another GPU orchestrator." The position is:

> GPU for AI engineers and AI agents who do not want to know cloud
> infrastructure.

SkyPilot, Vast.ai, Beam, Brev.dev, Cudo Compute, and adjacent tools still ask
developers to understand too much provider and infrastructure detail. gpucall
should instead sell the computation result and the governance evidence around
that result.

To deserve that position, gpucall must make these product promises real:

- **Zero-config first experience**: the first successful run should prove that
  GPU-backed computation can be requested without learning provider mechanics.
  The aspirational first impression is a fast `gpucall login` + `gpucall run`
  path where provider account setup can come later through gpucall-managed
  credit or a guided operator setup flow.
- **Data sovereignty proof**: users need technical evidence that data,
  checkpoints, volumes, artifacts, and logs are handled under an explicit
  lifecycle and do not remain in cloud infrastructure after cleanup.
- **Multi-provider market arbitration**: current prices, availability, billing
  granularity, queue depth, and provider reliability should be monitored so
  gpucall can choose a governed, cost-effective route without user involvement.
- **AI-agent-first protocol**: CLI output and APIs must be structured enough for
  Codex, Claude, Gemini, and other agents to reason about readiness, execution,
  errors, retries, and next operator actions.
- **Provider plugin platform**: Modal, RunPod, Hyperstack, local runtimes, and
  future providers should fit behind a stable provider-evidence and execution
  contract so new providers do not require ad hoc core redesign.

## Product Promise

gpucall is not just an OpenAI-compatible facade and not just a GPU rental helper.
It is a governed GPU execution control plane.

Its value is to remove provider, GPU, model, cost, and readiness decisions from
external application code while keeping those decisions deterministic and
auditable.

## Non-Negotiable Principles

1. External systems declare workload intent; they do not select production
   providers, GPUs, models, or endpoint IDs.
2. Routing remains deterministic. LLMs do not choose recipes, tuples, providers,
   prices, fallback order, validation status, cleanup action, or production
   promotion.
3. Automation is governed automation. gpucall may automate provider selection
   only through policy, recipe requirements, provider evidence, price freshness,
   validation evidence, and tenant constraints.
4. Data sovereignty is part of the product, not an add-on. DataRef boundaries,
   cleanup, audit evidence, artifact lifecycle, and fail-closed behavior matter
   as much as successful inference.
5. Cost is a first-class routing input. Estimates, current prices, freshness
   windows, and budget caps must be visible and enforceable before billable
   execution.
6. AI agents are first-class callers. Outputs, errors, readiness status,
   failure artifacts, and operator actions must be structured, idempotent, and
   machine-readable.
7. Unknown, stale, unvalidated, over-budget, or provider-ambiguous states fail
   closed. gpucall must not silently route to a weaker or ungoverned execution
   path.

## Near-Term Milestone: Provider Panopticon

The provider monitoring/control-plane concept is named:

- Japanese: `プロバイダー・パノプティコン`
- English working name: `Provider Panopticon`

Provider Panopticon is not the north star. It is a small near-term milestone on
the path toward the north star.

It exists because provider capacity discovery must be removed from gpucall's
request execution path. Long-running execution attempts must not perform
provider discovery by retrying and falling through stale candidates.

Provider Panopticon is responsible for observing provider market state and
provider operational state on a timer, then publishing freshness-bounded
snapshots that gpucall can use before routing.

MVP observations:

- endpoint existence
- endpoint health
- worker readiness, idle/running counts, and queue depth
- model serving readiness, including `/models` where available
- model id and context length / `max_model_len`
- template, image, and declared runtime configuration
- current or fresh price evidence
- validation evidence freshness

Provider Panopticon does not execute workloads and does not guarantee that a
freshly observed provider will still be available at dispatch time. Its job is
to remove dead, stale, unpriced, unvalidated, or currently unready tuples before
gpucall builds the execution chain. gpucall may still keep short fallback for
last-moment race conditions, but fallback must not be provider discovery.

## Responsibility Boundary

```text
external system / AI agent
  -> declares task, mode, inputs/DataRefs, constraints, and budget

gpucall gateway
  -> selects recipe and tuple deterministically
  -> enforces policy, validation evidence, price freshness, and fail-closed gates
  -> dispatches work and records audit/cleanup evidence

Provider Panopticon
  -> observes provider market and readiness state
  -> publishes freshness-bounded provider evidence
  -> never runs generation work
```

## Product Direction

The v2 direction is to make gpucall the place where external systems hand off
GPU execution intent without knowing provider infrastructure.

This is the consolidated roadmap from the current Codex discussion, Claude Code
review, and a separate GPT-5.5 Codex review.

### v2: Governed Job Gateway Kernel

v2 is the first practical step toward "GPU as electricity" for `infer` and
`vision`. It is not a full platform release.

Required:

- minimal workload-declarative input: task, intent, mode, input/DataRef,
  constraints, and budget
- deterministic routing, policy gates, validation evidence, price freshness,
  provider readiness, and fail-closed behavior
- structured readiness, failure artifacts, and operator action
- Provider Panopticon MVP as a freshness-bounded provider evidence source
- persistent job kernel: job id, attempt id, idempotency key, phase state, and
  attempt records
- minimal metering hooks and a budget hard-ceiling enforcement point
- storage lifecycle records and minimal cleanup evidence

Out of scope for v2:

- public full async platform
- MCP/tool transport
- training, LoRA, and checkpoint lifecycle
- enterprise billing
- public provider plugin SDK

### v2.5: Agent-Native Execution Layer

v2.5 makes gpucall a first-class tool for AI agents while staying within
`infer` and `vision`.

- public async API and CLI: submit, status, result, cancel, logs
- workload-declarative CLI/API hardening
- agent-readable structured output, retry taxonomy, and failure taxonomy
- estimate and budget hardening
- MCP/tool interface
- Provider Panopticon TTL enforcement, stale fail-closed behavior, and probe
  failure escalation

### v3: Training / LoRA / Artifact Lifecycle

v3 extends the electricity model beyond inference into long-running artifact
work.

- `gpucall train` and LoRA/fine-tune job types
- checkpoint synchronization and resumable jobs
- encrypted artifact export
- local result retention
- stronger cleanup proof
- first data-sovereignty report

Prerequisites: v2 persistent job kernel, v2 metering hooks, v2 storage lifecycle
records, and v2.5 async lifecycle.

### v4: Managed Product / Enterprise Control Plane

v4 turns the governed execution system into a managed product.

- SaaS credit and zero-config first run
- tenant quota, org policy, and enterprise reporting
- managed Provider Panopticon
- market arbitration history and provider cost/performance history
- sovereignty and audit reporting as a product surface

### v5: Provider Platform

v5 turns gpucall from a product into a provider ecosystem.

- public provider plugin SDK
- provider conformance tests and certification
- community/provider-owned adapters
- local GPU, private GPU, niche provider, and enterprise private GPU support
- stable provider ABI

Provider plugins are intentionally late. The internal provider abstraction
should not be public until it has survived inference, vision, metering, cleanup
evidence, artifact lifecycle, and managed-product requirements.
