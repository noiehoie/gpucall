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


def test_production_launch_report_blocks_without_live_requirements(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))

    report = build_launch_report(copy_config(tmp_path), profile="production")

    assert report["go"] is False
    checks = {blocker["check"] for blocker in report["blockers"]}
    assert "gateway_auth" in checks
    assert "gateway_live_smoke" in checks


def test_launch_report_blocks_smoke_provider_in_auto_recipe(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    root = copy_config(tmp_path)
    for path in (root / "providers").glob("*.yml"):
        provider = yaml.safe_load(path.read_text(encoding="utf-8"))
        provider["adapter"] = "echo"
        provider.pop("model", None)
        path.write_text(yaml.safe_dump(provider, sort_keys=False), encoding="utf-8")

    try:
        report = build_launch_report(root)
    except ConfigError as exc:
        assert "no provider satisfying" in str(exc)
        return

    assert report["go"] is False
    assert any(blocker["check"] == "routing_hygiene" for blocker in report["blockers"])
