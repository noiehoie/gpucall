from __future__ import annotations

import json
from pathlib import Path

import pytest

from gpucall.artifact_reclaim import load_reclaim_master_key, reclaim_artifact
from gpucall.worker_contracts.artifacts import execute_artifact_workload

MASTER_HEX = "11" * 32


@pytest.fixture(autouse=True)
def isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("GPUCALL_STATE_DIR", raising=False)


def _export_manifest(tmp_path, monkeypatch) -> dict:
    """Run the real worker-side encrypted export against a file:// destination."""
    cipher_path = tmp_path / "cloud" / "artifact.bin"
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_URI", f"file://{cipher_path}")
    monkeypatch.setenv("GPUCALL_ALLOW_ARTIFACT_DEK_ENV", "1")
    monkeypatch.setenv("GPUCALL_WORKER_ARTIFACT_DEK_HEX", MASTER_HEX)
    payload = {
        "task": "train",
        "recipe": "train-lora-draft",
        "plan_id": "plan-123",
        "attestations": {"governance_hash": "gh-abc"},
        "input_refs": [],
        "artifact_export": {
            "artifact_chain_id": "lora-chain-1",
            "version": "v1",
            "key_id": "operator-master-1",
        },
    }
    result = execute_artifact_workload(payload)
    assert result is not None
    manifest = result["artifact_manifest"] if "artifact_manifest" in result else result
    assert manifest["ciphertext_uri"].startswith("file://")
    assert cipher_path.exists()
    return manifest


def test_round_trip_reclaim_decrypts_and_purges_cloud_copy(tmp_path, monkeypatch) -> None:
    manifest = _export_manifest(tmp_path, monkeypatch)
    dek_file = tmp_path / "master.key"
    dek_file.write_text(MASTER_HEX, encoding="utf-8")
    out = tmp_path / "recovered" / "artifact.json"

    receipt = reclaim_artifact(
        manifest,
        master_key=load_reclaim_master_key(dek_file=str(dek_file)),
        output_path=out,
        delete_remote=True,
    )

    recovered = json.loads(out.read_text(encoding="utf-8"))
    assert recovered["kind"] == "gpucall-chained-artifact"
    assert recovered["task"] == "train"
    assert recovered["plan_id"] == "plan-123"
    assert receipt["integrity_verified"] is True
    assert receipt["remote_deleted"] is True
    assert receipt["remote_verified_absent"] is True
    # master key file must survive operator-side use (unlike the worker's one-shot file)
    assert dek_file.exists()
    saved = json.loads(Path(receipt["receipt_path"]).read_text(encoding="utf-8"))
    assert saved["phase"] == "artifact-reclamation"
    assert saved["artifact_chain_id"] == "lora-chain-1"
    # cloud copy is gone
    assert not Path(manifest["ciphertext_uri"].removeprefix("file://")).exists()


def test_reclaim_rejects_tampered_ciphertext(tmp_path, monkeypatch) -> None:
    manifest = _export_manifest(tmp_path, monkeypatch)
    cipher_path = Path(manifest["ciphertext_uri"].removeprefix("file://"))
    blob = bytearray(cipher_path.read_bytes())
    blob[-1] ^= 0xFF
    cipher_path.write_bytes(bytes(blob))
    dek_file = tmp_path / "master.key"
    dek_file.write_text(MASTER_HEX, encoding="utf-8")

    with pytest.raises(RuntimeError, match="sha256 mismatch"):
        reclaim_artifact(
            manifest,
            master_key=load_reclaim_master_key(dek_file=str(dek_file)),
            output_path=tmp_path / "out.bin",
        )


def test_reclaim_rejects_wrong_master_key(tmp_path, monkeypatch) -> None:
    manifest = _export_manifest(tmp_path, monkeypatch)
    wrong = tmp_path / "wrong.key"
    wrong.write_text("22" * 32, encoding="utf-8")

    with pytest.raises(Exception):
        reclaim_artifact(
            manifest,
            master_key=load_reclaim_master_key(dek_file=str(wrong)),
            output_path=tmp_path / "out.bin",
        )


def test_master_key_loader_validates_length(tmp_path) -> None:
    short = tmp_path / "short.key"
    short.write_bytes(b"tooshort")
    with pytest.raises(RuntimeError, match="32-byte"):
        load_reclaim_master_key(dek_file=str(short))


def test_reclaim_refuses_out_of_scope_bucket(tmp_path) -> None:
    manifest = {
        "ciphertext_uri": "s3://someone-elses-bucket/key.bin",
        "ciphertext_sha256": "a" * 64,
        "artifact_chain_id": "c",
        "version": "v1",
        "key_id": "k",
        "producer_plan_hash": "gh",
    }
    with pytest.raises(RuntimeError, match="outside the operator scope"):
        reclaim_artifact(
            manifest,
            master_key=b"\x11" * 32,
            output_path=tmp_path / "out.bin",
            allowed_buckets={"gpucall-v2-production"},
        )
