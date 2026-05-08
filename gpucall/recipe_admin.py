from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.compiler import GovernanceCompiler
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_admin_automation, load_config
from gpucall.domain import ExecutionMode, ExecutionTupleSpec, Recipe, RecipeAdminAutomationConfig, TaskRequest, recipe_requirements
from gpucall.execution.contracts import artifact_tuple_evidence_key, tuple_evidence_key
from gpucall.execution.registry import adapter_descriptor
from gpucall.registry import ObservedRegistry
from gpucall.recipe_intents import capabilities_for
from gpucall.recipe_materialize import canonical_recipe_from_artifact, materialization_report, to_yaml, write_recipe_yaml
from gpucall.tuple_promotion import promote_candidate, promote_production_tuple
from gpucall.routing import tuple_route_rejection_reason


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpucall-recipe-admin")
    subcommands = parser.add_subparsers(dest="command", required=True)

    materialize = subcommands.add_parser("materialize", help="materialize caller intake/draft into canonical gpucall recipe YAML")
    materialize.add_argument("--input", "-i", required=True, help="path to caller intake/draft JSON, or '-' for stdin")
    materialize.add_argument("--output-dir", help="directory to write recipe YAML")
    materialize.add_argument("--report", help="write materialization report JSON")
    materialize.add_argument("--accept-all", action="store_true", help="explicitly accept caller artifact into a recipe intent candidate")
    materialize.add_argument("--force", action="store_true", help="overwrite existing recipe YAML")
    materialize.add_argument("--dry-run", action="store_true", help="print YAML without writing files")

    review = subcommands.add_parser("review", help="review a submitted recipe request against config, tuples, policy, and live evidence")
    review.add_argument("--input", "-i", required=True, help="path to caller submission/intake/draft JSON, or '-' for stdin")
    review.add_argument("--config-dir", help="gpucall config directory to review against")
    review.add_argument("--validation-dir", help="tuple live validation artifact directory")
    review.add_argument("--output", "-o", help="write review report JSON")

    promote = subcommands.add_parser("promote", help="prepare, validate, and optionally activate a production tuple from a review report")
    promote.add_argument("--review", required=True, help="admin review JSON produced by gpucall-recipe-admin review")
    promote.add_argument("--candidate", help="tuple candidate name; defaults to the first tuple_candidate_matches entry")
    promote.add_argument("--config-dir", required=True, help="active gpucall config directory")
    promote.add_argument("--work-dir", required=True, help="promotion workspace for generated config and reports")
    promote.add_argument("--validation-dir", help="tuple live validation artifact directory")
    promote.add_argument("--run-validation", action="store_true", help="run billable gpucall tuple-smoke in the promotion workspace")
    promote.add_argument("--activate", action="store_true", help="copy validated recipe/tuple into the active config directory")
    promote.add_argument("--force", action="store_true", help="overwrite generated or active recipe/tuple files")
    promote.add_argument("--output", "-o", help="write promotion report JSON")

    process = subcommands.add_parser("process-inbox", help="process file-based recipe request submissions once")
    process.add_argument("--inbox-dir", required=True)
    process.add_argument("--output-dir", required=True)
    process.add_argument("--processed-dir")
    process.add_argument("--failed-dir")
    process.add_argument("--report-dir")
    process.add_argument("--accept-all", action="store_true")
    process.add_argument("--force", action="store_true")
    process.add_argument("--config-dir", help="gpucall config directory to review against")
    process.add_argument("--validation-dir", help="tuple live validation artifact directory")

    status = subcommands.add_parser("status", help="show status for a submitted recipe request id")
    status.add_argument("--request-id", required=True)
    status.add_argument("--inbox-dir", required=True)

    watch = subcommands.add_parser("watch", help="poll a file-based recipe request inbox and materialize submissions")
    watch.add_argument("--inbox-dir", required=True)
    watch.add_argument("--output-dir", required=True)
    watch.add_argument("--processed-dir")
    watch.add_argument("--failed-dir")
    watch.add_argument("--report-dir")
    watch.add_argument("--accept-all", action="store_true")
    watch.add_argument("--force", action="store_true")
    watch.add_argument("--config-dir", help="gpucall config directory to review against")
    watch.add_argument("--validation-dir", help="tuple live validation artifact directory")
    watch.add_argument("--interval-seconds", type=float, default=10.0)
    watch.add_argument("--max-iterations", type=int)

    args = parser.parse_args(argv)
    if args.command == "materialize":
        if not args.accept_all:
            raise SystemExit("refusing to materialize without --accept-all")
        artifact = _load_json(args.input)
        recipe = canonical_recipe_from_artifact(artifact)
        report = materialization_report(artifact, recipe)
        if args.dry_run or not args.output_dir:
            sys.stdout.write(to_yaml(recipe))
        else:
            path = write_recipe_yaml(recipe, args.output_dir, force=args.force)
            report["recipe_path"] = str(path)
        if args.report:
            Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.command == "review":
        report = review_artifact(
            _load_json(args.input),
            config_dir=args.config_dir,
            validation_dir=args.validation_dir,
        )
        _write_json(report, args.output)
        return 0
    if args.command == "promote":
        report = promote_production_tuple(
            review=_load_json(args.review),
            candidate_name=args.candidate,
            config_dir=args.config_dir,
            work_dir=args.work_dir,
            validation_dir=args.validation_dir,
            run_validation=args.run_validation,
            activate=args.activate,
            force=args.force,
        )
        _write_json(report, args.output)
        return 0
    if args.command == "process-inbox":
        if not _accept_all_allowed(args.accept_all, args.config_dir):
            raise SystemExit("refusing to process inbox without --accept-all or admin.yml recipe_inbox_auto_materialize: true")
        results = process_inbox(
            inbox_dir=args.inbox_dir,
            output_dir=args.output_dir,
            processed_dir=args.processed_dir,
            failed_dir=args.failed_dir,
            report_dir=args.report_dir,
            force=args.force,
            config_dir=args.config_dir,
            validation_dir=args.validation_dir,
            accept_all=args.accept_all,
        )
        sys.stdout.write(json.dumps({"processed": results}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command == "status":
        sys.stdout.write(json.dumps(recipe_request_status(args.request_id, args.inbox_dir), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command == "watch":
        if not _accept_all_allowed(args.accept_all, args.config_dir):
            raise SystemExit("refusing to watch inbox without --accept-all or admin.yml recipe_inbox_auto_materialize: true")
        iterations = 0
        while True:
            results = process_inbox(
                inbox_dir=args.inbox_dir,
                output_dir=args.output_dir,
                processed_dir=args.processed_dir,
                failed_dir=args.failed_dir,
                report_dir=args.report_dir,
                force=args.force,
                config_dir=args.config_dir,
                validation_dir=args.validation_dir,
                accept_all=args.accept_all,
            )
            if results:
                sys.stdout.write(json.dumps({"processed": results}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                sys.stdout.flush()
            iterations += 1
            if args.max_iterations is not None and iterations >= args.max_iterations:
                return 0
            time.sleep(args.interval_seconds)
    raise AssertionError(args.command)


def review_artifact(
    artifact_or_submission: Mapping[str, Any],
    *,
    config_dir: str | Path | None = None,
    validation_dir: str | Path | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "admin-review",
        "reviewed_at": started,
        "decision": "REJECT",
        "production_ready": False,
        "auto_select_safe": False,
        "findings": [],
        "blockers": [],
        "warnings": [],
        "tuple_matrix": {},
        "tuple_candidate_matches": [],
        "required_execution_contract": {},
        "live_validation": {"matched": []},
    }
    try:
        artifact = _artifact_from_submission(artifact_or_submission)
        report["request_id"] = artifact_or_submission.get("request_id") if isinstance(artifact_or_submission, Mapping) else None
        report["submission_kind"] = artifact_or_submission.get("kind") if isinstance(artifact_or_submission, Mapping) else None
        report["intake_phase"] = artifact.get("phase")
        _review_redaction(artifact, report)
        recipe_dict = canonical_recipe_from_artifact(artifact)
        recipe = Recipe.model_validate(recipe_dict)
        report["canonical_recipe"] = recipe.model_dump(mode="json")
        report["required_execution_contract"] = tuple_contract_requirements(artifact, recipe)
        _review_config_and_providers(
            report,
            recipe=recipe,
            artifact=artifact,
            config_dir=Path(config_dir).expanduser() if config_dir else None,
            validation_dir=Path(validation_dir).expanduser() if validation_dir else None,
        )
    except Exception as exc:
        report["blockers"].append({"check": "review_exception", "reason": str(exc)})
    _finalize_review_decision(report)
    return report


def process_inbox(
    *,
    inbox_dir: str | Path,
    output_dir: str | Path,
    processed_dir: str | Path | None = None,
    failed_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    force: bool = False,
    config_dir: str | Path | None = None,
    validation_dir: str | Path | None = None,
    accept_all: bool = False,
) -> list[dict[str, Any]]:
    automation = _admin_automation(config_dir)
    if not _accept_all_allowed(accept_all, config_dir, automation=automation):
        raise PermissionError("recipe inbox auto-materialize is disabled")
    inbox = Path(inbox_dir)
    processed = Path(processed_dir) if processed_dir else inbox / "processed"
    failed = Path(failed_dir) if failed_dir else inbox / "failed"
    reports = Path(report_dir) if report_dir else inbox / "reports"
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    for path in sorted(inbox.glob("*.json")):
        if path.parent != inbox:
            continue
        try:
            submission = _load_json(str(path))
            review = review_artifact(submission, config_dir=config_dir, validation_dir=validation_dir)
            if review.get("decision") == "REJECT":
                raise ValueError("admin review rejected submission: " + "; ".join(_finding_reasons(review.get("blockers"))))
            artifact = _artifact_from_submission(submission)
            recipe = canonical_recipe_from_artifact(artifact)
            report = materialization_report(artifact, recipe)
            report["admin_review"] = review
            recipe_path = write_recipe_yaml(recipe, output, force=force)
            report["recipe_path"] = str(recipe_path)
            report["submission_path"] = str(path)
            promotion = _auto_promote_from_inbox(
                review=review,
                submission_stem=path.stem,
                config_dir=config_dir,
                validation_dir=validation_dir,
                automation=automation,
                force=force,
            )
            if promotion is not None:
                report["promotion"] = promotion
            report_path = reports / f"{path.stem}.report.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            destination = processed / path.name
            _move_submission(path, destination)
            results.append({"submission": str(destination), "recipe": str(recipe_path), "report": str(report_path), "ok": True})
        except Exception as exc:
            destination = failed / path.name
            _move_submission(path, destination)
            results.append({"submission": str(destination), "ok": False, "error": str(exc)})
    return results


def _auto_promote_from_inbox(
    *,
    review: Mapping[str, Any],
    submission_stem: str,
    config_dir: str | Path | None,
    validation_dir: str | Path | None,
    automation: RecipeAdminAutomationConfig,
    force: bool,
) -> dict[str, Any] | None:
    if not automation.recipe_inbox_auto_promote:
        return None
    root = Path(config_dir) if config_dir is not None else default_config_dir()
    if not review.get("tuple_candidate_matches") and review.get("eligible_tuples"):
        return _auto_validate_existing_tuple(
            review=review,
            config_dir=root,
            validation_dir=validation_dir or automation.recipe_inbox_validation_dir,
            automation=automation,
        )
    work_root = (
        Path(automation.recipe_inbox_promotion_work_dir).expanduser()
        if automation.recipe_inbox_promotion_work_dir
        else default_state_dir() / "recipe-promotions"
    )
    validation_root = validation_dir or automation.recipe_inbox_validation_dir
    return promote_production_tuple(
        review=review,
        candidate_name=None,
        config_dir=root,
        work_dir=work_root / submission_stem,
        validation_dir=validation_root,
        run_validation=automation.recipe_inbox_auto_run_validation,
        activate=automation.recipe_inbox_auto_activate,
        force=force,
    )


def _auto_validate_existing_tuple(
    *,
    review: Mapping[str, Any],
    config_dir: Path,
    validation_dir: str | Path | None,
    automation: RecipeAdminAutomationConfig,
) -> dict[str, Any]:
    recipe = _mapping(review.get("canonical_recipe"))
    recipe_name = str(recipe.get("name") or "")
    tuple_name = _selected_existing_tuple_for_review(review, config_dir=config_dir)
    if not recipe_name or not tuple_name:
        raise ValueError("review does not contain an eligible active tuple")
    report: dict[str, Any] = {
        "phase": "active-tuple-validation",
        "recipe": recipe_name,
        "tuple": tuple_name,
        "activated": False,
        "validation": None,
    }
    if not automation.recipe_inbox_auto_run_validation:
        report["decision"] = "READY_FOR_BILLABLE_VALIDATION"
        return report
    validation = _run_existing_tuple_validation(tuple_name, recipe_name, config_dir, validation_dir=validation_dir)
    report["validation"] = validation
    if validation.get("returncode") != 0 or validation.get("passed") is not True:
        report["decision"] = "VALIDATION_FAILED"
        return report
    if automation.recipe_inbox_auto_activate:
        report["activated"] = True
        report["decision"] = "ACTIVATED"
    else:
        report["decision"] = "VALIDATED_READY_TO_ACTIVATE"
    return report


def _run_existing_tuple_validation(
    tuple_name: str,
    recipe_name: str,
    config_dir: Path,
    *,
    validation_dir: str | Path | None,
) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "gpucall.cli",
        "tuple-smoke",
        tuple_name,
        "--config-dir",
        str(config_dir),
        "--recipe",
        recipe_name,
        "--mode",
        "sync",
        "--write-artifact",
    ]
    env = dict(os.environ)
    if validation_dir is not None:
        env["GPUCALL_STATE_DIR"] = str(Path(validation_dir).expanduser().parent)
    completed = subprocess.run(command, capture_output=True, text=True, env=env, check=False)
    result: dict[str, Any] = {
        "command": command,
        "returncode": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "passed": False,
    }
    if completed.stdout.strip():
        try:
            payload = json.loads(completed.stdout)
            result["artifact"] = payload
            result["passed"] = payload.get("passed") is True
        except json.JSONDecodeError:
            result["parse_error"] = "tuple-smoke stdout was not JSON"
    return result


def _selected_existing_tuple_for_review(review: Mapping[str, Any], *, config_dir: Path) -> str:
    recipe = _mapping(review.get("canonical_recipe"))
    recipe_name = str(recipe.get("name") or "")
    task = str(recipe.get("task") or "")
    modes = recipe.get("allowed_modes")
    mode = str(modes[0]) if isinstance(modes, list) and modes else "sync"
    if recipe_name and task:
        config = load_config(config_dir)
        compiler = GovernanceCompiler(
            policy=config.policy,
            recipes=config.recipes,
            tuples=config.tuples,
            models=config.models,
            engines=config.engines,
            registry=ObservedRegistry(path=default_state_dir() / "registry.db"),
        )
        plan = compiler.compile(TaskRequest(task=task, mode=ExecutionMode(mode), recipe=recipe_name))
        if plan.tuple_chain:
            return str(plan.tuple_chain[0])
    eligible = review.get("eligible_tuples")
    return str(eligible[0]) if isinstance(eligible, list) and eligible else ""


def _admin_automation(config_dir: str | Path | None) -> RecipeAdminAutomationConfig:
    root = Path(config_dir) if config_dir is not None else default_config_dir()
    return load_admin_automation(root)


def _accept_all_allowed(
    accept_all: bool,
    config_dir: str | Path | None,
    *,
    automation: RecipeAdminAutomationConfig | None = None,
) -> bool:
    if accept_all:
        return True
    automation = automation or _admin_automation(config_dir)
    return bool(automation.recipe_inbox_auto_materialize)


def recipe_request_status(request_id: str, inbox_dir: str | Path) -> dict[str, Any]:
    inbox = Path(inbox_dir)
    candidates = [
        ("pending", inbox / f"{request_id}.json"),
        ("processed", inbox / "processed" / f"{request_id}.json"),
        ("failed", inbox / "failed" / f"{request_id}.json"),
    ]
    for state, path in candidates:
        if path.exists():
            result: dict[str, Any] = {"request_id": request_id, "state": state, "path": str(path)}
            report_path = inbox / "reports" / f"{request_id}.report.json"
            if report_path.exists():
                result["report_path"] = str(report_path)
                try:
                    result["report"] = json.loads(report_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    result["report_error"] = "invalid report JSON"
            return result
    return {"request_id": request_id, "state": "missing"}


def _review_redaction(artifact: Mapping[str, Any], report: dict[str, Any]) -> None:
    redaction = artifact.get("redaction_report")
    if not isinstance(redaction, Mapping):
        report["blockers"].append({"check": "redaction_report", "reason": "missing redaction_report"})
        return
    unsafe = {
        str(key): value
        for key, value in redaction.items()
        if (str(key).endswith("_forwarded") or str(key).endswith("_included")) and value is not False
    }
    if unsafe:
        report["blockers"].append({"check": "redaction_report", "reason": "sensitive fields may have been forwarded", "fields": unsafe})
    else:
        report["findings"].append({"check": "redaction_report", "ok": True})


def _review_config_and_providers(
    report: dict[str, Any],
    *,
    recipe: Recipe,
    artifact: Mapping[str, Any],
    config_dir: Path | None,
    validation_dir: Path | None,
) -> None:
    try:
        config = load_config(config_dir)
    except ConfigError as exc:
        report["warnings"].append({"check": "config", "reason": str(exc)})
        report["decision_hint"] = "CANDIDATE_ONLY"
        return
    report["config_dir"] = str(config_dir) if config_dir else None
    report["policy_version"] = config.policy.version
    required_contracts = _required_input_contracts(recipe)
    tuple_matrix: dict[str, Any] = {}
    eligible: list[str] = []
    for tuple in sorted(config.tuples.values(), key=lambda item: item.name):
        reason = tuple_route_rejection_reason(
            policy=config.policy,
            recipe=recipe,
            tuple=tuple,
            model=config.models.get(tuple.model_ref) if tuple.model_ref else None,
            engine=config.engines.get(tuple.engine_ref) if tuple.engine_ref else None,
            mode=_first_mode(recipe),
            required_len=recipe_requirements(recipe).context_budget_tokens,
            required_input_contracts=required_contracts,
            auto_selected=True,
        )
        tuple_matrix[tuple.name] = _provider_review_row(tuple, reason)
        if reason is None:
            eligible.append(tuple.name)
    report["tuple_matrix"] = tuple_matrix
    report["eligible_tuples"] = eligible
    if not eligible:
        matches = _candidate_matches(
            config_dir=config_dir,
            config=config,
            contract=report.get("required_execution_contract") or {},
        )
        report["tuple_candidate_matches"] = matches
        report["warnings"].append({"check": "tuple_fit", "reason": "no execution tuple satisfies recipe, policy, mode, and contract requirements"})
        report["warnings"].append(
            {
                "check": "tuple_authoring_required",
                "reason": "existing execution tuples are insufficient; use required_execution_contract to add or update surface/worker specs and run billable validation",
            }
        )
        report["decision_hint"] = "CANDIDATE_ONLY"
        return
    capability_warnings = _capability_warnings(artifact, recipe, [config.tuples[name] for name in eligible])
    report["capability_review"] = {"warnings": capability_warnings}
    if capability_warnings:
        report["warnings"].extend(capability_warnings)
    live = _matching_live_validation(
        tuples=[config.tuples[name] for name in eligible],
        recipe=recipe,
        config_dir=config_dir,
        validation_dir=validation_dir,
    )
    report["live_validation"] = live
    shadowing = _auto_select_shadowing(recipe, config.recipes)
    report["auto_select_review"] = shadowing
    if not live["matched"]:
        report["decision_hint"] = "READY_FOR_VALIDATION"
        return
    if capability_warnings:
        report["decision_hint"] = "READY_FOR_VALIDATION"
        return
    report["decision_hint"] = "READY_FOR_PRODUCTION"
    if shadowing["safe"]:
        report["auto_select_safe"] = True
        report["decision_hint"] = "AUTO_SELECT_SAFE"


def _provider_review_row(tuple: ExecutionTupleSpec, reason: str | None) -> dict[str, Any]:
    return {
        "eligible": reason is None,
        "reason": reason,
        "adapter": tuple.adapter,
        "execution_surface": tuple.execution_surface.value if tuple.execution_surface else _surface_for_adapter(tuple.adapter),
        "model": tuple.model,
        "model_ref": tuple.model_ref,
        "engine_ref": tuple.engine_ref,
        "max_data_classification": str(tuple.max_data_classification),
        "gpu": tuple.gpu,
        "vram_gb": tuple.vram_gb,
        "max_model_len": tuple.max_model_len,
        "modes": [str(mode) for mode in tuple.modes],
        "input_contracts": list(tuple.input_contracts),
        "endpoint_contract": tuple.endpoint_contract,
        "output_contract": tuple.output_contract,
        "stream_contract": tuple.stream_contract,
    }


def _candidate_matches(*, config_dir: Path | None, config: Any, contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = _load_tuple_candidates(config_dir)
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        result = _candidate_match(candidate, config=config, contract=contract)
        if result["eligible"]:
            matches.append(result)
    return sorted(matches, key=lambda item: (item["promotion_rank"], item["name"]))


def _load_tuple_candidates(config_dir: Path | None) -> list[dict[str, Any]]:
    if config_dir is None:
        return []
    return load_tuple_candidate_payloads(config_dir)


def _candidate_match(candidate: Mapping[str, Any], *, config: Any, contract: Mapping[str, Any]) -> dict[str, Any]:
    reasons: list[str] = []
    model_ref = str(candidate.get("model_ref") or "")
    engine_ref = str(candidate.get("engine_ref") or "")
    model = config.models.get(model_ref)
    engine = config.engines.get(engine_ref)
    if candidate.get("status") not in {None, "candidate", "ready_for_validation"}:
        reasons.append("candidate status is not eligible for promotion planning")
    if model is None:
        reasons.append("candidate model_ref is not present in model catalog")
    if engine is None:
        reasons.append("candidate engine_ref is not present in engine catalog")
    if _positive_int(candidate.get("max_model_len"), default=0) < _positive_int(contract.get("min_model_len"), default=0):
        reasons.append("candidate max_model_len is below required contract")
    if _positive_int(candidate.get("vram_gb"), default=0) < _positive_int(contract.get("min_vram_gb"), default=0):
        reasons.append("candidate vram_gb is below required contract")
    missing_inputs = sorted(set(_strings(contract.get("input_contracts"))) - set(_strings(candidate.get("input_contracts"))))
    if missing_inputs:
        reasons.append("candidate input_contracts missing: " + ", ".join(missing_inputs))
    missing_modes = sorted(set(_strings(contract.get("modes"))) - set(_strings(candidate.get("modes") or contract.get("modes"))))
    if missing_modes:
        reasons.append("candidate modes missing: " + ", ".join(missing_modes))
    if model is not None:
        missing_capabilities = sorted(set(_strings(contract.get("model_capabilities"))) - set(model.capabilities))
        if missing_capabilities:
            reasons.append("candidate model capabilities missing: " + ", ".join(missing_capabilities))
    if not _candidate_output_satisfies(str(candidate.get("output_contract") or ""), str(contract.get("output_contract") or "")):
        reasons.append("candidate output_contract does not satisfy required contract")
    if not _classification_satisfies(str(candidate.get("max_data_classification") or "confidential"), str(contract.get("max_data_classification") or "confidential")):
        reasons.append("candidate max_data_classification is below required contract")
    eligible = not reasons
    return {
        "eligible": eligible,
        "name": candidate.get("name"),
        "path": candidate.get("_path"),
        "tuple_source": "candidate_catalog",
        "adapter": candidate.get("adapter"),
        "execution_surface": candidate.get("execution_surface") or _surface_for_adapter(str(candidate.get("adapter") or "")),
        "gpu": candidate.get("gpu"),
        "vram_gb": candidate.get("vram_gb"),
        "max_model_len": candidate.get("max_model_len"),
        "model_ref": model_ref or None,
        "engine_ref": engine_ref or None,
        "endpoint_contract": candidate.get("endpoint_contract"),
        "missing": reasons,
        "promotion_rank": _candidate_rank(candidate, contract=contract),
        "promotion_actions": _promotion_actions(candidate),
    }


def _candidate_rank(candidate: Mapping[str, Any], *, contract: Mapping[str, Any]) -> int:
    vram_overhead = _positive_int(candidate.get("vram_gb"), default=0) - _positive_int(contract.get("min_vram_gb"), default=0)
    len_overhead = _positive_int(candidate.get("max_model_len"), default=0) - _positive_int(contract.get("min_model_len"), default=0)
    return max(vram_overhead, 0) * 10_000_000 + max(len_overhead, 0)


def _promotion_actions(candidate: Mapping[str, Any]) -> list[str]:
    name = str(candidate.get("name") or "<candidate>")
    return [
        f"review official execution contract conformance for tuple {name}",
        f"materialize active surface/worker YAML from tuple candidate {name!r} only after credentials/endpoint ids are filled",
        f"run gpucall tuple-smoke {name} --write-artifact against the exact billable tuple",
        "rerun gpucall-recipe-admin review with --validation-dir pointing to tuple-validation artifacts",
        "promote to production auto-routing only after review returns READY_FOR_PRODUCTION or AUTO_SELECT_SAFE",
    ]


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _surface_for_adapter(adapter: str) -> str | None:
    descriptor = adapter_descriptor(adapter)
    if descriptor is None or descriptor.execution_surface is None:
        return None
    return descriptor.execution_surface.value


def _candidate_output_satisfies(candidate_contract: str, required_contract: str) -> bool:
    if not required_contract:
        return True
    if candidate_contract == required_contract:
        return True
    if required_contract == "plain-text" and candidate_contract in {"openai-chat-completions", "gpucall-tuple-result"}:
        return True
    if required_contract in {"json_object", "json_schema"} and candidate_contract in {"openai-chat-completions", "gpucall-tuple-result"}:
        return True
    return False


def _classification_satisfies(candidate: str, required: str) -> bool:
    order = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
    return order.get(candidate, 0) >= order.get(required, 2)


def tuple_contract_requirements(artifact: Mapping[str, Any], recipe: Recipe) -> dict[str, Any]:
    sanitized = _mapping(artifact.get("sanitized_request"))
    desired_capabilities = sanitized.get("desired_capabilities")
    if not isinstance(desired_capabilities, list) or not desired_capabilities:
        desired_capabilities = capabilities_for(task=recipe.task, intent=str(sanitized.get("intent") or "") or None)
    quality_feedback = _mapping(sanitized.get("quality_feedback"))
    requirements: dict[str, Any] = {
        "task": recipe.task,
        "model_capabilities": [str(item) for item in desired_capabilities],
        "min_vram_gb": recipe_requirements(recipe).minimum_vram_gb,
        "min_model_len": recipe_requirements(recipe).context_budget_tokens,
        "modes": [str(mode) for mode in recipe.allowed_modes],
        "input_contracts": sorted(_required_input_contracts(recipe)),
        "max_data_classification": str(recipe.data_classification),
        "endpoint_contract": _endpoint_contract_for(recipe),
        "output_contract": "plain-text",
        "stream_contract": "none" if ExecutionMode.STREAM not in recipe.allowed_modes else "token-incremental",
        "live_validation_required": True,
        "official_provider_spec_required": True,
        "acceptance_checks": [
            "tuple spec validates under gpucall validate-config",
            "tuple adapter contract matches official tuple API or SDK documentation",
            "tuple model explicitly declares the required context window",
            "tuple live validation artifact matches current commit and config hash",
            "cleanup and cost evidence are present in the live validation artifact",
        ],
    }
    if recipe.task == "vision":
        requirements["model_family_requirement"] = "vision-language model with document/image understanding; short-answer VQA alone is insufficient"
        requirements["input_contracts"] = ["data_refs", "image", "text"]
        requirements["acceptance_checks"].append("live quality validation covers the caller-declared visual task category")
    if quality_feedback:
        requirements["quality_failure_to_correct"] = {
            "kind": quality_feedback.get("kind"),
            "observed_output_kind": quality_feedback.get("observed_output_kind"),
            "expected_output": sanitized.get("expected_output"),
        }
    return requirements


def _endpoint_contract_for(recipe: Recipe) -> str:
    if recipe.task == "vision":
        return "serverless-function-or-official-vlm-endpoint"
    return "official-chat-completions-or-gpucall-tuple-result"


def _required_input_contracts(recipe: Recipe) -> set[str]:
    if recipe.task == "vision":
        return {"image", "text", "data_refs"}
    if recipe.task in {"train", "fine-tune"}:
        return {"data_refs", "artifact_refs"}
    if recipe.task == "split-infer":
        return {"activation_refs"}
    return {"chat_messages"}


def _first_mode(recipe: Recipe) -> ExecutionMode | None:
    return recipe.allowed_modes[0] if recipe.allowed_modes else None


def _capability_warnings(artifact: Mapping[str, Any], recipe: Recipe, tuples: list[ExecutionTupleSpec]) -> list[dict[str, Any]]:
    sanitized = _mapping(artifact.get("sanitized_request"))
    capabilities = sanitized.get("desired_capabilities")
    warnings: list[dict[str, Any]] = []
    if isinstance(capabilities, list) and capabilities:
        warnings.append(
            {
                "check": "model_capability_evidence",
                "reason": "tuple specs do not yet carry explicit semantic model capability evidence",
                "requested_capabilities": [str(item) for item in capabilities],
                "eligible_tuple_models": {tuple.name: tuple.model for tuple in tuples},
            }
        )
    if recipe.task == "vision" and any("document_understanding" == str(item) for item in capabilities or []):
        warnings.append(
            {
                "check": "document_vision_quality",
                "reason": "document image understanding requires model/live validation evidence; image input support alone is insufficient",
            }
        )
    return warnings


def _matching_live_validation(
    *,
    tuples: list[ExecutionTupleSpec],
    recipe: Recipe,
    config_dir: Path | None,
    validation_dir: Path | None,
) -> dict[str, Any]:
    root = validation_dir or (default_state_dir() / "tuple-validation")
    expected_hash = _config_hash(config_dir) if config_dir is not None and config_dir.exists() else None
    expected_commit = _git_commit(Path.cwd())
    provider_keys = {tuple_evidence_key(tuple): tuple for tuple in tuples}
    result: dict[str, Any] = {
        "dir": str(root),
        "matched": [],
        "checked": 0,
        "missing_for_tuples": sorted(provider_keys),
    }
    if not root.exists():
        result["reason"] = "validation artifact directory does not exist"
        return result
    matched: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        result["checked"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("validation_schema_version") != 1 or data.get("passed") is not True:
            continue
        if data.get("recipe") != recipe.name:
            continue
        matched_key = None
        for key, tuple in provider_keys.items():
            if artifact_tuple_evidence_key(data, tuple) == key:
                matched_key = key
                break
        if matched_key is None:
            continue
        if expected_hash and data.get("config_hash") != expected_hash:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        matched.append(
            {
                "path": str(path),
                "tuple": data.get("tuple"),
                "recipe": data.get("recipe"),
                "mode": data.get("mode"),
                "tuple_key": matched_key,
            }
        )
    result["matched"] = matched
    matched_tuples = {str(item.get("tuple_key")) for item in matched}
    result["missing_for_tuples"] = sorted(set(provider_keys) - matched_tuples)
    return result


def _auto_select_shadowing(recipe: Recipe, recipes: Mapping[str, Recipe]) -> dict[str, Any]:
    same_task = [item for item in recipes.values() if item.task == recipe.task and item.auto_select]
    candidate_requirements = recipe_requirements(recipe)
    larger_than_existing = all(candidate_requirements.context_budget_tokens > recipe_requirements(item).context_budget_tokens for item in same_task) if same_task else True
    cheaper_or_equal = all(candidate_requirements.minimum_vram_gb <= recipe_requirements(item).minimum_vram_gb for item in same_task) if same_task else True
    return {
        "candidate_auto_select": recipe.auto_select,
        "existing_auto_select_recipes": [item.name for item in same_task],
        "safe": bool(recipe.auto_select and larger_than_existing and cheaper_or_equal),
        "reason": None
        if recipe.auto_select and larger_than_existing and cheaper_or_equal
        else "candidate may be shadowed by existing recipes or may widen routing without enough evidence",
    }


def _finalize_review_decision(report: dict[str, Any]) -> None:
    if report.get("blockers"):
        report["decision"] = "REJECT"
        return
    hint = report.get("decision_hint")
    if hint == "AUTO_SELECT_SAFE":
        report["decision"] = "AUTO_SELECT_SAFE"
        report["production_ready"] = True
        report["auto_select_safe"] = True
    elif hint == "READY_FOR_PRODUCTION":
        report["decision"] = "READY_FOR_PRODUCTION"
        report["production_ready"] = True
    elif hint == "READY_FOR_VALIDATION":
        report["decision"] = "READY_FOR_VALIDATION"
    else:
        report["decision"] = "CANDIDATE_ONLY"


def _finding_reasons(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    reasons = []
    for item in value:
        if isinstance(item, Mapping):
            reasons.append(str(item.get("reason") or item.get("check") or item))
        else:
            reasons.append(str(item))
    return reasons


def _write_json(data: Mapping[str, Any], output: str | None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _config_hash(config_dir: Path | None) -> str | None:
    if config_dir is None or not config_dir.exists():
        return None
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*.yml")):
        if not path.is_file():
            continue
        digest.update(str(path.relative_to(config_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_commit(root: Path) -> str | None:
    head = root / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            ref = root / ".git" / value.removeprefix("ref: ")
            return ref.read_text(encoding="utf-8").strip()
        return value
    except OSError:
        return None


def _artifact_from_submission(data: Mapping[str, Any]) -> Mapping[str, Any]:
    if data.get("kind") == "gpucall.recipe_request_submission":
        draft = data.get("draft")
        if isinstance(draft, Mapping):
            return draft
        intake = data.get("intake")
        if isinstance(intake, Mapping):
            return intake
        raise ValueError("submission does not contain intake or draft")
    return data


def _move_submission(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        destination = destination.with_name(destination.stem + "-" + str(int(time.time())) + destination.suffix)
    shutil.move(str(source), str(destination))


def _load_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("input JSON must be an object")
    return data


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)
