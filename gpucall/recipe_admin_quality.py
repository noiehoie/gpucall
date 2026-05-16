from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from gpucall.config import load_config
from gpucall.recipe_admin_files import move_submission
from gpucall.quality_feedback_index import QualityFeedbackIndex, default_quality_feedback_index_path
from gpucall.recipe_intents import normalize_intent


def process_quality_inbox(
    *,
    inbox_dir: str | Path,
    processed_dir: str | Path | None = None,
    failed_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    index_db: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    inbox = Path(inbox_dir)
    processed = Path(processed_dir) if processed_dir else inbox / "processed"
    failed = Path(failed_dir) if failed_dir else inbox / "failed"
    reports = Path(report_dir) if report_dir else inbox / "reports"
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    reports.mkdir(parents=True, exist_ok=True)
    index = QualityFeedbackIndex(index_db or default_quality_feedback_index_path(inbox))
    results: list[dict[str, Any]] = []
    for path in sorted(inbox.glob("*.json")):
        if path.parent != inbox:
            continue
        current_path = path
        feedback_id = path.stem
        report_path: Path | None = None
        try:
            submission = _load_json_file(path)
            feedback_id = index.upsert_pending(path, submission)["feedback_id"]
            report = quality_feedback_report(submission, config_dir=config_dir)
            if report.get("decision") == "REJECT":
                raise ValueError("quality feedback rejected: " + "; ".join(_finding_reasons(report.get("blockers"))))
            report_path = reports / f"{path.stem}.report.json"
            report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            destination = move_submission(current_path, processed / path.name)
            current_path = destination
            index.mark_processed(feedback_id, original_path=destination, report_path=report_path)
            results.append({"submission": str(destination), "report": str(report_path), "ok": True})
        except Exception as exc:
            destination = move_submission(current_path, failed / current_path.name) if current_path.exists() else failed / path.name
            index.mark_failed(feedback_id, original_path=destination, error=str(exc), report_path=report_path)
            results.append({"submission": str(destination), "ok": False, "error": str(exc)})
    return results


def quality_feedback_report(
    submission_or_intake: Mapping[str, Any],
    *,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    started = datetime.now(timezone.utc).isoformat()
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "quality-feedback-review",
        "reviewed_at": started,
        "decision": "ACCEPT",
        "production_config_written": False,
        "blockers": [],
        "warnings": [],
        "findings": [],
        "next_actions": [],
    }
    artifact = _artifact_from_submission(submission_or_intake)
    report["feedback_id"] = submission_or_intake.get("request_id") if isinstance(submission_or_intake, Mapping) else None
    report["submission_kind"] = submission_or_intake.get("kind") if isinstance(submission_or_intake, Mapping) else None
    if artifact.get("phase") != "deterministic-quality-feedback-intake":
        report["blockers"].append({"check": "phase", "reason": "submission is not deterministic quality feedback intake"})
    _review_redaction(artifact, report)
    sanitized = _mapping(artifact.get("sanitized_request"))
    quality = _mapping(sanitized.get("quality_feedback"))
    runtime = _mapping(sanitized.get("runtime_selection"))
    if not sanitized:
        report["blockers"].append({"check": "sanitized_request", "reason": "missing sanitized_request"})
    if not quality:
        report["blockers"].append({"check": "quality_feedback", "reason": "missing quality_feedback"})
    output_contract_feedback = _mapping(quality.get("output_contract_feedback"))
    report["task"] = sanitized.get("task")
    report["intent"] = sanitized.get("intent")
    report["classification"] = sanitized.get("classification")
    report["expected_output"] = sanitized.get("expected_output")
    report["observed"] = {
        "recipe": runtime.get("observed_recipe"),
        "tuple": runtime.get("observed_tuple"),
        "tuple_model": runtime.get("observed_tuple_model"),
        "output_validated": runtime.get("output_validated"),
    }
    report["quality_feedback"] = {
        "kind": quality.get("kind"),
        "observed_output_kind": quality.get("observed_output_kind"),
        "reason": quality.get("reason"),
    }
    if output_contract_feedback:
        report["output_contract_feedback"] = {
            "response_format": output_contract_feedback.get("response_format"),
            "expected_json_schema_present": bool(output_contract_feedback.get("expected_json_schema")),
            "observed_json_schema_present": bool(output_contract_feedback.get("observed_json_schema")),
            "schema_success_count": output_contract_feedback.get("schema_success_count"),
            "schema_failure_count": output_contract_feedback.get("schema_failure_count"),
            "raw_output_forwarded": bool(output_contract_feedback.get("raw_output_forwarded")),
        }
        if output_contract_feedback.get("raw_output_forwarded"):
            report["blockers"].append({"check": "output_contract_feedback", "reason": "raw output must not be forwarded"})
    if config_dir and sanitized.get("task"):
        try:
            config = load_config(Path(config_dir))
            matching = [
                recipe.name
                for recipe in config.recipes.values()
                if str(recipe.task) == str(sanitized.get("task")) and (not sanitized.get("intent") or recipe.intent == normalize_intent(str(sanitized.get("intent"))))
            ]
            report["matching_recipes"] = sorted(matching)
        except Exception as exc:
            report["warnings"].append({"check": "config", "reason": str(exc)})
    if quality.get("kind") in {"schema_mismatch", "schema_noncompliance", "missing_required_json_field", "malformed_business_output"}:
        report["next_actions"].append("review structured-output schema adherence for the observed tuple")
        report["next_actions"].append("consider recipe schema tightening or promotion of a stronger JSON-capable tuple")
    else:
        report["next_actions"].append("review observed tuple quality evidence before changing production routing")
    if report["blockers"]:
        report["decision"] = "REJECT"
    return report


def quality_feedback_status(feedback_id: str, inbox_dir: str | Path, *, index_db: str | Path | None = None) -> dict[str, Any]:
    inbox = Path(inbox_dir)
    db_path = Path(index_db) if index_db is not None else default_quality_feedback_index_path(inbox)
    index_record: dict[str, Any] | None = None
    if db_path.exists():
        index_record = QualityFeedbackIndex(db_path).get(feedback_id)
    candidates = [
        ("pending", inbox / f"{feedback_id}.json"),
        ("processed", inbox / "processed" / f"{feedback_id}.json"),
        ("failed", inbox / "failed" / f"{feedback_id}.json"),
    ]
    for state, path in candidates:
        if path.exists():
            result: dict[str, Any] = {"feedback_id": feedback_id, "state": state, "path": str(path)}
            if index_record is not None:
                result["index_record"] = index_record
            report_path = inbox / "reports" / f"{feedback_id}.report.json"
            if report_path.exists():
                result["report_path"] = str(report_path)
                try:
                    result["report"] = json.loads(report_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    result["report_error"] = "invalid report JSON"
            return result
    if index_record is not None:
        result = {"feedback_id": feedback_id, "state": index_record.get("status") or "indexed", "path": index_record.get("original_path"), "index_record": index_record}
        report_path = index_record.get("report_path")
        if report_path and Path(str(report_path)).exists():
            result["report_path"] = str(report_path)
            try:
                result["report"] = json.loads(Path(str(report_path)).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                result["report_error"] = "invalid report JSON"
        return result
    return {"feedback_id": feedback_id, "state": "missing"}


def quality_inbox_command_report(
    *,
    action: str,
    inbox_dir: str | Path,
    feedback_id: str | None = None,
    index_db: str | Path | None = None,
    config_dir: str | Path | None = None,
) -> dict[str, Any]:
    inbox = Path(inbox_dir)
    db_path = Path(index_db) if index_db else default_quality_feedback_index_path(inbox)
    if action == "list":
        rows = QualityFeedbackIndex(db_path).list() if db_path.exists() else []
        return {"phase": "quality-feedback-inbox-list", "inbox_dir": str(inbox), "index_db": str(db_path), "feedback": rows}
    if action == "status":
        if not feedback_id:
            raise SystemExit("quality-inbox status requires --feedback-id")
        return quality_feedback_status(feedback_id, inbox, index_db=db_path)
    if action == "process":
        return {
            "phase": "quality-feedback-inbox-process",
            "processed": process_quality_inbox(inbox_dir=inbox, index_db=db_path, config_dir=config_dir),
        }
    raise AssertionError(action)


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


def _load_json_file(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("input JSON must be an object")
    return data


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


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
