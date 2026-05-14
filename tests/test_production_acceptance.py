from __future__ import annotations

import json

from gpucall.production_acceptance import run_production_acceptance


def test_production_acceptance_suite_passes() -> None:
    report = run_production_acceptance()

    assert report["phase"] == "production_acceptance"
    assert report["passed"] is True
    checks = {item["id"]: item for item in report["checks"]}
    assert {"F2/F6", "F4", "F5", "F7", "F9", "F10"} <= set(checks)
    assert checks["F4"]["details"]["bad_tuple_started_count"] == 1
    assert checks["F7"]["details"]["accepted_n"] == 2
    assert checks["F9"]["details"]["statuses"][0]["status"] == "committed"
    assert checks["F9"]["details"]["statuses"][1]["status"] == "released"


def test_production_acceptance_cli_outputs_json(capsys) -> None:
    from gpucall.cli import production_acceptance_command

    production_acceptance_command(None)

    payload = json.loads(capsys.readouterr().out)
    assert payload["passed"] is True
