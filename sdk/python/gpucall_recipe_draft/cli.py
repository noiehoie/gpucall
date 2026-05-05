from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from gpucall_recipe_draft.core import DraftInputs, draft_from_intake, dumps_json, intake_from_error, llm_prompt_from_intake
from gpucall_recipe_draft.llm import DEFAULT_CONFIG_PATH, draft_with_llm, load_llm_config, write_default_config


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

    draft = subcommands.add_parser("draft", help="create a human-reviewed recipe/provider draft from sanitized intake JSON")
    draft.add_argument("--input", "-i", required=True, help="path to sanitized intake JSON, or '-' for stdin")
    draft.add_argument("--output", "-o", help="write draft JSON to this path")

    llm_prompt = subcommands.add_parser("llm-prompt", help="emit a safe prompt for an approved LLM using sanitized intake only")
    llm_prompt.add_argument("--input", "-i", required=True, help="path to sanitized intake JSON, or '-' for stdin")
    llm_prompt.add_argument("--output", "-o", help="write prompt text to this path")

    init_config = subcommands.add_parser("init-config", help="write a user-controlled LLM config template")
    init_config.add_argument("--output", "-o", default=str(DEFAULT_CONFIG_PATH))
    init_config.add_argument("--force", action="store_true")

    draft_llm = subcommands.add_parser("draft-llm", help="call the user-configured LLM with sanitized intake only")
    draft_llm.add_argument("--input", "-i", required=True, help="path to sanitized intake JSON, or '-' for stdin")
    draft_llm.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="path to recipe-draft LLM JSON config")
    draft_llm.add_argument("--output", "-o", help="write LLM draft JSON to this path")

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
        return 0
    if args.command == "draft":
        result = draft_from_intake(_load_json(args.input))
        _write_json(result, args.output)
        return 0
    if args.command == "llm-prompt":
        result = llm_prompt_from_intake(_load_json(args.input))
        if args.output:
            Path(args.output).write_text(result, encoding="utf-8")
        else:
            sys.stdout.write(result)
        return 0
    if args.command == "init-config":
        path = write_default_config(args.output, force=args.force)
        sys.stdout.write(str(path) + "\n")
        return 0
    if args.command == "draft-llm":
        result = draft_with_llm(_load_json(args.input), load_llm_config(args.config))
        _write_json(result, args.output)
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


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
