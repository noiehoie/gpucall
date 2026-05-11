from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from gpucall.catalog import SQLiteCapabilityCatalog
from gpucall.config import load_config


def test_capability_catalog_materializes_config(tmp_path) -> None:
    config = load_config(Path("config"))
    catalog = SQLiteCapabilityCatalog(tmp_path / "catalog.db")

    catalog.replace_from_config(config, config_dir=Path("config"))
    snapshot = catalog.snapshot()

    assert {"name": "qwen2.5-7b-instruct-1m", "provider_model_id": "Qwen/Qwen2.5-7B-Instruct-1M"} in snapshot["models"]
    assert {"name": "qwen2.5-vl-7b-instruct", "provider_model_id": "Qwen/Qwen2.5-VL-7B-Instruct"} in snapshot["models"]
    assert {"name": "hyperstack-vllm", "kind": "vllm"} in snapshot["engines"]
    assert {"name": "runpod-vllm-openai", "kind": "vllm"} in snapshot["engines"]
    assert any(
        provider["name"] == "hyperstack-qwen-1m"
        and provider["model_ref"] == "qwen2.5-7b-instruct-1m-524k"
        and provider["execution_surface"] == "iaas_vm"
        for provider in snapshot["tuples"]
    )
    assert {"name": "gemma4-31b-ollama-local", "provider_model_id": "gemma4-31b:latest"} in snapshot["models"]
    assert any(
        candidate["name"] == "modal-h200x4-qwen25-14b-1m"
        and candidate["execution_surface"] == "function_runtime"
        for candidate in snapshot["tuple_candidates"]
    )
    assert any(
        candidate["name"] == "hyperstack-a100-qwen25-7b-524k"
        and candidate["execution_surface"] == "iaas_vm"
        for candidate in snapshot["tuple_candidates"]
    )
    assert any(
        candidate["name"] == "local-ollama-gemma4-31b-262k"
        and candidate["execution_surface"] == "local_runtime"
        for candidate in snapshot["tuple_candidates"]
    )


def test_catalog_cli_builds_sqlite_snapshot(tmp_path) -> None:
    db = tmp_path / "catalog.db"

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "gpucall" / "cli.py"),
            "catalog",
            "build",
            "--config-dir",
            "config",
            "--db",
            str(db),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert db.exists()
    assert payload["path"] == str(db)
    assert any(model["name"] == "salesforce-blip-vqa-base" for model in payload["models"])
    assert any(
        candidate["name"] == "modal-h100-qwen25-vl-7b"
        and candidate["execution_surface"] == "function_runtime"
        for candidate in payload["tuple_candidates"]
    )
    assert any(
        candidate["name"] == "modal-l40s-qwen25-14b"
        and candidate["execution_surface"] == "function_runtime"
        for candidate in payload["tuple_candidates"]
    )
