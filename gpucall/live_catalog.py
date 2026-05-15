from __future__ import annotations

import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Literal, Mapping


LiveSeverity = Literal["error", "info"]
LiveDimension = Literal["contract", "price", "stock", "endpoint", "credential", "cost"]


@dataclass(frozen=True)
class LiveCatalogObservation:
    tuple: str
    adapter: str
    dimension: LiveDimension
    severity: LiveSeverity = "info"
    reason: str | None = None
    field: str | None = None
    source: str | None = None
    live_price_per_second: float | None = None
    live_price_source: str | None = None
    live_stock_state: Literal["available", "unavailable", "unknown"] | None = None
    raw: Mapping[str, Any] = dataclass_field(default_factory=dict)

    def as_finding(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tuple": self.tuple,
            "adapter": self.adapter,
            "dimension": self.dimension,
            "severity": self.severity,
        }
        if self.reason:
            payload["reason"] = self.reason
        if self.field:
            payload["field"] = self.field
        if self.source:
            payload["source"] = self.source
        if self.live_price_per_second is not None:
            payload["live_price_per_second"] = self.live_price_per_second
            payload["live_price_source"] = self.live_price_source or self.source or "live_catalog"
        if self.live_stock_state:
            payload["live_stock_state"] = self.live_stock_state
        if self.raw:
            payload["raw"] = dict(self.raw)
        return payload


def live_error(
    tuple: Any,
    *,
    dimension: LiveDimension,
    reason: str,
    field: str | None = None,
    source: str | None = None,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return LiveCatalogObservation(
        tuple=str(tuple.name),
        adapter=str(tuple.adapter),
        dimension=dimension,
        severity="error",
        reason=reason,
        field=field,
        source=source,
        raw=raw or {},
    ).as_finding()


def live_info(
    tuple: Any,
    *,
    dimension: LiveDimension,
    reason: str | None = None,
    field: str | None = None,
    source: str | None = None,
    live_price_per_second: float | None = None,
    live_stock_state: Literal["available", "unavailable", "unknown"] | None = None,
    raw: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return LiveCatalogObservation(
        tuple=str(tuple.name),
        adapter=str(tuple.adapter),
        dimension=dimension,
        severity="info",
        reason=reason,
        field=field,
        source=source,
        live_price_per_second=live_price_per_second,
        live_price_source=source,
        live_stock_state=live_stock_state,
        raw=raw or {},
    ).as_finding()


def price_per_second_from_mapping(payload: Mapping[str, Any]) -> tuple[float | None, str | None]:
    second_keys = (
        "price_per_second",
        "cost_per_second",
        "currentPricePerSecond",
        "flexCostPerSecond",
        "activeCostPerSecond",
    )
    hour_keys = (
        "price_per_hour",
        "cost_per_hour",
        "pricePerHour",
        "costPerHour",
        "hourly_price",
        "hourlyPrice",
        "currentPricePerGpuHour",
    )
    for key in second_keys:
        price = _floatish(payload.get(key))
        if price is not None and price >= 0:
            return price, key
    for key in hour_keys:
        price = _floatish(payload.get(key))
        if price is not None and price >= 0:
            return price / 3600.0, key
    current = _floatish(payload.get("currentPricePerGpu"))
    if current is not None and 0 <= current < 0.1:
        return current, "currentPricePerGpu"
    return None, None


def price_per_second_from_hourly_text(text: str, label_patterns: list[str]) -> float | None:
    normalized = re.sub(r"\s+", " ", text)
    for pattern in label_patterns:
        match = re.search(pattern + r".{0,160}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)?\s*(?:hr|hour)", normalized, re.I)
        if match:
            return float(match.group(1)) / 3600.0
    return None


def price_per_second_from_pricing_text(text: str, label_patterns: list[str]) -> float | None:
    """Extract a provider-published GPU price from simple pricing HTML/text.

    Provider pricing pages are not stable APIs. This parser only accepts values
    adjacent to a declared GPU label and supports the two public formats used by
    Modal and Hyperstack: explicit per-second prices and hourly table cells.
    """
    normalized = re.sub(r"<[^>]+>", " ", text)
    normalized = re.sub(r"&nbsp;", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    for pattern in label_patterns:
        second = re.search(pattern + r".{0,240}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)?\s*(?:sec|second)", normalized, re.I)
        if second:
            return float(second.group(1))
        hourly = re.search(pattern + r".{0,240}?\$?\s*([0-9]+(?:\.[0-9]+)?)\s*(?:/|per)?\s*(?:hr|hour)", normalized, re.I)
        if hourly:
            return float(hourly.group(1)) / 3600.0
        table_cell = re.search(pattern + r".{0,700}?\$\s*([0-9]+(?:\.[0-9]+)?)", normalized, re.I)
        if table_cell:
            return float(table_cell.group(1)) / 3600.0
    return None


def _floatish(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace("$", "").replace(",", "")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
