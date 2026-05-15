from __future__ import annotations

import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

from gpucall.compiler import GovernanceError
from gpucall.domain import DataRef, PresignGetRequest, TaskRequest, TupleError
from gpucall.provider_errors import provider_error_class
from gpucall.routing import route_warning_tags
from gpucall.sqlite_store import SQLiteIdempotencyStore
from gpucall.tenant import TenantBudgetError, enforce_tenant_budget, tenant_identity

if TYPE_CHECKING:
    from gpucall.app import Runtime


def warning_headers(plan, tuples=None) -> dict[str, str]:
    warnings = route_warning_tags(plan, tuples)
    headers: dict[str, str] = {
        "X-GPUCall-Timeout-Seconds": str(getattr(plan, "timeout_seconds", "")),
        "X-GPUCall-Lease-TTL-Seconds": str(getattr(plan, "lease_ttl_seconds", "")),
    }
    if warnings:
        headers["X-GPUCall-Warning"] = ", ".join(warnings)
        if "remote_worker_cold_start_possible" in warnings:
            headers["X-GPUCall-Min-Client-Timeout-Seconds"] = str(getattr(plan, "timeout_seconds", ""))
    return {key: value for key, value in headers.items() if value}


def tenant_headers(headers: dict[str, str], request: Request) -> dict[str, str]:
    tenant_id = getattr(request.state, "tenant_id", None)
    if not tenant_id:
        return headers
    return {**headers, "X-GPUCall-Tenant": str(tenant_id)}


async def enforce_request_budget(runtime: Runtime, request: Request, plan: Any) -> None:
    api_key = getattr(request.state, "api_key", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    tenant_name = tenant_identity(tenant_id, api_key)
    tenant = runtime.tenants.get(tenant_name) or runtime.tenants.get("default")
    cost = getattr(plan, "attestations", {}).get("cost_estimate", {}) if getattr(plan, "attestations", None) else {}
    estimated = float(cost.get("estimated_cost_usd") or 0)
    tuple_chain = list(getattr(plan, "tuple_chain", []) or [])
    await asyncio.to_thread(
        enforce_tenant_budget,
        tenant_id=tenant_name,
        tenant=tenant,
        ledger=runtime.tenant_usage,
        estimated_cost_usd=estimated,
        tuple=tuple_chain[0] if tuple_chain else None,
        recipe=getattr(plan, "recipe_name", None),
        plan_id=getattr(plan, "plan_id", None),
    )


def refund_request_budget(runtime: Runtime, plan: Any | None) -> None:
    if plan is not None:
        runtime.tenant_usage.release_plan(getattr(plan, "plan_id", None))


def commit_request_budget(runtime: Runtime, plan: Any | None) -> None:
    if plan is not None:
        runtime.tenant_usage.commit_plan(getattr(plan, "plan_id", None))


def object_tenant_prefix(runtime: Runtime, request: Request) -> str | None:
    api_key = getattr(request.state, "api_key", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    tenant_name = tenant_identity(tenant_id, api_key)
    if tenant_name == "anonymous":
        if os.getenv("GPUCALL_ALLOW_ANONYMOUS_OBJECTS", "").strip().lower() in {"1", "true", "yes", "on"}:
            return None
        raise HTTPException(status_code=401, detail="object store access requires authenticated tenant")
    tenant = runtime.tenants.get(tenant_name) or runtime.tenants.get("default")
    if tenant is not None and tenant.object_prefix:
        return safe_tenant_object_prefix(tenant.object_prefix)
    return tenant_name


def safe_tenant_object_prefix(prefix: str) -> str:
    path = PurePosixPath(prefix)
    if path.is_absolute() or path.name != prefix or prefix in {"", ".", ".."}:
        raise HTTPException(status_code=500, detail="invalid tenant object prefix")
    return prefix


def request_needs_worker_object_access(request: TaskRequest) -> bool:
    """Only inbound DataRefs require gateway object-store tenant checks; artifact export is worker-owned."""
    return bool(request.input_refs) or (
        request.split_learning is not None and request.split_learning.activation_ref is not None
    )


def tenant_budget_error_response(exc: TenantBudgetError, *, request: TaskRequest | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "detail": str(exc),
            "code": exc.code,
            "failure_artifact": {
                "schema_version": 1,
                "failure_kind": "tenant_budget",
                "code": exc.code,
                "message": str(exc),
                "safe_request_summary": safe_request_summary(request),
                "redaction_guarantee": redaction_guarantee(),
            },
        },
    )


def enforce_gateway_owned_routing(request: TaskRequest) -> None:
    if request.bypass_circuit_for_validation:
        raise GovernanceError("caller-controlled circuit bypass is disabled; omit bypass_circuit_for_validation")
    if _allow_caller_routing():
        return
    forbidden: list[str] = []
    if request.recipe is not None:
        forbidden.append("recipe")
    if request.requested_tuple is not None:
        forbidden.append("requested_tuple")
    if forbidden:
        joined = ", ".join(forbidden)
        raise GovernanceError(f"caller-controlled routing is disabled; omit {joined}")


def _allow_caller_routing() -> bool:
    return not _production_auth_required() and os.getenv("GPUCALL_ALLOW_CALLER_ROUTING", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _public_metrics_enabled() -> bool:
    return os.getenv("GPUCALL_PUBLIC_METRICS", "").strip().lower() in {"1", "true", "yes", "on"}


def _production_auth_required() -> bool:
    return os.getenv("GPUCALL_ENV", "").strip().lower() in {"prod", "production"} or os.getenv(
        "GPUCALL_PRODUCTION", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


def _allow_unauthenticated_gateway() -> bool:
    return not _production_auth_required() and os.getenv("GPUCALL_ALLOW_UNAUTHENTICATED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }



def metric_route_path(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str):
        return path
    return "UNMATCHED"


def governance_status_code(exc: GovernanceError) -> int:
    if exc.code in {"NO_AUTO_SELECTABLE_RECIPE", "REQUEST_EXCEEDS_RECIPE_CONTEXT"}:
        return 422
    if exc.code == "NO_ELIGIBLE_TUPLE":
        return 503
    message = str(exc)
    if "circuit breaker" in message or "no eligible tuple after policy, recipe, and circuit constraints" in message:
        return 503
    return 400


def governance_error_response(exc: GovernanceError, *, request: TaskRequest | None = None) -> JSONResponse:
    content: dict[str, Any] = {"detail": str(exc), "code": exc.code}
    if exc.context:
        content["context"] = exc.context
    content["failure_artifact"] = build_governance_failure_artifact(exc, request)
    return JSONResponse(status_code=governance_status_code(exc), content=content)


def tuple_error_response(exc: TupleError, *, request: TaskRequest | None = None) -> JSONResponse:
    content = {
        "detail": public_tuple_error(exc),
        "code": exc.code or "PROVIDER_ERROR",
        "failure_artifact": build_provider_failure_artifact(exc, request),
    }
    return JSONResponse(status_code=exc.status_code, content=content)


def build_governance_failure_artifact(exc: GovernanceError, request: TaskRequest | None = None) -> dict[str, Any]:
    status_code = governance_status_code(exc)
    failure_kind = governance_failure_kind(exc)
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "failure_id": f"gf-{uuid4().hex}",
        "failure_kind": failure_kind,
        "code": exc.code,
        "status_code": status_code,
        "message": str(exc),
        "recipe_request_recommended": failure_kind in {"no_recipe", "input_contract"},
        "caller_action": caller_action_for_governance_failure(failure_kind),
        "safe_request_summary": safe_request_summary(request),
        "capability_gap": capability_gap_for_governance_failure(exc),
        "rejection_matrix": rejection_matrix_from_context(exc.context),
        "context": exc.context,
        "redaction_guarantee": redaction_guarantee(),
    }
    return artifact


def build_provider_failure_artifact(exc: TupleError, request: TaskRequest | None = None) -> dict[str, Any]:
    provider_class = provider_error_class(exc.code)
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "failure_id": f"pf-{uuid4().hex}",
        "failure_kind": "provider_temporary_unavailable" if provider_class is not None else "tuple_runtime",
        "code": exc.code or "PROVIDER_ERROR",
        "status_code": exc.status_code,
        "message": public_tuple_error(exc),
        "retryable": exc.retryable,
        "fallback_eligible": bool(provider_class.fallback_eligible) if provider_class is not None else exc.retryable,
        "cancel_remote": bool(provider_class.cancel_remote) if provider_class is not None else exc.retryable,
        "recipe_request_recommended": False,
        "caller_action": provider_class.caller_action if provider_class is not None else ("retry_later" if exc.retryable else "contact_gpucall_admin"),
        "safe_request_summary": safe_request_summary(request),
        "capability_gap": None,
        "rejection_matrix": {},
        "redaction_guarantee": redaction_guarantee(),
    }
    if provider_class is not None:
        artifact["provider_error_class"] = {
            "meaning": provider_class.meaning,
            "typical_state": provider_class.typical_state,
        }
    if exc.raw_output is not None and tuple_error_body_is_safe_for_artifact(exc):
        artifact["tuple_error_body_redacted"] = exc.raw_output
        artifact["tuple_error_body_sha256"] = hashlib.sha256(exc.raw_output.encode("utf-8")).hexdigest()
    return artifact


def tuple_error_body_is_safe_for_artifact(exc: TupleError) -> bool:
    return (exc.code or "") in {"PROVIDER_PROVISION_FAILED", "PROVIDER_PROVISION_UNAVAILABLE"}


def governance_failure_kind(exc: GovernanceError) -> str:
    if exc.code in {"NO_AUTO_SELECTABLE_RECIPE", "REQUEST_EXCEEDS_RECIPE_CONTEXT"}:
        return "no_recipe"
    if exc.code == "NO_ELIGIBLE_TUPLE":
        return "no_tuple"
    message = str(exc)
    if "not eligible" in message or "security" in message or "policy" in message:
        return "policy_denied"
    return "input_contract"


def caller_action_for_governance_failure(failure_kind: str) -> str:
    if failure_kind == "no_recipe":
        return "run_gpucall_recipe_draft_intake"
    if failure_kind == "no_tuple":
        return "check_tuple_health_or_retry_later"
    if failure_kind == "policy_denied":
        return "contact_gpucall_admin"
    return "fix_request_or_run_gpucall_recipe_draft_intake"


def capability_gap_for_governance_failure(exc: GovernanceError) -> str | None:
    context = exc.context
    if exc.code == "NO_ELIGIBLE_TUPLE":
        return "no_eligible_tuple"
    required_len = context.get("required_model_len")
    largest_len = context.get("largest_auto_recipe_model_len") or context.get("recipe_max_model_len")
    if isinstance(required_len, int) and isinstance(largest_len, int) and required_len > largest_len:
        return "context_window_too_small"
    rejections = context.get("rejections")
    if isinstance(rejections, list) and any("content_type" in str(item) for item in rejections):
        return "unsupported_content_type"
    return None


def rejection_matrix_from_context(context: dict[str, object]) -> dict[str, Any]:
    matrix: dict[str, Any] = {}
    rejections = context.get("rejections")
    if isinstance(rejections, list):
        matrix["recipes"] = _named_rejection_list(rejections)
    tuple_rejections = context.get("tuple_rejections")
    if isinstance(tuple_rejections, dict):
        matrix["tuples"] = {str(name): str(reason) for name, reason in sorted(tuple_rejections.items())}
    return matrix


def _named_rejection_list(rejections: list[object]) -> dict[str, str]:
    named: dict[str, str] = {}
    for item in rejections:
        text = str(item)
        name, separator, reason = text.partition(": ")
        if separator:
            named[name] = reason
        else:
            named[text] = ""
    return named


def safe_request_summary(request: TaskRequest | None) -> dict[str, Any]:
    if request is None:
        return {}
    input_ref_bytes = [ref.bytes for ref in request.input_refs if ref.bytes is not None]
    input_ref_content_types = sorted({ref.content_type for ref in request.input_refs if ref.content_type})
    inline_content_types = sorted({item.content_type for item in request.inline_inputs.values() if item.content_type})
    message_lengths = [len(message.content.encode("utf-8")) for message in request.messages]
    return {
        "task": request.task,
        "mode": request.mode.value,
        "classification": request.metadata.get("classification") or request.metadata.get("data_classification"),
        "input_ref_count": len(request.input_refs),
        "input_ref_content_types": input_ref_content_types,
        "input_ref_max_bytes": max(input_ref_bytes) if input_ref_bytes else None,
        "input_ref_total_bytes": sum(input_ref_bytes) if input_ref_bytes else None,
        "inline_input_count": len(request.inline_inputs),
        "inline_input_content_types": inline_content_types,
        "message_count": len(request.messages),
        "message_max_bytes": max(message_lengths) if message_lengths else None,
        "message_total_bytes": sum(message_lengths) if message_lengths else None,
        "response_format": request.response_format.type.value if request.response_format is not None else None,
        "max_tokens": request.max_tokens,
    }


def redaction_guarantee() -> dict[str, bool]:
    return {
        "prompt_body_included": False,
        "message_content_included": False,
        "data_ref_uri_included": False,
        "presigned_url_included": False,
        "api_key_included": False,
        "tuple_raw_output_included": False,
    }


def worker_readable_request(request: TaskRequest, runtime: Runtime, *, tenant_prefix: str | None = None) -> TaskRequest:
    split_ref = request.split_learning.activation_ref if request.split_learning is not None else None
    refs = list(request.input_refs)
    if split_ref is not None:
        refs.append(split_ref)
    if not refs:
        return request
    for ref in refs:
        if not str(ref.uri).startswith("s3://"):
            raise ValueError("input data_ref must be an object-store s3:// reference")
    if runtime.object_store is None:
        raise ValueError("object store is required for data_ref worker access")
    converted = [_worker_readable_ref(ref, runtime, tenant_prefix=tenant_prefix) for ref in request.input_refs]
    updates: dict[str, Any] = {"input_refs": converted}
    if request.split_learning is not None:
        converted_activation = _worker_readable_ref(request.split_learning.activation_ref, runtime, tenant_prefix=tenant_prefix)
        updates["split_learning"] = request.split_learning.model_copy(update={"activation_ref": converted_activation})
    return request.model_copy(update=updates)


def _worker_readable_ref(ref: DataRef, runtime: Runtime, *, tenant_prefix: str | None = None) -> DataRef:
    if not str(ref.uri).startswith("s3://"):
        return ref
    if runtime.object_store is None:
        return ref
    request = PresignGetRequest(data_ref=ref)
    try:
        return runtime.object_store.presign_get(request, tenant_prefix=tenant_prefix).data_ref
    except TypeError:
        if type(runtime.object_store).__module__.startswith("gpucall."):
            raise
        return runtime.object_store.presign_get(request).data_ref


def plan_with_worker_refs(plan: Any, refs: list[DataRef], *, split_learning: Any = None) -> Any:
    if not refs and split_learning is None:
        return plan
    updates: dict[str, Any] = {"input_refs": refs}
    if split_learning is not None:
        updates["split_learning"] = split_learning
    updated = plan.model_copy(update=updates)
    attestations = dict(getattr(updated, "attestations", {}) or {})
    original_hash = attestations.get("governance_hash")
    if original_hash is not None:
        attestations["caller_governance_hash"] = original_hash
    attestations["governance_hash"] = compiled_plan_hash(updated)
    return updated.model_copy(update={"attestations": attestations})


def compiled_plan_hash(plan: Any) -> str:
    material = plan.model_dump(mode="json", exclude={"attestations", "plan_id"})
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@asynccontextmanager
async def idempotency_execution_lock(
    locks: dict[str, asyncio.Lock],
    guard: asyncio.Lock,
    key: str | None,
):
    if key is None:
        yield
        return
    async with guard:
        lock = locks.setdefault(key, asyncio.Lock())
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
        waiters = getattr(lock, "_waiters", None)
        has_waiters = bool(waiters)
        async with guard:
            if locks.get(key) is lock and not lock.locked() and not has_waiters:
                locks.pop(key, None)


def idempotency_lookup(
    request: TaskRequest,
    cache: SQLiteIdempotencyStore | PostgresIdempotencyStore,
    *,
    request_hash: str,
    identity: str,
    ttl_seconds: float,
    max_entries: int,
) -> tuple[int, dict[str, Any], dict[str, str]] | str | None:
    if not request.idempotency_key:
        return None
    key = idempotency_cache_key(request, identity)
    cached = cache.get(key, ttl_seconds=ttl_seconds, max_entries=max_entries)
    if cached is None:
        return None
    cached_hash, status, content, headers, idempotency_status = cached
    if cached_hash != request_hash:
        raise HTTPException(status_code=409, detail="idempotency key reused with different request body")
    if idempotency_status == "pending":
        return "pending"
    return status, content, headers


def idempotency_reserve(
    request: TaskRequest,
    cache: SQLiteIdempotencyStore | PostgresIdempotencyStore,
    *,
    request_hash: str,
    identity: str,
    max_entries: int,
) -> bool:
    if not request.idempotency_key:
        return True
    key = idempotency_cache_key(request, identity)
    return cache.reserve(key, request_hash=request_hash, max_entries=max_entries)


def idempotency_store(
    request: TaskRequest,
    cache: SQLiteIdempotencyStore | PostgresIdempotencyStore,
    status: int,
    content: dict[str, Any],
    headers: dict[str, str],
    *,
    request_hash: str,
    identity: str,
    max_entries: int,
) -> None:
    if request.idempotency_key:
        cache.set(
            idempotency_cache_key(request, identity),
            request_hash=request_hash,
            status=status,
            content=content,
            headers=headers,
            max_entries=max_entries,
        )


def idempotency_release(
    request: TaskRequest,
    cache: SQLiteIdempotencyStore | PostgresIdempotencyStore,
    *,
    request_hash: str,
    identity: str,
) -> None:
    if request.idempotency_key and hasattr(cache, "release"):
        cache.release(idempotency_cache_key(request, identity), request_hash=request_hash)


def idempotency_cache_key(request: TaskRequest, identity: str) -> str:
    recipe_key = request.recipe or "auto"
    return f"{identity}:{request.task}:{recipe_key}:{request.mode}:{request.idempotency_key}"


def prune_rate_limit(rate_limit: dict[str, list[float]], now: float, *, max_identities: int) -> None:
    stale = [identity for identity, stamps in rate_limit.items() if not any(now - stamp < 60.0 for stamp in stamps)]
    for identity in stale:
        rate_limit.pop(identity, None)
    if len(rate_limit) <= max_identities:
        return
    ordered = sorted(rate_limit, key=lambda identity: max(rate_limit[identity] or [0.0]))
    for identity in ordered[: max(0, len(rate_limit) - max_identities)]:
        rate_limit.pop(identity, None)


def idempotency_identity(request: Request) -> str:
    api_key = getattr(request.state, "api_key", None)
    if api_key:
        return "api:" + hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()
    host = request.client.host if request.client else "unknown"
    return f"client:{host}"


def idempotency_request_hash(request: TaskRequest) -> str:
    body = request.model_dump(mode="json", exclude={"idempotency_key"})
    material = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def error_response(status_code: int, detail: str, *, code: str | None = None) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code or detail.replace(" ", "_"), "message": detail}})


def public_plan_summary(plan: Any, tuples: dict[str, Any] | None = None) -> dict[str, Any]:
    chain = list(getattr(plan, "tuple_chain", []) or [])
    selected_tuple = chain[0] if chain else None
    selected_spec = tuples.get(selected_tuple) if tuples and selected_tuple else None
    attestations = getattr(plan, "attestations", {}) or {}
    metadata = getattr(plan, "metadata", {}) or {}
    return {
        "recipe_name": getattr(plan, "recipe_name", None),
        "tuple_chain": chain,
        "selected_tuple": selected_tuple,
        "selected_tuple_model": getattr(selected_spec, "model", None) if selected_spec is not None else None,
        "requested_model": metadata.get("openai.model"),
        "governance_hash": attestations.get("governance_hash"),
        "system_prompt_transform": attestations.get("system_prompt_transform"),
        "output_validation_attempts": getattr(plan, "output_validation_attempts", None),
        "timeout_seconds": getattr(plan, "timeout_seconds", None),
    }


def public_tuple_error(exc: TupleError) -> str:
    return f"tuple execution failed ({exc.code or 'PROVIDER_ERROR'})"


def openai_error_response(
    status_code: int,
    message: str,
    *,
    code: str = "bad_request",
    headers: dict[str, str] | None = None,
    gpucall_failure_artifact: dict[str, Any] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"message": message, "type": "gpucall_error", "code": code}
    if gpucall_failure_artifact is not None:
        error["gpucall_failure_artifact"] = gpucall_failure_artifact
    return JSONResponse(status_code=status_code, content={"error": error}, headers=headers)
