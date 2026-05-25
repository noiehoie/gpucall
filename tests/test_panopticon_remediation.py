from __future__ import annotations

import json
import sys

import pytest

from gpucall.domain import Policy
from gpucall.panopticon import load_panopticon_evidence, store_panopticon_evidence
from gpucall.panopticon_remediation import (
    PanopticonRemediationPlan,
    apply_remediation_plan,
    build_remediation_plan,
    dumps_remediation_plan,
)


def test_remediation_plan_proposes_scale_and_delete_with_safe_defaults(tmp_path) -> None:
    snapshot_path = tmp_path / "provider-panopticon.json"
    store_panopticon_evidence(_sample_evidence(), snapshot_path)
    snapshot = load_panopticon_evidence(snapshot_path)

    plan = build_remediation_plan(snapshot, _policy(), source_snapshot_path=snapshot_path)

    actions = {(action.action, action.resource_id): action for action in plan.actions}
    assert plan.non_generation_probe_only is True
    assert actions[("exclude_from_routing", "runpod-vllm-vision")].policy_mode == "auto"
    scale = actions[("scale_workers_min_to_zero", "endpoint-1")]
    assert scale.policy_mode == "approval_required"
    assert scale.requires_approval is True
    assert scale.provider_mutation is True
    assert scale.destructive is False
    assert scale.apply_supported is True
    assert scale.parameters == {"workersMin": 0}
    delete_volume = actions[("delete_network_volume", "vol-unused")]
    assert delete_volume.destructive is True
    assert delete_volume.apply_supported is False
    assert delete_volume.apply_blocked_reasons == ["delete_network_volume is not supported by gpucall v2 apply"]


def test_remediation_policy_can_disable_actions(tmp_path) -> None:
    snapshot_path = tmp_path / "provider-panopticon.json"
    store_panopticon_evidence(_sample_evidence(), snapshot_path)
    snapshot = load_panopticon_evidence(snapshot_path)
    policy = _policy(
        {
            "exclude_from_routing": {"mode": "disabled"},
            "scale_workers_min_to_zero": {"mode": "disabled"},
            "delete_endpoint": {"mode": "approval_required"},
            "delete_network_volume": {"mode": "disabled"},
        }
    )

    plan = build_remediation_plan(snapshot, policy)

    assert plan.actions == []
    assert plan.status == "no_actions"


def test_auto_scale_policy_is_blocked_when_workers_are_active(tmp_path) -> None:
    snapshot_path = tmp_path / "provider-panopticon.json"
    evidence = _sample_evidence(active_workers=1, active_pods=1)
    store_panopticon_evidence(evidence, snapshot_path)
    snapshot = load_panopticon_evidence(snapshot_path)
    policy = _policy({"scale_workers_min_to_zero": {"mode": "auto", "require_no_inflight_jobs": True}})

    plan = build_remediation_plan(snapshot, policy)

    scale = next(action for action in plan.actions if action.action == "scale_workers_min_to_zero")
    assert scale.policy_mode == "auto"
    assert scale.requires_approval is False
    assert scale.apply_supported is False
    assert scale.apply_blocked_reasons == ["inflight_workers_or_pods_present"]


def test_apply_dry_run_never_calls_provider(monkeypatch) -> None:
    plan = PanopticonRemediationPlan.model_validate(
        {
            "schema_version": 1,
            "phase": "provider-panopticon-remediation-plan",
            "generated_at": "2026-05-22T00:00:00Z",
            "non_generation_probe_only": True,
            "status": "actions_proposed",
            "action_count": 1,
            "policy": {},
            "actions": [
                {
                    "action_id": "remediate-test",
                    "action": "scale_workers_min_to_zero",
                    "provider": "runpod",
                    "resource_type": "endpoint",
                    "resource_id": "endpoint-1",
                    "reason": "test",
                    "policy_mode": "approval_required",
                    "requires_approval": True,
                    "provider_mutation": True,
                    "destructive": False,
                    "apply_supported": True,
                    "parameters": {"workersMin": 0},
                }
            ],
        }
    )

    def fail_patch(*_args, **_kwargs):
        raise AssertionError("dry-run must not call RunPod")

    monkeypatch.setattr("gpucall.panopticon_remediation._runpod_patch_endpoint_workers_min_to_zero", fail_patch)

    result = apply_remediation_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=True)

    assert result.applied_count == 0
    assert result.skipped_count == 1
    assert result.results[0]["status"] == "dry_run"


def test_apply_yes_only_patches_supported_runpod_scale(monkeypatch) -> None:
    plan = PanopticonRemediationPlan.model_validate(
        {
            "schema_version": 1,
            "phase": "provider-panopticon-remediation-plan",
            "generated_at": "2026-05-22T00:00:00Z",
            "non_generation_probe_only": True,
            "status": "actions_proposed",
            "action_count": 2,
            "policy": {},
            "actions": [
                {
                    "action_id": "remediate-scale",
                    "action": "scale_workers_min_to_zero",
                    "provider": "runpod",
                    "resource_type": "endpoint",
                    "resource_id": "endpoint-1",
                    "reason": "test",
                    "policy_mode": "approval_required",
                    "requires_approval": True,
                    "provider_mutation": True,
                    "destructive": False,
                    "apply_supported": True,
                    "parameters": {"workersMin": 0},
                },
                {
                    "action_id": "remediate-delete",
                    "action": "delete_network_volume",
                    "provider": "runpod",
                    "resource_type": "network_volume",
                    "resource_id": "vol-unused",
                    "reason": "test",
                    "policy_mode": "approval_required",
                    "requires_approval": True,
                    "provider_mutation": True,
                    "destructive": True,
                    "apply_supported": False,
                    "apply_blocked_reasons": ["delete_network_volume is not supported by gpucall v2 apply"],
                },
            ],
        }
    )
    calls: list[tuple[str, str]] = []

    def fake_patch(endpoint_id: str, api_key: str):
        calls.append((endpoint_id, api_key))
        return {"id": endpoint_id, "workersMin": 0}

    monkeypatch.setattr("gpucall.panopticon_remediation._runpod_patch_endpoint_workers_min_to_zero", fake_patch)

    result = apply_remediation_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=False)

    assert calls == [("endpoint-1", "rk_test")]
    assert result.applied_count == 1
    assert result.skipped_count == 1
    assert result.results[0]["status"] == "applied"
    assert result.results[1]["status"] == "skipped"


def test_apply_runpod_scale_uses_official_patch_endpoint(monkeypatch) -> None:
    plan = PanopticonRemediationPlan.model_validate(
        {
            "schema_version": 1,
            "phase": "provider-panopticon-remediation-plan",
            "generated_at": "2026-05-22T00:00:00Z",
            "non_generation_probe_only": True,
            "status": "actions_proposed",
            "action_count": 1,
            "policy": {},
            "actions": [
                {
                    "action_id": "remediate-scale",
                    "action": "scale_workers_min_to_zero",
                    "provider": "runpod",
                    "resource_type": "endpoint",
                    "resource_id": "endpoint-1",
                    "reason": "test",
                    "policy_mode": "auto",
                    "requires_approval": False,
                    "provider_mutation": True,
                    "destructive": False,
                    "apply_supported": True,
                    "parameters": {"workersMin": 0},
                }
            ],
        }
    )
    calls: list[dict[str, object]] = []

    class Response:
        status_code = 200
        text = '{"id":"endpoint-1","workersMin":0}'

        def json(self) -> dict[str, object]:
            return {"id": "endpoint-1", "workersMin": 0}

    class Session:
        def patch(self, url: str, **kwargs: object) -> Response:
            calls.append({"url": url, **kwargs})
            return Response()

    monkeypatch.setattr("gpucall.panopticon_remediation.requests_session", lambda: Session())

    result = apply_remediation_plan(plan, credentials={"runpod": {"api_key": "rk_test"}}, dry_run=False)

    assert result.applied_count == 1
    assert calls == [
        {
            "url": "https://rest.runpod.io/v1/endpoints/endpoint-1",
            "headers": {"authorization": "Bearer rk_test", "content-type": "application/json", "accept": "application/json"},
            "json": {"workersMin": 0},
            "timeout": 30,
        }
    ]


def test_panopticon_plan_and_apply_cli_round_trip(tmp_path, monkeypatch, capsys) -> None:
    from gpucall.cli import main

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / "policy.yml").write_text(
        """
version: "2026-04-30"
inline_bytes_limit: 8192
default_lease_ttl_seconds: 300
max_lease_ttl_seconds: 3600
max_timeout_seconds: 1800
tuples:
  allow: []
  deny: []
panopticon_remediation:
  scale_workers_min_to_zero:
    mode: auto
""".strip()
        + "\n",
        encoding="utf-8",
    )
    snapshot_path = tmp_path / "provider-panopticon.json"
    store_panopticon_evidence(_sample_evidence(), snapshot_path)
    plan_path = tmp_path / "plan.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "panopticon",
            "plan",
            "--config-dir",
            str(config_dir),
            "--panopticon-path",
            str(snapshot_path),
            "--output-json",
            str(plan_path),
        ],
    )

    main()
    stdout_plan = json.loads(capsys.readouterr().out)

    assert stdout_plan["phase"] == "provider-panopticon-remediation-plan"
    assert plan_path.exists()
    assert any(action["action"] == "scale_workers_min_to_zero" for action in stdout_plan["actions"])

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "panopticon",
            "apply",
            str(plan_path),
        ],
    )
    main()
    apply_stdout = json.loads(capsys.readouterr().out)
    assert apply_stdout["dry_run"] is True
    assert {result["status"] for result in apply_stdout["results"]} == {"dry_run"}


def test_remediation_plan_round_trip_is_strict_json(tmp_path) -> None:
    snapshot_path = tmp_path / "provider-panopticon.json"
    store_panopticon_evidence(_sample_evidence(), snapshot_path)
    plan = build_remediation_plan(load_panopticon_evidence(snapshot_path), _policy())

    payload = json.loads(dumps_remediation_plan(plan))

    assert PanopticonRemediationPlan.model_validate(payload).action_count == len(plan.actions)


def _policy(remediation: dict[str, object] | None = None) -> Policy:
    payload = {
        "version": "2026-04-30",
        "inline_bytes_limit": 8192,
        "default_lease_ttl_seconds": 300,
        "max_lease_ttl_seconds": 3600,
        "max_timeout_seconds": 1800,
        "tuples": {"allow": [], "deny": []},
    }
    if remediation is not None:
        payload["panopticon_remediation"] = remediation
    return Policy.model_validate(payload)


def _sample_evidence(*, active_workers: int = 0, active_pods: int = 0) -> dict[str, dict[str, object]]:
    return {
        "runpod-vllm-vision": {
            "tuple": "runpod-vllm-vision",
            "adapter": "runpod-vllm-serverless",
            "status": "blocked",
            "checked": True,
            "findings": [
                {
                    "tuple": "runpod-vllm-vision",
                    "adapter": "runpod-vllm-serverless",
                    "dimension": "cost",
                    "severity": "error",
                    "field": "runpod_serverless_billing_guard",
                    "reason": "live RunPod Serverless endpoint has workersMin > 0 without explicit standing cost approval",
                    "source": "https://rest.runpod.io/v1/endpoints",
                    "raw": {
                        "endpoint_id": "endpoint-1",
                        "workers_min": 1,
                        "workers_max": 2,
                        "active_workers": active_workers,
                        "active_pods": active_pods,
                        "live_reason": "workers_min_positive",
                    },
                }
            ],
        },
        "runpod-network-volume-vol-unused": {
            "tuple": "runpod-network-volume-vol-unused",
            "adapter": "runpod",
            "status": "blocked",
            "checked": True,
            "findings": [
                {
                    "tuple": "runpod-network-volume-vol-unused",
                    "adapter": "runpod",
                    "dimension": "storage",
                    "severity": "error",
                    "field": "runpod_network_volume_inventory",
                    "reason": "RunPod network volume is present but not attached to live endpoint inventory or declared by a production tuple",
                    "source": "https://rest.runpod.io/v1/networkvolumes",
                    "raw": {
                        "provider": "runpod",
                        "resource_type": "network_volume",
                        "resource_id": "vol-unused",
                        "attached_endpoint_count": 0,
                        "declared_by_tuple_count": 0,
                        "content_inventory_status": "missing_runpod_s3_credentials",
                        "live_reason": "persistent_storage_unattached_undeclared",
                    },
                }
            ],
        },
    }
