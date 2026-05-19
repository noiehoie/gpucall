import json
import os
from pathlib import Path
from gpucall.migrate import main
import gpucall.migrate as migrate_module

def _write_log(tmp_path, name, content):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path

def test_onboard_proceeds_with_unobserved_inventory_workload(tmp_path, monkeypatch):
    project = tmp_path / "project"
    output = tmp_path / "out"
    project.mkdir()
    # Detect two workloads
    (project / "engine.py").write_text("call_llm('rank topics')\ncall_llm('summarize text')\n", encoding="utf-8")
    
    # Baseline only contains one of them
    baseline = _write_log(tmp_path, "baseline.log", "response_len=1000\nsource_count=5\nAnalysis complete: 5 topics ranked\n")
    
    monkeypatch.setenv("GPUCALL_MIGRATION_READINESS_GATE", "1")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    # Mock readiness check for the observed one
    def fake_readiness_check(**kwargs):
        if kwargs.get("intent") == "rank_text_items":
            return {
                "task": "infer",
                "intent": "rank_text_items",
                "ok": True,
                "recipe": "recipe-id",
                "live_ready_tuple_count": 1,
            }
        return {"task": kwargs.get("task"), "intent": kwargs.get("intent"), "ok": False, "reason": "no_production_ready_route"}

    monkeypatch.setattr(migrate_module, "_gateway_readiness_check", fake_readiness_check)

    # Should exit with 0 (ready for canary) or 2 (if it still defers for some reason, but we want it to NOT fail due to unobserved)
    # Actually main returns 0 if everything is ok.
    exit_code = main(["onboard", str(project), "--source", "test", "--output-dir", str(output), "--log-file", str(baseline), "--yes"])
    
    report = json.loads((output / "onboard-report.json").read_text(encoding="utf-8"))
    
    # The report should show gateway_readiness as ok
    assert report["gateway_readiness"]["ok"] is True
    assert len(report["gateway_readiness"]["checks"]) == 2
    
    # Check that summarize_text was reported as unobserved
    unobserved = [c for c in report["gateway_readiness"]["checks"] if c["intent"] == "summarize_text"][0]
    assert unobserved["reason"] == "inventory_only_unobserved_workload"
    assert unobserved["ok"] is True
    
    # Check that rank_text_items was checked against gateway
    observed = [c for c in report["gateway_readiness"]["checks"] if c["intent"] == "rank_text_items"][0]
    assert observed["ok"] is True
    assert observed["recipe"] == "recipe-id"

def test_onboard_fails_closed_when_observed_workload_has_failed_trace(tmp_path, monkeypatch):
    project = tmp_path / "project"
    output = tmp_path / "out"
    project.mkdir()
    (project / "engine.py").write_text("call_llm('rank topics')\n", encoding="utf-8")
    
    # Baseline contains a failure (nonzero return code)
    # We mock trace_project to return a failed trace
    from gpucall.workload_contract import TRACE_SCHEMA_VERSION
    def fake_trace_project(*args, **kwargs):
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "phase": "workload-trace",
            "returncode": 1,
            "metrics": {"response_chars": 100}, # Observed but failed
            "workload_metrics": {},
            "workload_hints": ["rank_text_items"],
        }
    
    monkeypatch.setattr(migrate_module, "trace_project", fake_trace_project)
    
    baseline = _write_log(tmp_path, "baseline.log", "some content")
    
    monkeypatch.setenv("GPUCALL_MIGRATION_READINESS_GATE", "1")
    monkeypatch.setenv("GPUCALL_BASE_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("GPUCALL_API_KEY", "x")

    exit_code = main(["onboard", str(project), "--source", "test", "--output-dir", str(output), "--log-file", str(baseline), "--yes"])
    
    assert exit_code != 0
    report = json.loads((output / "onboard-report.json").read_text(encoding="utf-8"))
    assert report["gateway_readiness"]["ok"] is False
    check = report["gateway_readiness"]["checks"][0]
    assert check["reason"] == "recipe_draft_not_materializable"
    assert any("non-zero exit code" in b for b in check["blockers"])
