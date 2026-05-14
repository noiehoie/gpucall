from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class ProviderRuntimeState(StrEnum):
    CANDIDATE = "candidate"
    ELIGIBLE = "eligible"
    LIVE_READY = "live_ready"
    IN_FLIGHT = "in_flight"
    SUPPRESSED = "suppressed"
    COOLING_DOWN = "cooling_down"
    CAPACITY_UNKNOWN = "capacity_unknown"
    EXHAUSTED = "exhausted"
    UNHEALTHY = "unhealthy"
    VALIDATION_REQUIRED = "validation_required"


class TenantBudgetState(StrEnum):
    OK = "ok"
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"
    REFUNDED = "refunded"
    EXHAUSTED = "exhausted"


class WorkloadAdmissionState(StrEnum):
    SYNC_SAFE = "sync_safe"
    ASYNC_REQUIRED = "async_required"
    QUEUED = "queued"
    RUNNING = "running"
    LATE_COMPLETED = "late_completed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class TupleQualityState(StrEnum):
    UNKNOWN = "unknown"
    PASSED = "passed"
    STRICT_SCHEMA_FAILED = "strict_schema_failed"
    SUPPRESSED = "suppressed"


@dataclass(frozen=True)
class OrthogonalRouteState:
    provider: ProviderRuntimeState
    tenant: TenantBudgetState
    workload: WorkloadAdmissionState
    tuple_quality: TupleQualityState

    def as_dict(self) -> dict[str, str]:
        return {
            "provider": self.provider.value,
            "tenant": self.tenant.value,
            "workload": self.workload.value,
            "tuple_quality": self.tuple_quality.value,
        }


def semantic_to_wire_transform_evidence(
    *,
    task: str,
    intent: str,
    mode: str,
    input_contract: str,
    output_contract: str,
) -> dict[str, Any]:
    """Return deterministic evidence that semantic caller intent became a worker contract.

    This is not a model-routing score. It is a stable audit record that a
    product-level semantic request was lowered to a declared wire shape.
    """

    payload = {
        "task": task,
        "intent": intent,
        "mode": mode,
        "input_contract": input_contract,
        "output_contract": output_contract,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "schema_version": 1,
        "transform": "semantic_to_worker_wire_contract",
        "payload": payload,
        "evidence_sha256": hashlib.sha256(encoded).hexdigest(),
    }


def production_route_gate(tuple_record: Mapping[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    if not tuple_record.get("endpoint_configured"):
        missing.append("endpoint_configured")
    if not tuple_record.get("validation_evidence"):
        missing.append("validation_evidence")
    if tuple_record.get("placeholder") is True:
        missing.append("not_placeholder")
    if tuple_record.get("quality_floor") == "smoke":
        missing.append("quality_floor_above_smoke")
    if tuple_record.get("candidate") is True and tuple_record.get("production_activated") is not True:
        missing.append("production_activated")
    return {
        "allowed": not missing,
        "missing": missing,
    }


def validate_failure_artifact_boundary(artifact: Mapping[str, Any]) -> dict[str, Any]:
    required = {
        "schema_version",
        "failure_id",
        "failure_kind",
        "code",
        "status_code",
        "caller_action",
        "redaction_guarantee",
    }
    missing = sorted(required - set(str(key) for key in artifact))
    serialized = json.dumps(_artifact_values_for_leak_scan(artifact), sort_keys=True, default=str)
    leaked_markers = [
        marker
        for marker in (
            "sk-",
            "gpk_",
            "presigned",
            "X-Amz-Signature",
            "prompt_body",
            "raw_prompt",
            "data_ref_uri",
        )
        if marker in serialized
    ]
    return {
        "valid": not missing and not leaked_markers,
        "missing": missing,
        "leaked_markers": leaked_markers,
    }


def _artifact_values_for_leak_scan(value: Any) -> Any:
    if isinstance(value, Mapping):
        return [_artifact_values_for_leak_scan(item) for item in value.values()]
    if isinstance(value, list):
        return [_artifact_values_for_leak_scan(item) for item in value]
    return value


def validate_openai_interaction_contract(payload: Mapping[str, Any]) -> dict[str, Any]:
    stream = bool(payload.get("stream"))
    stream_options = payload.get("stream_options") if isinstance(payload.get("stream_options"), Mapping) else {}
    response_format = payload.get("response_format") if isinstance(payload.get("response_format"), Mapping) else {}
    tools = payload.get("tools")
    return {
        "stream": stream,
        "include_usage": bool(stream_options.get("include_usage")),
        "response_format_type": response_format.get("type"),
        "tools_present": isinstance(tools, list) and bool(tools),
        "tool_choice_present": payload.get("tool_choice") is not None,
        "n": int(payload.get("n") or 1),
    }


def dataref_lifecycle_summary(ref: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "has_uri": bool(ref.get("uri")),
        "has_sha256": isinstance(ref.get("sha256"), str) and len(str(ref.get("sha256"))) == 64,
        "bytes": int(ref.get("bytes") or 0),
        "content_type": str(ref.get("content_type") or "application/octet-stream"),
        "expiry_seconds": int(ref.get("expiry_seconds") or 0),
        "uri_redacted": bool(ref.get("uri")) and "://" in str(ref.get("uri")),
        "body_included": "body" in ref or "bytes_body" in ref,
    }


def state_axes_are_orthogonal(state: Mapping[str, Any]) -> bool:
    required = {"provider", "tenant", "workload", "tuple_quality"}
    if set(state) != required:
        return False
    return all(isinstance(state[key], str) and state[key] for key in required)
