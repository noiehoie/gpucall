"""Data-sovereignty evidence: what caller data exists where, and its reclamation.

gpucall's promise is that caller data only ever lives in three places: the
caller's own machine, the operator-controlled object store, and — transiently —
an ephemeral provider worker. This module makes that promise auditable:

- ``object_inventory`` lists what the operator object store currently holds.
- ``reap_objects`` deletes aged objects and writes deletion receipts, each
  verified absent after deletion.
- ``sovereignty_report`` combines the inventory, the receipt history, and the
  static provider-residue model into one machine-readable artifact.

Everything here is deterministic and operator-side. No provider generation
APIs are called; deletion is scoped to the configured bucket only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from gpucall.config import default_state_dir

SOVEREIGNTY_SCHEMA_VERSION = 1

# Static residue model for the built-in provider execution surfaces. This is
# product documentation in machine-readable form: it states where caller
# payload bytes can and cannot persist for each surface family.
PROVIDER_RESIDUE_MODEL: dict[str, dict[str, Any]] = {
    "modal-function-runtime": {
        "payload_persistence": "ephemeral-container-memory-only",
        "worker_fetch": "presigned GET from the operator object store, TTL-bounded",
        "provider_storage": "no volumes are mounted for payload data; model weights are cached from public registries only",
        "logs": "worker code never prints payload bytes; stdout carries lengths, hashes, and status only",
        "after_job": "container is reclaimed after the tuple's scaledown window",
    },
    "local-runtime": {
        "payload_persistence": "operator host only",
        "worker_fetch": "localhost",
        "provider_storage": "none (operator-controlled machine)",
        "logs": "operator-controlled",
        "after_job": "process memory only",
    },
    "managed-endpoint": {
        "payload_persistence": "provider endpoint memory during execution",
        "worker_fetch": "presigned GET from the operator object store, TTL-bounded",
        "provider_storage": "no gpucall-managed volumes; provider-side request logging is provider policy",
        "logs": "gpucall sends no credentials in payloads; prompts transit the provider queue",
        "after_job": "governed by provider retention; prefer function-runtime surfaces for restricted data",
    },
}


def object_inventory(store: Any, *, prefix: str | None = None) -> list[dict[str, Any]]:
    """List objects in the operator object store bucket (keys, sizes, ages)."""
    client = store.client
    bucket = store.config.bucket
    paginator = client.get_paginator("list_objects_v2")
    kwargs: dict[str, Any] = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    now = datetime.now(timezone.utc)
    items: list[dict[str, Any]] = []
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents") or []:
            modified = obj.get("LastModified")
            age_seconds = int((now - modified).total_seconds()) if modified else None
            items.append(
                {
                    "key": obj["Key"],
                    "bytes": int(obj.get("Size") or 0),
                    "last_modified": modified.isoformat() if modified else None,
                    "age_seconds": age_seconds,
                }
            )
    return sorted(items, key=lambda item: item["key"])


def reap_objects(
    store: Any,
    *,
    older_than_days: float,
    prefix: str | None = None,
    apply: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete aged objects from the operator store and write deletion receipts.

    Dry-run by default. With ``apply=True`` each deletion is verified absent
    afterwards and the receipt records both facts.
    """
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    current = now or datetime.now(timezone.utc)
    cutoff_seconds = older_than_days * 86400
    inventory = object_inventory(store, prefix=prefix)
    candidates = [item for item in inventory if item["age_seconds"] is not None and item["age_seconds"] >= cutoff_seconds]
    receipts: list[dict[str, Any]] = []
    client = store.client
    bucket = store.config.bucket
    for item in candidates:
        receipt: dict[str, Any] = {
            "key": item["key"],
            "bytes": item["bytes"],
            "age_seconds": item["age_seconds"],
            "action": "delete" if apply else "would_delete",
        }
        if apply:
            client.delete_object(Bucket=bucket, Key=item["key"])
            receipt["deleted_at"] = datetime.now(timezone.utc).isoformat()
            receipt["verified_absent"] = _verified_absent(client, bucket, item["key"])
        receipts.append(receipt)
    report = {
        "schema_version": SOVEREIGNTY_SCHEMA_VERSION,
        "phase": "object-reclamation",
        "bucket": bucket,
        "prefix": prefix,
        "older_than_days": older_than_days,
        "generated_at": current.isoformat(),
        "apply": apply,
        "object_count_total": len(inventory),
        "object_count_eligible": len(candidates),
        "bytes_eligible": sum(item["bytes"] for item in candidates),
        "receipts": receipts,
        "all_verified_absent": all(r.get("verified_absent") is True for r in receipts) if (apply and receipts) else None,
    }
    if apply:
        report["receipt_path"] = str(_write_receipt(report, current))
    return report


def sovereignty_report(store: Any | None, *, prefix: str | None = None, now: datetime | None = None) -> dict[str, Any]:
    """One machine-readable answer to: where can caller data currently exist?"""
    current = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {
        "schema_version": SOVEREIGNTY_SCHEMA_VERSION,
        "phase": "sovereignty-report",
        "generated_at": current.isoformat(),
        "provider_residue_model": PROVIDER_RESIDUE_MODEL,
        "reclamation_receipts": _receipt_history(),
    }
    if store is None:
        report["object_store"] = {
            "configured": False,
            "note": "no object store configured; DataRef workflows are disabled and inline payloads never leave the request path",
        }
        return report
    try:
        inventory = object_inventory(store, prefix=prefix)
    except Exception as exc:
        report["object_store"] = {
            "configured": True,
            "bucket": store.config.bucket,
            "error": f"inventory unavailable: {type(exc).__name__}",
            "next_action": "run on the gateway host where object-store credentials are configured",
        }
        return report
    ages = [item["age_seconds"] for item in inventory if item["age_seconds"] is not None]
    report["object_store"] = {
        "configured": True,
        "bucket": store.config.bucket,
        "object_count": len(inventory),
        "bytes_total": sum(item["bytes"] for item in inventory),
        "oldest_age_seconds": max(ages) if ages else None,
        "presign_ttl_seconds": getattr(store.config, "presign_ttl_seconds", None),
        "objects": inventory,
    }
    return report


def _verified_absent(client: Any, bucket: str, key: str) -> bool:
    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception:
        return True
    return False


def _receipt_dir() -> Path:
    return default_state_dir() / "sovereignty"


def _write_receipt(report: dict[str, Any], current: datetime) -> Path:
    directory = _receipt_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"reclamation-{current.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _receipt_history(limit: int = 20) -> list[dict[str, Any]]:
    directory = _receipt_dir()
    if not directory.exists():
        return []
    entries: list[dict[str, Any]] = []
    for path in sorted(directory.glob("reclamation-*.json"), reverse=True)[:limit]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        entries.append(
            {
                "path": str(path),
                "generated_at": data.get("generated_at"),
                "object_count_eligible": data.get("object_count_eligible"),
                "bytes_eligible": data.get("bytes_eligible"),
                "all_verified_absent": data.get("all_verified_absent"),
            }
        )
    return entries
