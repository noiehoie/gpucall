from __future__ import annotations

from pathlib import Path
from typing import Any

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.domain import ExecutionTupleSpec
from gpucall.tuple_audit import _tuple_from_candidate


def live_catalog_scope(config: Any, config_dir: Path) -> dict[str, ExecutionTupleSpec]:
    scope: dict[str, ExecutionTupleSpec] = dict(config.tuples)
    for candidate in load_tuple_candidate_payloads(config_dir):
        try:
            tuple_spec = _tuple_from_candidate(candidate, config)
        except Exception:
            continue
        scope[tuple_spec.name] = tuple_spec
    return scope
