from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml


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

    process = subcommands.add_parser("process-inbox", help="process file-based recipe request submissions once")
    process.add_argument("--inbox-dir", required=True)
    process.add_argument("--output-dir", required=True)
    process.add_argument("--processed-dir")
    process.add_argument("--failed-dir")
    process.add_argument("--report-dir")
    process.add_argument("--accept-all", action="store_true")
    process.add_argument("--force", action="store_true")

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


def process_inbox(
    *,
    inbox_dir: str | Path,
    output_dir: str | Path,
    processed_dir: str | Path | None = None,
    failed_dir: str | Path | None = None,
    report_dir: str | Path | None = None,
    force: bool = False,
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
            artifact = _artifact_from_submission(_load_json(str(path)))
            recipe = canonical_recipe_from_artifact(artifact)
            report = materialization_report(artifact, recipe)
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
    for candidate in (8192, 32768, 65536, 131072, 262144, 524288, 1048576):
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
