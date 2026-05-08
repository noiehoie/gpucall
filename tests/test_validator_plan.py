from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gpucall.config import load_config
from gpucall.execution_catalog import build_resource_catalog_snapshot
from gpucall.validator_plan import build_validator_plan


def test_validator_plan_is_budgeted_and_active_tuple_scoped_by_default() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))
    plan = build_validator_plan(snapshot, budget_usd=0.001, max_items=10)

    assert plan.budget_usd == 0.001
    assert plan.selected_estimated_cost_usd <= plan.budget_usd
    assert all(item.source == "active_tuple" for item in plan.queue)
    assert all(item.selected for item in plan.queue)
    assert any(item.reason == "missing_validation_evidence" for item in plan.queue + plan.skipped)
    assert any(item.skip_reason == "validation_budget_exhausted" for item in plan.skipped)


def test_validator_plan_can_include_candidate_queue_without_selecting_unconfigured_endpoints() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))
    plan = build_validator_plan(snapshot, budget_usd=1.0, include_candidates=True)

    candidate_skips = [item for item in plan.skipped if item.source == "tuple_candidate"]
    assert candidate_skips
    assert any(item.skip_reason == "candidate_missing_endpoint_or_target" for item in candidate_skips)


def test_validator_plan_cli_outputs_json() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "validator-plan",
            "--config-dir",
            "config",
            "--budget-usd",
            "0.001",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["plan_schema_version"] == 1
    assert payload["budget_usd"] == 0.001
    assert payload["selected_estimated_cost_usd"] <= 0.001
