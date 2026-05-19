from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from gpucall.panopticon import (
    default_panopticon_path,
    load_panopticon_evidence,
    merge_panopticon_evidence,
    store_panopticon_evidence,
)


def default_price_cache_path() -> Path:
    return default_panopticon_path()


def load_cached_price_evidence(path: Path | None = None, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    now = _utc_datetime(now or datetime.now(timezone.utc))
    evidence = load_panopticon_evidence(
        path or default_price_cache_path(),
        now=now,
        expired_status="unknown",
        expired_reason="cached live price TTL expired",
        allow_legacy_price_finding=True,
    )
    cached: dict[str, dict[str, Any]] = {}
    for tuple_name, row in evidence.items():
        findings = _price_findings(row, now=now)
        if not findings:
            continue
        cached[str(tuple_name)] = {
            "tuple": str(row.get("tuple") or tuple_name),
            "status": row.get("status", "unknown"),
            "checked": bool(row.get("checked")),
            "findings": findings,
        }
    return cached


def store_live_price_evidence(
    evidence: Mapping[str, Mapping[str, Any]],
    path: Path | None = None,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 3600,
) -> None:
    price_evidence: dict[str, dict[str, Any]] = {}
    for tuple_name, row in evidence.items():
        findings = [
            dict(item)
            for item in row.get("findings") or []
            if isinstance(item, Mapping)
            and item.get("dimension") == "price"
            and item.get("live_price_per_second") is not None
        ]
        if findings:
            price_evidence[str(tuple_name)] = {**dict(row), "findings": findings}
    if price_evidence:
        store_panopticon_evidence(price_evidence, path or default_price_cache_path(), now=now, ttl_seconds=ttl_seconds)


def merge_price_evidence(*items: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return merge_panopticon_evidence(*items)


def _price_findings(row: Mapping[str, Any], *, now: datetime) -> list[dict[str, Any]]:
    raw_findings = [dict(item) for item in row.get("findings") or [] if isinstance(item, Mapping)]
    price_findings = [finding for finding in raw_findings if finding.get("dimension") == "price"]
    valid_price_findings = [
        finding
        for finding in price_findings
        if finding.get("live_price_per_second") is not None and _finding_expires_after(finding, now)
    ]
    if valid_price_findings:
        return valid_price_findings
    has_price_evidence = bool(price_findings) or "price" in {str(item) for item in row.get("dimensions") or []}
    if row.get("panopticon_stale") and has_price_evidence:
        for finding in raw_findings:
            raw = finding.get("raw") if isinstance(finding.get("raw"), Mapping) else {}
            if raw.get("live_reason") == "panopticon_evidence_expired":
                return [{**finding, "dimension": "price"}]
        return [
            {
                "tuple": row.get("tuple"),
                "dimension": "price",
                "severity": "error",
                "reason": "cached live price TTL expired",
                "raw": {"live_reason": "panopticon_evidence_expired"},
            }
        ]
    return []


def _finding_expires_after(finding: Mapping[str, Any], now: datetime) -> bool:
    now = _utc_datetime(now)
    raw = str(finding.get("expires_at") or "")
    if not raw:
        return False
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > now


def _utc_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
