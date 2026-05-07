from __future__ import annotations

import hashlib
import importlib
import os
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def prompt_from_payload(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    if messages:
        return "\n".join(str(message.get("content", "")) for message in messages if str(message.get("content", "")))
    inline = payload.get("inline_inputs") or {}
    parts: list[str] = []
    if "prompt" in inline:
        parts.append(str(inline["prompt"]["value"]))
    else:
        parts.extend(str(value.get("value", "")) for value in inline.values())

    for ref in payload.get("input_refs") or []:
        parts.append(fetch_data_ref_text(ref))
    return "\n".join(part for part in parts if part)


def fetch_data_ref_text(ref: dict[str, Any]) -> str:
    return _decode_ref_body(fetch_data_ref_bytes(ref), ref)


def fetch_data_ref_bytes(ref: dict[str, Any]) -> bytes:
    uri = str(ref["uri"])
    max_bytes = _max_ref_bytes(ref)
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if not _ambient_s3_allowed(ref):
            raise ValueError("s3 data refs require gateway-presigned worker capability")
        return _fetch_s3_ref_bytes(parsed.netloc, parsed.path.lstrip("/"), max_bytes, ref)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"unsupported data ref scheme: {parsed.scheme}")
    if ref.get("gateway_presigned") is not True:
        raise ValueError("http(s) data refs must be gateway-presigned")
    request = Request(uri, headers={"user-agent": "gpucall-worker/2.0"})
    with urlopen(request, timeout=_ref_timeout_seconds()) as response:
        body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    expected = ref.get("sha256")
    if expected:
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise ValueError("data ref sha256 mismatch")
    return body


def _fetch_s3_ref_bytes(bucket: str, key: str, max_bytes: int, ref: dict[str, Any]) -> bytes:
    if not bucket or not key:
        raise ValueError("s3 data ref must include bucket and key")
    try:
        boto3 = importlib.import_module("boto3")
    except ImportError as exc:
        raise RuntimeError("boto3 is required for s3:// data refs") from exc

    client_kwargs: dict[str, Any] = {}
    endpoint_url = ref.get("endpoint_url") or os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("R2_ENDPOINT_URL")
    region_name = ref.get("region") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if endpoint_url:
        client_kwargs["endpoint_url"] = str(endpoint_url)
    if region_name:
        client_kwargs["region_name"] = str(region_name)

    client = boto3.client("s3", **client_kwargs)
    response = client.get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = stream.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    return b"".join(chunks)


def _decode_ref_body(body: bytes, ref: dict[str, Any]) -> str:
    expected = ref.get("sha256")
    if expected:
        actual = hashlib.sha256(body).hexdigest()
        if actual != expected:
            raise ValueError("data ref sha256 mismatch")
    content_type = str(ref.get("content_type") or "").lower()
    if content_type and not (content_type.startswith("text/") or "json" in content_type):
        return body.hex()
    return body.decode("utf-8")


def _max_ref_bytes(ref: dict[str, Any]) -> int:
    configured = int(os.getenv("GPUCALL_WORKER_MAX_REF_BYTES", "16777216"))
    declared = ref.get("bytes")
    if declared is None:
        return configured
    return min(configured, int(declared))


def _ref_timeout_seconds() -> float:
    try:
        return max(float(os.getenv("GPUCALL_WORKER_REF_TIMEOUT_SECONDS", "30")), 1.0)
    except ValueError:
        return 30.0


def _ambient_s3_allowed(ref: dict[str, Any]) -> bool:
    if ref.get("allow_worker_s3_credentials") is True:
        return True
    return os.getenv("GPUCALL_WORKER_ALLOW_AMBIENT_S3", "").strip().lower() in {"1", "true", "yes", "on"}
