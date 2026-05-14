from __future__ import annotations

import json
from pathlib import Path

from gpucall.cli import launch_check_command


def test_launch_check_summary_default(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    report = {
        "go": True,
        "config_dir": str(config_dir),
        "state_dir": str(tmp_path / "state"),
        "checks": {"launch_profile": "production", "launch_gates": {"config_valid": True}},
        "tuple_live_validation": {"required_tuples": [], "missing_tuples": [], "gateway_live_tuples": []},
        "blockers": [],
    }
    monkeypatch.setattr("gpucall.cli.build_launch_report", lambda *args, **kwargs: report)
    monkeypatch.setattr("gpucall.cli.default_state_dir", lambda: tmp_path / "state")

    launch_check_command(config_dir, profile="production", print_json=False)

    output = capsys.readouterr().out
    assert "gpucall launch-check: GO" in output
    assert "profile: production" in output
    assert "details_json:" in output
    assert '"checks":' not in output
    assert json.loads((tmp_path / "state" / "launch" / "launch-check.json").read_text(encoding="utf-8"))["go"] is True


def test_launch_check_json_flag(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    report = {
        "go": True,
        "config_dir": str(config_dir),
        "state_dir": str(tmp_path / "state"),
        "checks": {"launch_profile": "production"},
        "blockers": [],
    }
    monkeypatch.setattr("gpucall.cli.build_launch_report", lambda *args, **kwargs: report)
    monkeypatch.setattr("gpucall.cli.default_state_dir", lambda: tmp_path / "state")

    launch_check_command(config_dir, profile="production", print_json=True)

    output = capsys.readouterr().out
    parsed = json.loads(output)
    assert parsed["go"] is True
    assert parsed["checks"]["launch_profile"] == "production"
    assert parsed["report_path"] == str(tmp_path / "state" / "launch" / "launch-check.json")


def test_launch_check_output_json_keeps_summary_stdout(tmp_path, monkeypatch, capsys):
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    report = {
        "go": False,
        "config_dir": str(config_dir),
        "state_dir": str(tmp_path / "state"),
        "checks": {"launch_profile": "production"},
        "blockers": [{"check": "gateway_live_smoke", "url": None}],
    }
    monkeypatch.setattr("gpucall.cli.build_launch_report", lambda *args, **kwargs: report)
    monkeypatch.setattr("gpucall.cli.default_state_dir", lambda: tmp_path / "state")
    output_json = tmp_path / "reports" / "launch-check.json"

    launch_check_command(config_dir, profile="production", print_json=False, output_json=output_json)

    output = capsys.readouterr().out
    assert "gpucall launch-check: NO-GO" in output
    assert f"details_json: {output_json}" in output
    assert "- gateway_live_smoke" in output
    assert json.loads(output_json.read_text(encoding="utf-8"))["blockers"][0]["check"] == "gateway_live_smoke"
