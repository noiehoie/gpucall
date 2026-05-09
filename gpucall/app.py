from __future__ import annotations

import asyncio
import ipaddress
import os
import secrets
import time
import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from uuid import uuid4
import yaml

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field

from gpucall.app_helpers import (
    _allow_unauthenticated_gateway,
    _public_metrics_enabled,
    compiled_plan_hash,
    enforce_gateway_owned_routing,
    enforce_request_budget,
    error_response,
    build_governance_failure_artifact,
    build_provider_failure_artifact,
    governance_error_response,
    governance_status_code,
    idempotency_cache_key,
    idempotency_execution_lock,
    idempotency_identity,
    idempotency_lookup,
    idempotency_request_hash,
    idempotency_store,
    metric_route_path,
    object_tenant_prefix,
    openai_chat_response,
    openai_error_response,
    plan_with_worker_refs,
    prune_rate_limit,
    public_plan_summary,
    public_tuple_error,
    request_needs_worker_object_access,
    safe_tenant_object_prefix,
    tenant_budget_error_response,
    tenant_headers,
    tuple_error_response,
    warning_headers,
    worker_readable_request,
)
from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.audit import AuditTrail
from gpucall.compiler import GovernanceCompiler, GovernanceError
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_config
from gpucall.credentials import credentials_path, load_credentials, save_credentials
from gpucall.dispatcher import Dispatcher, LeaseReaper, TupleReconciler
from gpucall.domain import ApiKeyHandoffMode, ChatMessage, DataRef, ExecutionMode, InlineValue, JobRecord, JobState, TenantSpec, TupleError, ResponseFormat, TaskRequest, recipe_requirements
from gpucall.domain import PresignGetRequest, PresignGetResponse, PresignPutRequest, PresignPutResponse
from gpucall.object_store import ObjectStore
from gpucall.execution.factory import build_adapters
from gpucall.postgres_store import PostgresIdempotencyStore, PostgresJobStore
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


class BatchTaskRequest(BaseModel):
    requests: list[TaskRequest] = Field(min_length=1, max_length=64)
    continue_on_error: bool = True


class BootstrapTenantKeyRequest(BaseModel):
    system_name: str = Field(min_length=2, max_length=63)
    requests_per_minute: int | None = Field(default=None, gt=0)
    daily_budget_usd: float | None = Field(default=None, ge=0)
    monthly_budget_usd: float | None = Field(default=None, ge=0)
    max_request_estimated_cost_usd: float | None = Field(default=None, ge=0)
    object_prefix: str | None = None


def _database_url() -> str | None:
    value = os.getenv("GPUCALL_DATABASE_URL") or os.getenv("DATABASE_URL")
    return value.strip() if value and value.strip() else None


def _job_store(state_dir: Path):
    database_url = _database_url()
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        return PostgresJobStore(database_url)
    return SQLiteJobStore(state_dir / "state.db")


def _idempotency_store(state_dir: Path):
    database_url = _database_url()
    if database_url and database_url.startswith(("postgres://", "postgresql://")):
        return PostgresIdempotencyStore(database_url)
    return SQLiteIdempotencyStore(state_dir / "idempotency.db")


_BOOTSTRAP_TENANT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,62}$")


def _bootstrap_client_allowed(client_host: str, cidrs: tuple[str, ...], hosts: tuple[str, ...]) -> bool:
    if client_host in hosts:
        return True
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return False
    for raw_cidr in cidrs:
        try:
            if client_ip in ipaddress.ip_network(raw_cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


def _bootstrap_tenant_spec(request: BootstrapTenantKeyRequest, tenant_name: str) -> TenantSpec:
    return TenantSpec(
        name=tenant_name,
        requests_per_minute=request.requests_per_minute or 120,
        daily_budget_usd=request.daily_budget_usd if request.daily_budget_usd is not None else 25.0,
        monthly_budget_usd=request.monthly_budget_usd if request.monthly_budget_usd is not None else 500.0,
        max_request_estimated_cost_usd=request.max_request_estimated_cost_usd if request.max_request_estimated_cost_usd is not None else 10.0,
        object_prefix=request.object_prefix or tenant_name,
    )


def _write_bootstrap_tenant(config_dir: Path, tenant: TenantSpec) -> None:
    tenants_dir = config_dir / "tenants"
    tenants_dir.mkdir(parents=True, exist_ok=True)
    path = tenants_dir / f"{tenant.name}.yml"
    if path.exists():
        return
    payload = tenant.model_dump(mode="json", exclude_none=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(payload, handle, sort_keys=False)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def _create_bootstrap_tenant_key(tenant_name: str) -> str:
    creds = load_credentials()
    auth = dict(creds.get("auth", {}))
    tenant_keys = _parse_bootstrap_tenant_keys(auth.get("tenant_keys", ""))
    if tenant_name in tenant_keys:
        raise HTTPException(status_code=409, detail="tenant key already exists")
    token = "gpk_" + secrets.token_urlsafe(32)
    tenant_keys[tenant_name] = token
    auth["tenant_keys"] = ",".join(f"{tenant}:{key}" for tenant, key in sorted(tenant_keys.items()))
    save_credentials("auth", auth)
    return token


def _parse_bootstrap_tenant_keys(raw: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in raw.split(","):
        tenant, sep, key = item.partition(":")
        tenant = tenant.strip()
        key = key.strip()
        if sep and tenant and key:
            pairs[tenant] = key
    return pairs


def _bootstrap_handoff_payload(*, tenant: str, token: str, gateway_url: str, recipe_inbox: str) -> dict[str, str]:
    return {
        "GPUCALL_TENANT": tenant,
        "GPUCALL_BASE_URL": gateway_url,
        "GPUCALL_API_KEY": token,
        "GPUCALL_RECIPE_INBOX": recipe_inbox,
        "GPUCALL_ONBOARDING_PROMPT_URL": "https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_PROMPT.md",
        "GPUCALL_ONBOARDING_MANUAL_URL": "https://raw.githubusercontent.com/noiehoie/gpucall3/main/docs/EXTERNAL_SYSTEM_ONBOARDING_MANUAL.md",
        "GPUCALL_SDK_WHEEL_URL": "https://raw.githubusercontent.com/noiehoie/gpucall3/main/sdk/python/dist/gpucall_sdk-2.0.0a2-py3-none-any.whl",
    }


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
    jobs = _job_store(state_dir)
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
            close_jobs = getattr(runtime.jobs, "close", None)
            if callable(close_jobs):
                close_jobs()
            idempotency_cache.close()

    app = FastAPI(title="gpucall v2.0", version="2.0.1", lifespan=lifespan)
    max_request_bytes = int(os.getenv("GPUCALL_MAX_REQUEST_BYTES", "1048576"))
    configured_api_keys = load_credentials().get("auth", {}).get("api_keys", "")
    idempotency_cache = _idempotency_store(default_state_dir())
    idempotency_locks: dict[str, asyncio.Lock] = {}
    idempotency_locks_guard = asyncio.Lock()
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
            body_replayed = asyncio.Event()

            async def replay_body():
                nonlocal delivered
                if delivered:
                    await body_replayed.wait()
                    return {"type": "http.disconnect"}
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}

            request._receive = replay_body
            request._stream_consumed = False
        return await call_next(request)

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        tenant_keys = tenant_key_map()
        configured = legacy_api_keys() + list(tenant_keys)
        if request.url.path in {"/healthz", "/readyz", "/v2/bootstrap/tenant-key"}:
            return await call_next(request)
        if not configured:
            if not _allow_unauthenticated_gateway():
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

    def record_error_code(runtime: Runtime, code: str) -> None:
        codes = runtime.metrics.setdefault("error_codes", {})
        codes[code] = codes.get(code, 0) + 1

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

    def _metrics_payload(runtime: Runtime) -> dict[str, object]:
        latencies = runtime.metrics.get("latency_ms", [])
        avg = sum(latencies) / len(latencies) if latencies else 0.0
        return {
            "requests": runtime.metrics.get("requests", {}),
            "error_codes": runtime.metrics.get("error_codes", {}),
            "latency_ms_avg": avg,
            "latency_samples": len(latencies),
            "registry": runtime.dispatcher.registry.snapshot(),
        }

    def _enforce_metrics_access() -> None:
        if not (os.getenv("GPUCALL_API_KEYS") or configured_api_keys or os.getenv("GPUCALL_TENANT_API_KEYS") or tenant_key_map()) and not _public_metrics_enabled():
            raise HTTPException(status_code=403, detail="metrics require authentication or GPUCALL_PUBLIC_METRICS=1")

    @app.get("/metrics")
    async def metrics(runtime: Runtime = Depends(runtime_dep)) -> dict[str, object]:
        _enforce_metrics_access()
        return _metrics_payload(runtime)

    @app.get("/metrics/prometheus")
    async def prometheus_metrics(runtime: Runtime = Depends(runtime_dep)) -> PlainTextResponse:
        _enforce_metrics_access()
        return PlainTextResponse(_prometheus_metrics_text(_metrics_payload(runtime)), media_type="text/plain; version=0.0.4")

    @app.post("/v2/bootstrap/tenant-key")
    async def bootstrap_tenant_key(request: BootstrapTenantKeyRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> JSONResponse:
        config = load_config(root)
        automation = config.admin_automation
        if automation.api_key_handoff_mode is not ApiKeyHandoffMode.TRUSTED_BOOTSTRAP:
            return error_response(403, "trusted bootstrap is disabled")
        client_host = http_request.client.host if http_request.client else ""
        if not _bootstrap_client_allowed(client_host, automation.api_key_bootstrap_allowed_cidrs, automation.api_key_bootstrap_allowed_hosts):
            return error_response(403, "client is not in trusted bootstrap scope")
        tenant_name = request.system_name.strip()
        if not _BOOTSTRAP_TENANT_NAME_RE.fullmatch(tenant_name):
            return error_response(422, "invalid system_name")
        if tenant_name in tenant_key_map():
            return error_response(409, "tenant key already exists")
        tenant = _bootstrap_tenant_spec(request, tenant_name)
        _write_bootstrap_tenant(root, tenant)
        token = _create_bootstrap_tenant_key(tenant_name)
        runtime.tenants[tenant_name] = tenant
        gateway_url = automation.api_key_bootstrap_gateway_url or str(http_request.base_url).rstrip("/")
        recipe_inbox = automation.api_key_bootstrap_recipe_inbox or ""
        payload = _bootstrap_handoff_payload(tenant=tenant_name, token=token, gateway_url=gateway_url, recipe_inbox=recipe_inbox)
        return JSONResponse(
            {
                "tenant": tenant_name,
                "api_key": token,
                "api_key_fingerprint": hashlib.sha256(token.encode("utf-8")).hexdigest()[:16],
                "credentials_path": str(credentials_path()),
                "handoff": payload,
                "secret_handling": "store this response in the caller system secret manager; do not log it",
            }
        )

    @app.post("/v2/tasks/sync")
    async def task_sync(
        request: TaskRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)
    ) -> JSONResponse:
        if request.mode is not ExecutionMode.SYNC:
            raise HTTPException(status_code=400, detail="use /v2/tasks/sync with mode=sync")
        try:
            enforce_gateway_owned_routing(request)
            plan = runtime.compiler.compile(request)
            caller_identity = idempotency_identity(http_request)
            caller_request_hash = idempotency_request_hash(request)
            async with idempotency_execution_lock(
                idempotency_locks,
                idempotency_locks_guard,
                idempotency_cache_key(request, caller_identity) if request.idempotency_key else None,
            ):
                cached = idempotency_lookup(
                    request,
                    idempotency_cache,
                    request_hash=caller_request_hash,
                    identity=caller_identity,
                    ttl_seconds=idempotency_ttl_seconds,
                    max_entries=idempotency_cache_max,
                )
                if cached is not None:
                    return JSONResponse(status_code=cached[0], content=cached[1], headers=cached[2])
                await enforce_request_budget(runtime, http_request, plan)
                tenant_prefix = object_tenant_prefix(runtime, http_request) if request_needs_worker_object_access(request) else None
                request = worker_readable_request(request, runtime, tenant_prefix=tenant_prefix)
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
                    identity=caller_identity,
                    max_entries=idempotency_cache_max,
                )
            return JSONResponse(status_code=200, content=content, headers=tenant_headers(headers, http_request))
        except GovernanceError as exc:
            record_error_code(runtime, exc.code)
            return governance_error_response(exc, request=request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TupleError as exc:
            record_error_code(runtime, exc.code or "TUPLE_ERROR")
            return tuple_error_response(exc, request=request)
        except TenantBudgetError as exc:
            record_error_code(runtime, exc.code)
            return tenant_budget_error_response(exc, request=request)

    @app.post("/v1/chat/completions", response_model=None)
    async def openai_chat_completions(
        request: OpenAIChatCompletionRequest,
        http_request: Request,
        runtime: Runtime = Depends(runtime_dep),
    ) -> Any:
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
            mode=ExecutionMode.STREAM if request.stream else ExecutionMode.SYNC,
            messages=messages,
            max_tokens=request.max_tokens,
            temperature=request.temperature,
            response_format=request.response_format,
            metadata=request.metadata,
        )
        try:
            plan = runtime.compiler.compile(task_request)
            await enforce_request_budget(runtime, http_request, plan)
            if request.stream:
                async def events():
                    yielded = False
                    try:
                        async for event in runtime.dispatcher.execute_stream(plan):
                            for chunk in _openai_stream_chunks(request.model, event, yielded):
                                yielded = True
                                yield chunk
                    except TupleError as exc:
                        record_error_code(runtime, exc.code or "TUPLE_ERROR")
                        yield "data: " + json.dumps({"error": {"message": public_tuple_error(exc), "code": exc.code or "tuple_error"}}, separators=(",", ":")) + "\n\n"
                    yield "data: " + json.dumps(_openai_stream_chunk(request.model, "", finish_reason="stop"), separators=(",", ":")) + "\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    events(),
                    status_code=200,
                    media_type="text/event-stream",
                    headers=tenant_headers(warning_headers(plan, runtime.compiler.tuples), http_request),
                )
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
            record_error_code(runtime, exc.code)
            status_code = governance_status_code(exc)
            code = "tuple_unavailable" if status_code == 503 else exc.code.lower()
            return openai_error_response(
                status_code,
                str(exc),
                code=code,
                gpucall_failure_artifact=build_governance_failure_artifact(exc, task_request),
            )
        except TupleError as exc:
            record_error_code(runtime, exc.code or "TUPLE_ERROR")
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
            record_error_code(runtime, exc.code)
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
            owner_identity = idempotency_identity(http_request)
            caller_request_hash = idempotency_request_hash(request)
            async with idempotency_execution_lock(
                idempotency_locks,
                idempotency_locks_guard,
                idempotency_cache_key(request, owner_identity) if request.idempotency_key else None,
            ):
                cached = idempotency_lookup(
                    request,
                    idempotency_cache,
                    request_hash=caller_request_hash,
                    identity=owner_identity,
                    ttl_seconds=idempotency_ttl_seconds,
                    max_entries=idempotency_cache_max,
                )
                if cached is not None:
                    return JSONResponse(status_code=cached[0], content=cached[1], headers=cached[2])
                await enforce_request_budget(runtime, http_request, plan)
                tenant_prefix = object_tenant_prefix(runtime, http_request) if request_needs_worker_object_access(request) else None
                request = worker_readable_request(request, runtime, tenant_prefix=tenant_prefix)
                plan = plan_with_worker_refs(plan, request.input_refs, split_learning=request.split_learning)
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
            record_error_code(runtime, exc.code)
            return governance_error_response(exc, request=request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except TenantBudgetError as exc:
            record_error_code(runtime, exc.code)
            return tenant_budget_error_response(exc, request=request)

    @app.post("/v2/tasks/stream")
    async def task_stream(request: TaskRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> StreamingResponse:
        if request.mode is not ExecutionMode.STREAM:
            raise HTTPException(status_code=400, detail="use /v2/tasks/stream with mode=stream")
        try:
            enforce_gateway_owned_routing(request)
            plan = runtime.compiler.compile(request)
            await enforce_request_budget(runtime, http_request, plan)
            tenant_prefix = object_tenant_prefix(runtime, http_request) if request_needs_worker_object_access(request) else None
            request = worker_readable_request(request, runtime, tenant_prefix=tenant_prefix)
            plan = plan_with_worker_refs(plan, request.input_refs, split_learning=request.split_learning)
        except GovernanceError as exc:
            record_error_code(runtime, exc.code)
            return governance_error_response(exc, request=request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def events():
            try:
                async for event in runtime.dispatcher.execute_stream(plan):
                    yield event
            except TupleError as exc:
                record_error_code(runtime, exc.code or "TUPLE_ERROR")
                yield "event: error\n"
                yield "data: " + json.dumps({"code": exc.code or "TUPLE_ERROR", "message": public_tuple_error(exc)}, separators=(",", ":")) + "\n\n"

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            status_code=200,
            headers=tenant_headers(warning_headers(plan, runtime.compiler.tuples), http_request),
        )

    @app.post("/v2/tasks/batch")
    async def task_batch(request: BatchTaskRequest, http_request: Request, runtime: Runtime = Depends(runtime_dep)) -> JSONResponse:
        results: list[dict[str, Any]] = []
        status_code = 200
        for index, item in enumerate(request.requests):
            if item.mode is not ExecutionMode.SYNC:
                results.append({"index": index, "ok": False, "status_code": 400, "error": "batch currently executes sync task requests"})
                status_code = 207
                if not request.continue_on_error:
                    break
                continue
            try:
                enforce_gateway_owned_routing(item)
                plan = runtime.compiler.compile(item)
                await enforce_request_budget(runtime, http_request, plan)
                tenant_prefix = object_tenant_prefix(runtime, http_request) if request_needs_worker_object_access(item) else None
                worker_request = worker_readable_request(item, runtime, tenant_prefix=tenant_prefix)
                plan = plan_with_worker_refs(plan, worker_request.input_refs, split_learning=worker_request.split_learning)
                result = await runtime.dispatcher.execute_sync(plan)
                results.append(
                    {
                        "index": index,
                        "ok": True,
                        "status_code": 200,
                        "plan_id": plan.plan_id,
                        "plan": public_plan_summary(plan, runtime.compiler.tuples),
                        "result": result.model_dump(mode="json"),
                    }
                )
            except GovernanceError as exc:
                record_error_code(runtime, exc.code)
                status_code = 207
                results.append({"index": index, "ok": False, "status_code": governance_status_code(exc), "code": exc.code, "error": str(exc)})
                if not request.continue_on_error:
                    break
            except TupleError as exc:
                record_error_code(runtime, exc.code or "TUPLE_ERROR")
                status_code = 207
                results.append({"index": index, "ok": False, "status_code": exc.status_code, "code": exc.code or "TUPLE_ERROR", "error": public_tuple_error(exc)})
                if not request.continue_on_error:
                    break
            except TenantBudgetError as exc:
                record_error_code(runtime, exc.code)
                status_code = 207
                results.append({"index": index, "ok": False, "status_code": exc.status_code, "code": exc.code, "error": str(exc)})
                if not request.continue_on_error:
                    break
        return JSONResponse(status_code=status_code, content={"results": results, "ok": all(item.get("ok") for item in results)})

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
    async def presign_get(
        request: PresignGetRequest,
        http_request: Request,
        runtime: Runtime = Depends(runtime_dep),
    ) -> PresignGetResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        try:
            return runtime.object_store.presign_get(request, tenant_prefix=object_tenant_prefix(runtime, http_request))
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


def _openai_message_to_chat_message(message: OpenAIChatMessage) -> ChatMessage:
    return ChatMessage(role=message.role, content=_message_content_to_text(message.content))


def _message_content_to_text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    raise HTTPException(
        status_code=400,
        detail="OpenAI facade accepts string message content only; use gpucall DataRef APIs for structured or multimodal inputs",
    )


def _openai_stream_chunks(model: str, event: str, already_started: bool):
    for line in event.splitlines():
        if not line.startswith("data:"):
            continue
        content = line.removeprefix("data:").strip()
        if not content or content == "[DONE]":
            continue
        if not already_started:
            yield "data: " + json.dumps(_openai_stream_chunk(model, "", role="assistant"), separators=(",", ":")) + "\n\n"
        yield "data: " + json.dumps(_openai_stream_chunk(model, content), separators=(",", ":")) + "\n\n"


def _openai_stream_chunk(model: str, content: str, *, role: str | None = None, finish_reason: str | None = None) -> dict[str, Any]:
    delta: dict[str, str] = {}
    if role is not None:
        delta["role"] = role
    if content:
        delta["content"] = content
    return {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }


def _prometheus_metrics_text(payload: dict[str, object]) -> str:
    lines = [
        "# HELP gpucall_request_total Gateway requests by route and status.",
        "# TYPE gpucall_request_total counter",
    ]
    requests = payload.get("requests") if isinstance(payload, dict) else {}
    if isinstance(requests, dict):
        for key, value in sorted(requests.items()):
            method, route, status = _split_metric_key(str(key))
            lines.append(f'gpucall_request_total{{method="{method}",route="{route}",status="{status}"}} {int(value)}')
    error_codes = payload.get("error_codes") if isinstance(payload, dict) else {}
    lines.extend(
        [
            "# HELP gpucall_governance_error_total Gateway governance and tuple errors by code.",
            "# TYPE gpucall_governance_error_total counter",
        ]
    )
    if isinstance(error_codes, dict):
        for code, value in sorted(error_codes.items()):
            lines.append(f'gpucall_governance_error_total{{code="{_prom_label(str(code))}"}} {int(value)}')
    registry = payload.get("registry") if isinstance(payload, dict) else {}
    lines.extend(
        [
            "# HELP gpucall_tuple_success_rate Observed tuple success rate.",
            "# TYPE gpucall_tuple_success_rate gauge",
            "# HELP gpucall_tuple_samples Observed tuple sample count.",
            "# TYPE gpucall_tuple_samples gauge",
            "# HELP gpucall_tuple_p50_latency_ms Observed tuple p50 latency in milliseconds.",
            "# TYPE gpucall_tuple_p50_latency_ms gauge",
            "# HELP gpucall_tuple_cost_per_success_usd Observed tuple cost per success in USD.",
            "# TYPE gpucall_tuple_cost_per_success_usd gauge",
        ]
    )
    if isinstance(registry, dict):
        for tuple_name, item in sorted(registry.items()):
            if not isinstance(item, dict):
                continue
            label = _prom_label(str(tuple_name))
            lines.append(f'gpucall_tuple_success_rate{{tuple="{label}"}} {float(item.get("success_rate") or 0.0)}')
            lines.append(f'gpucall_tuple_samples{{tuple="{label}"}} {int(item.get("samples") or 0)}')
            lines.append(f'gpucall_tuple_p50_latency_ms{{tuple="{label}"}} {float(item.get("p50_latency_ms") or 0.0)}')
            lines.append(f'gpucall_tuple_cost_per_success_usd{{tuple="{label}"}} {float(item.get("cost_per_success") or 0.0)}')
    lines.extend(
        [
            "# HELP gpucall_latency_ms_avg Recent average gateway latency in milliseconds.",
            "# TYPE gpucall_latency_ms_avg gauge",
            f"gpucall_latency_ms_avg {float(payload.get('latency_ms_avg') or 0.0)}",
            "# HELP gpucall_latency_samples Recent latency sample count.",
            "# TYPE gpucall_latency_samples gauge",
            f"gpucall_latency_samples {int(payload.get('latency_samples') or 0)}",
        ]
    )
    return "\n".join(lines) + "\n"


def _split_metric_key(key: str) -> tuple[str, str, str]:
    parts = key.rsplit(" ", 1)
    status = parts[1] if len(parts) == 2 else "unknown"
    left = parts[0] if parts else key
    method_route = left.split(" ", 1)
    method = method_route[0] if method_route else "unknown"
    route = method_route[1] if len(method_route) == 2 else "unknown"
    return method, route.replace('"', '\\"'), status


def _prom_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


app = create_app()
