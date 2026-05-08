from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from gpucall.config import default_state_dir


def default_price_cache_path() -> Path:
    return default_state_dir() / "catalog" / "live-price-cache.json"


def load_cached_price_evidence(path: Path | None = None, *, now: datetime | None = None) -> dict[str, dict[str, Any]]:
    path = path or default_price_cache_path()
    now = now or datetime.now(timezone.utc)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    cached: dict[str, dict[str, Any]] = {}
    for tuple_name, row in (payload.get("tuples") or {}).items():
        if not isinstance(row, Mapping):
            continue
        expires_at = _parse_time(str(row.get("expires_at") or ""))
        finding = dict(row.get("finding") or {})
        if not finding:
            continue
        if expires_at is not None and expires_at > now:
            cached[str(tuple_name)] = {
                "tuple": str(tuple_name),
                "status": "live_revalidated",
                "checked": True,
                "findings": [finding],
            }
        else:
            cached[str(tuple_name)] = {
                "tuple": str(tuple_name),
                "status": "unknown",
                "checked": True,
                "findings": [
                    {
                        "tuple": str(tuple_name),
                        "adapter": str(finding.get("adapter") or ""),
                        "dimension": "price",
                        "severity": "error",
                        "reason": "cached live price TTL expired",
                        "source": str(finding.get("live_price_source") or finding.get("source") or "live-price-cache"),
                    }
                ],
            }
    return cached


def store_live_price_evidence(
    evidence: Mapping[str, Mapping[str, Any]],
    path: Path | None = None,
    *,
    now: datetime | None = None,
    ttl_seconds: int = 3600,
) -> None:
    path = path or default_price_cache_path()
    now = now or datetime.now(timezone.utc)
    existing = _read_payload(path)
    tuples = dict(existing.get("tuples") or {})
    for tuple_name, row in evidence.items():
        for finding in row.get("findings") or []:
            if not isinstance(finding, Mapping) or finding.get("dimension") != "price":
                continue
            if finding.get("live_price_per_second") is None:
                continue
            enriched = dict(finding)
            enriched["observed_at"] = now.isoformat()
            enriched["expires_at"] = (now + timedelta(seconds=ttl_seconds)).isoformat()
            tuples[str(tuple_name)] = {
                "finding": enriched,
                "observed_at": enriched["observed_at"],
                "expires_at": enriched["expires_at"],
                "ttl_seconds": ttl_seconds,
            }
    payload = {
        "schema_version": 1,
        "updated_at": now.isoformat(),
        "tuples": tuples,
    }
    _atomic_write_json(path, payload)


def merge_price_evidence(*items: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for evidence in items:
        if not evidence:
            continue
        for tuple_name, row in evidence.items():
            current = merged.get(tuple_name)
            if current is None:
                merged[tuple_name] = {
                    "tuple": str(row.get("tuple") or tuple_name),
                    "adapter": row.get("adapter"),
                    "status": row.get("status", "unknown"),
                    "checked": bool(row.get("checked")),
                    "findings": list(row.get("findings") or []),
                }
                continue
            current["checked"] = bool(current.get("checked")) or bool(row.get("checked"))
            # A blocked live observation wins over cached price success; budget and routing gates fail closed.
            if row.get("status") == "blocked" or current.get("status") == "blocked":
                current["status"] = "blocked"
            elif row.get("status") == "live_revalidated":
                current["status"] = "live_revalidated"
            current["findings"] = [*list(current.get("findings") or []), *list(row.get("findings") or [])]
    return merged


def _read_payload(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": 1, "tuples": {}}


def _atomic_write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        Path(tmp_name).replace(path)
    finally:
        tmp = Path(tmp_name)
        if tmp.exists():
            tmp.unlink()


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed
