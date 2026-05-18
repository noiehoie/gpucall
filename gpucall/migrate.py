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
from textwrap import dedent
from typing import Any

from gpucall.workload_contract import (
    compare_trace_to_contract,
    contract_to_recipe_intake,
    draft_workload_contract,
    load_json_file,
    merge_traces,
    parse_trace_text,
    read_trace_log,
    workload_profile_from_assessment,
)


EXCLUDED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "artifacts",
    "build",
    "dist",
    "docs",
    "logs",
    "node_modules",
    "output",
    "tasks",
    "tests",
    "__pycache__",
}
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
    for name in ("assess", "preflight", "report", "canary", "patch"):
        cmd = sub.add_parser(name)
        cmd.add_argument("project", type=Path)
        cmd.add_argument("--output-dir", type=Path, default=None)
        cmd.add_argument("--source", default=None)
        cmd.add_argument("--remote-inbox", default=None)
        cmd.add_argument("--inbox-dir", default=None)
        cmd.add_argument("--command", dest="run_command", default=None, help="canary/onboard command to run inside the project")
        cmd.add_argument("--apply", action="store_true", help="write a local gpucall migration helper and annotate direct provider call sites")
    trace = sub.add_parser("trace", help="run or parse a caller command and record sanitized workload metrics")
    trace.add_argument("project", type=Path)
    trace.add_argument("--output-dir", type=Path)
    trace.add_argument("--source")
    trace.add_argument("--backend")
    trace.add_argument("--command", dest="run_command")
    trace.add_argument("--log-file", type=Path)
    trace.add_argument("--timeout-seconds", type=float, default=1800.0)

    profile = sub.add_parser("profile", help="combine assessment and sanitized traces into a workload profile")
    profile.add_argument("project", type=Path)
    profile.add_argument("--output-dir", type=Path)
    profile.add_argument("--source")
    profile.add_argument("--trace", dest="trace_paths", action="append", type=Path, default=[])

    contract = sub.add_parser("draft-contract", help="generate deterministic workload contracts from a profile")
    contract.add_argument("project", type=Path)
    contract.add_argument("--output-dir", type=Path)
    contract.add_argument("--source")
    contract.add_argument("--profile", type=Path)
    contract.add_argument("--trace", dest="trace_paths", action="append", type=Path, default=[])
    contract.add_argument("--write-intake", action="store_true", help="also write recipe-intake.json derived from the primary workload contract")

    compare = sub.add_parser("compare", help="compare a candidate trace against a workload contract")
    compare.add_argument("project", type=Path)
    compare.add_argument("--output-dir", type=Path)
    compare.add_argument("--source")
    compare.add_argument("--contract", required=True, type=Path)
    compare.add_argument("--trace", dest="trace_paths", action="append", type=Path, default=[])
    compare.add_argument("--log-file", type=Path)
    compare.add_argument("--backend")

    onboard = sub.add_parser("onboard", help="assess, optionally trace, draft contract, and emit onboarding artifacts")
    onboard.add_argument("project", type=Path)
    onboard.add_argument("--output-dir", type=Path, default=None)
    onboard.add_argument("--source", default=None)
    onboard.add_argument("--remote-inbox", default=None)
    onboard.add_argument("--inbox-dir", default=None)
    onboard.add_argument("--command", dest="run_command", default=None, help="command to trace inside the project")
    onboard.add_argument(
        "--log-file",
        dest="log_files",
        action="append",
        type=Path,
        default=[],
        help="existing baseline log or JSON artifact to parse; repeatable",
    )
    onboard.add_argument("--backend")
    onboard.add_argument("--timeout-seconds", type=float, default=1800.0)
    onboard.add_argument("--apply", action="store_true", help="write a local gpucall migration helper and annotate direct provider call sites")
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
    if args.command == "trace":
        report = trace_project(
            args.project,
            command=args.run_command,
            log_file=args.log_file,
            source=args.source,
            backend=args.backend,
            timeout_seconds=args.timeout_seconds,
        )
        _write_outputs(report, output_dir, "workload-trace")
        return 0
    if args.command == "profile":
        report = profile_project(args.project, trace_paths=args.trace_paths, source=args.source)
        _write_outputs(report, output_dir, "workload-profile")
        return 0
    if args.command == "draft-contract":
        report = draft_contract_project(
            args.project,
            profile_path=args.profile,
            trace_paths=args.trace_paths,
            source=args.source,
        )
        _write_outputs(report, output_dir, "workload-contract")
        if args.write_intake:
            intake = contract_to_recipe_intake(report)
            _write_outputs(intake, output_dir, "recipe-intake")
        return 0
    if args.command == "compare":
        report = compare_project(
            args.project,
            contract_path=args.contract,
            trace_paths=args.trace_paths,
            log_file=args.log_file,
            source=args.source,
            backend=args.backend,
        )
        _write_outputs(report, output_dir, "contract-comparison")
        return 0
    if args.command == "onboard":
        report = onboard_project(
            args.project,
            command=args.run_command,
            log_files=args.log_files,
            source=args.source,
            backend=args.backend,
            apply=args.apply,
            timeout_seconds=args.timeout_seconds,
        )
        _write_outputs(report, output_dir, "onboard-report")
        profile_report = report.get("workload_profile")
        contract_report = report.get("workload_contract")
        recipe_intake = report.get("recipe_intake")
        if isinstance(profile_report, dict):
            _write_outputs(profile_report, output_dir, "workload-profile")
        if isinstance(contract_report, dict):
            _write_outputs(contract_report, output_dir, "workload-contract")
        if isinstance(recipe_intake, dict):
            _write_outputs(recipe_intake, output_dir, "recipe-intake")
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
    timed_out = False
    try:
        result = subprocess.run(shlex.split(command), cwd=project, shell=False, capture_output=True, text=True, timeout=1800)
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _process_output_text(exc.stdout)
        stderr = _process_output_text(exc.stderr) + "\ncommand timed out after 1800s"
        returncode = None
    output = stdout + "\n" + stderr
    return {
        "schema_version": 1,
        "phase": "migration-canary",
        "source": source,
        "project": str(project.resolve()),
        "ran": True,
        "command": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "duration_seconds": round(time.time() - started, 3),
        "error_codes": _error_code_counts(output),
        "timeout_count": len(re.findall(r"Timeout|TimeoutException|timed out", output)),
        "circuit_breaker_mentions": len(re.findall(r"circuit|CB_", output, flags=re.IGNORECASE)),
        "stdout_tail": stdout[-8000:],
        "stderr_tail": stderr[-8000:],
    }


def trace_project(
    project: Path,
    *,
    command: str | None = None,
    log_file: Path | None = None,
    source: str | None = None,
    backend: str | None = None,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    root = project.resolve()
    if command and log_file:
        raise ValueError("use either --command or --log-file, not both")
    if log_file:
        text = read_trace_log(log_file)
        return parse_trace_text(text, source=source, backend=backend, command=None, log_path=str(log_file))
    if not command:
        return {
            "schema_version": 1,
            "phase": "workload-trace",
            "source": source,
            "backend": backend,
            "project": str(root),
            "ran": False,
            "reason": "no command or log file supplied",
            "metrics": {},
            "redaction_report": {"raw_log_forwarded": False},
        }
    started = time.time()
    timed_out = False
    try:
        result = subprocess.run(
            shlex.split(command),
            cwd=root,
            shell=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
        stdout = result.stdout
        stderr = result.stderr
        returncode = result.returncode
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout = _process_output_text(exc.stdout)
        stderr = _process_output_text(exc.stderr) + f"\ncommand timed out after {timeout_seconds}s"
        returncode = None
    trace = parse_trace_text(
        stdout + "\n" + stderr,
        source=source,
        backend=backend,
        command=command,
        returncode=returncode,
        duration_seconds=time.time() - started,
    )
    trace["project"] = str(root)
    trace["ran"] = True
    trace["timed_out"] = timed_out
    return trace


def profile_project(project: Path, *, trace_paths: list[Path], source: str | None = None) -> dict[str, Any]:
    assessment = assess_project(project, source=source)
    traces = [load_json_file(path) for path in trace_paths]
    return workload_profile_from_assessment(assessment, traces=traces, source=source)


def draft_contract_project(
    project: Path,
    *,
    profile_path: Path | None = None,
    trace_paths: list[Path] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    if profile_path:
        profile = load_json_file(profile_path)
    else:
        profile = profile_project(project, trace_paths=trace_paths or [], source=source)
    return draft_workload_contract(profile, source=source)


def compare_project(
    project: Path,
    *,
    contract_path: Path,
    trace_paths: list[Path] | None = None,
    log_file: Path | None = None,
    source: str | None = None,
    backend: str | None = None,
) -> dict[str, Any]:
    contract = load_json_file(contract_path)
    if trace_paths:
        traces = [load_json_file(path) for path in trace_paths]
        trace = traces[0] if len(traces) == 1 else merge_traces(traces, source=source, backend=backend)
    elif log_file:
        trace = trace_project(project, log_file=log_file, source=source, backend=backend)
    else:
        raise ValueError("compare requires --trace or --log-file")
    return compare_trace_to_contract(contract, trace)


def onboard_project(
    project: Path,
    *,
    command: str | None = None,
    log_files: list[Path] | None = None,
    source: str | None = None,
    backend: str | None = None,
    apply: bool = False,
    timeout_seconds: float = 1800.0,
) -> dict[str, Any]:
    assessment = assess_project(project, source=source)
    traces = []
    trace_report = None
    if command:
        trace_report = trace_project(
            project,
            command=command,
            source=source,
            backend=backend,
            timeout_seconds=timeout_seconds,
        )
        traces.append(trace_report)
    for path in log_files or []:
        traces.append(trace_project(project, log_file=path, source=source, backend=backend, timeout_seconds=timeout_seconds))
    if trace_report is None and traces:
        trace_report = traces[0] if len(traces) == 1 else merge_traces(traces, source=source, backend=backend)
    profile = workload_profile_from_assessment(assessment, traces=traces, source=source)
    contract = draft_workload_contract(profile, source=source)
    recipe_intake = contract_to_recipe_intake(contract) if contract.get("workloads") else None
    return {
        "schema_version": 1,
        "phase": "migration-onboard",
        "source": source,
        "project": str(project.resolve()),
        "assessment": assessment,
        "preflight_requests": build_preflight_requests(assessment, source=source),
        "patch_suggestions": patch_suggestions(project, source=source, apply=apply)["patches"],
        "workload_trace": trace_report,
        "workload_profile": profile,
        "workload_contract": contract,
        "recipe_intake": recipe_intake,
        "next_actions": _onboard_next_actions(contract, trace_report),
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
    helper_needed = False
    paths = sorted({str(row["path"]) for row in rows if str(row.get("path", "")).endswith(".py")})
    for rel in paths:
        path = project / rel
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        marker = "# gpucall-migrate: direct provider path migrated to gpucall compatibility helpers."
        updated = text
        updated = _rewrite_anthropic_imports(updated)
        updated = re.sub(
            r"(?m)^(\s*)import anthropic\s*(?:#.*)?$",
            r"\1from gpucall_migration import AnthropicCompat, AsyncAnthropicCompat",
            updated,
        )
        updated = updated.replace("anthropic.Anthropic(", "AnthropicCompat(")
        updated = updated.replace("anthropic.AsyncAnthropic(", "AsyncAnthropicCompat(")
        if "from openai import OpenAI" in updated:
            updated = updated.replace("from openai import OpenAI", "from gpucall_migration import gpucall_openai_client")
            updated = re.sub(r"\bOpenAI\s*\(", "gpucall_openai_client(", updated)
        if updated != text:
            helper_needed = "gpucall_migration" in updated
            if marker not in updated:
                updated = _insert_after_future_imports(updated, marker + "\n")
            path.write_text(updated, encoding="utf-8")
            changed.add(rel)
    if helper_needed:
        helper_text = _migration_helper_text(source=source)
        if not helper.exists() or helper.read_text(encoding="utf-8") != helper_text:
            helper.write_text(helper_text, encoding="utf-8")
            changed.add(str(helper.relative_to(project)))
    manifest = {
        "schema_version": 1,
        "source": source,
        "changed_files": sorted(changed),
        "note": "The patch adds deterministic stdlib gpucall compatibility helpers only when provider constructors are actually rewritten.",
    }
    manifest_dir = project / ".gpucall-migration"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = manifest_dir / "applied-patch.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    changed.add(str(manifest_path.relative_to(project)))
    return sorted(changed)


def _rewrite_anthropic_imports(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        indent = match.group("indent")
        symbols = [item.strip() for item in match.group("symbols").split(",")]
        rewritten: list[str] = []
        passthrough: list[str] = []
        for symbol in symbols:
            if symbol == "Anthropic":
                rewritten.append("AnthropicCompat as Anthropic")
            elif symbol == "AsyncAnthropic":
                rewritten.append("AsyncAnthropicCompat as AsyncAnthropic")
            else:
                passthrough.append(symbol)
        lines: list[str] = []
        if rewritten:
            lines.append(f"{indent}from gpucall_migration import {', '.join(rewritten)}")
        if passthrough:
            lines.append(f"{indent}from anthropic import {', '.join(passthrough)}")
        return "\n".join(lines)

    return re.sub(r"(?m)^(?P<indent>\s*)from anthropic import (?P<symbols>[A-Za-z0-9_, ]+)\s*$", replace, text)


def _migration_helper_text(*, source: str | None) -> str:
    source_repr = repr(source) if source is not None else "None"
    return (
        dedent(
            """
            from __future__ import annotations

            import json
            import os
            import urllib.error
            import urllib.request
            from dataclasses import dataclass
            from typing import Any

            SOURCE = __SOURCE_REPR__


            class _AttrDict(dict):
                def __getattr__(self, name: str) -> Any:
                    try:
                        return self[name]
                    except KeyError as exc:
                        raise AttributeError(name) from exc


            def _objectify(value: Any) -> Any:
                if isinstance(value, dict):
                    return _AttrDict({key: _objectify(item) for key, item in value.items()})
                if isinstance(value, list):
                    return [_objectify(item) for item in value]
                return value


            def _required_env(name: str) -> str:
                value = os.environ.get(name)
                if not value:
                    raise RuntimeError(f"{name} is required for gpucall migration helper")
                return value


            def _openai_base_url(base_url: str | None = None) -> str:
                base = base_url or os.environ.get("GPUCALL_OPENAI_BASE_URL") or _required_env("GPUCALL_BASE_URL")
                base = base.rstrip("/")
                return base if base.endswith("/v1") else base + "/v1"


            def _api_key(api_key: str | None = None) -> str:
                return api_key or _required_env("GPUCALL_API_KEY")


            def _chat_completion(
                *,
                messages: list[dict[str, Any]],
                model: str | None = None,
                max_tokens: int | None = None,
                temperature: float | None = None,
                base_url: str | None = None,
                api_key: str | None = None,
                timeout: float | None = None,
                **extra: Any,
            ) -> dict[str, Any]:
                if extra.get("stream") is True:
                    raise RuntimeError("gpucall migration helper fallback does not support stream=True")
                payload: dict[str, Any] = {
                    "model": model or os.environ.get("GPUCALL_MODEL") or "gpucall",
                    "messages": messages,
                }
                if max_tokens is not None:
                    payload["max_tokens"] = max_tokens
                if temperature is not None:
                    payload["temperature"] = temperature
                payload.update({key: value for key, value in extra.items() if value is not None})
                request = urllib.request.Request(
                    _openai_base_url(base_url) + "/chat/completions",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Authorization": f"Bearer {_api_key(api_key)}",
                        "Content-Type": "application/json",
                    },
                    method="POST",
                )
                request_timeout = timeout if timeout is not None else float(os.environ.get("GPUCALL_MIGRATION_TIMEOUT_SECONDS", "900"))
                try:
                    with urllib.request.urlopen(request, timeout=request_timeout) as response:
                        return json.loads(response.read().decode("utf-8"))
                except urllib.error.HTTPError as exc:
                    body = exc.read().decode("utf-8", errors="replace")[:2000]
                    raise RuntimeError(f"gpucall request failed status={exc.code} body={body}") from exc


            def _extract_text(response: dict[str, Any]) -> str:
                choices = response.get("choices")
                if isinstance(choices, list) and choices:
                    first = choices[0]
                    if isinstance(first, dict):
                        message = first.get("message")
                        if isinstance(message, dict) and message.get("content") is not None:
                            return str(message["content"])
                        if first.get("text") is not None:
                            return str(first["text"])
                return ""


            class _GPUCallClientCompat:
                def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
                    self.base_url = base_url
                    self.api_key = api_key

                def infer(self, *, prompt: str, max_tokens: int | None = None, temperature: float | None = None, model: str | None = None, **kwargs: Any) -> dict[str, Any]:
                    response = _chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        base_url=self.base_url,
                        api_key=self.api_key,
                        **kwargs,
                    )
                    text = _extract_text(response)
                    return {"result": {"value": text}, "value": text, "raw": response}


            def gpucall_client(base_url: str | None = None, api_key: str | None = None) -> Any:
                try:
                    from gpucall_sdk import GPUCallClient  # type: ignore
                except ModuleNotFoundError:
                    return _GPUCallClientCompat(base_url=base_url, api_key=api_key)
                return GPUCallClient(
                    base_url or _required_env("GPUCALL_BASE_URL"),
                    api_key=api_key or _required_env("GPUCALL_API_KEY"),
                )


            class _OpenAIChatCompletionsCompat:
                def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
                    self.base_url = base_url
                    self.api_key = api_key

                def create(self, *, messages: list[dict[str, Any]], model: str | None = None, max_tokens: int | None = None, temperature: float | None = None, **kwargs: Any) -> Any:
                    return _objectify(
                        _chat_completion(
                            messages=messages,
                            model=model,
                            max_tokens=max_tokens,
                            temperature=temperature,
                            base_url=self.base_url,
                            api_key=self.api_key,
                            **kwargs,
                        )
                    )


            class _OpenAIChatCompat:
                def __init__(self, *, base_url: str | None = None, api_key: str | None = None) -> None:
                    self.completions = _OpenAIChatCompletionsCompat(base_url=base_url, api_key=api_key)


            class _OpenAICompat:
                def __init__(self, *, base_url: str | None = None, api_key: str | None = None, **_: Any) -> None:
                    self.chat = _OpenAIChatCompat(base_url=base_url, api_key=api_key)

                def __getattr__(self, name: str) -> Any:
                    raise RuntimeError(f"gpucall migration helper fallback only supports chat.completions, not OpenAI.{name}")


            def gpucall_openai_client(*args: Any, **kwargs: Any) -> Any:
                base = kwargs.setdefault("base_url", _openai_base_url())
                kwargs.setdefault("api_key", _api_key())
                try:
                    from openai import OpenAI  # type: ignore
                except ModuleNotFoundError:
                    return _OpenAICompat(base_url=str(base), api_key=str(kwargs["api_key"]))
                return OpenAI(*args, **kwargs)


            @dataclass
            class _AnthropicContent:
                text: str


            @dataclass
            class _AnthropicMessage:
                content: list[_AnthropicContent]


            def _anthropic_prompt(messages: list[dict[str, Any]] | None) -> str:
                parts: list[str] = []
                for item in messages or []:
                    content = item.get("content") if isinstance(item, dict) else None
                    if isinstance(content, str):
                        parts.append(content)
                    elif isinstance(content, list):
                        parts.extend(str(part["text"]) for part in content if isinstance(part, dict) and part.get("type") == "text" and part.get("text") is not None)
                return "\\n".join(parts)


            class _AnthropicMessagesCompat:
                def create(
                    self,
                    *,
                    messages: list[dict[str, Any]] | None = None,
                    system: str | None = None,
                    model: str | None = None,
                    max_tokens: int | None = None,
                    temperature: float | None = None,
                    **kwargs: Any,
                ) -> _AnthropicMessage:
                    prompt = _anthropic_prompt(messages)
                    openai_messages = []
                    if system:
                        openai_messages.append({"role": "system", "content": system})
                    openai_messages.append({"role": "user", "content": prompt})
                    response = _chat_completion(
                        messages=openai_messages,
                        model=model,
                        max_tokens=max_tokens,
                        temperature=temperature,
                        **kwargs,
                    )
                    return _AnthropicMessage(content=[_AnthropicContent(text=_extract_text(response))])


            class _AsyncAnthropicMessagesCompat:
                async def create(self, *, messages: list[dict[str, Any]] | None = None, max_tokens: int | None = None, temperature: float | None = None, **kwargs: Any) -> _AnthropicMessage:
                    return _AnthropicMessagesCompat().create(messages=messages, max_tokens=max_tokens, temperature=temperature, **kwargs)


            class AnthropicCompat:
                def __init__(self, *_: Any, **__: Any) -> None:
                    self.messages = _AnthropicMessagesCompat()


            class AsyncAnthropicCompat:
                def __init__(self, *_: Any, **__: Any) -> None:
                    self.messages = _AsyncAnthropicMessagesCompat()
            """
        )
        .lstrip()
        .replace("__SOURCE_REPR__", source_repr)
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
    if "rss" in text or "feed" in text or "semantic" in text:
        return "infer", "rss_semantic_match", 131072
    if "pair" in text or "match" in text:
        return "infer", "pairwise_match", 131072
    if "rank" in text or "ranking" in text or "score" in text or "topic" in text:
        return "infer", "rank_text_items", 131072
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


def _process_output_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _onboard_next_actions(contract: dict[str, Any], trace: dict[str, Any] | None) -> list[str]:
    actions = [
        "review workload-contract.json; it contains deterministic caller success metrics only",
        "materialize recipe-intake.json with gpucall-recipe-admin materialize --accept-all in a staging config",
        "run gpucall validate-config and launch-check before production activation",
    ]
    if trace is None:
        actions.insert(0, "run gpucall-migrate trace with the caller baseline command to populate quality metrics")
    if contract.get("workloads"):
        actions.append("run gpucall-migrate compare against a gpucall canary trace before declaring onboarding Go")
    return actions


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
    if report.get("metrics"):
        lines.append("")
        lines.append("## Metrics")
        for key, value in sorted(report["metrics"].items()):
            lines.append(f"- `{key}`: {value}")
    if report.get("workloads"):
        lines.append("")
        lines.append("## Workloads")
        for item in report["workloads"]:
            quality = item.get("quality_contract") or {}
            metrics = quality.get("metrics") if isinstance(quality, dict) else {}
            lines.append(f"- `{item.get('id')}` task=`{item.get('task')}` intent=`{item.get('intent')}`")
            if metrics:
                for key, value in sorted(metrics.items()):
                    lines.append(f"  - `{key}`: {value}")
    if report.get("violations"):
        lines.append("")
        lines.append("## Violations")
        for item in report["violations"]:
            lines.append(
                f"- `{item.get('workload_id')}` `{item.get('metric')}` required={item.get('required')} observed={item.get('observed')}: {item.get('reason')}"
            )
    if report.get("next_actions"):
        lines.append("")
        lines.append("## Next Actions")
        for item in report["next_actions"]:
            lines.append(f"- {item}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
