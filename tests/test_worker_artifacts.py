from __future__ import annotations

import json

import pytest

from gpucall.domain import ArtifactManifest
from gpucall.worker_contracts.artifacts import execute_artifact_workload


def test_worker_artifact_export_encrypts_and_returns_manifest(monkeypatch, tmp_path) -> None:
    output = tmp_path / "artifact.bin"
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_DEK_HEX", "11" * 32)
    monkeypatch.setenv("GPUCALL_ALLOW_ARTIFACT_DEK_ENV", "1")
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_URI", f"file://{output}")
    monkeypatch.setattr("gpucall.worker_contracts.artifacts.fetch_data_ref_bytes", lambda _ref: b'{"prompt":"train"}\n')
    payload = {
        "plan_id": "plan-1",
        "task": "fine-tune",
        "recipe": "lora-train",
        "data_classification": "restricted",
        "input_refs": [{"uri": "https://example.com/train.jsonl", "sha256": "a" * 64, "gateway_presigned": True}],
        "artifact_export": {"artifact_chain_id": "chain-1", "version": "0001", "key_id": "tenant-key", "parent_artifact_ids": []},
        "attestations": {"governance_hash": "c" * 64},
    }

    result = execute_artifact_workload(payload)

    assert result is not None
    assert result["kind"] == "artifact_manifest"
    manifest = ArtifactManifest.model_validate(result["artifact_manifest"])
    assert manifest.artifact_chain_id == "chain-1"
    assert manifest.version == "0001"
    assert manifest.key_id == "tenant-key"
    assert manifest.producer_plan_hash == "c" * 64
    assert output.exists()
    assert output.read_bytes()


def test_worker_artifact_export_uses_random_nonce(monkeypatch, tmp_path) -> None:
    output = tmp_path / "artifact.bin"
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_DEK_HEX", "11" * 32)
    monkeypatch.setenv("GPUCALL_ALLOW_ARTIFACT_DEK_ENV", "1")
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_URI", f"file://{output}")
    monkeypatch.setattr("gpucall.worker_contracts.artifacts.fetch_data_ref_bytes", lambda _ref: b'{"prompt":"train"}\n')
    payload = {
        "plan_id": "plan-1",
        "task": "fine-tune",
        "recipe": "lora-train",
        "data_classification": "restricted",
        "input_refs": [{"uri": "https://example.com/train.jsonl", "sha256": "a" * 64, "gateway_presigned": True}],
        "artifact_export": {"artifact_chain_id": "chain-1", "version": "0001", "key_id": "tenant-key", "parent_artifact_ids": []},
        "attestations": {"governance_hash": "c" * 64},
    }

    first = execute_artifact_workload(payload)
    first_blob = output.read_bytes()
    second = execute_artifact_workload(payload)
    second_blob = output.read_bytes()

    assert first is not None
    assert second is not None
    assert first["artifact_manifest"]["ciphertext_sha256"] != second["artifact_manifest"]["ciphertext_sha256"]
    assert first["artifact_manifest"]["encryption_nonce"] != second["artifact_manifest"]["encryption_nonce"]
    assert first_blob[:12] != second_blob[:12]


def test_worker_artifact_export_rejects_env_dek_without_opt_in(monkeypatch, tmp_path) -> None:
    output = tmp_path / "artifact.bin"
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_DEK_HEX", "11" * 32)
    monkeypatch.delenv("GPUCALL_ALLOW_ARTIFACT_DEK_ENV", raising=False)
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_URI", f"file://{output}")
    payload = {
        "plan_id": "plan-1",
        "task": "fine-tune",
        "recipe": "lora-train",
        "data_classification": "restricted",
        "input_refs": [],
        "artifact_export": {"artifact_chain_id": "chain-1", "version": "0001", "key_id": "tenant-key", "parent_artifact_ids": []},
        "attestations": {"governance_hash": "c" * 64},
    }

    with pytest.raises(RuntimeError, match="GPUCALL_ALLOW_ARTIFACT_DEK_ENV"):
        execute_artifact_workload(payload)


def test_worker_split_learning_accepts_activation_ref(monkeypatch) -> None:
    monkeypatch.setattr("gpucall.worker_contracts.artifacts.fetch_data_ref_bytes", lambda _ref: b"activation")
    payload = {
        "task": "split-infer",
        "split_learning": {
            "activation_ref": {"uri": "https://example.com/activation.bin", "sha256": "b" * 64, "gateway_presigned": True},
            "irreversibility_claim": "empirical",
            "dp_epsilon": 3.0,
        },
    }

    result = execute_artifact_workload(payload)

    assert result is not None
    assert result["kind"] == "inline"
    value = json.loads(result["value"])
    assert value["kind"] == "split_learning_activation_accepted"
    assert value["irreversibility_claim"] == "empirical"
    assert value["dp_epsilon"] == 3.0
