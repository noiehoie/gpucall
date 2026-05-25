from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any


CALLER_OWNER = "caller"
ADMIN_OWNER = "admin"
PROVIDER_OWNER = "provider"

HANDOFF_BY_OWNER = {
    CALLER_OWNER: "caller-c-kit",
    ADMIN_OWNER: "gpucall-recipe-admin",
    PROVIDER_OWNER: "provider-ops",
}

CALLER_QUALITY_BASELINE_MISSING = "CALLER_QUALITY_BASELINE_MISSING"
CALLER_BASELINE_FAILED = "CALLER_BASELINE_FAILED"
CALLER_WORKLOAD_NOT_OBSERVED = "CALLER_WORKLOAD_NOT_OBSERVED"
CALLER_CONTRACT_INCOMPLETE = "CALLER_CONTRACT_INCOMPLETE"
ADMIN_RECIPE_MISSING = "ADMIN_RECIPE_MISSING"
ADMIN_TUPLE_MISSING = "ADMIN_TUPLE_MISSING"
ADMIN_VALIDATION_MISSING = "ADMIN_VALIDATION_MISSING"
ADMIN_PRICE_EVIDENCE_MISSING = "ADMIN_PRICE_EVIDENCE_MISSING"
PROVIDER_ENDPOINT_STALE = "PROVIDER_ENDPOINT_STALE"
PROVIDER_SUPPLY_MISSING = "PROVIDER_SUPPLY_MISSING"

_INTAKE_PRIORITY = (
    CALLER_BASELINE_FAILED,
    CALLER_QUALITY_BASELINE_MISSING,
    CALLER_WORKLOAD_NOT_OBSERVED,
    CALLER_CONTRACT_INCOMPLETE,
    ADMIN_RECIPE_MISSING,
)


def typed_intake_blocker(message: str) -> dict[str, str]:
    text = str(message)
    lowered = text.lower()
    if "baseline trace contains" in lowered or "baseline command returned" in lowered:
        return _typed(
            code=CALLER_BASELINE_FAILED,
            owner=CALLER_OWNER,
            reason=text,
            next_action="rerun the caller baseline command until it exits cleanly and emits zero model/API, vision, and JSON extraction failures",
            next_artifact_required="workload-trace.json",
        )
    if "baseline metrics" in lowered or "metrics must not be empty" in lowered:
        return _typed(
            code=CALLER_QUALITY_BASELINE_MISSING,
            owner=CALLER_OWNER,
            reason=text,
            next_action="rerun gpucall-migrate trace or onboard with a successful caller baseline command so quality_contract.metrics can be populated",
            next_artifact_required="workload-trace.json",
        )
    if "detected statically" in lowered or "not observed in the supplied baseline trace" in lowered:
        return _typed(
            code=CALLER_WORKLOAD_NOT_OBSERVED,
            owner=CALLER_OWNER,
            reason=text,
            next_action="run a targeted caller baseline trace that exercises this workload before recipe materialization",
            next_artifact_required="workload-trace.json",
        )
    if "unknown workload" in lowered or "production intent registry" in lowered:
        return _typed(
            code=ADMIN_RECIPE_MISSING,
            owner=ADMIN_OWNER,
            reason=text,
            next_action="map the sanitized workload intent to a supported recipe intent or author a new admin-side recipe candidate",
            next_artifact_required="recipe-candidate.yml",
        )
    return _typed(
        code=CALLER_CONTRACT_INCOMPLETE,
        owner=CALLER_OWNER,
        reason=text,
        next_action="regenerate the caller workload contract with explicit intent, supported output contract, context budget, and quality metrics",
        next_artifact_required="workload-contract.json",
    )


def typed_intake_blockers(messages: Iterable[Any]) -> list[dict[str, str]]:
    return [typed_intake_blocker(str(message)) for message in messages if str(message)]


def primary_typed_blocker(blockers: Iterable[Mapping[str, Any]]) -> dict[str, Any] | None:
    rows = [dict(item) for item in blockers if isinstance(item, Mapping)]
    if not rows:
        return None
    by_code = {str(item.get("code") or ""): item for item in rows}
    for code in _INTAKE_PRIORITY:
        if code in by_code:
            return by_code[code]
    return rows[0]


def typed_blocker_counts(blockers: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    owner_counts: Counter[str] = Counter()
    code_counts: Counter[str] = Counter()
    handoff_counts: Counter[str] = Counter()
    for blocker in blockers:
        if not isinstance(blocker, Mapping):
            continue
        owner = str(blocker.get("owner") or "")
        code = str(blocker.get("code") or "")
        handoff = str(blocker.get("handoff") or "")
        if owner:
            owner_counts[owner] += 1
        if code:
            code_counts[code] += 1
        if handoff:
            handoff_counts[handoff] += 1
    return {
        "owner_counts": dict(sorted(owner_counts.items())),
        "code_counts": dict(sorted(code_counts.items())),
        "handoff_counts": dict(sorted(handoff_counts.items())),
    }


def shipment_blocker_metadata(category: str, reason: str) -> dict[str, str]:
    normalized = str(reason or "")
    if category == "endpoint_stale":
        return _typed(
            code=PROVIDER_ENDPOINT_STALE,
            owner=PROVIDER_OWNER,
            reason=normalized,
            next_action="repair, recreate, or decommission the provider endpoint, then refresh Provider Panopticon evidence",
            next_artifact_required="provider-panopticon.json",
        )
    if category == "price_unknown":
        return _typed(
            code=ADMIN_PRICE_EVIDENCE_MISSING,
            owner=ADMIN_OWNER,
            reason=normalized,
            next_action="refresh Provider Panopticon price evidence or add a fresh operator-approved price record before routing",
            next_artifact_required="provider-panopticon.json",
        )
    if category == "validation_missing":
        return _typed(
            code=ADMIN_VALIDATION_MISSING,
            owner=ADMIN_OWNER,
            reason=normalized,
            next_action="run gpucall-recipe-admin promotion validation for the listed tuple and activate only after validation evidence is current",
            next_artifact_required="validation-evidence.json",
        )
    if category == "supply_provisioning_required":
        return _typed(
            code=PROVIDER_SUPPLY_MISSING,
            owner=PROVIDER_OWNER,
            reason=normalized,
            next_action="run gpucall panopticon provision-plan/provision-apply for the reviewed tuple, then refresh Provider Panopticon readiness",
            next_artifact_required="provider-supply-provisioning-plan.json",
        )
    if normalized == "no_matching_readiness_recipe":
        return _typed(
            code=ADMIN_RECIPE_MISSING,
            owner=ADMIN_OWNER,
            reason=normalized,
            next_action="author or materialize an admin-side recipe candidate for this caller intent",
            next_artifact_required="recipe-candidate.yml",
        )
    if normalized == "invalid_workload_contract_context_budget_tokens":
        return _typed(
            code=CALLER_CONTRACT_INCOMPLETE,
            owner=CALLER_OWNER,
            reason=normalized,
            next_action="regenerate the caller workload contract with an integer context_budget_tokens value",
            next_artifact_required="workload-contract.json",
        )
    if normalized in {"no_static_eligible_tuple", "no_contract_compatible_readiness_recipe", "readiness_shipment_status_provider_lack"}:
        return _typed(
            code=ADMIN_TUPLE_MISSING,
            owner=ADMIN_OWNER,
            reason=normalized,
            next_action="author or enable recipe, tuple, surface, and worker candidates in an isolated promotion workspace; do not ask the caller to choose a provider",
            next_artifact_required="recipe-candidate.yml, tuple-candidate.yml, surface-candidate.yml, worker-candidate.yml",
        )
    return _typed(
        code=PROVIDER_SUPPLY_MISSING,
        owner=PROVIDER_OWNER,
        reason=normalized,
        next_action="restore provider worker readiness or capacity, then refresh Provider Panopticon evidence",
        next_artifact_required="provider-panopticon.json",
    )


def _typed(
    *,
    code: str,
    owner: str,
    reason: str,
    next_action: str,
    next_artifact_required: str,
) -> dict[str, str]:
    return {
        "code": code,
        "owner": owner,
        "handoff": HANDOFF_BY_OWNER[owner],
        "reason": reason,
        "next_action": next_action,
        "next_artifact_required": next_artifact_required,
    }
