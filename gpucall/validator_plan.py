from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from gpucall.execution_catalog import ResourceCatalogSnapshot


class ValidatorQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_ref: str
    worker_ref: str | None = None
    tuple_name: str
    source: Literal["active_tuple", "tuple_candidate"]
    execution_surface: str
    account_ref: str
    reason: str
    priority: int
    estimated_validation_cost_usd: float
    validation_budget_usd: float
    price_freshness: Literal["fresh", "stale", "unknown"]
    next_revalidate_after: str | None = None
    selected: bool = False
    skip_reason: str | None = None


class ValidatorPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_schema_version: int = 1
    plan_id: str
    snapshot_id: str
    generated_at: str
    budget_usd: float
    selected_estimated_cost_usd: float
    selected_count: int
    queue: tuple[ValidatorQueueItem, ...] = Field(default_factory=tuple)
    skipped: tuple[ValidatorQueueItem, ...] = Field(default_factory=tuple)


def build_validator_plan(
    snapshot: ResourceCatalogSnapshot,
    *,
    budget_usd: float,
    max_items: int | None = None,
    include_candidates: bool = False,
    strict_price: bool = True,
    now: datetime | None = None,
) -> ValidatorPlan:
    now = now or datetime.now(timezone.utc)
    overlays = {overlay.resource_ref: overlay for overlay in snapshot.live_status_overlay}
    evidence = {item.resource_ref: item for item in snapshot.validation_evidence if item.resource_ref}
    prices = {rule.resource_ref: rule for rule in snapshot.pricing_rules}
    workers = {worker.worker_ref: worker for worker in snapshot.workers}

    raw_items: list[ValidatorQueueItem] = []
    for claim in snapshot.capability_claims:
        source = "tuple_candidate" if claim.claim_source == "candidate_matrix" else "active_tuple"
        if source == "tuple_candidate" and not include_candidates:
            continue
        resource = next((item for item in snapshot.resources if item.resource_ref == claim.resource_ref), None)
        if resource is None:
            continue
        overlay = overlays.get(claim.resource_ref)
        validation = evidence.get(claim.resource_ref)
        reason = _validation_reason(validation, overlay, now)
        if reason is None:
            continue
        price = prices.get(claim.resource_ref)
        cost = _estimated_validation_cost(price)
        price_freshness = _price_freshness(price, overlay, now)
        worker = workers.get(claim.worker_ref)
        raw_items.append(
            ValidatorQueueItem(
                resource_ref=claim.resource_ref,
                worker_ref=claim.worker_ref,
                tuple_name=resource.tuple_name,
                source=source,
                execution_surface=resource.execution_surface,
                account_ref=resource.account_ref,
                reason=reason,
                priority=_priority(reason, source),
                estimated_validation_cost_usd=cost,
                validation_budget_usd=budget_usd,
                price_freshness=price_freshness,
                next_revalidate_after=overlay.next_revalidate_after if overlay is not None else None,
                skip_reason=_structural_skip_reason(resource, worker, overlay, price_freshness=price_freshness, strict_price=strict_price),
            )
        )

    selected: list[ValidatorQueueItem] = []
    skipped: list[ValidatorQueueItem] = []
    spent = 0.0
    ordered = sorted(raw_items, key=lambda item: (item.skip_reason is not None, item.priority, item.estimated_validation_cost_usd, item.tuple_name))
    for item in ordered:
        if item.skip_reason is not None:
            skipped.append(item)
            continue
        if max_items is not None and len(selected) >= max_items:
            skipped.append(item.model_copy(update={"skip_reason": "max_items_exhausted"}))
            continue
        if spent + item.estimated_validation_cost_usd > budget_usd:
            skipped.append(item.model_copy(update={"skip_reason": "validation_budget_exhausted"}))
            continue
        selected_item = item.model_copy(update={"selected": True})
        selected.append(selected_item)
        spent += item.estimated_validation_cost_usd

    generated_at = now.isoformat()
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "generated_at": generated_at,
        "budget_usd": budget_usd,
        "selected": [item.model_dump(mode="json") for item in selected],
        "skipped": [item.model_dump(mode="json") for item in skipped],
    }
    plan_id = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return ValidatorPlan(
        plan_id=plan_id,
        snapshot_id=snapshot.snapshot_id,
        generated_at=generated_at,
        budget_usd=budget_usd,
        selected_estimated_cost_usd=round(spent, 8),
        selected_count=len(selected),
        queue=tuple(selected),
        skipped=tuple(skipped),
    )


def dumps_validator_plan(plan: ValidatorPlan) -> str:
    return json.dumps(plan.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _validation_reason(validation: Any, overlay: Any, now: datetime) -> str | None:
    if validation is None:
        return "missing_validation_evidence"
    if validation.latest_passed is False:
        return "latest_validation_failed"
    expires_at = _parse_time(validation.expires_at)
    if expires_at is not None and expires_at <= now:
        return "validation_evidence_expired"
    if overlay is not None and overlay.status in {"unknown", "not_checked"}:
        return "live_status_unknown"
    next_revalidate = _parse_time(getattr(overlay, "next_revalidate_after", None)) if overlay is not None else None
    if next_revalidate is not None and next_revalidate <= now:
        return "live_status_ttl_expired"
    return None


def _estimated_validation_cost(price: Any) -> float:
    if price is None:
        return 0.0
    seconds = float(price.min_billable_seconds or price.billing_granularity_seconds or 1.0)
    seconds = max(1.0, seconds)
    return round(float(price.price_per_second) * seconds, 8)


def _priority(reason: str, source: str) -> int:
    base = {
        "latest_validation_failed": 10,
        "validation_evidence_expired": 20,
        "missing_validation_evidence": 30,
        "live_status_ttl_expired": 40,
        "live_status_unknown": 50,
    }.get(reason, 100)
    return base + (100 if source == "tuple_candidate" else 0)


def _structural_skip_reason(
    resource: Any,
    worker: Any,
    overlay: Any,
    *,
    price_freshness: str,
    strict_price: bool,
) -> str | None:
    if worker is None:
        return "missing_worker_contract"
    if strict_price and price_freshness != "fresh":
        return "price_not_fresh"
    if overlay is not None and overlay.status == "blocked":
        return "live_status_blocked"
    if resource.source == "tuple_candidate" and not worker.endpoint_configured:
        return "candidate_missing_endpoint_or_target"
    return None


def _price_freshness(price: Any, overlay: Any, now: datetime) -> Literal["fresh", "stale", "unknown"]:
    if price is None:
        return "unknown"
    if float(price.price_per_second or 0.0) == 0.0 and price.configured_price_source == "local-free":
        return "fresh"
    next_revalidate = _parse_time(getattr(overlay, "next_revalidate_after", None)) if overlay is not None else None
    if getattr(overlay, "price_per_second", None) is not None and next_revalidate is not None:
        return "fresh" if next_revalidate > now else "stale"
    observed = _parse_time(getattr(price, "configured_price_observed_at", None))
    ttl = getattr(price, "configured_price_ttl_seconds", None)
    if observed is None or ttl is None or float(price.price_per_second or 0.0) <= 0.0:
        return "unknown"
    return "fresh" if observed + timedelta(seconds=float(ttl)) > now else "stale"


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
