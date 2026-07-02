# v5 Provider Platform Plan

Updated: 2026-07-02 JST
Status: plan — first conformance surface shipped in v2.0.68

## Objective

Make providers a plugin platform: adding a provider must not require core
redesign, and a third-party adapter must be provably held to the same
contract as built-in adapters (Modal / RunPod / Hyperstack / local).

## What Already Exists (verified 2026-07-02)

| Capability | Anchor | State |
| --- | --- | --- |
| Plugin entry points | `gpucall.adapters`, `gpucall.configure_targets`, `gpucall.credential_sources` in pyproject | implemented |
| Adapter ABC | `TupleAdapter` start/wait/cancel_remote/stream, `ResourceLease` | implemented |
| Adapter descriptors | `TupleAdapterDescriptor`: surface, contracts, production eligibility, config/catalog validators, official sources | implemented |
| Registry conformance checks | `gpucall provider-conformance` (v2.0.68): descriptor, surface, production contracts or rejection reason, stream scope, vendor family | shipped |
| Execution-cycle conformance | `run_execution_cycle_conformance` against owned local (echo) adapter | shipped (test harness) |
| Same-abstraction proof | Modal / RunPod / Hyperstack / local all behind `build_registered_adapter` | implemented |
| Canonical error vocabulary | `ProviderErrorCode` + `/v2/failure-taxonomy` (v2.5) | implemented |

## Gap List (v5 scope)

1. **Provider plugin SDK (public)**: a documented, versioned package boundary
   (`gpucall-provider-sdk`) exporting exactly: `TupleAdapter`, `ResourceLease`,
   `TupleAdapterDescriptor`, `register_adapter`, canonical errors, and the
   payload/normalization helpers a plugin legitimately needs. Nothing else is
   ABI. Built-ins migrate to consume the same boundary.
2. **Evidence contract**: plugins must supply the Panopticon probe surface —
   non-generation health, existence, readiness, price observation callables —
   declared in the descriptor; Panopticon refuses to route through adapters
   without probe support (fail closed, already the default for unknowns).
3. **Pricing contract**: descriptor-declared price source (static config /
   live API probe) with TTL semantics matching the packaged TTL table.
4. **Readiness contract**: standardized `supply-missing / supply-pending /
   supply-exists / supply-ready` reporting from plugin probes (state grammar
   already exists in the OOB spec).
5. **Full conformance test suite**: extend `gpucall provider-conformance`
   beyond registry checks:
   - execution-cycle conformance against the plugin's own sandbox tuple
   - error-mapping conformance: plugin must map its failure modes to
     `ProviderErrorCode` (table-driven fixture test)
   - cleanup conformance: lease → cancel → verified-absent receipt
   - payload conformance: OpenAI-contract fixture in, normalized result out
   - security/data-sovereignty conformance: no ambient credentials, no
     DataRef body logging, redaction guarantees (static + runtime checks)
6. **Certification flow**: `gpucall provider-conformance --adapter X --full`
   emits a signed conformance report artifact; the config validator refuses
   `production_eligible: true` for uncertified third-party adapters unless the
   operator explicitly accepts (`allow_uncertified_adapter: true` per tuple).
7. **Sample provider implementation**: a documented reference plugin
   (`gpucall-provider-example`) implementing the echo semantics through the
   public SDK — the template third parties copy.
8. **Stable ABI policy**: semver on the provider SDK; conformance suite is the
   compatibility gate for SDK upgrades.

## Non-Negotiables

- Provider adapters remain execution devices, not decision makers: no routing,
  no fallback, no model swap, no budget interpretation inside plugins.
- Conformance checks are deterministic and non-generation by default; any
  billable conformance step is explicit and budget-gated exactly like tuple
  validation.
- Descriptor `stream_contract=None` (tuple-defined) semantics are preserved.

## Sequencing

1. Error-mapping + payload conformance fixtures (pure tests, high value)
2. Cleanup conformance with receipts (pairs with v3 cleanup proof)
3. Public SDK package boundary + built-in migration
4. Probe/pricing/readiness descriptor contracts + Panopticon integration
5. Certification artifact + config gate
6. Sample provider + docs

## Acceptance (machine-checkable)

- `gpucall provider-conformance` full mode passes for Modal/RunPod/Hyperstack/local
- reference plugin passes certification from a clean checkout using only the
  public SDK
- an uncertified adapter marked production-eligible fails `validate-config`
