# v4 Managed Product / Enterprise Control Plane Plan

Updated: 2026-07-02 JST
Status: plan

## Objective

Turn the governed execution system into a managed product: tenants, quota,
RBAC, audit, billing/metering, policy management, operator UX, and compliance
evidence — without weakening the deterministic v2 kernel.

## What Already Exists (verified 2026-07-02)

| Capability | Anchor | State |
| --- | --- | --- |
| Tenants | `TenantSpec` (rpm, daily/monthly budget), `admin tenant-create/list/onboard` | implemented |
| Tenant keys | `admin tenant-key-create/list/rotate/revoke`, fingerprints, expiry, caller auth lifecycle records | implemented |
| Budget enforcement | atomic reserve/commit/release ledger (SQLite/Postgres), fail-closed on cap | implemented |
| Audit trail | `AuditTrail` hash-chained, `audit verify/rotate/tail`, `immutable_audit` policy | implemented |
| Metering hooks | dispatcher success/terminal callbacks, per-plan cost commit | implemented |
| Handoff/onboarding automation | handoff packages, caller auth lifecycle, synthetic dry-run gate | implemented (v2 OOB) |
| Machine-readable ops | readiness, shipment classification, blocker taxonomy with owner/next action | implemented |

## Gap List (v4 scope)

1. **RBAC**: role model `owner / operator / auditor / caller` over the admin
   surface. v2 has caller-vs-operator separation by construction (tenant keys
   vs local CLI access); v4 adds scoped admin API keys so a managed service can
   delegate: `auditor` = read audit/readiness only, `operator` = recipe/route
   management, `owner` = tenant and key management. Enforced at the gateway
   admin endpoints, recorded in the audit trail.
2. **Quota beyond budget**: per-tenant concurrency caps, per-recipe request
   quotas, and per-intent rate classes; deterministic rejection with
   `tenant_quota` failure kind (extends the v2.5 failure taxonomy).
3. **Billing/metering export**: monthly metering statement per tenant from the
   usage ledger (jobs, tuple seconds, committed USD, provider split) as a
   deterministic artifact (`gpucall admin metering-export --tenant --month`),
   suitable for invoicing; no live pricing logic in the export path.
4. **Policy management surface**: versioned policy bundles with plan/apply and
   diff (`gpucall admin policy plan/apply`), reusing the setup-plan consent
   grammar (plan hash binding) so policy mutation is as governed as provider
   mutation.
5. **Team admin / multi-operator**: operator identity in every admin mutation
   audit record; concurrent admin mutation protection via config-hash
   compare-and-set (reject stale applies).
6. **Compliance evidence**: one-command evidence bundle — audit chain
   verification result, cleanup receipts, data-sovereignty report (where did
   DataRefs go, what remains), validation evidence inventory, TTL policy —
   as a redacted tarball for auditors.
7. **SLA / health dashboard**: Prometheus/Grafana assets exist; add
   route-scoped SLO recording rules (readiness %, validation freshness,
   provider evidence staleness) and a tenant-facing status JSON.
8. **Provider spend guard**: cross-tenant provider-level spend ceiling
   (daily/monthly USD per provider account) enforced before dispatch,
   independent of tenant budgets — the "runaway provider bill" backstop.
9. **Post-launch / incident reports**: `post-launch-report` exists; add
   incident report artifact generation from failure-artifact clusters
   (same failure_kind + tuple + window) with owner and next action.
10. **Tenant-scoped cleanup**: `admin tenant-offboard` — revoke keys, purge
    tenant object-store prefix, expire jobs, produce cleanup receipts.

## Non-Negotiables

- RBAC and quota decisions are deterministic policy evaluation; no inference.
- Billing export is derived from the committed ledger only.
- Evidence bundles are redacted by construction (reuse handoff redaction).
- Managed-product surfaces must not weaken fail-closed defaults.

## Sequencing

1. Quota + failure-taxonomy extension (small, high value)
2. Provider spend guard (safety)
3. Metering export (unblocks external billing)
4. RBAC admin scopes + audited operator identity
5. Policy plan/apply with consent hash
6. Compliance evidence bundle + tenant offboarding
7. SLO rules + status surface + incident reports

## Acceptance (machine-checkable)

- pytest per stage; quota/spend-guard fail-closed tests
- metering export reproducible byte-for-byte from a fixed ledger fixture
- RBAC matrix test: every admin endpoint × role → allow/deny as declared
- offboarding leaves zero tenant-prefixed objects and revoked keys return 401
