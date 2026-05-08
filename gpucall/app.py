from __future__ import annotations

import os
import time
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.audit import AuditTrail
from gpucall.compiler import GovernanceCompiler, GovernanceError
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_config
from gpucall.credentials import load_credentials
from gpucall.dispatcher import Dispatcher, LeaseReaper, TupleReconciler
from gpucall.domain import ChatMessage, DataRef, ExecutionMode, InlineValue, JobRecord, JobState, TupleError, ResponseFormat, TaskRequest, recipe_requirements
from gpucall.domain import PresignGetRequest, PresignGetResponse, PresignPutRequest, PresignPutResponse
from gpucall.object_store import ObjectStore
from gpucall.execution.factory import build_adapters
from gpucall.registry import ObservedRegistry
from gpucall.routing import route_warning_tags
from gpucall.tuple_catalog import live_tuple_catalog_evidence
from gpucall.sqlite_store import SQLiteIdempotencyStore, SQLiteJobStore
from gpucall.tenant import (
    TenantBudgetError,
    TenantUsageLedger,
    enforce_tenant_budget,
    legacy_api_keys,
    tenant_for_api_key,
    tenant_identity,
    tenant_key_map,
)


class Runtime(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    compiler: GovernanceCompiler
    dispatcher: Dispatcher
    jobs: Any
    reaper: LeaseReaper
    reconciler: TupleReconciler
    artifact_registry: SQLiteArtifactRegistry
    object_store: ObjectStore | None = None
    tenants: dict[str, Any] = {}
    tenant_usage: Any = None
    metrics: dict[str, Any] = {}


class OpenAIChatMessage(BaseModel):
    role: Literal["system", "user", "assistant", "tool"] = "user"
    content: str | list[dict[str, Any]]


class OpenAIChatCompletionRequest(BaseModel):
    model: str = "gpucall:auto"
    messages: list[OpenAIChatMessage] = Field(min_length=1)
    response_format: ResponseFormat | None = None
    temperature: float | None = None
    max_tokens: int | None = Field(default=None, gt=0)
    stream: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)


def build_runtime(config_dir: Path) -> Runtime:
    config = load_config(config_dir)
    state_dir = default_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    policy = config.policy
    recipes = config.recipes
    tuples = config.tuples
    registry = ObservedRegistry(path=state_dir / "registry.db")
    if os.getenv("GPUCALL_LIVE_CATALOG_ON_STARTUP", "").strip().lower() in {"1", "true", "yes", "on"}:
        for tuple_name, evidence in live_tuple_catalog_evidence(tuples, load_credentials()).items():
            if evidence.get("status") == "blocked":
                registry.mark_unavailable(tuple_name)
    audit = AuditTrail(state_dir / "audit" / "trail.jsonl")
    artifact_registry = SQLiteArtifactRegistry(state_dir / "artifacts.db")
    object_store = ObjectStore(config.object_store) if config.object_store is not None else None
    jobs = SQLiteJobStore(state_dir / "state.db")
    adapters = build_adapters(tuples)
    compiler = GovernanceCompiler(
        policy=policy,
        recipes=recipes,
        tuples=tuples,
        models=config.models,
        engines=config.engines,
        registry=registry,
    )
    dispatcher = Dispatcher(
        adapters=adapters,
        registry=registry,
        audit=audit,
        jobs=jobs,
        tuple_costs={name: float(tuple.cost_per_second) for name, tuple in tuples.items()},
        artifact_registry=artifact_registry,
    )
    reaper = LeaseReaper(jobs=jobs, audit=audit, cancel_job=dispatcher.cancel_job)
    reconciler = TupleReconciler(adapters=adapters, audit=audit)
    return Runtime(
        compiler=compiler,
        dispatcher=dispatcher,
        jobs=jobs,
        reaper=reaper,
        reconciler=reconciler,
        artifact_registry=artifact_registry,
        object_store=object_store,
        tenants=config.tenants,
        tenant_usage=TenantUsageLedger(state_dir / "tenant_usage.db"),
        metrics={"requests": {}, "latency_ms": []},
    )


def create_app(config_dir: Path | None = None) -> FastAPI:
    root = config_dir or default_config_dir()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            runtime = build_runtime(root)
        except ConfigError as exc:
            raise RuntimeError(str(exc)) from exc
        app.state.runtime = runtime
        await recover_interrupted_jobs(runtime)
        runtime.reaper.start()
        runtime.reconciler.start()
        try:
            yield
        finally:
            await runtime.reconciler.stop()
            await runtime.reaper.stop()

    app = FastAPI(title="gpucall v2.0", version="2.0.1", lifespan=lifespan)
    max_request_bytes = int(os.getenv("GPUCALL_MAX_REQUEST_BYTES", "1048576"))
    configured_api_keys = load_credentials().get("auth", {}).get("api_keys", "")
    idempotency_cache = SQLiteIdempotencyStore(default_state_dir() / "idempotency.db")
    rate_limit: dict[str, list[float]] = {}
    requests_per_minute = int(os.getenv("GPUCALL_RATE_LIMIT_PER_MINUTE", "120"))
    idempotency_ttl_seconds = float(os.getenv("GPUCALL_IDEMPOTENCY_TTL_SECONDS", "3600"))
    idempotency_cache_max = int(os.getenv("GPUCALL_IDEMPOTENCY_CACHE_MAX", "10000"))
    rate_limit_identity_max = int(os.getenv("GPUCALL_RATE_LIMIT_IDENTITY_MAX", "10000"))

    @app.exception_handler(RequestValidationError)
    async def sanitized_validation_error(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = []
        for error in exc.errors():
            errors.append(
                {
                    "type": error.get("type"),
                    "loc": list(error.get("loc", [])),
                    "msg": error.get("msg"),
                }
            )
        return JSONResponse(status_code=422, content={"detail": errors})

    @app.middleware("http")
    async def reject_oversized_requests(request: Request, call_next):
        if request.method in {"POST", "PUT", "PATCH"}:
            raw_length = request.headers.get("content-length")
            if raw_length is not None:
                try:
                    content_length = int(raw_length)
                except ValueError:
                    return JSONResponse(status_code=400, content={"detail": "invalid Content-Length"})
                if content_length > max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "detail": "request body too large; use the gpucall SDK DataRef upload path for large inputs"
                        },
                    )
            chunks: list[bytes] = []
            total = 0
            async for chunk in request.stream():
                total += len(chunk)
                if total > max_request_bytes:
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "request body too large; use the gpucall SDK DataRef upload path for large inputs"},
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
            if len(body) > max_request_bytes:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "request body too large; use the gpucall SDK DataRef upload path for large inputs"},
                )
            delivered = False

            async def replay_body():
                nonlocal delivered
                if delivered:
                    return {"type": "http.request", "body": b"", "more_body": False}
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = replay_body
            request._stream_consumed = False
        return await call_next(request)

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        tenant_keys = tenant_key_map()
        configured = legacy_api_keys() + list(tenant_keys)
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        if not configured:
            if _production_auth_required():
                return error_response(401, "unauthorized")
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if not any(hmac.compare_digest(token, key) for key in configured):
            return error_response(401, "unauthorized")
        request.state.api_key = token
        request.state.tenant_id = tenant_for_api_key(token)
        return await call_next(request)

    @app.middleware("http")
    async def basic_rate_limit(request: Request, call_next):
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        tenant_id = getattr(request.state, "tenant_id", None)
        client_host = request.client.host if request.client else "unknown"
        identity = tenant_id or getattr(request.state, "api_key", None) or client_host
        tenant = app.state.runtime.tenants.get(tenant_id) if tenant_id and hasattr(app.state, "runtime") else None
        effective_rpm = int(tenant.requests_per_minute) if tenant is not None and tenant.requests_per_minute else requests_per_minute
        now = time.monotonic()
        window = [stamp for stamp in rate_limit.get(identity, []) if now - stamp < 60.0]
        if len(window) >= effective_rpm:
            return error_response(429, "rate limit exceeded")
        window.append(now)
        rate_limit[identity] = window
        prune_rate_limit(rate_limit, now, max_identities=rate_limit_identity_max)
        started = time.monotonic()
        response = await call_next(request)
        runtime = getattr(app.state, "runtime", None)
        if runtime is not None:
            key = f"{request.method} {metric_route_path(request)} {response.status_code}"
            runtime.metrics["requests"][key] = runtime.metrics["requests"].get(key, 0) + 1
            runtime.metrics["latency_ms"].append((time.monotonic() - started) * 1000)
            runtime.metrics["latency_ms"] = runtime.metrics["latency_ms"][-1000:]
        return response

    def runtime_dep() -> Runtime:
        return app.state.runtime

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(runtime: Runtime = Depends(runtime_dep)) -> dict[str, object]:
        return {
            "status": "ready",
            "object_store": runtime.object_store is not None,
            "tenants_configured": sorted(runtime.tenants),
            "recipes": {
                name: {
                    "task": recipe.task,
                    "auto_select": recipe.auto_select,
                    "context_budget_tokens": recipe_requirements(recipe).context_budget_tokens,
                }
                for name, recipe in sorted(runtime.compiler.recipes.items())
            },
            "tuples": {
                name: {
                    "adapter": tuple.adapter,
                    "execution_surface": tuple.execution_surface.value if tuple.execution_surface else None,
                    "max_model_len": tuple.max_model_len,
                    "model": tuple.model,
                    "modes": [mode.value for mode in tuple.modes],
                    "input_contracts": tuple.input_contracts,
                }
                for name, tuple in sorted(runtime.compiler.tuples.items())
            },
        }

    @app.get("/metrics")
    async def metrics(runtime: Runtime = Depends(runtime_dep)) -> dict[str, object]:
        if not (os.getenv("GPUCALL_API_KEYS") or configured_api_keys or os.getenv("GPUCALL_TENANT_API_KEYS") or tenant_key_map()) and not _public_metrics_enabled():
            raise HTTPException(status_code=403, detail="metrics require authentication or GPUCALL_PUBLIC_METRICS=1")
        latencies = runtime.metrics.get("latency_ms", [])
        avg = sum(latencies) / len(latencies) if latencies else 0.0
        return {
            "requests": runtime.metrics.get("requests", {}),
            "latency_ms_avg": avg,
            "latency_samples": len(latencies),
            "registry": runtime.dispatcher.registry.snapshot(),
        }

    @app.post("/v2/tasks/sync")
    async def task_sync(
        request: TaskRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)
    ) -> JSONResponse:
        if request.mode is not ExecutionMode.SYNC:
            raise HTTPException(status_code=400, detail="use /v2/tasks/sync with mode=sync")
        try:
            enforce_gateway_owned_routing(request)
            plan = runtime.compiler.compile(request)
            caller_request_hash = idempotency_request_hash(request)
            cached = idempotency_lookup(
                request,
                idempotency_cache,
                request_hash=caller_request_hash,
                identity=idempotency_identity(http_request),
                ttl_seconds=idempotency_ttl_seconds,
                max_entries=idempotency_cache_max,
            )
            if cached is not None:
                return JSONResponse(status_code=cached[0], content=cached[1], headers=cached[2])
            enforce_request_budget(runtime, http_request, plan)
            request = worker_readable_request(request, runtime)
            plan = plan_with_worker_refs(plan, request.input_refs, split_learning=request.split_learning)
            result = await runtime.dispatcher.execute_sync(plan)
            headers = warning_headers(plan, runtime.compiler.tuples)
            content = {
                "plan_id": plan.plan_id,
                "plan": public_plan_summary(plan, runtime.compiler.tuples),
                "result": result.model_dump(mode="json"),
            }
            idempotency_store(
                request,
                idempotency_cache,
                200,
                content,
                headers,
                request_hash=caller_request_hash,
                identity=idempotency_identity(http_request),
                max_entries=idempotency_cache_max,
            )
            return JSONResponse(status_code=200, content=content, headers=tenant_headers(headers, http_request))
        except GovernanceError as exc:
            return governance_error_response(exc, request=request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TupleError as exc:
            return tuple_error_response(exc, request=request)
        except TenantBudgetError as exc:
            return tenant_budget_error_response(exc, request=request)

    @app.post("/v1/chat/completions")
    async def openai_chat_completions(
        request: OpenAIChatCompletionRequest,
        http_request: Request,
        runtime: Runtime = Depends(runtime_dep),
    ) -> JSONResponse:
        if request.stream:
            return openai_error_response(400, "stream is not supported by the gpucall OpenAI facade in v2.0 MVP")
        allowed_models = {"gpucall:auto", "gpucall:chat"}
        if request.model not in allowed_models:
            return openai_error_response(
                400,
                "OpenAI facade model must be one of: gpucall:auto, gpucall:chat",
                code="unsupported_model",
            )
        messages = [_openai_message_to_chat_message(message) for message in request.messages]
        message_bytes = sum(len(message.content.encode("utf-8")) for message in messages)
        if message_bytes > runtime.compiler.policy.inline_bytes_limit:
            return openai_error_response(
                413,
                "OpenAI facade inline prompt exceeds policy limit; use the gpucall SDK DataRef upload path for large inputs",
                code="payload_too_large",
            )
        task_request = TaskRequest(
            task="infer",
            mode=ExecutionMode.SYNC,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            response_format=request.response_format,
            metadata=request.metadata,
            )
        try:
            plan = runtime.compiler.compile(task_request)
            enforce_request_budget(runtime, http_request, plan)
            result = await runtime.dispatcher.execute_sync(plan)
            headers = warning_headers(plan, runtime.compiler.tuples)
            if result.output_validated is not None:
                headers["X-GPUCall-Output-Validated"] = "true" if result.output_validated else "false"
            return JSONResponse(
                status_code=200,
                headers=tenant_headers(headers, http_request),
                content=openai_chat_response(
                    request.model,
                    result.value or "",
                    result.usage,
                    gpucall=public_plan_summary(plan, runtime.compiler.tuples),
                    output_validated=result.output_validated,
                ),
            )
        except GovernanceError as exc:
            status_code = governance_status_code(exc)
            code = "tuple_unavailable" if status_code == 503 else exc.code.lower()
            return openai_error_response(
                status_code,
                str(exc),
                code=code,
                gpucall_failure_artifact=build_governance_failure_artifact(exc, task_request),
            )
        except TupleError as exc:
            headers: dict[str, str] = {}
            if exc.raw_output is not None:
                headers["X-GPUCall-Output-Validated"] = "false"
            return openai_error_response(
                exc.status_code,
                public_tuple_error(exc),
                code=exc.code or "tuple_error",
                headers=headers,
                gpucall_failure_artifact=build_provider_failure_artifact(exc, task_request),
            )
        except TenantBudgetError as exc:
            return openai_error_response(exc.status_code, str(exc), code=exc.code.lower())

    @app.post("/v2/tasks/async")
    async def task_async(
        request: TaskRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)
    ) -> JSONResponse:
        if request.mode is not ExecutionMode.ASYNC:
            raise HTTPException(status_code=400, detail="use /v2/tasks/async with mode=async")
        try:
            enforce_gateway_owned_routing(request)
            plan = runtime.compiler.compile(request)
            caller_request_hash = idempotency_request_hash(request)
            cached = idempotency_lookup(
                request,
                idempotency_cache,
                request_hash=caller_request_hash,
                identity=idempotency_identity(http_request),
                ttl_seconds=idempotency_ttl_seconds,
                max_entries=idempotency_cache_max,
            )
            if cached is not None:
                return JSONResponse(status_code=cached[0], content=cached[1], headers=cached[2])
            enforce_request_budget(runtime, http_request, plan)
            request = worker_readable_request(request, runtime)
            plan = plan_with_worker_refs(plan, request.input_refs, split_learning=request.split_learning)
            owner_identity = idempotency_identity(http_request)
            job = await runtime.dispatcher.submit_async(plan, owner_identity=owner_identity)
            headers = warning_headers(plan, runtime.compiler.tuples)
            content = {
                "job_id": job.job_id,
                "state": job.state,
                "status_url": f"/v2/jobs/{job.job_id}",
                "plan": public_plan_summary(plan, runtime.compiler.tuples),
            }
            idempotency_store(
                request,
                idempotency_cache,
                202,
                content,
                headers,
                request_hash=caller_request_hash,
                identity=owner_identity,
                max_entries=idempotency_cache_max,
            )
            return JSONResponse(
                status_code=202,
                content=content,
                headers=tenant_headers(headers, http_request),
            )
        except GovernanceError as exc:
            return governance_error_response(exc, request=request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TenantBudgetError as exc:
            return tenant_budget_error_response(exc, request=request)

    @app.post("/v2/tasks/stream")
    async def task_stream(request: TaskRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> StreamingResponse:
        if request.mode is not ExecutionMode.STREAM:
            raise HTTPException(status_code=400, detail="use /v2/tasks/stream with mode=stream")
        try:
            enforce_gateway_owned_routing(request)
            plan = runtime.compiler.compile(request)
            enforce_request_budget(runtime, http_request, plan)
            request = worker_readable_request(request, runtime)
            plan = plan_with_worker_refs(plan, request.input_refs, split_learning=request.split_learning)
        except GovernanceError as exc:
            return governance_error_response(exc, request=request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def events():
            async for event in runtime.dispatcher.execute_stream(plan):
                yield event

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            status_code=200,
            headers=tenant_headers(warning_headers(plan, runtime.compiler.tuples), http_request),
        )

    @app.get("/v2/jobs/{job_id}")
    async def get_job(job_id: str, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> JobRecord:
        job = await runtime.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="job not found")
        if job.owner_identity is not None and job.owner_identity != idempotency_identity(http_request):
            raise HTTPException(status_code=404, detail="job not found")
        return job

    @app.post("/v2/objects/presign-put")
    async def presign_put(request: PresignPutRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> PresignPutResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        return runtime.object_store.presign_put(request, tenant_prefix=object_tenant_prefix(runtime, http_request))

    @app.post("/v2/objects/presign-get")
    async def presign_get(request: PresignGetRequest, runtime: Runtime = Depends(runtime_dep)) -> PresignGetResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        try:
            return runtime.object_store.presign_get(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v2/results/presign-put")
    async def result_presign_put(request: PresignPutRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> PresignPutResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        return runtime.object_store.presign_put(request, tenant_prefix=object_tenant_prefix(runtime, http_request))

    return app


async def recover_interrupted_jobs(runtime: Runtime) -> None:
    for job in await runtime.jobs.all():
        if job.state not in {JobState.PENDING, JobState.RUNNING}:
            continue
        await runtime.jobs.update(job.job_id, state=JobState.EXPIRED, error="gateway restarted before job completion")
        runtime.dispatcher.audit.append("job.interrupted", {"job_id": job.job_id, "plan_id": job.plan.plan_id})


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


def enforce_request_budget(runtime: Runtime, request: Request, plan: Any) -> None:
    api_key = getattr(request.state, "api_key", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    tenant_name = tenant_identity(tenant_id, api_key)
    tenant = runtime.tenants.get(tenant_name) or runtime.tenants.get("default")
    cost = getattr(plan, "attestations", {}).get("cost_estimate", {}) if getattr(plan, "attestations", None) else {}
    estimated = float(cost.get("estimated_cost_usd") or 0)
    tuple_chain = list(getattr(plan, "tuple_chain", []) or [])
    enforce_tenant_budget(
        tenant_id=tenant_name,
        tenant=tenant,
        ledger=runtime.tenant_usage,
        estimated_cost_usd=estimated,
        tuple=tuple_chain[0] if tuple_chain else None,
        recipe=getattr(plan, "recipe_name", None),
        plan_id=getattr(plan, "plan_id", None),
    )


def object_tenant_prefix(runtime: Runtime, request: Request) -> str | None:
    api_key = getattr(request.state, "api_key", None)
    tenant_id = getattr(request.state, "tenant_id", None)
    tenant_name = tenant_identity(tenant_id, api_key)
    tenant = runtime.tenants.get(tenant_name) or runtime.tenants.get("default")
    if tenant is not None and tenant.object_prefix:
        return tenant.object_prefix
    return tenant_name if tenant_name != "anonymous" else None


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
    return os.getenv("GPUCALL_ALLOW_CALLER_ROUTING", "").strip().lower() in {"1", "true", "yes", "on"}


def _public_metrics_enabled() -> bool:
    return os.getenv("GPUCALL_PUBLIC_METRICS", "").strip().lower() in {"1", "true", "yes", "on"}


def _production_auth_required() -> bool:
    return os.getenv("GPUCALL_ENV", "").strip().lower() in {"prod", "production"} or os.getenv(
        "GPUCALL_PRODUCTION", ""
    ).strip().lower() in {"1", "true", "yes", "on"}


_HEX_ID_RE = re.compile(r"/[0-9a-fA-F]{16,}(?=/|$)")


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
    artifact: dict[str, Any] = {
        "schema_version": 1,
        "failure_id": f"pf-{uuid4().hex}",
        "failure_kind": "tuple_runtime",
        "code": exc.code or "PROVIDER_ERROR",
        "status_code": exc.status_code,
        "message": public_tuple_error(exc),
        "retryable": exc.retryable,
        "recipe_request_recommended": False,
        "caller_action": "retry_later" if exc.retryable else "contact_gpucall_admin",
        "safe_request_summary": safe_request_summary(request),
        "capability_gap": None,
        "rejection_matrix": {},
        "redaction_guarantee": redaction_guarantee(),
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


def worker_readable_request(request: TaskRequest, runtime: Runtime) -> TaskRequest:
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
    converted = [_worker_readable_ref(ref, runtime) for ref in request.input_refs]
    updates: dict[str, Any] = {"input_refs": converted}
    if request.split_learning is not None:
        converted_activation = _worker_readable_ref(request.split_learning.activation_ref, runtime)
        updates["split_learning"] = request.split_learning.model_copy(update={"activation_ref": converted_activation})
    return request.model_copy(update=updates)


def _worker_readable_ref(ref: DataRef, runtime: Runtime) -> DataRef:
    if not str(ref.uri).startswith("s3://"):
        return ref
    if runtime.object_store is None:
        return ref
    return runtime.object_store.presign_get(PresignGetRequest(data_ref=ref)).data_ref


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


def idempotency_lookup(
    request: TaskRequest,
    cache: SQLiteIdempotencyStore,
    *,
    request_hash: str,
    identity: str,
    ttl_seconds: float,
    max_entries: int,
):
    if not request.idempotency_key:
        return None
    key = idempotency_cache_key(request, identity)
    cached = cache.get(key, ttl_seconds=ttl_seconds, max_entries=max_entries)
    if cached is None:
        return None
    cached_hash, status, content, headers = cached
    if cached_hash != request_hash:
        raise HTTPException(status_code=409, detail="idempotency key reused with different request body")
    return status, content, headers


def idempotency_store(
    request: TaskRequest,
    cache: SQLiteIdempotencyStore,
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


def openai_chat_response(
    model: str,
    content: str,
    usage: dict[str, int],
    *,
    gpucall: dict[str, Any] | None = None,
    output_validated: bool | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    payload: dict[str, Any] = {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }
    if gpucall is not None:
        payload["gpucall"] = gpucall
    if output_validated is not None:
        payload["output_validated"] = output_validated
    return payload


def public_plan_summary(plan: Any, tuples: dict[str, Any] | None = None) -> dict[str, Any]:
    chain = list(getattr(plan, "tuple_chain", []) or [])
    selected_tuple = chain[0] if chain else None
    selected_spec = tuples.get(selected_tuple) if tuples and selected_tuple else None
    attestations = getattr(plan, "attestations", {}) or {}
    return {
        "recipe_name": getattr(plan, "recipe_name", None),
        "tuple_chain": chain,
        "selected_tuple": selected_tuple,
        "selected_tuple_model": getattr(selected_spec, "model", None) if selected_spec is not None else None,
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


def _openai_message_to_chat_message(message: OpenAIChatMessage) -> ChatMessage:
    return ChatMessage(role=message.role, content=_message_content_to_text(message.content))


def _message_content_to_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    raise HTTPException(
        status_code=400,
        detail="OpenAI facade accepts string message content only; use gpucall DataRef APIs for structured or multimodal inputs",
    )


app = create_app()
