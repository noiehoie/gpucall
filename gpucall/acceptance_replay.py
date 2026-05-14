from __future__ import annotations

import json
from pathlib import Path
from typing import Any


FORBIDDEN_FIXTURE_MARKERS = (
    "news-system",
    "tamotsu",
    "sugano",
    "100.91.",
    "gpk_",
    "sk-",
    "X-Amz-Signature",
    "/Users/",
    "/opt/gpucall/state",
)


def load_anonymous_replay_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    violations = fixture_contamination_markers(payload)
    if violations:
        raise ValueError(f"anonymous replay fixture contains forbidden markers: {', '.join(violations)}")
    return payload


def fixture_contamination_markers(payload: Any) -> list[str]:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return sorted({marker for marker in FORBIDDEN_FIXTURE_MARKERS if marker in text})


def replay_workload_classes(payload: dict[str, Any]) -> set[str]:
    return {str(item.get("class")) for item in payload.get("workloads", []) if item.get("class")}
