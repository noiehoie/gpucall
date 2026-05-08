from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


EXCLUDED_DIRS = {".git", ".hg", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".venv", "build", "dist", "node_modules", "__pycache__"}
SOURCE_SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}
MAX_FILE_BYTES = 1_000_000


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    kind: str
    symbol: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {"path": self.path, "line": self.line, "kind": self.kind, "symbol": self.symbol, "detail": self.detail}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gpucall-migrate")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("assess", "preflight", "report", "canary", "patch", "onboard"):
        cmd = sub.add_parser(name)
        cmd.add_argument("project", type=Path)
        cmd.add_argument("--output-dir", type=Path, default=None)
        cmd.add_argument("--source", default=None)
        cmd.add_argument("--remote-inbox", default=None)
        cmd.add_argument("--inbox-dir", default=None)
        cmd.add_argument("--command", dest="run_command", default=None, help="canary/onboard command to run inside the project")
        cmd.add_argument("--apply", action="store_true", help="reserved for future patch application; current command writes patches only")
    args = parser.parse_args(argv)
    output_dir = args.output_dir or (args.project / ".gpucall-migration")
    if args.command == "assess":
        report = assess_project(args.project, source=args.source)
        _write_outputs(report, output_dir, "migration-report")
        return 0
    if args.command == "preflight":
        report = assess_project(args.project, source=args.source)
        preflights = build_preflight_requests(report, source=args.source)
        _write_outputs({"schema_version": 1, "phase": "migration-preflight", "requests": preflights}, output_dir, "preflight")
        return 0
    if args.command == "report":
        report = assess_project(args.project, source=args.source)
        report["preflight_requests"] = build_preflight_requests(report, source=args.source)
        _write_outputs(report, output_dir, "migration-report")
        return 0
    if args.command == "canary":
        report = canary_project(args.project, command=args.run_command, source=args.source)
        _write_outputs(report, output_dir, "canary-report")
        return 0
    if args.command == "patch":
        report = patch_suggestions(args.project, source=args.source)
        _write_outputs(report, output_dir, "migration-patch")
        return 0
    if args.command == "onboard":
        report = assess_project(args.project, source=args.source)
        report["preflight_requests"] = build_preflight_requests(report, source=args.source)
        report["patch_suggestions"] = patch_suggestions(args.project, source=args.source)["patches"]
        if args.run_command:
            report["canary"] = canary_project(args.project, command=args.run_command, source=args.source)
        report["phase"] = "migration-onboard"
        _write_outputs(report, output_dir, "onboard-report")
        return 0
    raise AssertionError(args.command)


def assess_project(project: Path, *, source: str | None = None) -> dict[str, Any]:
    root = project.resolve()
    findings: list[Finding] = []
    for path in _iter_source_files(root):
        rel = str(path.relative_to(root))
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            findings.extend(_line_findings(rel, number, line))
    rows = [item.as_dict() for item in findings]
    return {
        "schema_version": 1,
        "phase": "migration-assessment",
        "source": source,
        "project": str(root),
        "summary": _summary(rows),
        "findings": rows,
        "preflight_required": _preflight_required(rows),
        "caller_routing_violations": [row for row in rows if row["kind"] == "caller_routing_selector"],
        "direct_provider_paths": [row for row in rows if row["kind"] in {"anthropic_direct", "openai_direct", "provider_model_literal"}],
        "gpucall_paths": [row for row in rows if row["kind"] == "gpucall_path"],
    }


def build_preflight_requests(report: dict[str, Any], *, source: str | None = None) -> list[dict[str, Any]]:
    requests: dict[tuple[str, str], dict[str, Any]] = {}
    for item in report.get("findings", []):
        task, intent, required_len = _workload_guess(str(item.get("path", "")), str(item.get("symbol", "")), str(item.get("detail", "")))
        if not intent:
            continue
        key = (task, intent)
        requests[key] = {
            "task": task,
            "intent": intent,
            "mode": "sync",
            "source": source or report.get("source"),
            "business_need": f"detected by gpucall-migrate at {item.get('path')}:{item.get('line')}",
            "content_type": "image/png" if task == "vision" else "text/plain",
            "bytes": 2_000_000 if task == "vision" else 8_000,
            "required_model_len": required_len,
            "command": _preflight_command(task, intent, required_len, source=source or report.get("source")),
        }
    return [requests[key] for key in sorted(requests)]


def canary_project(project: Path, *, command: str | None, source: str | None = None) -> dict[str, Any]:
    started = time.time()
    if not command:
        return {"schema_version": 1, "phase": "migration-canary", "source": source, "project": str(project.resolve()), "ran": False, "reason": "no command supplied"}
    result = subprocess.run(command, cwd=project, shell=True, capture_output=True, text=True, timeout=None)
    output = result.stdout + "\n" + result.stderr
    return {
        "schema_version": 1,
        "phase": "migration-canary",
        "source": source,
        "project": str(project.resolve()),
        "ran": True,
        "command": command,
        "returncode": result.returncode,
        "duration_seconds": round(time.time() - started, 3),
        "error_codes": _error_code_counts(output),
        "timeout_count": len(re.findall(r"Timeout|TimeoutException|timed out", output)),
        "circuit_breaker_mentions": len(re.findall(r"circuit|CB_", output, flags=re.IGNORECASE)),
        "stdout_tail": result.stdout[-8000:],
        "stderr_tail": result.stderr[-8000:],
    }


def patch_suggestions(project: Path, *, source: str | None = None) -> dict[str, Any]:
    report = assess_project(project, source=source)
    patches = []
    for row in report["direct_provider_paths"]:
        patches.append(
            {
                "path": row["path"],
                "line": row["line"],
                "kind": "manual_patch_required",
                "reason": "direct provider path should be routed through gpucall SDK/OpenAI facade or converted into preflight intake",
                "suggestion": "replace provider SDK call with GPUCallClient/OpenAI base_url gpucall endpoint; do not pass provider/model/GPU selectors",
            }
        )
    return {"schema_version": 1, "phase": "migration-patch", "source": source, "project": str(project.resolve()), "patches": patches, "applied": False}


def _iter_source_files(root: Path):
    for path in root.rglob("*"):
        if any(part in EXCLUDED_DIRS for part in path.parts):
            continue
        if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
            continue
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        yield path


def _line_findings(path: str, line_number: int, line: str) -> list[Finding]:
    lower = line.lower()
    findings: list[Finding] = []
    if "anthropic" in lower or "claude" in lower:
        findings.append(Finding(path, line_number, "anthropic_direct", "anthropic_or_claude", line.strip()[:240]))
    if "openai" in lower and "gpucall" not in lower:
        findings.append(Finding(path, line_number, "openai_direct", "openai", line.strip()[:240]))
    if "gpucall" in lower:
        findings.append(Finding(path, line_number, "gpucall_path", "gpucall", line.strip()[:240]))
    if re.search(r"\b(recipe|requested_tuple|requested_gpu|provider|requested_provider)\s*=", line):
        findings.append(Finding(path, line_number, "caller_routing_selector", "routing_selector", line.strip()[:240]))
    if re.search(r"model\s*=\s*[\"'][^\"']+(claude|gpt|qwen|llama|mistral|haiku|sonnet)", line, re.IGNORECASE):
        findings.append(Finding(path, line_number, "provider_model_literal", "model_literal", line.strip()[:240]))
    if re.search(r"call_llm|call_claude|chat\.completions|messages\.create|generate", line):
        findings.append(Finding(path, line_number, "llm_call_candidate", "llm_call", line.strip()[:240]))
    return findings


def _summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row["kind"])] = counts.get(str(row["kind"]), 0) + 1
    return counts


def _preflight_required(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    required = []
    for row in rows:
        task, intent, _required_len = _workload_guess(str(row.get("path", "")), str(row.get("symbol", "")), str(row.get("detail", "")))
        if intent:
            required.append({"path": str(row["path"]), "line": str(row["line"]), "task": task, "intent": intent})
    return required


def _workload_guess(path: str, symbol: str, detail: str) -> tuple[str, str | None, int]:
    text = " ".join([path, symbol, detail]).lower()
    if "translate" in text or "translation" in text:
        return "infer", "translate_text", 32768
    if "vision" in text or "image" in text or "ocr" in text:
        return "vision", "understand_document_image", 8192
    if "summary" in text or "summarize" in text or "topic" in text:
        return "infer", "summarize_text", 1_048_576 if "topic" in text else 65536
    return "infer", None, 32768


def _preflight_command(task: str, intent: str, required_len: int, *, source: str | None) -> str:
    parts = [
        "gpucall-recipe-draft",
        "preflight",
        "--task",
        task,
        "--intent",
        intent,
        "--content-type",
        "image/png" if task == "vision" else "text/plain",
        "--bytes",
        "2000000" if task == "vision" else "8000",
        "--required-model-len",
        str(required_len),
    ]
    if source:
        parts.extend(["--source", source])
    return " ".join(shlex.quote(item) for item in parts)


def _error_code_counts(text: str) -> dict[str, int]:
    codes = ["NO_AUTO_SELECTABLE_RECIPE", "NO_ELIGIBLE_TUPLE", "EMPTY_OUTPUT", "MALFORMED_OUTPUT"]
    return {code: text.count(code) for code in codes}


def _write_outputs(report: dict[str, Any], output_dir: Path, stem: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    md_path = output_dir / f"{stem}.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(report), encoding="utf-8")
    sys.stdout.write(str(json_path) + "\n" + str(md_path) + "\n")


def _markdown(report: dict[str, Any]) -> str:
    lines = [f"# {report.get('phase', 'gpucall migration report')}", ""]
    lines.append(f"- project: `{report.get('project', '')}`")
    lines.append(f"- source: `{report.get('source', '')}`")
    if "summary" in report:
        lines.append("")
        lines.append("## Summary")
        for key, value in sorted(report["summary"].items()):
            lines.append(f"- `{key}`: {value}")
    if report.get("preflight_requests"):
        lines.append("")
        lines.append("## Preflight")
        for item in report["preflight_requests"]:
            lines.append(f"- `{item['task']}` / `{item['intent']}`: `{item['command']}`")
    if report.get("patches"):
        lines.append("")
        lines.append("## Patches")
        for item in report["patches"]:
            lines.append(f"- `{item['path']}:{item['line']}` {item['reason']}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
