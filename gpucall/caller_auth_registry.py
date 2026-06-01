from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpucall.config import default_state_dir


CALLER_AUTH_REGISTRY_SCHEMA_VERSION = 1


def caller_auth_registry_path() -> Path:
    return default_state_dir() / "setup" / "caller-auth.json"


def fingerprint_secret(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def load_caller_auth_registry(path: Path | None = None) -> dict[str, Any]:
    target = path or caller_auth_registry_path()
    if not target.exists():
        return {"schema_version": CALLER_AUTH_REGISTRY_SCHEMA_VERSION, "records": {}}
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": CALLER_AUTH_REGISTRY_SCHEMA_VERSION, "records": {}}
    if not isinstance(payload, dict):
        return {"schema_version": CALLER_AUTH_REGISTRY_SCHEMA_VERSION, "records": {}}
    records = payload.get("records")
    if not isinstance(records, dict):
        records = {}
    return {"schema_version": CALLER_AUTH_REGISTRY_SCHEMA_VERSION, **payload, "records": records}


def record_caller_auth(
    system_name: str,
    *,
    scope: str,
    token: str,
    expires_at: str | None = None,
    non_expiring_policy_reason: str | None = None,
    last_verification_status: str = "created",
    path: Path | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    current = load_caller_auth_registry(path)
    records = dict(current.get("records") or {})
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    records[system_name] = {
        "system_name": system_name,
        "scope": scope,
        "fingerprint": fingerprint_secret(token),
        "created_at": timestamp,
        "expires_at": expires_at,
        "non_expiring_policy_reason": non_expiring_policy_reason,
        "rotation_command": f"gpucall admin tenant-key-rotate --name {system_name}",
        "revocation_command": f"gpucall admin tenant-key-revoke --name {system_name}",
        "last_verification_status": last_verification_status,
        "updated_at": timestamp,
    }
    payload = {
        "schema_version": CALLER_AUTH_REGISTRY_SCHEMA_VERSION,
        "updated_at": timestamp,
        "records": records,
    }
    _write_json(path or caller_auth_registry_path(), payload)
    return payload


def mark_caller_auth_revoked(system_name: str, *, path: Path | None = None, now: datetime | None = None) -> dict[str, Any]:
    current = load_caller_auth_registry(path)
    records = dict(current.get("records") or {})
    timestamp = (now or datetime.now(timezone.utc)).isoformat()
    record = dict(records.get(system_name) or {"system_name": system_name})
    record["last_verification_status"] = "revoked"
    record["revoked_at"] = timestamp
    record["updated_at"] = timestamp
    records[system_name] = record
    payload = {
        "schema_version": CALLER_AUTH_REGISTRY_SCHEMA_VERSION,
        "updated_at": timestamp,
        "records": records,
    }
    _write_json(path or caller_auth_registry_path(), payload)
    return payload


def caller_auth_status_summary(path: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    current = load_caller_auth_registry(path)
    records = current.get("records") if isinstance(current, dict) else {}
    if not isinstance(records, dict) or not records:
        return {"state": "missing", "count": 0, "records": []}
    timestamp = now or datetime.now(timezone.utc)
    summaries: list[dict[str, Any]] = []
    for name, record in sorted(records.items()):
        if not isinstance(record, dict):
            continue
        created = _parse_datetime(str(record.get("created_at") or ""))
        age_seconds = int((timestamp - created).total_seconds()) if created else None
        summaries.append(
            {
                "system_name": name,
                "scope": record.get("scope"),
                "fingerprint": record.get("fingerprint"),
                "age_seconds": age_seconds,
                "expires_at": record.get("expires_at"),
                "last_verification_status": record.get("last_verification_status"),
            }
        )
    return {"state": "ok" if summaries else "missing", "count": len(summaries), "records": summaries}


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
