from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from gpucall_recipe_draft.core import (
    DraftInputs,
    PreflightInputs,
    QualityFeedbackInputs,
    compare_preflight_to_failure,
    draft_from_intake,
    dumps_json,
    intake_from_error,
    intake_from_preflight,
    intake_from_quality_feedback,
)
from gpucall_recipe_draft.submit import build_submission_bundle, submit_bundle, submit_bundle_to_remote


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpucall-recipe-draft")
    subcommands = parser.add_subparsers(dest="command", required=True)

    intake = subcommands.add_parser("intake", help="sanitize a gpucall failure payload without using an LLM")
    intake.add_argument("--error", required=True, help="path to gpucall error JSON, or '-' for stdin")
    intake.add_argument("--task")
    intake.add_argument("--mode")
    intake.add_argument("--intent")
    intake.add_argument("--business-need", default="")
    intake.add_argument("--classification", default="confidential")
    intake.add_argument("--expected-output")
    intake.add_argument("--output", "-o", help="write sanitized intake JSON to this path")
    intake.add_argument("--inbox-dir", help="also submit the sanitized intake to this file-based admin inbox")
    intake.add_argument("--remote-inbox", help="also submit to USER@HOST:/absolute/admin/inbox over SSH")
    intake.add_argument("--source", help="caller/source label for automatic submission")

    draft = subcommands.add_parser("draft", help="create a human-reviewed recipe/provider draft from sanitized intake JSON")
    draft.add_argument("--input", "-i", required=True, help="path to sanitized intake JSON, or '-' for stdin")
    draft.add_argument("--output", "-o", help="write draft JSON to this path")

    preflight = subcommands.add_parser("preflight", help="create sanitized intake before running an unknown workload")
    preflight.add_argument("--task", required=True)
    preflight.add_argument("--mode", default="sync")
    preflight.add_argument("--intent")
    preflight.add_argument("--business-need", default="")
    preflight.add_argument("--classification", default="confidential")
    preflight.add_argument("--expected-output", default="plain_text")
    preflight.add_argument("--content-type", action="append", default=[])
    preflight.add_argument("--bytes", dest="byte_values", action="append", type=int, default=[])
    preflight.add_argument("--required-model-len", type=int)
    preflight.add_argument("--output", "-o", help="write preflight intake JSON to this path")
    preflight.add_argument("--inbox-dir", help="also submit the sanitized intake to this file-based admin inbox")
    preflight.add_argument("--remote-inbox", help="also submit to USER@HOST:/absolute/admin/inbox over SSH")
    preflight.add_argument("--source", help="caller/source label for automatic submission")

    quality = subcommands.add_parser(
        "quality",
        help="create sanitized intake for a 200 OK result that failed caller-side business quality checks",
    )
    quality.add_argument("--task", required=True)
    quality.add_argument("--mode", default="sync")
    quality.add_argument("--intent")
    quality.add_argument("--business-need", default="")
    quality.add_argument("--classification", default="confidential")
    quality.add_argument("--expected-output", default="plain_text")
    quality.add_argument("--content-type", action="append", default=[])
    quality.add_argument("--bytes", dest="byte_values", action="append", type=int, default=[])
    quality.add_argument("--dimension", action="append", default=[], help="input dimensions such as 1200x2287; never pass raw media")
    quality.add_argument("--required-model-len", type=int)
    quality.add_argument("--selected-recipe")
    quality.add_argument("--selected-provider")
    quality.add_argument("--selected-provider-model")
    quality.add_argument("--output-validated", choices=["true", "false", "unknown"], default="unknown")
    quality.add_argument("--quality-failure-kind", default="low_quality_success")
    quality.add_argument("--quality-failure-reason", default="")
    quality.add_argument("--observed-output-kind", default="")
    quality.add_argument("--output", "-o", help="write quality feedback intake JSON to this path")
    quality.add_argument("--inbox-dir", help="also submit the sanitized intake to this file-based admin inbox")
    quality.add_argument("--remote-inbox", help="also submit to USER@HOST:/absolute/admin/inbox over SSH")
    quality.add_argument("--source", help="caller/source label for automatic submission")

    compare = subcommands.add_parser("compare", help="compare a preflight intake with a post-failure intake")
    compare.add_argument("--preflight", required=True)
    compare.add_argument("--failure", required=True)
    compare.add_argument("--output", "-o")

    submit = subcommands.add_parser("submit", help="submit sanitized intake/draft to a file-based gpucall recipe request inbox")
    submit.add_argument("--intake", required=True, help="path to sanitized intake JSON")
    submit.add_argument("--draft", help="path to deterministic draft JSON")
    submit.add_argument("--inbox-dir", help="shared inbox directory managed outside the gpucall API")
    submit.add_argument("--remote-inbox", help="submit to USER@HOST:/absolute/admin/inbox over SSH")
    submit.add_argument("--source", help="caller/source label to include in the submission bundle")

    args = parser.parse_args(argv)
    if args.command == "intake":
        error_payload = _load_json(args.error)
        result = intake_from_error(
            DraftInputs(
                error_payload=error_payload,
                task=args.task,
                mode=args.mode,
                intent=args.intent,
                business_need=args.business_need,
                classification=args.classification,
                expected_output=args.expected_output,
            )
        )
        _write_json(result, args.output)
        _submit_if_requested(result, args.inbox_dir, args.remote_inbox, args.source)
        return 0
    if args.command == "draft":
        result = draft_from_intake(_load_json(args.input))
        _write_json(result, args.output)
        return 0
    if args.command == "preflight":
        result = intake_from_preflight(
            PreflightInputs(
                task=args.task,
                mode=args.mode,
                intent=args.intent,
                business_need=args.business_need,
                classification=args.classification,
                expected_output=args.expected_output,
                content_types=tuple(args.content_type),
                byte_values=tuple(args.byte_values),
                required_model_len=args.required_model_len,
            )
        )
        _write_json(result, args.output)
        _submit_if_requested(result, args.inbox_dir, args.remote_inbox, args.source)
        return 0
    if args.command == "quality":
        result = intake_from_quality_feedback(
            QualityFeedbackInputs(
                task=args.task,
                mode=args.mode,
                intent=args.intent,
                business_need=args.business_need,
                classification=args.classification,
                expected_output=args.expected_output,
                content_types=tuple(args.content_type),
                byte_values=tuple(args.byte_values),
                dimensions=tuple(args.dimension),
                required_model_len=args.required_model_len,
                selected_recipe=args.selected_recipe,
                selected_provider=args.selected_provider,
                selected_provider_model=args.selected_provider_model,
                output_validated=_parse_bool(args.output_validated),
                quality_failure_kind=args.quality_failure_kind,
                quality_failure_reason=args.quality_failure_reason,
                observed_output_kind=args.observed_output_kind,
            )
        )
        _write_json(result, args.output)
        _submit_if_requested(result, args.inbox_dir, args.remote_inbox, args.source)
        return 0
    if args.command == "compare":
        result = compare_preflight_to_failure(_load_json(args.preflight), _load_json(args.failure))
        _write_json(result, args.output)
        return 0
    if args.command == "submit":
        bundle = build_submission_bundle(
            intake=_load_json(args.intake),
            draft=_load_json(args.draft) if args.draft else None,
            source=args.source,
        )
        paths = _submit_bundle(bundle, args.inbox_dir, args.remote_inbox)
        if not paths:
            raise SystemExit("submit requires --inbox-dir or --remote-inbox")
        sys.stdout.write("\n".join(paths) + "\n")
        return 0
    raise AssertionError(args.command)


def _load_json(path: str) -> dict[str, Any]:
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise SystemExit("input JSON must be an object")
    return data


def _write_json(data: dict[str, Any], output: str | None) -> None:
    text = dumps_json(data)
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


def _submit_if_requested(
    intake: dict[str, Any],
    inbox_dir: str | None,
    remote_inbox: str | None,
    source: str | None,
) -> None:
    if not inbox_dir and not remote_inbox:
        return
    bundle = build_submission_bundle(intake=intake, draft=None, source=source)
    for path in _submit_bundle(bundle, inbox_dir, remote_inbox):
        sys.stderr.write(str(path) + "\n")


def _submit_bundle(bundle: dict[str, Any], inbox_dir: str | None, remote_inbox: str | None) -> list[str]:
    paths: list[str] = []
    if inbox_dir:
        paths.append(str(submit_bundle(bundle, inbox_dir)))
    if remote_inbox:
        paths.append(submit_bundle_to_remote(bundle, remote_inbox))
    return paths


def _parse_bool(value: str) -> bool | None:
    if value == "true":
        return True
    if value == "false":
        return False
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
