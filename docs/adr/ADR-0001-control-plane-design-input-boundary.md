# ADR-0001: Control-Plane Design Input Boundary

Status: Accepted

## Context

gpucall is a deterministic GPU governance control plane. Production incidents
from external callers are useful because they expose product-level weaknesses,
but a single caller must not become the design target. The gateway must not gain
tenant-specific routes, recipe names, timeout constants, catalog rules, or
operator procedures.

The design input for control-plane work is therefore the normalized failure
class, not the triggering caller's private logs, business vocabulary, prompts,
file layout, or tenant name.

## Decision

Control-plane redesign and remediation use this boundary:

- External callers may trigger an investigation.
- New caller-specific logs are not design input once product-level failure
  classes have been extracted.
- Minimal normalized metadata may be retained when needed to define a generic
  test: task, intent, mode, input contract, output contract, response format,
  provider failure code, concurrency shape, and lifecycle state.
- Implementation and verification use synthetic fixtures and generic
  acceptance tests.
- The original caller may be used only as final external confirmation after the
  generic acceptance suite passes.

## Guardrails

Product code and product documentation must not contain external-system tenant
names, operator-local paths, private endpoints, API keys, provider endpoint ids,
or incident transcript material. Public documentation may describe the boundary
using generic terms such as "external caller", "tenant", "workload", and
"operator".

Runtime decisions must remain deterministic. This ADR does not permit LLM-based
routing, tenant-specific policy branches, or caller-specific fallback order.

## Consequences

This prevents caller contamination while keeping real incidents useful. It also
forces each remediation to become an acceptance-testable product behavior rather
than a one-off operational workaround.
