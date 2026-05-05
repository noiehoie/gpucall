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
    command = "set -eu; mkdir -p -- {dir}; umask 077; cat > {tmp}; mv -f -- {tmp} {final}".format(
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
