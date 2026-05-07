from __future__ import annotations

from pathlib import Path

import yaml

from gpucall.cli import build_launch_report
from gpucall.config import ConfigError


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    for path in source.rglob("*.yml"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def test_launch_report_is_go_for_sample_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    report = build_launch_report(copy_config(tmp_path), profile="static")

    assert report["go"] is True
    assert report["blockers"] == []
    checks = report["checks"]
    assert checks["config_valid"] is True
    assert checks["secret_scan_ok"] is True
    assert "/v2/tasks/sync" in checks["openapi_paths"]
    assert checks["mvp_scope"]["tasks"] == ["infer", "vision"]
    assert checks["cost_audit"]["providers"]
    assert all(row["metadata_complete"] for row in checks["cost_audit"]["providers"])
    assert checks["cleanup_audit"]["ok"] is True


def test_launch_report_blocks_missing_cost_metadata(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    provider_path = root / "surfaces" / "modal-a10g.yml"
    provider = yaml.safe_load(provider_path.read_text(encoding="utf-8"))
    provider.pop("scaledown_window_seconds", None)
    provider_path.write_text(yaml.safe_dump(provider, sort_keys=False), encoding="utf-8")

    report = build_launch_report(root, profile="static")

    assert report["go"] is False
    assert any(blocker["check"] == "cost_metadata" for blocker in report["blockers"])


def test_launch_report_blocks_active_cleanup_lease(tmp_path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(state))
    lease = state / "hyperstack_leases.jsonl"
    lease.parent.mkdir(parents=True)
    lease.write_text(
        '{"event":"provision.created","vm_id":"vm-1","expires_at":"2000-01-01T00:00:00+00:00"}\n',
        encoding="utf-8",
    )

    report = build_launch_report(copy_config(tmp_path), profile="static")

    assert report["go"] is False
    assert any(blocker["check"] == "cleanup_audit" for blocker in report["blockers"])


def test_production_launch_report_blocks_without_live_requirements(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    report = build_launch_report(copy_config(tmp_path), profile="production")

    assert report["go"] is False
    checks = {blocker["check"] for blocker in report["blockers"]}
    assert "gateway_auth" in checks
    assert "gateway_live_smoke" in checks
    assert "provider_live_validation" in checks
    assert "provider_live_cost_audit" in checks
    assert report["checks"]["cost_audit_live_ok"] is False
    assert report["checks"]["cost_audit_live_findings"]
    provider_live_validation = report["provider_live_validation"]
    assert provider_live_validation["missing_tuples"] == provider_live_validation["required_tuples"]
    labels = {row["label"] for row in provider_live_validation["required_tuples"]}
    assert "function_runtime:modal-function:qwen2.5-1.5b-instruct:modal-vllm" in labels
    assert "managed_endpoint:openai-chat-completions:qwen2.5-1.5b-instruct:runpod-vllm-openai" in labels
    assert "iaas_vm:hyperstack-vm:qwen2.5-7b-instruct-1m:hyperstack-vllm" in labels


def test_launch_report_blocks_smoke_provider_in_auto_recipe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    for path in (root / "surfaces").glob("*.yml"):
        provider = yaml.safe_load(path.read_text(encoding="utf-8"))
        provider["adapter"] = "echo"
        path.write_text(yaml.safe_dump(provider, sort_keys=False), encoding="utf-8")
    for path in (root / "workers").glob("*.yml"):
        worker = yaml.safe_load(path.read_text(encoding="utf-8"))
        worker["adapter"] = "echo"
        worker.pop("model", None)
        path.write_text(yaml.safe_dump(worker, sort_keys=False), encoding="utf-8")

    try:
        report = build_launch_report(root)
    except ConfigError as exc:
        assert "no provider satisfying" in str(exc)
        return

    assert report["go"] is False
    assert any(blocker["check"] == "routing_hygiene" for blocker in report["blockers"])
