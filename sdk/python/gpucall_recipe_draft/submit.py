from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9_.-]+$")
_SAFE_REMOTE_HOST = re.compile(r"^[A-Za-z0-9_.@%:+-]+$")


def build_submission_bundle(
    *,
    intake: dict[str, Any],
    draft: dict[str, Any] | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    request_id = "rr-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:12]
    return {
        "schema_version": 1,
        "kind": "gpucall.recipe_request_submission",
        "request_id": request_id,
        "created_at": datetime.now(UTC).isoformat(),
        "source": source or os.getenv("HOSTNAME") or "unknown",
        "intake": intake,
        "draft": draft,
    }


def submit_bundle(bundle: dict[str, Any], inbox_dir: str | Path) -> Path:
    root = Path(inbox_dir)
    root.mkdir(parents=True, exist_ok=True)
    request_id = _safe_request_id(str(bundle.get("request_id") or uuid.uuid4().hex))
    final_path = root / f"{request_id}.json"
    tmp_path = root / f".{request_id}.tmp"
    tmp_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(final_path)
    return final_path


def submit_bundle_to_remote(bundle: dict[str, Any], remote_inbox: str) -> str:
    target = parse_remote_inbox(remote_inbox)
    request_id = _safe_request_id(str(bundle.get("request_id") or uuid.uuid4().hex))
    payload = json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path = posixpath.join(target.inbox_dir, f".{request_id}.tmp")
    final_path = posixpath.join(target.inbox_dir, f"{request_id}.json")
    command = "set -eu; mkdir -p -- {dir}; umask 077; cat > {tmp}; mv -f -- {tmp} {final}; chmod 0644 -- {final}".format(
        dir=shlex.quote(target.inbox_dir),
        tmp=shlex.quote(tmp_path),
        final=shlex.quote(final_path),
    )
    subprocess.run(
        ["ssh", target.host, command],
        input=payload.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return f"{target.host}:{final_path}"


def get_submission_status(*, request_id: str, inbox_dir: str | Path, pipeline: str) -> dict[str, Any]:
    request_id = _safe_request_id(request_id)
    pipeline = _safe_pipeline(pipeline)
    inbox = Path(inbox_dir)
    report_path = inbox / "reports" / f"{request_id}.report.json"
    if report_path.exists():
        return _status_from_report(request_id=request_id, pipeline=pipeline, report_path=report_path)
    candidates = [
        ("pending", inbox / f"{request_id}.json"),
        ("processed", inbox / "processed" / f"{request_id}.json"),
        ("failed", inbox / "failed" / f"{request_id}.json"),
    ]
    for state, path in candidates:
        if path.exists():
            return {
                "pipeline": pipeline,
                "request_id": request_id,
                "status": state,
                "report_available": False,
            }
    return {
        "pipeline": pipeline,
        "request_id": request_id,
        "status": "missing",
        "report_available": False,
    }


def get_remote_submission_status(*, request_id: str, remote_inbox: str, pipeline: str) -> dict[str, Any]:
    target = parse_remote_inbox(remote_inbox)
    request_id = _safe_request_id(request_id)
    pipeline = _safe_pipeline(pipeline)
    command = _remote_status_command(inbox_dir=target.inbox_dir, request_id=request_id, pipeline=pipeline)
    completed = subprocess.run(
        ["ssh", target.host, command],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    )
    data = json.loads(completed.stdout)
    if not isinstance(data, dict):
        raise ValueError("remote status response was not a JSON object")
    return summarize_status(data)


def summarize_status(status: dict[str, Any]) -> dict[str, Any]:
    pipeline = _safe_pipeline(str(status.get("pipeline") or "recipe"))
    request_id = _safe_request_id(str(status.get("request_id") or status.get("feedback_id") or ""))
    report = status.get("report") if isinstance(status.get("report"), dict) else {}
    index = status.get("index_record") if isinstance(status.get("index_record"), dict) else {}
    quality = report.get("quality_feedback") if isinstance(report.get("quality_feedback"), dict) else {}
    observed = report.get("observed") if isinstance(report.get("observed"), dict) else {}
    result: dict[str, Any] = {
        "pipeline": pipeline,
        "request_id": request_id,
        "status": str(status.get("status") or status.get("state") or "unknown"),
        "report_available": bool(report),
    }
    if report:
        result["decision"] = report.get("decision")
        result["task"] = report.get("task")
        result["intent"] = report.get("intent")
        if report.get("phase"):
            result["phase"] = report.get("phase")
        if report.get("next_actions"):
            result["next_actions"] = report.get("next_actions")
        if report.get("blockers"):
            result["blockers"] = _safe_findings(report.get("blockers"))
        if report.get("warnings"):
            result["warnings"] = _safe_findings(report.get("warnings"))
        if pipeline == "quality":
            result["quality_kind"] = quality.get("kind")
            result["observed_tuple"] = observed.get("tuple")
            result["observed_tuple_model"] = observed.get("tuple_model")
    elif index:
        result["task"] = index.get("task")
        result["intent"] = index.get("intent")
        if pipeline == "quality":
            result["quality_kind"] = index.get("quality_kind")
            result["observed_tuple"] = index.get("observed_tuple")
    return {key: value for key, value in result.items() if value not in (None, "", [], {})}


class RemoteInboxTarget:
    def __init__(self, host: str, inbox_dir: str) -> None:
        self.host = host
        self.inbox_dir = inbox_dir


def parse_remote_inbox(remote_inbox: str) -> RemoteInboxTarget:
    host, separator, path_tail = remote_inbox.partition(":/")
    if not separator:
        raise ValueError("remote inbox must use USER@HOST:/absolute/path")
    if not host or not _SAFE_REMOTE_HOST.match(host):
        raise ValueError("remote inbox host contains unsupported characters")
    inbox_dir = "/" + path_tail
    if not path_tail or "\x00" in inbox_dir or "\n" in inbox_dir:
        raise ValueError("remote inbox path must be a non-empty absolute path")
    return RemoteInboxTarget(host=host, inbox_dir=inbox_dir)


def _safe_request_id(value: str) -> str:
    if not value or not _SAFE_REQUEST_ID.match(value):
        raise ValueError("submission request_id contains unsupported characters")
    return value


def _safe_pipeline(value: str) -> str:
    if value not in {"recipe", "quality"}:
        raise ValueError("pipeline must be recipe or quality")
    return value


def _status_from_report(*, request_id: str, pipeline: str, report_path: Path) -> dict[str, Any]:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(report, dict):
        raise ValueError("status report JSON must be an object")
    return summarize_status(
        {
            "pipeline": pipeline,
            "request_id": request_id,
            "status": "processed",
            "report": report,
        }
    )


def _remote_status_command(*, inbox_dir: str, request_id: str, pipeline: str) -> str:
    pipeline_json = json.dumps(pipeline)
    request_id_json = json.dumps(request_id)
    return (
        "set -eu; "
        f"inbox={shlex.quote(inbox_dir)}; "
        f"id={shlex.quote(request_id)}; "
        'report="$inbox/reports/$id.report.json"; '
        'processed="$inbox/processed/$id.json"; '
        'failed="$inbox/failed/$id.json"; '
        'pending="$inbox/$id.json"; '
        'if [ -f "$report" ]; then '
        f"printf '{{\"pipeline\":{pipeline_json},\"request_id\":{request_id_json},\"status\":\"processed\",\"report\":'; "
        'cat "$report"; '
        "printf '}\\n'; "
        'elif [ -f "$processed" ]; then '
        f"printf '{{\"pipeline\":{pipeline_json},\"request_id\":{request_id_json},\"status\":\"processed\",\"report_available\":false}}\\n'; "
        'elif [ -f "$failed" ]; then '
        f"printf '{{\"pipeline\":{pipeline_json},\"request_id\":{request_id_json},\"status\":\"failed\",\"report_available\":false}}\\n'; "
        'elif [ -f "$pending" ]; then '
        f"printf '{{\"pipeline\":{pipeline_json},\"request_id\":{request_id_json},\"status\":\"pending\",\"report_available\":false}}\\n'; "
        "else "
        f"printf '{{\"pipeline\":{pipeline_json},\"request_id\":{request_id_json},\"status\":\"missing\",\"report_available\":false}}\\n'; "
        "fi"
    )


def _safe_findings(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        safe = {key: item.get(key) for key in ("check", "reason", "ok") if key in item}
        if safe:
            output.append(safe)
    return output
