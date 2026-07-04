from __future__ import annotations

import json
import os
from pathlib import Path

from gpucall.config import load_config
from gpucall.validation_evidence import (
    load_route_validation_evidence,
    load_route_validation_statuses,
    route_validation_key,
)


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    for path in source.rglob("*.yml"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


def _valid_artifact(tuple_spec, *, config_hash: str) -> dict:
    from gpucall.execution.contracts import official_contract, official_contract_hash

    contract = official_contract(tuple_spec)
    return {
        "tuple": tuple_spec.name,
        "recipe": "text-infer-light",
        "mode": "sync",
        "started_at": "2026-01-01T00:00:00+00:00",
        "ended_at": "2026-01-01T00:00:01+00:00",
        "commit": os.getenv("GPUCALL_GIT_COMMIT"),
        "config_hash": config_hash,
        "governance_hash": "c" * 64,
        "validation_schema_version": 1,
        "passed": True,
        "cleanup": {"required": False, "completed": None},
        "cost": {"observed": None, "estimated": None},
        "audit": {"event_ids": []},
        "official_contract": contract,
        "official_contract_hash": official_contract_hash(contract),
    }


def test_equal_mtime_tiebreak_is_deterministic(tmp_path, monkeypatch) -> None:
    # Two artifacts for the SAME (tuple, recipe, mode) key with the SAME mtime but
    # different config_hash must resolve to the same record on every call: the one
    # whose timestamped filename sorts last (newest by name).
    monkeypatch.setenv("GPUCALL_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GPUCALL_GIT_COMMIT", "deadbeef")
    root = copy_config(tmp_path)
    config = load_config(root)
    tuple_spec = config.tuples["modal-a10g"]

    artifact_dir = tmp_path / "state" / "tuple-validation"
    artifact_dir.mkdir(parents=True)

    older = artifact_dir / "20260101T000000Z-a.json"
    newer = artifact_dir / "20260101T000000Z-b.json"
    older.write_text(json.dumps(_valid_artifact(tuple_spec, config_hash="hash-older")), encoding="utf-8")
    newer.write_text(json.dumps(_valid_artifact(tuple_spec, config_hash="hash-newer")), encoding="utf-8")

    # Force identical mtimes so the primary sort key ties and the filename secondary
    # key decides — deterministically, not by filesystem glob order.
    equal_mtime = 1_700_000_000.0
    os.utime(older, (equal_mtime, equal_mtime))
    os.utime(newer, (equal_mtime, equal_mtime))

    key = route_validation_key(tuple_spec.name, "text-infer-light", "sync")

    selections = []
    for _ in range(5):
        evidence = load_route_validation_evidence(config_dir=root, strict_config_hash=False)
        selections.append(evidence[key].path)
    assert len(set(selections)) == 1
    # Descending filename == newest timestamped artifact wins.
    assert Path(selections[0]).name == newer.name

    status_selections = []
    for _ in range(5):
        statuses = load_route_validation_statuses(config_dir=root, strict_config_hash=False)
        status_selections.append(statuses[key].path)
    assert len(set(status_selections)) == 1
    assert Path(status_selections[0]).name == newer.name
