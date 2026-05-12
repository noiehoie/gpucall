from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from gpucall.candidate_sources import load_tuple_candidate_payloads


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parents[1] / "config"
    root = tmp_path / "config"
    shutil.copytree(source, root)
    return root


def test_tuple_audit_reports_active_and_candidate_tuples(tmp_path) -> None:
    root = copy_config(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gpucall.cli",
            "tuple-audit",
            "--config-dir",
            str(root),
            "--recipe",
            "text-infer-standard",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    recipe = payload["recipes"]["text-infer-standard"]
    assert payload["phase"] == "execution-tuple-governance-audit"
    assert payload["ideal_contract"]["recipe_is_authority"] is True
    assert recipe["routing_decision"]["decision"] in {"ROUTABLE", "READY_FOR_VALIDATION", "CANDIDATE_ONLY"}
    assert any(row["name"] == "modal-a10g" for row in recipe["active_tuples"])
    assert any(row["name"].startswith("runpod-vllm-") for row in recipe["candidate_tuples"])
    assert any(row["name"].startswith("runpod-native-") for row in recipe["candidate_tuples"])
    assert recipe["surfaces"]["active"]["function_runtime"] >= 1
    assert recipe["surfaces"]["active"]["iaas_vm"] >= 1
    assert recipe["surfaces"]["candidate"]["managed_endpoint"] >= 1
    assert all("production_decision" in row for row in recipe["active_tuples"])
    assert all("production_decision" in row for row in recipe["candidate_tuples"])
    assert all("execution_surface" in row["tuple"] for row in recipe["active_tuples"])
    assert all("execution_surface" in row["tuple"] for row in recipe["candidate_tuples"])


def test_runpod_candidates_are_generated_from_catalog_source() -> None:
    root = Path(__file__).resolve().parents[1] / "config"
    candidates = load_tuple_candidate_payloads(root)
    runpod = [row for row in candidates if str(row.get("name", "")).startswith("runpod-")]

    assert not list((root / "tuple_candidates").glob("runpod-*.yml"))
    assert len(runpod) >= 296
    assert sum(1 for row in runpod if row["adapter"] == "runpod-vllm-serverless") >= 108
    assert sum(1 for row in runpod if row["adapter"] == "runpod-serverless") >= 80
    assert sum(1 for row in runpod if row["adapter"] == "runpod-vllm-flashboot") >= 108
    assert any(row["model_ref"] == "qwen2.5-7b-instruct-1m" for row in runpod)
    assert any(
        row["model_ref"] == "qwen2.5-7b-instruct-1m-524k"
        and row["gpu"] in {"ADA_80_PRO", "AMPERE_80", "HOPPER_141"}
        for row in runpod
    )
    qwen_vl = next(
        row
        for row in runpod
        if row["adapter"] == "runpod-vllm-serverless"
        and row["model_ref"] == "qwen2.5-vl-7b-instruct"
    )
    assert qwen_vl["input_contracts"] == ["chat_messages", "image", "data_refs"]


def test_hyperstack_multi_gpu_candidates_set_worker_parallelism() -> None:
    root = Path(__file__).resolve().parents[1] / "config"
    candidates = load_tuple_candidate_payloads(root)
    candidate = next(
        row
        for row in candidates
        if row["adapter"] == "hyperstack"
        and row["gpu"] == "n3-RTX-A6000x2"
        and row["model_ref"] == "qwen2.5-7b-instruct-1m-524k"
    )

    worker_env = candidate["provider_params"]["worker_env"]
    assert worker_env["GPUCALL_WORKER_TENSOR_PARALLEL_SIZE"] == "2"
    assert worker_env["GPUCALL_WORKER_GPU_MEMORY_UTILIZATION"] == "0.95"
    assert worker_env["GPUCALL_WORKER_MAX_MODEL_LEN"] == str(candidate["max_model_len"])


def test_tuple_audit_fails_closed_for_unknown_recipe(tmp_path) -> None:
    root = copy_config(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "gpucall.cli",
            "tuple-audit",
            "--config-dir",
            str(root),
            "--recipe",
            "missing-recipe",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "unknown recipe: missing-recipe" in result.stderr
