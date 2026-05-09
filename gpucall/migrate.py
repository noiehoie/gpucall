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
        cmd.add_argument("--apply", action="store_true", help="write a local gpucall migration helper and annotate direct provider call sites")
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
        report = patch_suggestions(args.project, source=args.source, apply=args.apply)
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
    result = subprocess.run(shlex.split(command), cwd=project, shell=False, capture_output=True, text=True, timeout=1800)
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


def patch_suggestions(project: Path, *, source: str | None = None, apply: bool = False) -> dict[str, Any]:
    report = assess_project(project, source=source)
    patches = []
    changed_files: list[str] = []
    for row in report["direct_provider_paths"]:
        patches.append(
            {
                "path": row["path"],
                "line": row["line"],
                "kind": "automatic_patch_available",
                "reason": "direct provider path should be routed through gpucall SDK/OpenAI facade or converted into preflight intake",
                "suggestion": "replace provider SDK call with GPUCallClient/OpenAI base_url gpucall endpoint; do not pass provider/model/GPU selectors",
            }
        )
    if apply:
        changed_files = _apply_migration_patch(project.resolve(), report["direct_provider_paths"], source=source)
    return {
        "schema_version": 1,
        "phase": "migration-patch",
        "source": source,
        "project": str(project.resolve()),
        "patches": patches,
        "applied": apply,
        "changed_files": changed_files,
    }


def _apply_migration_patch(project: Path, rows: list[dict[str, Any]], *, source: str | None = None) -> list[str]:
    changed: set[str] = set()
    helper = project / "gpucall_migration.py"
    helper_text = _migration_helper_text(source=source)
    if not helper.exists() or helper.read_text(encoding="utf-8") != helper_text:
        helper.write_text(helper_text, encoding="utf-8")
        changed.add(str(helper.relative_to(project)))
    paths = sorted({str(row["path"]) for row in rows if str(row.get("path", "")).endswith(".py")})
    for rel in paths:
        path = project / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        marker = "# gpucall-migrate: direct provider path migrated to gpucall compatibility helpers."
        import_line = "from gpucall_migration import gpucall_client  # gpucall-migrate\n"
        updated = text
        updated = updated.replace("from anthropic import Anthropic", "from gpucall_migration import AnthropicCompat as Anthropic")
        updated = updated.replace("from anthropic import AsyncAnthropic", "from gpucall_migration import AsyncAnthropicCompat as AsyncAnthropic")
        if "from openai import OpenAI" in updated:
            updated = updated.replace("from openai import OpenAI", "from gpucall_migration import gpucall_openai_client")
            updated = re.sub(r"\bOpenAI\s*\(", "gpucall_openai_client(", updated)
        if import_line not in updated and "gpucall_migration" not in updated:
            updated = _insert_after_future_imports(updated, import_line)
        if marker not in updated:
            lines = updated.splitlines(keepends=True)
            line_numbers = sorted({int(row["line"]) for row in rows if row["path"] == rel}, reverse=True)
            for line_number in line_numbers:
                index = max(0, min(line_number - 1, len(lines)))
                lines.insert(index, marker + "\n")
            updated = "".join(lines)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed.add(rel)
    manifest = {
        "schema_version": 1,
        "source": source,
        "changed_files": sorted(changed),
        "note": "The patch adds deterministic gpucall compatibility helpers and rewrites common Anthropic/OpenAI client constructors to route through gpucall.",
    }
    manifest_dir = project / ".gpucall-migration"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "applied-patch.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    changed.add(str(manifest_path.relative_to(project)))
    return sorted(changed)


def _migration_helper_text(*, source: str | None) -> str:
    source_line = f'SOURCE = "{source}"\n' if source else 'SOURCE = None\n'
    return (
        "from __future__ import annotations\n\n"
        "import os\n\n"
        "from dataclasses import dataclass\n"
        "from typing import Any\n\n"
        "from openai import OpenAI\n"
        "from gpucall_sdk import GPUCallClient\n\n"
        f"{source_line}\n"
        "def _required_env(name: str) -> str:\n"
        "    value = os.environ.get(name)\n"
        "    if not value:\n"
        "        raise RuntimeError(f\"{name} is required for gpucall migration helper\")\n"
        "    return value\n"
        "\n\n"
        "def gpucall_client(base_url: str | None = None, api_key: str | None = None) -> GPUCallClient:\n"
        "    return GPUCallClient(\n"
        "        base_url or _required_env(\"GPUCALL_BASE_URL\"),\n"
        "        api_key=api_key or _required_env(\"GPUCALL_API_KEY\"),\n"
        "    )\n"
        "\n\n"
        "def gpucall_openai_client(*args: Any, **kwargs: Any) -> OpenAI:\n"
        "    base = os.environ.get(\"GPUCALL_OPENAI_BASE_URL\") or _required_env(\"GPUCALL_BASE_URL\")\n"
        "    kwargs.setdefault(\"base_url\", base.rstrip(\"/\") + \"/v1\")\n"
        "    kwargs.setdefault(\"api_key\", _required_env(\"GPUCALL_API_KEY\"))\n"
        "    return OpenAI(*args, **kwargs)\n"
        "\n\n"
        "@dataclass\n"
        "class _AnthropicContent:\n"
        "    text: str\n"
        "\n\n"
        "@dataclass\n"
        "class _AnthropicMessage:\n"
        "    content: list[_AnthropicContent]\n"
        "\n\n"
        "def _anthropic_prompt(messages: list[dict[str, Any]] | None) -> str:\n"
        "    parts: list[str] = []\n"
        "    for item in messages or []:\n"
        "        content = item.get(\"content\") if isinstance(item, dict) else None\n"
        "        if isinstance(content, str):\n"
        "            parts.append(content)\n"
        "        elif isinstance(content, list):\n"
        "            parts.extend(str(part.get(\"text\")) for part in content if isinstance(part, dict) and part.get(\"type\") == \"text\")\n"
        "    return \"\\n\".join(parts)\n"
        "\n\n"
        "class _AnthropicMessagesCompat:\n"
        "    def create(self, *, messages: list[dict[str, Any]] | None = None, max_tokens: int | None = None, temperature: float | None = None, **_: Any) -> _AnthropicMessage:\n"
        "        result = gpucall_client().infer(prompt=_anthropic_prompt(messages), max_tokens=max_tokens, temperature=temperature)\n"
        "        text = str(((result.get(\"result\") or {}).get(\"value\")) or result.get(\"value\") or \"\")\n"
        "        return _AnthropicMessage(content=[_AnthropicContent(text=text)])\n"
        "\n\n"
        "class AnthropicCompat:\n"
        "    def __init__(self, *_: Any, **__: Any) -> None:\n"
        "        self.messages = _AnthropicMessagesCompat()\n"
        "\n\n"
        "class AsyncAnthropicCompat(AnthropicCompat):\n"
        "    pass\n"
    )


def _insert_after_future_imports(text: str, insertion: str) -> str:
    lines = text.splitlines(keepends=True)
    index = 0
    if lines and lines[0].startswith("#!"):
        index = 1
    while index < len(lines) and (lines[index].startswith("from __future__ import") or not lines[index].strip()):
        index += 1
    lines.insert(index, insertion)
    return "".join(lines)


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
    if "rank" in text or "ranking" in text or "score" in text or "topic" in text:
        return "infer", "rank_text_items", 65536
    if "summary" in text or "summarize" in text:
        return "infer", "summarize_text", 65536
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
