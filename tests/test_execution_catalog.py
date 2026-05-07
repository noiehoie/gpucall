from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gpucall.config import load_config
from gpucall.execution_catalog import build_resource_catalog_snapshot, generate_tuple_candidates


def test_execution_catalog_separates_accounts_surfaces_and_workers() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))

    account_refs = {account.account_ref for account in snapshot.accounts}
    surfaces = {resource.execution_surface for resource in snapshot.resources}

    assert {"hyperstack", "modal", "runpod"}.issubset(account_refs)
    assert {"iaas_vm", "function_runtime", "managed_endpoint"}.issubset(surfaces)
    assert all(resource.account_ref for resource in snapshot.resources)
    assert all(resource.worker_binding_ref for resource in snapshot.resources)
    assert all(worker.execution_surface for worker in snapshot.workers)
    assert all(worker.worker_binding_ref for worker in snapshot.workers)
    assert len(snapshot.snapshot_id) == 64
    assert len(snapshot.config_hash) == 64


def test_execution_catalog_generates_snapshot_pinned_tuple_candidates() -> None:
    config = load_config(Path("config"))
    snapshot = build_resource_catalog_snapshot(config, config_dir=Path("config"))
    candidates = generate_tuple_candidates(snapshot, recipe=config.recipes["text-infer-standard"])

    assert any(candidate.tuple_name == "hyperstack-a100" and candidate.execution_surface == "iaas_vm" for candidate in candidates)
    assert any(candidate.tuple_name == "modal-a10g" and candidate.execution_surface == "function_runtime" for candidate in candidates)
    assert any(
        candidate.tuple_name.startswith("runpod-vllm-")
        and candidate.execution_surface == "managed_endpoint"
        and candidate.production_state == "candidate_draft"
        for candidate in candidates
    )
    assert any(
        candidate.tuple_name.startswith("runpod-native-")
        and candidate.execution_surface == "managed_endpoint"
        and candidate.production_state == "candidate_draft"
        for candidate in candidates
    )
    assert all(candidate.snapshot_id == snapshot.snapshot_id for candidate in candidates)
    assert all(candidate.snapshot_pinned is True for candidate in candidates)
    assert all(candidate.recipe_fit is not None for candidate in candidates)


def test_execution_catalog_cli_outputs_candidates() -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "execution-catalog",
            "candidates",
            "--config-dir",
            "config",
            "--recipe",
            "text-infer-standard",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert any(item["execution_surface"] == "iaas_vm" for item in payload)
    assert any(item["execution_surface"] == "function_runtime" for item in payload)
    assert any(item["source"] == "provider_candidate" for item in payload)
