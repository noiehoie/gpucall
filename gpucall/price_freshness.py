from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
