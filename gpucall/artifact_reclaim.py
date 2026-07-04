"""Operator-side artifact reclamation: fetch, verify, decrypt, and purge.

The Modal worker exports train/fine-tune artifacts as AES-256-GCM ciphertext
(nonce || ciphertext) whose data key is derived per artifact via
HKDF-SHA256(master, salt=producer_plan_hash, info="gpucall-artifact:<chain>:<version>")
and whose associated data binds plan hash, chain id, version, and key id.
This module is the matching operator half: it brings the artifact home,
proves integrity, decrypts with the operator-held master key, optionally
deletes the cloud copy, verifies absence, and writes a reclamation receipt.

The worker deliberately never holds the master key longer than one export;
this module never uploads anything.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from gpucall.config import default_state_dir

RECLAIM_SCHEMA_VERSION = 1


def load_reclaim_master_key(*, dek_file: str | None = None) -> bytes:
    """Load the operator master key (32-byte AES-256, raw or hex) without consuming it."""
    path = (dek_file or os.getenv("GPUCALL_ARTIFACT_DEK_FILE", "")).strip()
    if not path:
        raise RuntimeError("artifact reclamation requires --dek-file or GPUCALL_ARTIFACT_DEK_FILE")
    raw = Path(path).read_bytes().strip()
    if len(raw) == 64:
        try:
            raw = bytes.fromhex(raw.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            pass
    if len(raw) != 32:
        raise RuntimeError("artifact master key must be a 32-byte AES-256 key (raw or hex)")
    return raw


def reclaim_artifact(
    manifest: Mapping[str, Any],
    *,
    master_key: bytes,
    output_path: str | Path,
    delete_remote: bool = False,
    allowed_buckets: set[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Fetch → verify → decrypt → (optionally) purge one exported artifact.

    ``allowed_buckets`` scopes every s3:// access: a manifest naming a bucket
    outside the operator's own artifact/object-store buckets is refused before
    any request is made, so a tampered manifest cannot steer the operator's
    AWS credentials at foreign data.
    """
    current = now or datetime.now(timezone.utc)
    uri = str(manifest.get("ciphertext_uri") or "")
    expected_sha = str(manifest.get("ciphertext_sha256") or "")
    chain_id = str(manifest.get("artifact_chain_id") or "")
    version = str(manifest.get("version") or "")
    if not uri or not expected_sha or not chain_id or not version:
        raise ValueError("manifest requires ciphertext_uri, ciphertext_sha256, artifact_chain_id, and version")
    _enforce_bucket_scope(uri, allowed_buckets)

    blob = _fetch_blob(uri)
    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha:
        raise RuntimeError(f"ciphertext sha256 mismatch: expected {expected_sha}, fetched {actual_sha}")
    if len(blob) <= 12:
        raise RuntimeError("ciphertext blob is too short to contain a nonce")

    plaintext = _decrypt(blob, manifest, master_key)

    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plaintext)
    destination.chmod(0o600)

    receipt: dict[str, Any] = {
        "schema_version": RECLAIM_SCHEMA_VERSION,
        "phase": "artifact-reclamation",
        "generated_at": current.isoformat(),
        "artifact_chain_id": chain_id,
        "version": version,
        "artifact_id": manifest.get("artifact_id"),
        "ciphertext_uri": uri,
        "ciphertext_sha256": expected_sha,
        "integrity_verified": True,
        "plaintext_bytes": len(plaintext),
        "plaintext_sha256": hashlib.sha256(plaintext).hexdigest(),
        "local_path": str(destination),
        "remote_deleted": False,
        "remote_verified_absent": None,
    }
    if delete_remote:
        _delete_blob(uri)
        receipt["remote_deleted"] = True
        receipt["remote_verified_absent"] = _blob_absent(uri)
    receipt["receipt_path"] = str(_write_receipt(receipt, current))
    return receipt


def _decrypt(blob: bytes, manifest: Mapping[str, Any], master_key: bytes) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    chain_id = str(manifest.get("artifact_chain_id") or "")
    version = str(manifest.get("version") or "")
    plan_hash = str(manifest.get("producer_plan_hash") or "")
    dek = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=plan_hash.encode("utf-8") or None,
        info=f"gpucall-artifact:{chain_id}:{version}".encode("utf-8"),
    ).derive(master_key)
    associated = json.dumps(
        {
            "plan_hash": plan_hash or None,
            "artifact_chain_id": chain_id,
            "version": version,
            "key_id": manifest.get("key_id"),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    nonce, ciphertext = blob[:12], blob[12:]
    return AESGCM(dek).decrypt(nonce, ciphertext, associated)


def _enforce_bucket_scope(uri: str, allowed_buckets: set[str] | None) -> None:
    if not uri.startswith("s3://"):
        return
    if allowed_buckets is None:
        return
    bucket = uri.removeprefix("s3://").partition("/")[0]
    if bucket not in allowed_buckets:
        raise RuntimeError(
            f"artifact bucket {bucket!r} is outside the operator scope {sorted(allowed_buckets)}; refusing to touch it"
        )


def _fetch_blob(uri: str) -> bytes:
    if uri.startswith("file://"):
        return Path(uri.removeprefix("file://")).read_bytes()
    if uri.startswith("s3://"):
        client, bucket, key = _s3_parts(uri)
        return client.get_object(Bucket=bucket, Key=key)["Body"].read()
    raise RuntimeError("artifact uri must be s3:// or file://")


def _delete_blob(uri: str) -> None:
    if uri.startswith("file://"):
        Path(uri.removeprefix("file://")).unlink(missing_ok=True)
        return
    client, bucket, key = _s3_parts(uri)
    client.delete_object(Bucket=bucket, Key=key)


def _blob_absent(uri: str) -> bool:
    if uri.startswith("file://"):
        return not Path(uri.removeprefix("file://")).exists()
    client, bucket, key = _s3_parts(uri)
    try:
        client.head_object(Bucket=bucket, Key=key)
    except Exception:
        return True
    return False


def _s3_parts(uri: str):
    import boto3

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
    return boto3.client("s3", **kwargs), bucket, key


def _write_receipt(receipt: Mapping[str, Any], current: datetime) -> Path:
    directory = default_state_dir() / "sovereignty"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"artifact-reclaim-{current.strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps(dict(receipt), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path
