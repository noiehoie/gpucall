from __future__ import annotations

import json

from gpucall.lease_reaper import active_manifest_leases, lease_reaper_report


def test_lease_reaper_reports_active_manifest_leases(tmp_path) -> None:
    manifest = tmp_path / "leases.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps({"event": "provision.created", "tuple": "hyperstack", "vm_id": "vm-1"}),
                json.dumps({"event": "provision.created", "tuple": "hyperstack", "vm_id": "vm-2"}),
                json.dumps({"event": "destroyed", "tuple": "hyperstack", "vm_id": "vm-1"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert [lease["vm_id"] for lease in active_manifest_leases(manifest)] == ["vm-2"]
    report = lease_reaper_report(manifest_path=manifest)
    assert report["ok"] is False
    assert report["active_lease_count"] == 1
    assert report["actions"][0]["status"] == "dry_run"


def test_lease_reaper_preserves_provision_fields_after_destroy_pending(tmp_path) -> None:
    manifest = tmp_path / "leases.jsonl"
    manifest.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "provision.created",
                        "tuple": "hyperstack-a100",
                        "resource_kind": "vm",
                        "vm_id": "vm-1",
                        "vm_name": "gpucall-managed-plan-vm",
                    }
                ),
                json.dumps({"event": "destroy.pending", "vm_id": "vm-1"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    active = active_manifest_leases(manifest)
    assert active == [
        {
            "event": "destroy.pending",
            "tuple": "hyperstack-a100",
            "resource_kind": "vm",
            "vm_id": "vm-1",
            "vm_name": "gpucall-managed-plan-vm",
        }
    ]
    report = lease_reaper_report(manifest_path=manifest)
    assert report["actions"][0]["remote_id"] == "vm-1"
    assert report["actions"][0]["resource_kind"] == "vm"
