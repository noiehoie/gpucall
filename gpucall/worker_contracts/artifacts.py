from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any

from gpucall.worker_contracts.io import fetch_data_ref_bytes


def execute_artifact_workload(payload: dict[str, Any]) -> dict[str, Any] | None:
    task = str(payload.get("task") or "")
    if task == "split-infer" and payload.get("split_learning") is not None:
        return _split_learning_result(payload)
    if task in {"train", "fine-tune"} and payload.get("artifact_export") is not None:
        manifest = _encrypted_artifact_export(payload, task=task)
        return {"kind": "artifact_manifest", "artifact_manifest": manifest}
    return None


def _split_learning_result(payload: dict[str, Any]) -> dict[str, Any]:
    spec = payload["split_learning"]
    activation_ref = spec["activation_ref"]
    activation = fetch_data_ref_bytes(activation_ref)
    digest = hashlib.sha256(activation).hexdigest()
    return {
        "kind": "inline",
        "value": json.dumps(
            {
                "kind": "split_learning_activation_accepted",
                "activation_sha256": digest,
                "dp_epsilon": spec.get("dp_epsilon"),
                "irreversibility_claim": spec.get("irreversibility_claim"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
    }


def _encrypted_artifact_export(payload: dict[str, Any], *, task: str) -> dict[str, Any]:
    export = payload["artifact_export"]
    key = _artifact_dek(payload, export)
    plaintext = _artifact_plaintext(payload, task=task)
    nonce = _artifact_nonce(payload, export)
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:
        raise RuntimeError("cryptography is required for worker artifact encryption") from exc
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _associated_data(payload, export))
    artifact_blob = nonce + ciphertext
    ciphertext_sha256 = hashlib.sha256(artifact_blob).hexdigest()
    uri = _write_artifact_ciphertext(export, artifact_blob)
    return {
        "artifact_id": hashlib.sha256(
            f"{export['artifact_chain_id']}:{export['version']}:{ciphertext_sha256}".encode("utf-8")
        ).hexdigest(),
        "artifact_chain_id": export["artifact_chain_id"],
        "version": export["version"],
        "classification": str(payload.get("data_classification") or "restricted"),
        "ciphertext_uri": uri,
        "ciphertext_sha256": ciphertext_sha256,
        "key_id": export["key_id"],
        "producer_plan_hash": str((payload.get("attestations") or {}).get("governance_hash") or ""),
        "attestation_evidence_ref": _attestation_ref(payload),
        "parent_artifact_ids": list(export.get("parent_artifact_ids") or []),
        "legal_hold": bool(export.get("legal_hold") or False),
        "retention_until": export.get("retention_until"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }


def _artifact_plaintext(payload: dict[str, Any], *, task: str) -> bytes:
    inputs = []
    for ref in payload.get("input_refs") or []:
        body = fetch_data_ref_bytes(ref)
        inputs.append(
            {
                "uri": str(ref.get("uri")),
                "sha256": hashlib.sha256(body).hexdigest(),
                "bytes": len(body),
                "content_type": ref.get("content_type"),
            }
        )
    bundle = {
        "kind": "gpucall-chained-artifact",
        "task": task,
        "recipe": payload.get("recipe"),
        "plan_id": payload.get("plan_id"),
        "inputs": inputs,
        "split_learning": _split_learning_summary(payload),
    }
    return json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _split_learning_summary(payload: dict[str, Any]) -> dict[str, Any] | None:
    spec = payload.get("split_learning")
    if not isinstance(spec, dict):
        return None
    activation_ref = spec.get("activation_ref") or {}
    return {
        "activation_sha256": activation_ref.get("sha256"),
        "irreversibility_claim": spec.get("irreversibility_claim"),
        "dp_epsilon": spec.get("dp_epsilon"),
    }


def _artifact_dek(payload: dict[str, Any], export: dict[str, Any]) -> bytes:
    raw = _artifact_dek_bytes()
    chain_id = str(export.get("artifact_chain_id") or "")
    version = str(export.get("version") or "")
    plan_hash = str((payload.get("attestations") or {}).get("governance_hash") or payload.get("plan_id") or "")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError as exc:
        raise RuntimeError("cryptography is required for worker artifact encryption") from exc
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=plan_hash.encode("utf-8") or None,
        info=f"gpucall-artifact:{chain_id}:{version}".encode("utf-8"),
    ).derive(raw)


def _artifact_dek_bytes() -> bytes:
    path = os.getenv("GPUCALL_WORKER_ARTIFACT_DEK_FILE", "").strip()
    if path:
        try:
            raw_bytes = open(path, "rb").read()
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        key = raw_bytes.strip()
        if len(key) == 64:
            try:
                key = bytes.fromhex(key.decode("ascii"))
            except Exception:
                pass
        if len(key) != 32:
            raise RuntimeError("GPUCALL_WORKER_ARTIFACT_DEK_FILE must contain a 32-byte AES-256 key")
        return key
    raw = os.getenv("GPUCALL_WORKER_ARTIFACT_DEK_HEX", "")
    if not raw:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_DEK_FILE or GPUCALL_WORKER_ARTIFACT_DEK_HEX is required for artifact export")
    try:
        key = bytes.fromhex(raw)
    except ValueError as exc:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_DEK_HEX must be hex") from exc
    if len(key) != 32:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_DEK_HEX must encode a 32-byte AES-256 key")
    return key


def _artifact_nonce(payload: dict[str, Any], export: dict[str, Any]) -> bytes:
    material = json.dumps(
        {
            "artifact_chain_id": export.get("artifact_chain_id"),
            "version": export.get("version"),
            "plan_hash": (payload.get("attestations") or {}).get("governance_hash"),
            "plan_id": payload.get("plan_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).digest()[:12]


def _associated_data(payload: dict[str, Any], export: dict[str, Any]) -> bytes:
    return json.dumps(
        {
            "plan_hash": (payload.get("attestations") or {}).get("governance_hash"),
            "artifact_chain_id": export.get("artifact_chain_id"),
            "version": export.get("version"),
            "key_id": export.get("key_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _write_artifact_ciphertext(export: dict[str, Any], ciphertext: bytes) -> str:
    uri = os.getenv("GPUCALL_WORKER_ARTIFACT_URI")
    if uri:
        if uri.startswith("file://"):
            path = uri.removeprefix("file://")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(ciphertext)
            return uri
        if not uri.startswith("s3://"):
            raise RuntimeError("GPUCALL_WORKER_ARTIFACT_URI must be s3:// or file://")
        _put_s3(uri, ciphertext)
        return uri
    bucket = os.getenv("GPUCALL_WORKER_ARTIFACT_BUCKET")
    prefix = os.getenv("GPUCALL_WORKER_ARTIFACT_PREFIX", "gpucall/artifacts").strip("/")
    if not bucket:
        raise RuntimeError("GPUCALL_WORKER_ARTIFACT_BUCKET or GPUCALL_WORKER_ARTIFACT_URI is required for artifact export")
    key = f"{prefix}/{export['artifact_chain_id']}/{export['version']}/artifact.bin"
    uri = f"s3://{bucket}/{key}"
    _put_s3(uri, ciphertext)
    return uri


def _put_s3(uri: str, body: bytes) -> None:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for s3 artifact export") from exc
    bucket_key = uri.removeprefix("s3://")
    bucket, _, key = bucket_key.partition("/")
    if not bucket or not key:
        raise RuntimeError("artifact s3 uri must be s3://bucket/key")
    kwargs: dict[str, str] = {}
    endpoint = os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("R2_ENDPOINT_URL")
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    if region:
        kwargs["region_name"] = region
    boto3.client("s3", **kwargs).put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/octet-stream")


def _attestation_ref(payload: dict[str, Any]) -> str | None:
    attestations = payload.get("attestations") or {}
    refs = attestations.get("attestation_evidence_refs")
    if isinstance(refs, list) and refs:
        return str(refs[0])
    return attestations.get("attestation_evidence_ref")
