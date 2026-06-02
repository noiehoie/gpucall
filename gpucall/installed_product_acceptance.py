from __future__ import annotations

import json
import os
import tempfile
import warnings
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import yaml

from gpucall.app import create_app
from gpucall.blocker_taxonomy import typed_intake_blocker
from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.cli_commands.setup import apply_setup_plan
from gpucall.domain import RecipeAdminAutomationConfig
from gpucall.handoff_package import human_readme_quality_blockers, prompt_quality_blockers, write_handoff_package
from gpucall.panopticon import store_panopticon_evidence
from gpucall.recipe_admin import _auto_promotion_report
from gpucall.shipment_gap import (
    ENDPOINT_STALE,
    PRICE_UNKNOWN,
    PROVIDER_MISSING,
    SHIPMENT_READY,
    SUPPLY_PROVISIONING_REQUIRED,
    VALIDATION_MISSING,
    classify_workload_demand,
)


EXPECTED_PHASE_C_CATEGORIES = {
    SHIPMENT_READY,
    VALIDATION_MISSING,
    PRICE_UNKNOWN,
    PROVIDER_MISSING,
    ENDPOINT_STALE,
    SUPPLY_PROVISIONING_REQUIRED,
}


def run_installed_product_acceptance(root: str | Path | None = None) -> dict[str, Any]:
    if root is None:
        with tempfile.TemporaryDirectory(prefix="gpucall-installed-acceptance-") as raw_root:
            return _run_installed_product_acceptance(Path(raw_root))
    return _run_installed_product_acceptance(Path(root))


def dumps_installed_product_acceptance(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


@contextmanager
def _quiet_test_client(app: Any) -> Iterator[Any]:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r"Using `httpx` with `starlette\.testclient` is deprecated.*",
            category=Warning,
        )
        from fastapi.testclient import TestClient

        with TestClient(app) as client:
            yield client


def _run_installed_product_acceptance(root: Path) -> dict[str, Any]:
    root = root.resolve()
    operator_root = root / "operator"
    caller_root = root / "caller" / "example-caller"
    xdg_config = root / "xdg" / "config"
    xdg_state = root / "xdg" / "state"
    xdg_cache = root / "xdg" / "cache"
    credentials = root / "credentials.yml"
    credentials.parent.mkdir(parents=True, exist_ok=True)
    credentials.write_text("version: 1\nproviders: {}\n", encoding="utf-8")
    credentials.chmod(0o600)
    with _temporary_env(
        XDG_CONFIG_HOME=str(xdg_config),
        XDG_STATE_HOME=str(xdg_state),
        XDG_CACHE_HOME=str(xdg_cache),
        GPUCALL_CREDENTIALS=str(credentials),
        GPUCALL_ALLOW_UNAUTHENTICATED="1",
    ):
        phase_a = _phase_a_router_bringup(operator_root=operator_root, xdg_config=xdg_config, xdg_state=xdg_state)
        phase_b = _phase_b_caller_handoff(caller_root=caller_root, handoff_dir=Path(phase_a["handoff_package"]["output_dir"]))
        phase_c = _phase_c_reconciliation()
        phase_d = _phase_d_admin_demand_to_supply_loop(operator_root=operator_root, config_dir=Path(phase_a["config_dir"]))
    passed = bool(phase_a["go"] and phase_b["go"] and phase_c["go"] and phase_d["go"])
    return {
        "schema_version": 1,
        "phase": "installed-product-acceptance",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "passed": passed,
        "production_traffic_go": False,
        "production_traffic_status": "NO-GO",
        "provider_mutation_performed": False,
        "generation_performed": False,
        "phases": {
            "A_router_side_one_shot_bringup": phase_a,
            "B_caller_side_acceptance_from_handoff": phase_b,
            "C_demand_supply_reconciliation": phase_c,
            "D_admin_demand_to_supply_loop": phase_d,
        },
    }


def _phase_a_router_bringup(*, operator_root: Path, xdg_config: Path, xdg_state: Path) -> dict[str, Any]:
    from gpucall.cli import build_launch_report

    config_dir = xdg_config / "gpucall"
    recipe_inbox = xdg_state / "gpucall" / "recipe_requests" / "inbox"
    plan_path = operator_root / "gpucall.setup.yml"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        f"""
setup_schema_version: 1
profile: internal-team
gateway:
  base_url: http://127.0.0.1:18088
  caller_auth:
    mode: generated_gateway_key
tenant_onboarding:
  mode: trusted_bootstrap
  allowed_cidrs:
    - 127.0.0.1/32
  recipe_inbox: {recipe_inbox}
recipe_automation:
  auto_materialize: true
handoff_assets:
  caller_sdk_wheel_url: https://assets.example/sdk/gpucall_sdk-2.0.38-py3-none-any.whl
external_systems:
  - name: example-caller
    expected_workloads: [infer, vision]
launch:
  run_static_check: true
""".lstrip(),
        encoding="utf-8",
    )
    setup_report = apply_setup_plan(config_dir, plan_path, dry_run=False, yes=True)
    quality_inbox = xdg_state / "gpucall" / "quality_feedback" / "inbox"
    panopticon_path = xdg_state / "gpucall" / "catalog" / "provider-panopticon.json"
    store_panopticon_evidence({}, panopticon_path)
    with _quiet_test_client(create_app(config_dir)) as client:
        readyz = client.get("/readyz")
    launch_report = build_launch_report(config_dir, profile="static")
    handoff_write = write_handoff_package(config_dir, "example-caller", operator_root / "handoff" / "example-caller")
    readyz_body = _safe_response_json(readyz)
    checks = {
        "setup_apply_completed": "Applied setup plan." in setup_report,
        "xdg_config_used": str(config_dir).startswith(str(xdg_config)),
        "xdg_state_used": recipe_inbox.exists() and str(recipe_inbox).startswith(str(xdg_state)),
        "xdg_cache_declared": bool(os.getenv("XDG_CACHE_HOME")),
        "recipe_inbox_created": recipe_inbox.is_dir(),
        "quality_inbox_created": quality_inbox.is_dir(),
        "panopticon_snapshot_initialized": panopticon_path.exists(),
        "gateway_readyz_ok": readyz.status_code == 200 and readyz_body.get("status") in {"ok", "ready"},
        "launch_check_explains_next_blocker": isinstance(launch_report.get("blockers"), list),
        "handoff_package_generated": Path(handoff_write["output_dir"]).joinpath("caller-ai-onboarding-prompt.md").exists(),
        "caller_engineer_readme_generated": Path(handoff_write["output_dir"]).joinpath("CALLER_ENGINEER_README.md").exists(),
    }
    return {
        "go": all(checks.values()),
        "config_dir": str(config_dir),
        "recipe_inbox": str(recipe_inbox),
        "quality_inbox": str(quality_inbox),
        "panopticon_path": str(panopticon_path),
        "readyz": {"status_code": readyz.status_code, "body": readyz_body},
        "launch_check": {
            "go": launch_report.get("go"),
            "blocker_count": len(launch_report.get("blockers") or []),
            "first_blocker": (launch_report.get("blockers") or [None])[0],
        },
        "handoff_package": handoff_write,
        "checks": checks,
    }


def _phase_b_caller_handoff(*, caller_root: Path, handoff_dir: Path) -> dict[str, Any]:
    caller_root.mkdir(parents=True, exist_ok=True)
    before_siblings = sorted(path.name for path in caller_root.parent.iterdir())
    handoff = json.loads((handoff_dir / "gpucall-handoff.json").read_text(encoding="utf-8"))
    prompt = (handoff_dir / "caller-ai-onboarding-prompt.md").read_text(encoding="utf-8")
    readme = (handoff_dir / "CALLER_ENGINEER_README.md").read_text(encoding="utf-8")
    prompt_blockers = prompt_quality_blockers(prompt, handoff)
    readme_blockers = human_readme_quality_blockers(readme, handoff)
    gateway_repo_root = Path(__file__).resolve().parents[1]
    (caller_root / "app.py").write_text("def main():\n    return 'caller baseline fixture'\n", encoding="utf-8")
    migration_dir = caller_root / ".gpucall-migration"
    migration_dir.mkdir()
    contract_path = migration_dir / "workload-contract.json"
    contract_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "phase": "workload-contract",
                "source": "example-caller",
                "workloads": [
                    {
                        "id": "infer.rank_text_items",
                        "task": "infer",
                        "intent": "rank_text_items",
                        "modes": ["sync"],
                        "input_profile": {"context_budget_tokens": 32768},
                        "output_profile": {"output_contract": "json_object"},
                        "quality_contract": {"metrics": {"min_topics": 3}, "gateway_may_infer_quality": False},
                    }
                ],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    after_siblings = sorted(path.name for path in caller_root.parent.iterdir())
    checks = {
        "handoff_readable": handoff["phase"] == "gpucall-caller-handoff",
        "prompt_quality_go": not prompt_blockers,
        "human_readme_quality_go": not readme_blockers,
        "human_readme_explains_responsibility_boundary": "Responsibility Boundary" in readme,
        "human_readme_explains_go_no_go": "Go / No-Go Rule" in readme,
        "sdk_wheel_url_present": bool(handoff["assets"]["sdk_wheel_url"]),
        "recipe_inbox_present": bool(handoff["inboxes"]["recipe"]),
        "quality_inbox_present": bool(handoff["inboxes"]["quality_feedback"]),
        "gateway_repo_not_referenced_as_workspace": str(gateway_repo_root) not in prompt and str(gateway_repo_root) not in readme,
        "caller_contract_created_inside_caller_repo": str(contract_path).startswith(str(caller_root)),
        "no_extra_caller_sibling_sandbox": before_siblings == ["example-caller"] and after_siblings == ["example-caller"],
    }
    return {
        "go": all(checks.values()),
        "caller_root": str(caller_root),
        "handoff_dir": str(handoff_dir),
        "workload_contract": str(contract_path),
        "prompt_quality_blockers": prompt_blockers,
        "human_readme_quality_blockers": readme_blockers,
        "checks": checks,
    }


def _safe_response_json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {"_non_json_body": response.text[:8192]}
    if isinstance(body, dict):
        return body
    return {"_json_body": body}


def _phase_c_reconciliation() -> dict[str, Any]:
    results = {
        "ready": classify_workload_demand(_workload("ready"), _readiness(_recipe("ready", eligible_tuples=[_ready_row()], live_ready_tuples=[_ready_row()]))),
        "validation_missing": classify_workload_demand(_workload("validation_missing"), _readiness(_recipe("validation_missing", eligible_tuples=[_validation_missing_row()], live_ready_tuples=[]))),
        "price_unknown": classify_workload_demand(_workload("price_unknown"), _readiness(_recipe("price_unknown", eligible_tuples=[_price_unknown_row()], live_ready_tuples=[_price_unknown_row()]))),
        "provider_missing": classify_workload_demand(_workload("provider_missing"), _readiness(_recipe("provider_missing", eligible_tuples=[], live_ready_tuples=[]))),
        "endpoint_stale": classify_workload_demand(_workload("endpoint_stale"), _readiness(_recipe("endpoint_stale", eligible_tuples=[_stale_row()], live_ready_tuples=[]))),
        "supply_provisioning_required": classify_workload_demand(_workload("supply"), _readiness(_recipe("supply", eligible_tuples=[_supply_row()], live_ready_tuples=[]))),
    }
    categories = {str(result["category"]) for result in results.values()}
    caller_blocker = typed_intake_blocker("baseline metrics must not be empty")
    checks = {
        "expected_categories_present": EXPECTED_PHASE_C_CATEGORIES.issubset(categories),
        "caller_contract_incomplete_classified": caller_blocker["code"] == "CALLER_QUALITY_BASELINE_MISSING",
        "blockers_have_owners": all(
            blocker.get("owner") and blocker.get("handoff")
            for result in results.values()
            for blocker in result.get("blockers", [])
        ),
    }
    return {
        "go": all(checks.values()),
        "categories": sorted(categories),
        "caller_contract_incomplete": caller_blocker,
        "results": results,
        "checks": checks,
    }


def _phase_d_admin_demand_to_supply_loop(*, operator_root: Path, config_dir: Path) -> dict[str, Any]:
    report_dir = operator_root / "demand-to-supply-reports"
    inbox_dir = operator_root / "demand-to-supply-inbox"
    report_dir.mkdir(parents=True, exist_ok=True)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    tuple_name = "runpod-vllm-ampere48-qwen2-5-vl-7b-instruct"
    candidate = next(row for row in load_tuple_candidate_payloads(config_dir) if row["name"] == tuple_name)
    recipe = yaml.safe_load((config_dir / "recipes" / "vision-image-standard.yml").read_text(encoding="utf-8")) or {}
    recipe["name"] = "vision-understand-document-image-draft"
    recipe["auto_select"] = False
    recipe["quality_floor"] = "draft"
    automation = RecipeAdminAutomationConfig(
        recipe_inbox_auto_materialize=True,
        recipe_inbox_auto_promote_candidates=True,
        recipe_inbox_auto_provision_supply=True,
        recipe_inbox_auto_apply_supply=False,
        recipe_inbox_auto_billable_validation=False,
        recipe_inbox_auto_activate_validated=False,
    )
    promotion = _auto_promotion_report(
        {
            "canonical_recipe": recipe,
            "tuple_candidate_matches": [{"name": tuple_name, "path": candidate["_path"]}],
        },
        request_id="rr-installed-product-loop",
        automation=automation,
        inbox_dir=inbox_dir,
        report_dir=report_dir,
        config_dir=config_dir,
        validation_dir=operator_root / "tuple-validation",
        force=True,
    )
    supply = promotion.get("supply_provisioning") or {}
    workflow = promotion.get("post_supply_workflow") or {}
    checks = {
        "candidate_promotion_prepared": promotion.get("decision") == "READY_FOR_ENDPOINT_CONFIGURATION",
        "provider_supply_plan_written": Path(str(supply.get("plan_path") or "")).exists(),
        "provider_supply_apply_is_dry_run": supply.get("apply_dry_run") is True,
        "provider_mutation_not_performed": supply.get("provider_mutation_enabled") is False,
        "billable_generation_not_performed": workflow.get("billable_generation_allowed") is False,
        "workflow_waits_for_provider_apply": workflow.get("decision") == "WAITING_FOR_PROVIDER_SUPPLY_APPLY",
        "machine_readable_next_action": bool(workflow.get("next_actions")),
    }
    return {
        "go": all(checks.values()),
        "tuple": tuple_name,
        "promotion_decision": promotion.get("decision"),
        "supply_decision": supply.get("decision"),
        "post_supply_decision": workflow.get("decision"),
        "promotion_report_path": promotion.get("auto_promotion_report_path"),
        "supply_plan_path": supply.get("plan_path"),
        "supply_apply_path": supply.get("apply_path"),
        "post_supply_workflow_report_path": workflow.get("post_supply_workflow_report_path"),
        "checks": checks,
    }


def _workload(intent: str) -> dict[str, Any]:
    return {
        "id": f"infer.{intent}",
        "task": "infer",
        "intent": intent,
        "modes": ["sync"],
        "input_profile": {"context_budget_tokens": 32768},
    }


def _readiness(recipe: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": 1, "phase": "readiness", "recipes": [recipe]}


def _recipe(intent: str, **overrides: Any) -> dict[str, Any]:
    row = {
        "recipe": f"infer-{intent}",
        "task": "infer",
        "intent": intent,
        "auto_select": True,
        "production_activated": True,
        "allowed_modes": ["sync"],
        "context_budget_tokens": 32768,
        "eligible_tuples": [],
        "live_ready_tuples": [],
    }
    row.update(overrides)
    return row


def _ready_row() -> dict[str, Any]:
    return {
        "tuple": "runpod-ready",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_validation_artifact": "/state/tuple-validation/ok.json",
    }


def _validation_missing_row() -> dict[str, Any]:
    return {
        "tuple": "runpod-validation-missing",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "missing_route_validation_evidence",
    }


def _price_unknown_row() -> dict[str, Any]:
    return {
        "tuple": "runpod-price-unknown",
        "mode": "sync",
        "price_freshness": "unknown",
        "route_validation_required": True,
        "live_validation_artifact": "/state/tuple-validation/ok.json",
    }


def _stale_row() -> dict[str, Any]:
    return {
        "tuple": "runpod-stale",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "endpoint_missing_from_inventory",
        "live_catalog_findings": [{"reason": "configured endpoint not present", "status_code": 404}],
    }


def _supply_row() -> dict[str, Any]:
    return {
        "tuple": "runpod-needs-supply",
        "target": "RUNPOD_ENDPOINT_ID_PLACEHOLDER",
        "mode": "sync",
        "price_freshness": "fresh",
        "route_validation_required": True,
        "live_reason": "RunPod endpoint target is not configured",
    }


@contextmanager
def _temporary_env(**updates: str) -> Iterator[None]:
    old = {key: os.environ.get(key) for key in updates}
    os.environ.update(updates)
    try:
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
