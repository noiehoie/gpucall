from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Mapping
from typing import Any

from gpucall.domain import ExecutionTupleSpec, PriceFreshness


def tuple_configured_price_freshness(tuple: ExecutionTupleSpec, *, now: datetime | None = None) -> PriceFreshness:
    now = now or datetime.now(timezone.utc)
    surface = tuple.execution_surface.value if tuple.execution_surface is not None else ""
    is_local_free = (
        tuple.configured_price_source == "local-free"
        or surface == "local_runtime"
        or str(tuple.adapter) in {"echo", "local", "local-echo", "local-ollama", "local-openai-compatible", "local-dataref-openai-worker"}
    )
    if float(tuple.cost_per_second) == 0.0 and is_local_free:
        return PriceFreshness.FRESH
    if float(tuple.cost_per_second) <= 0.0:
        return PriceFreshness.UNKNOWN
    return configured_price_freshness(
        price_source=tuple.configured_price_source,
        observed_at=tuple.configured_price_observed_at,
        ttl_seconds=tuple.configured_price_ttl_seconds,
        now=now,
    )


def validator_price_freshness(price: Any, overlay: Any, now: datetime) -> PriceFreshness:
    if price is None:
        return PriceFreshness.UNKNOWN
    if float(price.price_per_second or 0.0) == 0.0 and price.configured_price_source == "local-free":
        return PriceFreshness.FRESH
    next_revalidate = parse_time(getattr(overlay, "next_revalidate_after", None)) if overlay is not None else None
    if getattr(overlay, "price_per_second", None) is not None and next_revalidate is not None:
        return PriceFreshness.FRESH if next_revalidate > now else PriceFreshness.STALE
    if float(price.price_per_second or 0.0) <= 0.0:
        return PriceFreshness.UNKNOWN
    return configured_price_freshness(
        price_source=getattr(price, "configured_price_source", None),
        observed_at=getattr(price, "configured_price_observed_at", None),
        ttl_seconds=getattr(price, "configured_price_ttl_seconds", None),
        now=now,
    )


def configured_price_freshness(
    *,
    price_source: str | None,
    observed_at: str | None,
    ttl_seconds: float | int | None,
    now: datetime,
) -> PriceFreshness:
    if not price_source or not observed_at or ttl_seconds is None:
        return PriceFreshness.UNKNOWN
    observed = parse_time(str(observed_at))
    if observed is None:
        return PriceFreshness.UNKNOWN
    return PriceFreshness.FRESH if observed + timedelta(seconds=float(ttl_seconds)) > now else PriceFreshness.STALE


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def tuple_with_live_price_evidence(
    tuple: ExecutionTupleSpec,
    evidence: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> ExecutionTupleSpec:
    overlay = live_price_overlay(evidence, now=now)
    if overlay is None:
        return tuple
    return tuple.model_copy(update=overlay)


def tuples_with_live_price_evidence(
    tuples: Mapping[str, ExecutionTupleSpec],
    evidence_by_tuple: Mapping[str, Mapping[str, Any]] | None,
    *,
    now: datetime | None = None,
) -> dict[str, ExecutionTupleSpec]:
    evidence_by_tuple = evidence_by_tuple or {}
    return {
        name: tuple_with_live_price_evidence(tuple, evidence_by_tuple.get(name), now=now)
        for name, tuple in tuples.items()
    }


def live_price_overlay(evidence: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any] | None:
    if not isinstance(evidence, Mapping):
        return None
    now = now or datetime.now(timezone.utc)
    findings = evidence.get("findings")
    if not isinstance(findings, list):
        return None
    best: tuple[datetime, dict[str, Any]] | None = None
    for finding in findings:
        if not isinstance(finding, Mapping):
            continue
        if finding.get("dimension") != "price" or finding.get("severity") == "error":
            continue
        price = _finite_float(finding.get("live_price_per_second"))
        if price is None:
            continue
        observed = parse_time(str(finding.get("observed_at") or ""))
        expires = parse_time(str(finding.get("expires_at") or ""))
        if observed is None or expires is None or expires <= now:
            continue
        ttl_seconds = max(1.0, (expires - observed).total_seconds())
        overlay = {
            "cost_per_second": price,
            "configured_price_source": str(finding.get("live_price_source") or finding.get("source") or "provider-panopticon"),
            "configured_price_observed_at": observed.isoformat(),
            "configured_price_ttl_seconds": ttl_seconds,
        }
        if best is None or observed > best[0]:
            best = (observed, overlay)
    return best[1] if best is not None else None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    if parsed in (float("inf"), float("-inf")) or parsed != parsed:
        return None
    return parsed
