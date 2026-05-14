from __future__ import annotations

import json

from gpucall.production_acceptance import run_production_acceptance


def test_production_acceptance_suite_passes() -> None:
    report = run_production_acceptance()

    assert report["phase"] == "production_acceptance"
    assert report["passed"] is True
    checks = {item["id"]: item for item in report["checks"]}
    assert {"F1", "F2/F6", "F3", "F4", "F5", "F7", "F8", "F9", "F10", "F12", "F13", "F14"} <= set(checks)
    assert checks["F1"]["details"]["transform"] == "semantic_to_worker_wire_contract"
    assert checks["F3"]["details"]["allowed"] == {"allowed": True, "missing": []}
    assert checks["F4"]["details"]["bad_tuple_started_count"] == 1
    assert checks["F4"]["details"]["suppressed_provider_families"] == {}
    assert checks["F7"]["details"]["accepted_n"] == 2
    assert checks["F7"]["details"]["parallel_tool_calls_fail_closed"] is True
    assert checks["F8"]["details"]["boundary"]["valid"] is True
    assert checks["F9"]["details"]["statuses"][0]["status"] == "committed"
    assert checks["F9"]["details"]["statuses"][1]["status"] == "released"
    assert checks["F12"]["details"]["states"][0]["provider"] == "exhausted"
    assert "document_vision_burst" in checks["F13"]["details"]["classes"]
    assert checks["F14"]["details"]["body_included"] is False


def test_production_acceptance_cli_outputs_json(capsys) -> None:
    from gpucall.cli import production_acceptance_command

    production_acceptance_command(None)

    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
