from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


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
    request_id = str(bundle.get("request_id") or uuid.uuid4().hex)
    final_path = root / f"{request_id}.json"
    tmp_path = root / f".{request_id}.tmp"
    tmp_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(final_path)
    return final_path
