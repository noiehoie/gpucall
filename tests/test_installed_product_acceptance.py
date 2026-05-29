from __future__ import annotations

import json

from gpucall.installed_product_acceptance import run_installed_product_acceptance


def test_installed_product_acceptance_connects_router_handoff_and_reconciliation(tmp_path) -> None:
    report = run_installed_product_acceptance(tmp_path / "ipa")

    assert report["phase"] == "installed-product-acceptance"
    assert report["passed"] is True
    assert report["provider_mutation_performed"] is False
    assert report["generation_performed"] is False

    phase_a = report["phases"]["A_router_side_one_shot_bringup"]
    assert phase_a["go"] is True
    assert phase_a["checks"]["recipe_inbox_created"] is True
    assert phase_a["checks"]["quality_inbox_created"] is True
    assert phase_a["checks"]["panopticon_snapshot_initialized"] is True
    assert phase_a["checks"]["gateway_readyz_ok"] is True
    assert phase_a["checks"]["handoff_package_generated"] is True
    assert phase_a["checks"]["caller_engineer_readme_generated"] is True

    phase_b = report["phases"]["B_caller_side_acceptance_from_handoff"]
    assert phase_b["go"] is True
    assert phase_b["checks"]["prompt_quality_go"] is True
    assert phase_b["checks"]["human_readme_quality_go"] is True
    assert phase_b["checks"]["human_readme_explains_responsibility_boundary"] is True
    assert phase_b["checks"]["human_readme_explains_go_no_go"] is True
    assert phase_b["checks"]["gateway_repo_not_referenced_as_workspace"] is True
    assert phase_b["checks"]["no_extra_caller_sibling_sandbox"] is True

    phase_c = report["phases"]["C_demand_supply_reconciliation"]
    assert phase_c["go"] is True
    assert set(phase_c["categories"]) >= {
        "shipment_ready",
        "validation_missing",
        "price_unknown",
        "provider_missing",
        "endpoint_stale",
        "supply_provisioning_required",
    }
    assert phase_c["caller_contract_incomplete"]["handoff"] == "caller-c-kit"

    phase_d = report["phases"]["D_admin_demand_to_supply_loop"]
    assert phase_d["go"] is True
    assert phase_d["checks"]["candidate_promotion_prepared"] is True
    assert phase_d["checks"]["provider_supply_plan_written"] is True
    assert phase_d["checks"]["provider_supply_apply_is_dry_run"] is True
    assert phase_d["checks"]["provider_mutation_not_performed"] is True
    assert phase_d["checks"]["billable_generation_not_performed"] is True
    assert phase_d["checks"]["workflow_waits_for_provider_apply"] is True


def test_installed_product_acceptance_cli_outputs_json(tmp_path, monkeypatch, capsys) -> None:
    from gpucall.cli import main
    import sys

    output = tmp_path / "ipa.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "gpucall",
            "installed-product-acceptance",
            "--root",
            str(tmp_path / "ipa"),
            "--output",
            str(output),
        ],
    )

    main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)

    assert payload["passed"] is True
    assert "DeprecationWarning" not in captured.err
    assert json.loads(output.read_text(encoding="utf-8"))["passed"] is True
