from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def lease_reaper_report(*, manifest_path: Path, apply: bool = False) -> dict[str, Any]:
    active = active_manifest_leases(manifest_path)
    actions = [
        {
            "action": "destroy_resource",
            "provider": lease.get("provider"),
            "resource_kind": lease.get("resource_kind") or "vm",
            "remote_id": lease.get("vm_id") or lease.get("remote_id"),
            "source": str(manifest_path),
            "status": "requires_manual_provider_reaper" if apply else "dry_run",
        }
        for lease in active
    ]
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "apply": apply,
        "manifest_path": str(manifest_path),
        "active_lease_count": len(active),
        "active_leases": active,
        "actions": actions,
        "ok": len(active) == 0,
    }


def active_manifest_leases(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    active: dict[str, dict[str, Any]] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        remote_id = str(row.get("vm_id") or row.get("remote_id") or "")
        if not remote_id:
            continue
        event = str(row.get("event") or "")
        if event in {"destroyed", "destroy.requested"}:
            active.pop(remote_id, None)
        elif event in {"provision.created", "lease.started"}:
            active[remote_id] = row
    return list(active.values())
