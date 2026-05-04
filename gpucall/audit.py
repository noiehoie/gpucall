from __future__ import annotations

import hashlib
import json
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class AuditEvent(BaseModel):
    event_id: str
    event_type: str
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any]
    previous_hash: str | None = None
    hash: str | None = None


class AuditTrail:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash_cache = self._read_last_hash()

    def append(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        with self._lock:
            with self._file_lock():
                previous = self._read_last_hash()
                event = AuditEvent(
                    event_id=hashlib.sha256(
                        f"{datetime.now(timezone.utc).isoformat()}:{event_type}:{previous}".encode("utf-8")
                    ).hexdigest(),
                    event_type=event_type,
                    payload=redact_for_audit(payload),
                    previous_hash=previous,
                )
                material = event.model_dump(mode="json", exclude={"hash"})
                event.hash = self._hash(material)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n")
                self._last_hash_cache = event.hash
                return event

    def verify(self) -> bool:
        if not self.path.exists():
            return True
        previous: str | None = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                event = AuditEvent.model_validate_json(line)
                if event.previous_hash != previous:
                    return False
                actual = self._hash(event.model_dump(mode="json", exclude={"hash"}))
                if event.hash != actual:
                    return False
                previous = event.hash
        return True

    def _read_last_hash(self) -> str | None:
        if not self.path.exists():
            return None
        last = None
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    last = AuditEvent.model_validate_json(line).hash
        return last

    def rotate_if_needed(self, max_bytes: int) -> Path | None:
        if max_bytes <= 0 or not self.path.exists() or self.path.stat().st_size < max_bytes:
            return None
        with self._lock:
            with self._file_lock():
                if not self.path.exists() or self.path.stat().st_size < max_bytes:
                    return None
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                rotated = self.path.with_name(f"{self.path.stem}.{stamp}{self.path.suffix}")
                os.replace(self.path, rotated)
                self._last_hash_cache = None
                return rotated

    @contextmanager
    def _file_lock(self):
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a", encoding="utf-8") as lock_file:
            try:
                import fcntl

                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass

    @staticmethod
    def _hash(value: object) -> str:
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def redact_for_audit(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "inline_inputs" and isinstance(item, dict):
                redacted[key] = {name: _redact_inline(inline) for name, inline in item.items()}
            elif key == "input_refs" and isinstance(item, list):
                redacted[key] = [_redact_data_ref(ref) for ref in item]
            elif key in {"uri", "webhook_url"} and isinstance(item, str):
                redacted[key] = _fingerprint("url", item)
            elif _secretish_key(key):
                redacted[key] = _fingerprint(key, str(item))
            elif key.lower() in {"error", "message", "detail"} and isinstance(item, str):
                redacted[key] = _redact_log_text(item)
            else:
                redacted[key] = redact_for_audit(item)
        return redacted
    if isinstance(value, list):
        return [redact_for_audit(item) for item in value]
    return value


def _redact_inline(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        text = str(value)
        return {"redacted": True, "sha256": _sha256(text), "bytes": len(text.encode("utf-8"))}
    if value.get("redacted") is True and "value" not in value:
        return redact_for_audit(value)
    text = str(value.get("value", ""))
    return {
        "redacted": True,
        "sha256": _sha256(text),
        "bytes": len(text.encode("utf-8")),
        "content_type": value.get("content_type"),
    }


def _redact_data_ref(value: Any) -> Any:
    if not isinstance(value, dict):
        return redact_for_audit(value)
    redacted = redact_for_audit({key: item for key, item in value.items() if key != "uri"})
    redacted["uri"] = _fingerprint("url", str(value.get("uri", "")))
    return redacted


def _secretish_key(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in {"token", "authorization"}
        or lowered.endswith("_token")
        or lowered in {"api_key", "access_key", "secret_key", "token_id", "token_secret"}
        or any(token in lowered for token in ("api_key", "access_key", "secret", "signature", "authorization"))
    )


def _fingerprint(kind: str, value: str) -> dict[str, Any]:
    return {"redacted": True, "kind": kind, "sha256": _sha256(value)}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:X-Amz-Signature|X-Amz-Credential|AWSAccessKeyId|api_key|access_key|token|secret|signature)=)[^&\s]+"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def _redact_log_text(value: str) -> str:
    redacted = _QUERY_SECRET_RE.sub(r"\1<redacted>", value)
    redacted = _BEARER_RE.sub("Bearer <redacted>", redacted)
    return redacted


def redacted_plan_for_audit(plan: Any) -> dict[str, Any]:
    """Return an allowlisted plan summary for immutable audit records."""
    response_format = getattr(plan, "response_format", None)
    attestations = getattr(plan, "attestations", {}) or {}
    return {
        "plan_id": getattr(plan, "plan_id", None),
        "policy_version": getattr(plan, "policy_version", None),
        "recipe_name": getattr(plan, "recipe_name", None),
        "task": getattr(plan, "task", None),
        "mode": _enum_value(getattr(plan, "mode", None)),
        "provider_chain": list(getattr(plan, "provider_chain", []) or []),
        "timeout_seconds": getattr(plan, "timeout_seconds", None),
        "lease_ttl_seconds": getattr(plan, "lease_ttl_seconds", None),
        "tokenizer_family": getattr(plan, "tokenizer_family", None),
        "token_budget": getattr(plan, "token_budget", None),
        "max_tokens": getattr(plan, "max_tokens", None),
        "temperature": getattr(plan, "temperature", None),
        "system_prompt": _text_audit_summary(getattr(plan, "system_prompt", None)),
        "stop_tokens": list(getattr(plan, "stop_tokens", []) or []),
        "repetition_penalty": getattr(plan, "repetition_penalty", None),
        "guided_decoding": getattr(plan, "guided_decoding", None),
        "output_validation_attempts": getattr(plan, "output_validation_attempts", None),
        "messages": [_message_audit_summary(message) for message in getattr(plan, "messages", []) or []],
        "response_format": _response_format_summary(response_format),
        "artifact_export": _artifact_export_summary(getattr(plan, "artifact_export", None)),
        "split_learning": _split_learning_summary(getattr(plan, "split_learning", None)),
        "input_refs": [_data_ref_audit_summary(ref) for ref in getattr(plan, "input_refs", []) or []],
        "inline_inputs": {
            name: _inline_audit_summary(value) for name, value in (getattr(plan, "inline_inputs", {}) or {}).items()
        },
        "attestations": _attestation_audit_summary(attestations),
    }


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _response_format_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    raw_type = getattr(value, "type", None)
    schema = getattr(value, "json_schema", None)
    return {
        "type": _enum_value(raw_type),
        "strict": getattr(value, "strict", None),
        "json_schema_sha256": _sha256(json.dumps(schema, sort_keys=True, separators=(",", ":"))) if schema is not None else None,
    }


def _data_ref_audit_summary(value: Any) -> dict[str, Any]:
    content_type = getattr(value, "content_type", None)
    sha256 = getattr(value, "sha256", None)
    return {
        "redacted": True,
        "bytes": getattr(value, "bytes", None),
        "content_type": content_type,
        "sha256_prefix": str(sha256)[:12] if sha256 else None,
        "expires_at": getattr(value, "expires_at", None).isoformat() if getattr(value, "expires_at", None) else None,
    }


def _inline_audit_summary(value: Any) -> dict[str, Any]:
    text = str(getattr(value, "value", ""))
    return {
        "redacted": True,
        "bytes": len(text.encode("utf-8")),
        "sha256": _sha256(text),
        "content_type": getattr(value, "content_type", None),
    }


def _message_audit_summary(value: Any) -> dict[str, Any]:
    text = str(getattr(value, "content", ""))
    return {
        "redacted": True,
        "role": getattr(value, "role", None),
        "bytes": len(text.encode("utf-8")),
        "sha256": _sha256(text),
    }


def _text_audit_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    text = str(value)
    return {"redacted": True, "bytes": len(text.encode("utf-8")), "sha256": _sha256(text)}


def _artifact_export_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return {
        "artifact_chain_id": getattr(value, "artifact_chain_id", None),
        "version": getattr(value, "version", None),
        "key_id": _fingerprint("key_id", str(getattr(value, "key_id", ""))),
        "parent_artifact_ids": list(getattr(value, "parent_artifact_ids", []) or []),
        "legal_hold": getattr(value, "legal_hold", None),
        "retention_until": getattr(value, "retention_until", None).isoformat()
        if getattr(value, "retention_until", None)
        else None,
    }


def _split_learning_summary(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    activation_ref = getattr(value, "activation_ref", None)
    return {
        "activation_ref": _data_ref_audit_summary(activation_ref) if activation_ref is not None else None,
        "encoder_hash_prefix": str(getattr(value, "encoder_hash", ""))[:12],
        "dp_epsilon": getattr(value, "dp_epsilon", None),
        "dp_delta": getattr(value, "dp_delta", None),
        "irreversibility_claim": getattr(value, "irreversibility_claim", None),
    }


def _attestation_audit_summary(value: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"governance_hash": value.get("governance_hash")}
    if value.get("caller_governance_hash") is not None:
        summary["caller_governance_hash"] = value.get("caller_governance_hash")
    if value.get("context_estimate") is not None:
        summary["context_estimate"] = value.get("context_estimate")
    if value.get("recipe_snapshot") is not None:
        summary["recipe_snapshot"] = _recipe_snapshot_audit_summary(value.get("recipe_snapshot"))
    for key in ("compile_artifact", "attestation_evidence", "key_release", "artifact_manifest"):
        if value.get(key) is not None:
            summary[key] = redact_for_audit(value.get(key))
    return summary


def _recipe_snapshot_audit_summary(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _recipe_snapshot_audit_summary(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_recipe_snapshot_audit_summary(item) for item in value]
    if isinstance(value, str):
        return _text_audit_summary(value)
    return value
