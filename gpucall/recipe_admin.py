from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from gpucall.config import ConfigError, default_state_dir, load_config
from gpucall.domain import ExecutionMode, ProviderSpec, Recipe
from gpucall.routing import provider_route_rejection_reason


TEXT_STOP_TOKENS = ["<|im_end|>", "<|endoftext|>"]

CAPABILITY_BY_INTENT = {
    "answer_question_about_image": ["visual_question_answering", "instruction_following"],
    "caption_image": ["image_captioning"],
    "understand_document_image": ["document_understanding", "visual_question_answering", "instruction_following"],
    "transcribe_audio": ["speech_to_text"],
    "summarize_audio": ["speech_to_text", "summarization"],
    "summarize_video": ["video_understanding", "summarization"],
    "translate_text": ["translation"],
    "summarize_text": ["summarization"],
    "extract_json": ["structured_output"],
}

TASK_DEFAULT_CAPABILITIES = {
    "infer": ["instruction_following"],
    "vision": ["visual_question_answering", "instruction_following"],
    "transcribe": ["speech_to_text"],
    "video": ["video_understanding"],
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpucall-recipe-admin")
    subcommands = parser.add_subparsers(dest="command", required=True)

    materialize = subcommands.add_parser("materialize", help="materialize caller intake/draft into canonical gpucall recipe YAML")
    materialize.add_argument("--input", "-i", required=True, help="path to caller intake/draft JSON, or '-' for stdin")
    materialize.add_argument("--output-dir", help="directory to write recipe YAML")
    materialize.add_argument("--report", help="write materialization report JSON")
    materialize.add_argument("--accept-all", action="store_true", help="explicitly accept caller artifact into a recipe candidate")
    materialize.add_argument("--force", action="store_true", help="overwrite existing recipe YAML")
    materialize.add_argument("--dry-run", action="store_true", help="print YAML without writing files")

    review = subcommands.add_parser("review", help="review a submitted recipe request against config, providers, policy, and live evidence")
    review.add_argument("--input", "-i", required=True, help="path to caller submission/intake/draft JSON, or '-' for stdin")
    review.add_argument("--config-dir", help="gpucall config directory to review against")
    review.add_argument("--validation-dir", help="provider live validation artifact directory")
    review.add_argument("--output", "-o", help="write review report JSON")

    promote = subcommands.add_parser("promote", help="prepare, validate, and optionally activate a provider candidate from a review report")
    promote.add_argument("--review", required=True, help="admin review JSON produced by gpucall-recipe-admin review")
    promote.add_argument("--candidate", help="candidate name; defaults to the first provider_candidate_matches entry")
    promote.add_argument("--config-dir", required=True, help="active gpucall config directory")
    promote.add_argument("--work-dir", required=True, help="promotion workspace for generated config and reports")
    promote.add_argument("--validation-dir", help="provider live validation artifact directory")
    promote.add_argument("--run-validation", action="store_true", help="run billable gpucall provider-smoke in the promotion workspace")
    promote.add_argument("--activate", action="store_true", help="copy validated recipe/provider into the active config directory")
    promote.add_argument("--force", action="store_true", help="overwrite generated or active recipe/provider files")
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
    process.add_argument("--validation-dir", help="provider live validation artifact directory")

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
    watch.add_argument("--validation-dir", help="provider live validation artifact directory")
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
        report = promote_candidate(
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
        if not args.accept_all:
            raise SystemExit("refusing to process inbox without --accept-all")
        results = process_inbox(
            inbox_dir=args.inbox_dir,
            output_dir=args.output_dir,
            processed_dir=args.processed_dir,
            failed_dir=args.failed_dir,
            report_dir=args.report_dir,
            force=args.force,
            config_dir=args.config_dir,
            validation_dir=args.validation_dir,
        )
        sys.stdout.write(json.dumps({"processed": results}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command == "status":
        sys.stdout.write(json.dumps(recipe_request_status(args.request_id, args.inbox_dir), ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        return 0
    if args.command == "watch":
        if not args.accept_all:
            raise SystemExit("refusing to watch inbox without --accept-all")
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
            )
            if results:
                sys.stdout.write(json.dumps({"processed": results}, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
                sys.stdout.flush()
            iterations += 1
            if args.max_iterations is not None and iterations >= args.max_iterations:
                return 0
            time.sleep(args.interval_seconds)
    raise AssertionError(args.command)


def canonical_recipe_from_artifact(artifact: Mapping[str, Any]) -> dict[str, Any]:
    proposed = _proposed_recipe_from_artifact(artifact)
    task = str(proposed.get("task") or "infer")
    name = _canonical_name(str(proposed.get("name") or f"{task}-draft"))
    max_model_len = _positive_int(proposed.get("max_model_len"), default=32768)
    recipe: dict[str, Any] = {
        "name": name,
        "task": task,
        "auto_select": bool(proposed.get("auto_select", True)),
        "data_classification": str(proposed.get("data_classification") or "confidential"),
        "allowed_modes": _allowed_modes(proposed),
        "min_vram_gb": _positive_int(proposed.get("min_vram_gb"), default=_default_vram(task, proposed)),
        "max_model_len": max_model_len,
        "timeout_seconds": _timeout_for(task, max_model_len),
        "lease_ttl_seconds": _lease_for(task, max_model_len),
        "tokenizer_family": "qwen",
        "gpu": "any",
        "max_input_bytes": _max_input_bytes(task, max_model_len),
        "allowed_mime_prefixes": _allowed_mime_prefixes(task, proposed),
        "default_temperature": 0.2 if task == "vision" else 0.7,
        "structured_temperature": 0.0,
        "structured_system_prompt": "Return only valid JSON when response_format requests JSON. Do not include markdown fences or prose.",
        "system_prompt": _system_prompt_for(task),
        "stop_tokens": TEXT_STOP_TOKENS,
        "repetition_penalty": 1.05,
        "guided_decoding": True,
        "output_validation_attempts": 1,
        "required_model_capabilities": [str(item) for item in proposed.get("required_model_capabilities") or []],
        "output_contract": _route_output_contract(proposed),
    }
    if task == "vision":
        recipe["allowed_inline_mime_prefixes"] = ["text/"]
    return recipe


def materialization_report(artifact: Mapping[str, Any], recipe: Mapping[str, Any]) -> dict[str, Any]:
    proposed = _proposed_recipe_from_artifact(artifact)
    return {
        "schema_version": 1,
        "phase": "admin-materialization",
        "policy": "accept-all",
        "human_review_bypassed": True,
        "canonical_recipe": dict(recipe),
        "discarded_draft_fields": sorted(set(proposed) - set(recipe)),
        "warnings": [
            "accept-all materialization writes a recipe candidate; it does not create a capable provider.",
            "run gpucall validate-config after copying the recipe into a real config directory.",
            "if validate-config reports no satisfying provider, add or enable a provider before production use.",
        ],
    }


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
        "provider_matrix": {},
        "provider_candidate_matches": [],
        "required_provider_contract": {},
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
        report["required_provider_contract"] = provider_contract_requirements(artifact, recipe)
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


def promote_candidate(
    *,
    review: Mapping[str, Any],
    candidate_name: str | None,
    config_dir: str | Path,
    work_dir: str | Path,
    validation_dir: str | Path | None = None,
    run_validation: bool = False,
    activate: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    config_root = Path(config_dir).expanduser()
    workspace = Path(work_dir).expanduser()
    if not config_root.exists():
        raise FileNotFoundError(f"config_dir does not exist: {config_root}")
    candidate = _select_review_candidate(review, candidate_name)
    recipe = _mapping(review.get("canonical_recipe"))
    if not recipe:
        raise ValueError("review does not contain canonical_recipe")
    active_config = load_config(config_root)
    candidate_path = candidate.get("path")
    if not candidate_path:
        raise ValueError("candidate match does not include source path")
    candidate_payload = _load_yaml_file(Path(str(candidate_path)))
    provider = _provider_from_candidate(candidate_payload, active_config=active_config)
    started = datetime.now(timezone.utc).isoformat()
    promotion_config = workspace / "config"
    reports = workspace / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    _copy_config_tree(config_root, promotion_config, force=force)
    recipe_path = _write_yaml_guarded(promotion_config / "recipes" / f"{recipe['name']}.yml", recipe, force=force)
    provider_path = _write_yaml_guarded(promotion_config / "providers" / f"{provider['name']}.yml", provider, force=force)
    promotion_report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "provider-candidate-promotion",
        "started_at": started,
        "candidate": candidate,
        "recipe": recipe["name"],
        "provider": provider["name"],
        "promotion_config_dir": str(promotion_config),
        "generated_recipe_path": str(recipe_path),
        "generated_provider_path": str(provider_path),
        "validation": None,
        "activated": False,
        "activation_paths": {},
        "next_actions": [
            f"run gpucall validate-config --config-dir {promotion_config}",
            f"run gpucall provider-smoke {provider['name']} --config-dir {promotion_config} --recipe {recipe['name']} --mode sync --write-artifact",
            "rerun gpucall-recipe-admin review with the validation artifact directory",
            "activate only after validation passes for the exact recipe/provider/model/engine tuple",
        ],
    }
    try:
        _validate_config_dir(promotion_config)
        promotion_report["config_valid"] = True
    except ConfigError as exc:
        promotion_report["config_valid"] = False
        promotion_report["config_error"] = str(exc)
        promotion_report["decision"] = "READY_FOR_ENDPOINT_CONFIGURATION"
        promotion_report["next_actions"].insert(
            0,
            "fill provider-specific required fields in the generated provider YAML, then rerun promote with --run-validation",
        )
        if run_validation or activate:
            raise ValueError("refusing validation/activation because generated promotion config is not valid: " + str(exc)) from exc
        return promotion_report
    if run_validation:
        validation = _run_provider_validation(provider["name"], recipe["name"], promotion_config, validation_dir=validation_dir)
        promotion_report["validation"] = validation
        if validation.get("returncode") != 0 or validation.get("passed") is not True:
            promotion_report["decision"] = "VALIDATION_FAILED"
            return promotion_report
    else:
        existing_validation = _find_validation_for_promotion(
            provider=provider["name"],
            recipe=recipe["name"],
            model_ref=provider.get("model_ref"),
            engine_ref=provider.get("engine_ref"),
            config_dir=promotion_config,
            validation_dir=Path(validation_dir).expanduser() if validation_dir else None,
        )
        promotion_report["validation"] = existing_validation
        if not existing_validation.get("matched"):
            promotion_report["decision"] = "READY_FOR_BILLABLE_VALIDATION"
            if activate:
                raise ValueError("refusing to activate without matching live validation artifact")
            return promotion_report
    if activate:
        active_recipe = _write_yaml_guarded(config_root / "recipes" / f"{recipe['name']}.yml", recipe, force=force)
        active_provider = _write_yaml_guarded(config_root / "providers" / f"{provider['name']}.yml", provider, force=force)
        _validate_config_dir(config_root)
        promotion_report["activated"] = True
        promotion_report["activation_paths"] = {"recipe": str(active_recipe), "provider": str(active_provider)}
        promotion_report["decision"] = "ACTIVATED"
    else:
        promotion_report["decision"] = "VALIDATED_READY_TO_ACTIVATE"
    return promotion_report


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
) -> list[dict[str, Any]]:
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
    provider_matrix: dict[str, Any] = {}
    eligible: list[str] = []
    for provider in sorted(config.providers.values(), key=lambda item: item.name):
        reason = provider_route_rejection_reason(
            policy=config.policy,
            recipe=recipe,
            provider=provider,
            model=config.models.get(provider.model_ref) if provider.model_ref else None,
            engine=config.engines.get(provider.engine_ref) if provider.engine_ref else None,
            mode=_first_mode(recipe),
            required_len=recipe.max_model_len,
            required_input_contracts=required_contracts,
            auto_selected=True,
        )
        provider_matrix[provider.name] = _provider_review_row(provider, reason)
        if reason is None:
            eligible.append(provider.name)
    report["provider_matrix"] = provider_matrix
    report["eligible_providers"] = eligible
    if not eligible:
        report["provider_candidate_matches"] = _candidate_matches(
            config_dir=config_dir,
            config=config,
            contract=report.get("required_provider_contract") or {},
        )
        report["warnings"].append({"check": "provider_fit", "reason": "no provider satisfies recipe, policy, mode, and contract requirements"})
        report["warnings"].append(
            {
                "check": "provider_authoring_required",
                "reason": "existing providers are insufficient; use required_provider_contract to add or update a provider spec and run billable validation",
            }
        )
        report["decision_hint"] = "CANDIDATE_ONLY"
        return
    capability_warnings = _capability_warnings(artifact, recipe, [config.providers[name] for name in eligible])
    report["capability_review"] = {"warnings": capability_warnings}
    if capability_warnings:
        report["warnings"].extend(capability_warnings)
    live = _matching_live_validation(
        providers=[config.providers[name] for name in eligible],
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


def _provider_review_row(provider: ProviderSpec, reason: str | None) -> dict[str, Any]:
    return {
        "eligible": reason is None,
        "reason": reason,
        "adapter": provider.adapter,
        "model": provider.model,
        "model_ref": provider.model_ref,
        "engine_ref": provider.engine_ref,
        "max_data_classification": str(provider.max_data_classification),
        "gpu": provider.gpu,
        "vram_gb": provider.vram_gb,
        "max_model_len": provider.max_model_len,
        "modes": [str(mode) for mode in provider.modes],
        "input_contracts": list(provider.input_contracts),
        "endpoint_contract": provider.endpoint_contract,
        "output_contract": provider.output_contract,
        "stream_contract": provider.stream_contract,
    }


def _candidate_matches(*, config_dir: Path | None, config: Any, contract: Mapping[str, Any]) -> list[dict[str, Any]]:
    candidates = _load_provider_candidates(config_dir)
    matches: list[dict[str, Any]] = []
    for candidate in candidates:
        result = _candidate_match(candidate, config=config, contract=contract)
        if result["eligible"]:
            matches.append(result)
    return sorted(matches, key=lambda item: (item["promotion_rank"], item["name"]))


def _load_provider_candidates(config_dir: Path | None) -> list[dict[str, Any]]:
    if config_dir is None:
        return []
    root = config_dir / "provider_candidates"
    if not root.exists():
        return []
    candidates: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.yml")):
        payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(payload, dict):
            continue
        payload["_path"] = str(path)
        candidates.append(payload)
    return candidates


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
        "adapter": candidate.get("adapter"),
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


def _select_review_candidate(review: Mapping[str, Any], candidate_name: str | None) -> Mapping[str, Any]:
    matches = review.get("provider_candidate_matches")
    if not isinstance(matches, list) or not matches:
        raise ValueError("review does not contain provider_candidate_matches")
    if candidate_name is None:
        selected = matches[0]
        if not isinstance(selected, Mapping):
            raise ValueError("invalid provider_candidate_matches entry")
        return selected
    for match in matches:
        if isinstance(match, Mapping) and match.get("name") == candidate_name:
            return match
    raise ValueError(f"candidate {candidate_name!r} is not present in review provider_candidate_matches")


def _provider_from_candidate(candidate: Mapping[str, Any], *, active_config: Any) -> dict[str, Any]:
    name = str(candidate.get("name") or "")
    model_ref = str(candidate.get("model_ref") or "")
    engine_ref = str(candidate.get("engine_ref") or "")
    if not name or not model_ref or not engine_ref:
        raise ValueError("candidate must define name, model_ref, and engine_ref")
    model = active_config.models.get(model_ref)
    if model is None:
        raise ValueError(f"candidate references unknown model_ref {model_ref!r}")
    source: dict[str, Any] = {
        "name": name,
        "adapter": str(candidate.get("adapter") or ""),
        "max_data_classification": str(candidate.get("max_data_classification") or "confidential"),
        "trust_profile": {
            "security_tier": str(candidate.get("security_tier") or "encrypted_capsule"),
            "sovereign_jurisdiction": candidate.get("sovereign_jurisdiction") or "unknown",
            "dedicated_gpu": bool(candidate.get("dedicated_gpu", False)),
            "requires_attestation": bool(candidate.get("requires_attestation", False)),
            "supports_key_release": bool(candidate.get("supports_key_release", False)),
            "allows_worker_s3_credentials": bool(candidate.get("allows_worker_s3_credentials", False)),
        },
        "gpu": str(candidate.get("gpu") or "unknown"),
        "vram_gb": _positive_int(candidate.get("vram_gb"), default=1),
        "max_model_len": _positive_int(candidate.get("max_model_len"), default=model.max_model_len),
        "cost_per_second": float(candidate.get("cost_per_second") or 0.0),
        "modes": _strings(candidate.get("modes") or ["sync", "async"]),
        "endpoint": candidate.get("endpoint"),
        "endpoint_contract": candidate.get("endpoint_contract"),
        "input_contracts": _strings(candidate.get("input_contracts")),
        "output_contract": candidate.get("output_contract"),
        "stream_contract": candidate.get("stream_contract") or "none",
        "supports_vision": bool(model.supports_vision),
        "target": candidate.get("target") or "",
        "stream_target": candidate.get("stream_target"),
        "model": model.provider_model_id,
        "declared_model_max_len": model.max_model_len,
        "model_ref": model_ref,
        "engine_ref": engine_ref,
        "provider_params": dict(candidate.get("provider_params") or {}),
    }
    for key in ("project_id", "region", "zone", "resource_group", "network", "subnet", "service_account", "instance", "image", "key_name", "ssh_remote_cidr", "lease_manifest_path"):
        if key in candidate:
            source[key] = candidate.get(key)
    ProviderSpec.model_validate(source)
    return source


def _copy_config_tree(source: Path, destination: Path, *, force: bool) -> None:
    if destination.exists():
        if not force:
            raise FileExistsError(f"promotion config already exists: {destination}")
        shutil.rmtree(destination)
    ignore = shutil.ignore_patterns("*.db", "*.db-shm", "*.db-wal", "__pycache__")
    shutil.copytree(source, destination, ignore=ignore)


def _write_yaml_guarded(path: Path, payload: Mapping[str, Any], *, force: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing file: {path}")
    path.write_text(to_yaml(payload), encoding="utf-8")
    return path


def _validate_config_dir(config_dir: Path) -> None:
    load_config(config_dir)


def _run_provider_validation(provider: str, recipe: str, config_dir: Path, *, validation_dir: str | Path | None) -> dict[str, Any]:
    command = [
        sys.executable,
        "-m",
        "gpucall.cli",
        "provider-smoke",
        provider,
        "--config-dir",
        str(config_dir),
        "--recipe",
        recipe,
        "--mode",
        "sync",
        "--write-artifact",
    ]
    env = dict(os.environ)
    credentials = config_dir / "credentials.yml"
    if credentials.exists() and not env.get("GPUCALL_CREDENTIALS"):
        env["GPUCALL_CREDENTIALS"] = str(credentials)
    modal_config = config_dir / ".modal.toml"
    if modal_config.exists() and not env.get("MODAL_CONFIG_PATH"):
        env["MODAL_CONFIG_PATH"] = str(modal_config)
    if validation_dir is not None:
        state_dir = Path(validation_dir).expanduser().parent
        env["GPUCALL_STATE_DIR"] = str(state_dir)
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
            result["parse_error"] = "provider-smoke stdout was not JSON"
    return result


def _find_validation_for_promotion(
    *,
    provider: str,
    recipe: str,
    model_ref: str | None,
    engine_ref: str | None,
    config_dir: Path,
    validation_dir: Path | None,
) -> dict[str, Any]:
    root = validation_dir or (default_state_dir() / "provider-validation")
    result = {"dir": str(root), "matched": []}
    if not root.exists():
        result["reason"] = "validation artifact directory does not exist"
        return result
    expected_hash = _config_hash(config_dir)
    expected_commit = _git_commit(Path.cwd())
    matched: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("validation_schema_version") != 1 or data.get("passed") is not True:
            continue
        if data.get("provider") != provider or data.get("recipe") != recipe:
            continue
        if data.get("model_ref") != model_ref or data.get("engine_ref") != engine_ref:
            continue
        if data.get("config_hash") != expected_hash:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        matched.append({"path": str(path), "provider": provider, "recipe": recipe})
    result["matched"] = matched
    return result


def _load_yaml_file(path: Path) -> dict[str, Any]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return payload


def _candidate_rank(candidate: Mapping[str, Any], *, contract: Mapping[str, Any]) -> int:
    vram_overhead = _positive_int(candidate.get("vram_gb"), default=0) - _positive_int(contract.get("min_vram_gb"), default=0)
    len_overhead = _positive_int(candidate.get("max_model_len"), default=0) - _positive_int(contract.get("min_model_len"), default=0)
    return max(vram_overhead, 0) * 10_000_000 + max(len_overhead, 0)


def _promotion_actions(candidate: Mapping[str, Any]) -> list[str]:
    name = str(candidate.get("name") or "<candidate>")
    return [
        f"review official adapter conformance for {name}",
        f"create active provider YAML from provider_candidates/{name}.yml only after credentials/endpoint ids are filled",
        f"run gpucall provider-smoke {name} --write-artifact against a billable endpoint",
        "rerun gpucall-recipe-admin review with --validation-dir pointing to provider-validation artifacts",
        "promote to production auto-routing only after review returns READY_FOR_PRODUCTION or AUTO_SELECT_SAFE",
    ]


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _candidate_output_satisfies(candidate_contract: str, required_contract: str) -> bool:
    if not required_contract:
        return True
    if candidate_contract == required_contract:
        return True
    if required_contract == "plain-text" and candidate_contract in {"openai-chat-completions", "gpucall-provider-result"}:
        return True
    if required_contract in {"json-object", "json_schema"} and candidate_contract in {"openai-chat-completions", "gpucall-provider-result"}:
        return True
    return False


def _classification_satisfies(candidate: str, required: str) -> bool:
    order = {"public": 0, "internal": 1, "confidential": 2, "restricted": 3}
    return order.get(candidate, 0) >= order.get(required, 2)


def provider_contract_requirements(artifact: Mapping[str, Any], recipe: Recipe) -> dict[str, Any]:
    sanitized = _mapping(artifact.get("sanitized_request"))
    desired_capabilities = sanitized.get("desired_capabilities")
    if not isinstance(desired_capabilities, list) or not desired_capabilities:
        desired_capabilities = CAPABILITY_BY_INTENT.get(str(sanitized.get("intent") or ""), TASK_DEFAULT_CAPABILITIES.get(recipe.task, []))
    quality_feedback = _mapping(sanitized.get("quality_feedback"))
    requirements: dict[str, Any] = {
        "task": recipe.task,
        "model_capabilities": [str(item) for item in desired_capabilities],
        "min_vram_gb": recipe.min_vram_gb,
        "min_model_len": recipe.max_model_len,
        "modes": [str(mode) for mode in recipe.allowed_modes],
        "input_contracts": sorted(_required_input_contracts(recipe)),
        "max_data_classification": str(recipe.data_classification),
        "endpoint_contract": _endpoint_contract_for(recipe),
        "output_contract": "plain-text",
        "stream_contract": "none" if ExecutionMode.STREAM not in recipe.allowed_modes else "token-incremental",
        "live_validation_required": True,
        "official_provider_spec_required": True,
        "acceptance_checks": [
            "provider spec validates under gpucall validate-config",
            "provider adapter contract matches official provider API or SDK documentation",
            "provider model explicitly declares the required context window",
            "provider live validation artifact matches current commit and config hash",
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
        return "modal-function-or-official-vlm-endpoint"
    return "official-chat-completions-or-gpucall-provider-result"


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


def _capability_warnings(artifact: Mapping[str, Any], recipe: Recipe, providers: list[ProviderSpec]) -> list[dict[str, Any]]:
    sanitized = _mapping(artifact.get("sanitized_request"))
    capabilities = sanitized.get("desired_capabilities")
    warnings: list[dict[str, Any]] = []
    if isinstance(capabilities, list) and capabilities:
        warnings.append(
            {
                "check": "model_capability_evidence",
                "reason": "provider specs do not yet carry explicit semantic model capability evidence",
                "requested_capabilities": [str(item) for item in capabilities],
                "eligible_provider_models": {provider.name: provider.model for provider in providers},
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
    providers: list[ProviderSpec],
    recipe: Recipe,
    config_dir: Path | None,
    validation_dir: Path | None,
) -> dict[str, Any]:
    root = validation_dir or (default_state_dir() / "provider-validation")
    expected_hash = _config_hash(config_dir) if config_dir is not None and config_dir.exists() else None
    expected_commit = _git_commit(Path.cwd())
    result: dict[str, Any] = {"dir": str(root), "matched": [], "checked": 0, "missing_for_providers": [provider.name for provider in providers]}
    if not root.exists():
        result["reason"] = "validation artifact directory does not exist"
        return result
    provider_names = {provider.name for provider in providers}
    matched: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        result["checked"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("validation_schema_version") != 1 or data.get("passed") is not True:
            continue
        if data.get("provider") not in provider_names:
            continue
        provider = next((item for item in providers if item.name == data.get("provider")), None)
        if provider is None:
            continue
        if data.get("model_ref") != provider.model_ref or data.get("engine_ref") != provider.engine_ref:
            continue
        if data.get("recipe") != recipe.name:
            continue
        if expected_hash and data.get("config_hash") != expected_hash:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        matched.append({"path": str(path), "provider": data.get("provider"), "recipe": data.get("recipe"), "mode": data.get("mode")})
    result["matched"] = matched
    matched_providers = {str(item.get("provider")) for item in matched}
    result["missing_for_providers"] = sorted(provider_names - matched_providers)
    return result


def _auto_select_shadowing(recipe: Recipe, recipes: Mapping[str, Recipe]) -> dict[str, Any]:
    same_task = [item for item in recipes.values() if item.task == recipe.task and item.auto_select]
    larger_than_existing = all(recipe.max_model_len > item.max_model_len for item in same_task) if same_task else True
    cheaper_or_equal = all(recipe.min_vram_gb <= item.min_vram_gb for item in same_task) if same_task else True
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


def write_recipe_yaml(recipe: Mapping[str, Any], output_dir: str | Path, *, force: bool = False) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{recipe['name']}.yml"
    if path.exists() and not force:
        raise FileExistsError(f"recipe already exists: {path}")
    path.write_text(to_yaml(recipe), encoding="utf-8")
    return path


def to_yaml(value: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(value), allow_unicode=True, sort_keys=False)


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


def _proposed_recipe_from_artifact(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    if "proposed_recipe" in artifact:
        return _mapping(artifact.get("proposed_recipe"))
    sanitized = _mapping(artifact.get("sanitized_request"))
    if sanitized:
        return _proposed_recipe_from_sanitized(sanitized)
    raise ValueError("artifact must be a gpucall-recipe-draft intake or draft JSON object")


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


def _proposed_recipe_from_sanitized(sanitized: Mapping[str, Any]) -> dict[str, Any]:
    task = str(sanitized.get("task") or "infer")
    intent = str(sanitized.get("intent") or task)
    capabilities = sanitized.get("desired_capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        capabilities = CAPABILITY_BY_INTENT.get(intent) or TASK_DEFAULT_CAPABILITIES.get(task, ["instruction_following"])
    required_len = _mapping(_mapping(sanitized.get("error")).get("context")).get("required_model_len")
    return {
        "name": _recipe_name(task, intent),
        "task": task,
        "auto_select": True,
        "data_classification": str(sanitized.get("classification") or "confidential"),
        "allowed_modes": [str(sanitized.get("mode") or "sync")],
        "required_model_capabilities": [str(item) for item in capabilities],
        "min_vram_gb": _default_vram(task, {"required_model_capabilities": capabilities, "max_model_len": required_len}),
        "max_model_len": _round_model_len(required_len),
        "allowed_mime_prefixes": _mime_prefixes_for(task),
        "output_contract": sanitized.get("expected_output") or "plain_text",
    }


def _route_output_contract(proposed: Mapping[str, Any]) -> str:
    raw = str(proposed.get("output_contract") or "").strip().lower().replace("_", "-")
    if raw in {"json-object", "json-schema"}:
        return raw.replace("-", "_")
    if raw in {"plain-text", "text", "plain"}:
        return "plain-text"
    return "plain-text"


def _load_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("input JSON must be an object")
    return data


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return cleaned or "recipe-draft"


def _recipe_name(task: str, intent: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", intent.lower()).strip("-")
    return f"{task}-{cleaned or 'standard'}-draft"


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _round_model_len(value: Any) -> int:
    try:
        required = int(value)
    except (TypeError, ValueError):
        required = 8192
    for candidate in (8192, 32768, 65536, 131072, 262144, 524288, 1010000):
        if required <= candidate:
            return candidate
    return required


def _allowed_modes(proposed: Mapping[str, Any]) -> list[str]:
    raw = proposed.get("allowed_modes")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw if str(item)]
    return ["sync", "async"]


def _default_vram(task: str, proposed: Mapping[str, Any]) -> int:
    capabilities = proposed.get("required_model_capabilities") or []
    max_model_len = _positive_int(proposed.get("max_model_len"), default=8192)
    base = 80 if task in {"vision", "video"} else 24
    if max_model_len > 524288:
        base = max(base, 320)
    if max_model_len > 131072:
        base = max(base, 80)
    if any(str(capability) in {"document_understanding", "video_understanding"} for capability in capabilities):
        base = max(base, 80)
    return base


def _timeout_for(task: str, max_model_len: int) -> int:
    if task == "vision" or max_model_len >= 131072:
        return 600
    return 180


def _lease_for(task: str, max_model_len: int) -> int:
    if task == "vision" or max_model_len >= 131072:
        return 900
    return 240


def _max_input_bytes(task: str, max_model_len: int) -> int:
    if task == "vision":
        return 16 * 1024 * 1024
    return max(16 * 1024 * 1024, min(1024 * 1024 * 1024, max_model_len * 1024))


def _allowed_mime_prefixes(task: str, proposed: Mapping[str, Any]) -> list[str]:
    raw = proposed.get("allowed_mime_prefixes")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    return _mime_prefixes_for(task)


def _mime_prefixes_for(task: str) -> list[str]:
    if task == "vision":
        return ["image/"]
    if task == "transcribe":
        return ["audio/"]
    if task == "video":
        return ["video/"]
    return ["text/"]


def _system_prompt_for(task: str) -> str:
    if task == "vision":
        return "Answer the user's vision request directly from the supplied image and prompt."
    return "Answer the user's request directly."
