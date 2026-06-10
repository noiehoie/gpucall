from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gpucall_recipe_draft.core import draft_from_intake


SOURCE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}
EXCLUDED_DIRS = {".git", ".gpucall-migration", ".venv", "node_modules", "dist", "build", "__pycache__"}
MAX_FILE_BYTES = 1_000_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpucall-migrate")
    sub = parser.add_subparsers(dest="command", required=True)

    assess = sub.add_parser("assess")
    assess.add_argument("project", type=Path)
    assess.add_argument("--source")
    assess.add_argument("--output-dir", type=Path)

    trace = sub.add_parser("trace")
    trace.add_argument("project", type=Path)
    trace.add_argument("--command", dest="run_command")
    trace.add_argument("--backend")
    trace.add_argument("--source")
    trace.add_argument("--output-dir", type=Path)
    trace.add_argument("--timeout-seconds", type=float, default=1800.0)

    profile = sub.add_parser("profile")
    profile.add_argument("project", type=Path)
    profile.add_argument("--trace", dest="trace_paths", action="append", type=Path, default=[])
    profile.add_argument("--source")
    profile.add_argument("--output-dir", type=Path)

    contract = sub.add_parser("draft-contract")
    contract.add_argument("project", type=Path)
    contract.add_argument("--profile", type=Path)
    contract.add_argument("--source")
    contract.add_argument("--output-dir", type=Path)
    contract.add_argument("--write-intake", action="store_true")

    preflight = sub.add_parser("preflight")
    preflight.add_argument("project", type=Path)
    preflight.add_argument("--source")
    preflight.add_argument("--output-dir", type=Path)

    args = parser.parse_args(argv)
    out = args.output_dir or args.project / ".gpucall-migration"

    if args.command == "assess":
        return _write(out, "assessment", assess_project(args.project, source=args.source))
    if args.command == "trace":
        return _write(
            out,
            "workload-trace",
            trace_project(
                args.project,
                command=args.run_command,
                backend=args.backend,
                source=args.source,
                timeout_seconds=args.timeout_seconds,
            ),
        )
    if args.command == "profile":
        return _write(out, "workload-profile", profile_project(args.project, args.trace_paths, source=args.source))
    if args.command == "draft-contract":
        report = draft_contract_project(args.project, profile_path=args.profile, source=args.source)
        _write(out, "workload-contract", report)
        if args.write_intake:
            if report.get("workloads"):
                intake = recipe_intake_from_contract(report)
                draft = draft_from_intake(intake)
                _write(out, "recipe-intake", intake)
                _write(out, "recipe-draft", draft)
                _write(out, "recipe-intakes", {"schema_version": 1, "phase": "recipe-intake-bundle", "count": 1, "intakes": [intake]})
                _write(out, "recipe-drafts", {"schema_version": 1, "phase": "recipe-draft-bundle", "count": 1, "drafts": [draft]})
            else:
                _write(out, "recipe-intakes", {"schema_version": 1, "phase": "recipe-intake-bundle", "count": 0, "intakes": []})
                _write(out, "recipe-drafts", {"schema_version": 1, "phase": "recipe-draft-bundle", "count": 0, "drafts": []})
        return 0
    if args.command == "preflight":
        return _write(out, "preflight", preflight_project(args.project, source=args.source))
    raise AssertionError(args.command)


def assess_project(project: Path, *, source: str | None = None) -> dict[str, Any]:
    root = project.resolve()
    findings: list[dict[str, Any]] = []
    for path in _iter_source_files(root):
        rel = str(path.relative_to(root))
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_no, line in enumerate(text.splitlines(), start=1):
            kind = _line_kind(line)
            if kind:
                findings.append({"path": rel, "line": line_no, "kind": kind, "detail": _bounded(line)})
    return {
        "schema_version": 1,
        "phase": "migration-assessment",
        "source": source,
        "project": str(root),
        "generated_at": datetime.now(UTC).isoformat(),
        "summary": _summary(findings),
        "findings": findings,
        "preflight_required": bool(findings),
        "direct_provider_paths": [item for item in findings if item["kind"] in {"anthropic_direct", "openai_direct", "hosted_ai_direct"}],
        "gpucall_paths": [item for item in findings if item["kind"] == "gpucall_path"],
    }


def trace_project(
    project: Path,
    *,
    command: str | None,
    backend: str | None = None,
    source: str | None = None,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    root = project.resolve()
    if not command:
        return {
            "schema_version": 1,
            "phase": "workload-trace",
            "source": source,
            "backend": backend,
            "project": str(root),
            "ran": False,
            "reason": "no command supplied",
            "metrics": {},
            "redaction_report": {"raw_log_forwarded": False},
        }
    started = time.time()
    timed_out = False
    returncode: int | None
    stdout = ""
    stderr = ""
    try:
        completed = subprocess.run(
            shlex.split(command),
            cwd=root,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = None
        stdout = _process_output(exc.stdout)
        stderr = _process_output(exc.stderr)
    combined = stdout + "\n" + stderr
    return {
        "schema_version": 1,
        "phase": "workload-trace",
        "source": source,
        "backend": backend,
        "project": str(root),
        "ran": True,
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 3),
        "metrics": _trace_metrics(combined, returncode=returncode),
        "redaction_report": {"raw_log_forwarded": False},
    }


def profile_project(project: Path, trace_paths: list[Path], *, source: str | None = None) -> dict[str, Any]:
    assessment = assess_project(project, source=source)
    traces = [_load_json(path) for path in trace_paths if path.exists()]
    task, intent, mode, context_budget = _classify_workload(assessment, traces, source=source)
    return {
        "schema_version": 1,
        "phase": "workload-profile",
        "source": source,
        "project": str(project.resolve()),
        "generated_at": datetime.now(UTC).isoformat(),
        "assessment_summary": assessment["summary"],
        "trace_count": len(traces),
        "workloads": [
            {
                "id": f"{task}.{intent}",
                "task": task,
                "intent": intent,
                "modes": [mode],
                "input_profile": {"context_budget_tokens": context_budget},
                "output_profile": {"output_contract": "json_object" if intent == "rank_text_items" else "plain_text"},
                "quality_contract": {
                    "gateway_may_infer_quality": False,
                    "metrics": _quality_metrics(intent, traces),
                },
            }
        ],
    }


def draft_contract_project(project: Path, *, profile_path: Path | None, source: str | None = None) -> dict[str, Any]:
    profile = _load_json(profile_path) if profile_path else profile_project(project, [], source=source)
    workloads = profile.get("workloads") if isinstance(profile.get("workloads"), list) else []
    return {
        "schema_version": 1,
        "phase": "workload-contract",
        "source": source or profile.get("source"),
        "workloads": workloads,
    }


def preflight_project(project: Path, *, source: str | None = None) -> dict[str, Any]:
    profile = profile_project(project, [], source=source)
    requests = []
    for workload in profile.get("workloads", []):
        task = workload.get("task")
        intent = workload.get("intent")
        context_budget = workload.get("input_profile", {}).get("context_budget_tokens")
        requests.append(
            {
                "task": task,
                "mode": (workload.get("modes") or ["sync"])[0],
                "intent": intent,
                "classification": "confidential",
                "expected_output": workload.get("output_profile", {}).get("output_contract", "plain_text"),
                "content_types": ["image/png"] if task == "vision" else ["text/plain"],
                "context_budget_tokens": context_budget,
            }
        )
    return {"schema_version": 1, "phase": "migration-preflight", "source": source, "requests": requests}


def recipe_intake_from_contract(contract: dict[str, Any]) -> dict[str, Any]:
    workload = (contract.get("workloads") or [{}])[0]
    output_contract = workload.get("output_profile", {}).get("output_contract", "plain_text")
    context_budget_tokens = workload.get("input_profile", {}).get("context_budget_tokens")
    return {
        "schema_version": 1,
        "phase": "deterministic-contract-intake",
        "llm_safe": True,
        "sanitized_request": {
            "task": workload.get("task", "infer"),
            "mode": (workload.get("modes") or ["sync"])[0],
            "intent": workload.get("intent", "summarize_text"),
            "business_need": f"materialized from workload contract {workload.get('id')}",
            "classification": "confidential",
            "expected_output": output_contract,
            "error": {
                "code": None,
                "detail_kind": "workload_contract",
                "context": {
                    "context_budget_tokens": context_budget_tokens,
                    "largest_auto_recipe_context_budget_tokens": None,
                },
                "rejections": [],
            },
            "input_summary": {
                "content_types": ["image/png"] if workload.get("task") == "vision" else ["text/plain"],
                "max_bytes": workload.get("input_profile", {}).get("max_bytes"),
                "input_count": workload.get("input_profile", {}).get("input_count"),
                "prompt_lengths": [],
                "raw_payload_forwarded": False,
            },
            "desired_capabilities": _capabilities_for(workload.get("task", "infer"), workload.get("intent", "summarize_text")),
            "quality_contract": workload.get("quality_contract", {}),
        },
        "workload_contract": workload,
        "redaction_report": {
            "removed_fields": [],
            "prompt_body_forwarded": False,
            "message_content_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
            "output_body_forwarded": False,
            "raw_log_forwarded": False,
        },
        "redacted_error_payload": {},
    }


def _capabilities_for(task: str, intent: str) -> list[str]:
    if task == "vision":
        return ["document_understanding", "visual_question_answering", "instruction_following"]
    if intent == "extract_json":
        return ["structured_output"]
    if intent == "translate_text":
        return ["translation"]
    if intent == "rank_text_items":
        return ["instruction_following", "reasoning", "structured_output"]
    return ["summarization"]


def _iter_source_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        if any(part in EXCLUDED_DIRS for part in path.relative_to(root).parts):
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _line_kind(line: str) -> str | None:
    lower = line.lower()
    if "gpucall" in lower:
        return "gpucall_path"
    if "anthropic" in lower:
        return "anthropic_direct"
    if "openai" in lower:
        return "openai_direct"
    if any(token in lower for token in ("gemini", "claude", "hosted ai", "hosted-ai")):
        return "hosted_ai_direct"
    if any(token in lower for token in ("ollama", "local model", "local-model")):
        return "local_model_path"
    if any(token in lower for token in ("call_llm", " llm", "language model")):
        return "llm_path"
    if any(token in lower for token in ("vision", "ocr", "image analysis", "画像", "画像解析")):
        return "vision_path"
    if "embedding" in lower:
        return "embedding_path"
    return None


def _summary(findings: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in findings:
        counts[str(item["kind"])] = counts.get(str(item["kind"]), 0) + 1
    return counts


def _classify_workload(
    assessment: dict[str, Any],
    traces: list[dict[str, Any]],
    *,
    source: str | None = None,
) -> tuple[str, str, str, int]:
    text = json.dumps({"source": source, "assessment": assessment, "traces": traces}, ensure_ascii=False).lower()
    if any(token in text for token in ("vision", "ocr", "image", "画像")):
        return "vision", "understand_document_image", "sync", 8192
    if any(token in text for token in ("news", "rss", "article", "topic", "rank", "記事")):
        return "infer", "rank_text_items", "async", 131072
    return "infer", "summarize_text", "sync", 32768


def _quality_metrics(intent: str, traces: list[dict[str, Any]]) -> dict[str, Any]:
    successes = sum(1 for trace in traces if trace.get("metrics", {}).get("success") is True)
    metrics: dict[str, Any] = {"baseline_success_count": successes}
    if intent == "rank_text_items":
        metrics["min_topics"] = 3
    return metrics


def _trace_metrics(text: str, *, returncode: int | None) -> dict[str, Any]:
    return {
        "success": returncode == 0,
        "response_chars": len(text),
        "json_object_mentions": len(re.findall(r"\{.*\}", text)),
        "error_mentions": len(re.findall(r"error|exception|traceback|failed", text, flags=re.IGNORECASE)),
    }


def _write(output_dir: Path, stem: str, data: dict[str, Any]) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(data), encoding="utf-8")
    sys.stdout.write(str(json_path) + "\n" + str(md_path) + "\n")
    return 0


def _markdown(data: dict[str, Any]) -> str:
    lines = [f"# {data.get('phase', 'gpucall migration')}", ""]
    for key in ("source", "project"):
        if data.get(key):
            lines.append(f"- {key}: `{data[key]}`")
    if isinstance(data.get("summary"), dict):
        lines.append("")
        lines.append("## Summary")
        for key, value in sorted(data["summary"].items()):
            lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def _load_json(path: Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit(f"{path} must contain a JSON object")
    return data


def _bounded(value: str) -> str:
    return value.strip()[:300]


def _process_output(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


if __name__ == "__main__":
    raise SystemExit(main())
