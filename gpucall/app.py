from __future__ import annotations

import os
import time
import hashlib
import json
import re
from contextlib import asynccontextmanager
from collections import OrderedDict
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from gpucall.audit import AuditTrail
from gpucall.compiler import GovernanceCompiler, GovernanceError
from gpucall.config import ConfigError, default_config_dir, default_state_dir, load_config
from gpucall.credentials import load_credentials
from gpucall.dispatcher import Dispatcher, LeaseReaper, ProviderReconciler
from gpucall.domain import ChatMessage, DataRef, ExecutionMode, InlineValue, JobRecord, JobState, ProviderError, ResponseFormat, TaskRequest
from gpucall.domain import PresignGetRequest, PresignGetResponse, PresignPutRequest, PresignPutResponse
from gpucall.object_store import ObjectStore
from gpucall.providers.factory import build_adapters
from gpucall.registry import ObservedRegistry
from gpucall.routing import route_warning_tags
from gpucall.sqlite_store import SQLiteJobStore


class Runtime(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    compiler: GovernanceCompiler
    dispatcher: Dispatcher
    jobs: Any
    reaper: LeaseReaper
    reconciler: ProviderReconciler
    object_store: ObjectStore | None = None
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
    providers = config.providers
    registry = ObservedRegistry(path=state_dir / "registry.db")
    audit = AuditTrail(state_dir / "audit" / "trail.jsonl")
    object_store = ObjectStore(config.object_store) if config.object_store is not None else None
    jobs = SQLiteJobStore(state_dir / "state.db")
    adapters = build_adapters(providers)
    compiler = GovernanceCompiler(policy=policy, recipes=recipes, providers=providers, registry=registry)
    dispatcher = Dispatcher(adapters=adapters, registry=registry, audit=audit, jobs=jobs)
    reaper = LeaseReaper(jobs=jobs, audit=audit, cancel_job=dispatcher.cancel_job)
    reconciler = ProviderReconciler(adapters=adapters, audit=audit)
    return Runtime(
        compiler=compiler,
        dispatcher=dispatcher,
        jobs=jobs,
        reaper=reaper,
        reconciler=reconciler,
        object_store=object_store,
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

    app = FastAPI(title="gpucall v2.0", version="2.0.0", lifespan=lifespan)
    max_request_bytes = int(os.getenv("GPUCALL_MAX_REQUEST_BYTES", "1048576"))
    configured_api_keys = load_credentials().get("auth", {}).get("api_keys", "")
    idempotency_cache: OrderedDict[str, tuple[float, str, int, dict[str, Any], dict[str, str]]] = OrderedDict()
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
        return await call_next(request)

    @app.middleware("http")
    async def api_key_auth(request: Request, call_next):
        configured = [key.strip() for key in (os.getenv("GPUCALL_API_KEYS") or configured_api_keys).split(",") if key.strip()]
        if not configured or request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip()
        if token not in configured:
            return error_response(401, "unauthorized")
        request.state.api_key = token
        return await call_next(request)

    @app.middleware("http")
    async def basic_rate_limit(request: Request, call_next):
        if request.url.path in {"/healthz", "/readyz"}:
            return await call_next(request)
        identity = getattr(request.state, "api_key", None) or request.client.host if request.client else "unknown"
        now = time.monotonic()
        window = [stamp for stamp in rate_limit.get(identity, []) if now - stamp < 60.0]
        if len(window) >= requests_per_minute:
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
        }

    @app.get("/metrics")
    async def metrics(runtime: Runtime = Depends(runtime_dep)) -> dict[str, object]:
        if not (os.getenv("GPUCALL_API_KEYS") or configured_api_keys) and not _public_metrics_enabled():
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
            request = worker_readable_request(request, runtime)
            plan = plan_with_worker_refs(plan, request.input_refs)
            result = await runtime.dispatcher.execute_sync(plan)
            headers = warning_headers(plan, runtime.compiler.providers)
            content = {
                "plan_id": plan.plan_id,
                "plan": public_plan_summary(plan, runtime.compiler.providers),
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
            return JSONResponse(status_code=200, content=content, headers=headers)
        except GovernanceError as exc:
            return governance_error_response(exc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except ProviderError as exc:
            raise HTTPException(status_code=exc.status_code, detail=public_provider_error(exc)) from exc

    @app.post("/v1/chat/completions")
    async def openai_chat_completions(
        request: OpenAIChatCompletionRequest,
        runtime: Runtime = Depends(runtime_dep),
    ) -> JSONResponse:
        if request.stream:
            return openai_error_response(400, "stream is not supported by the gpucall OpenAI facade in v2.0 MVP")
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
            result = await runtime.dispatcher.execute_sync(plan)
            headers = warning_headers(plan, runtime.compiler.providers)
            if result.output_validated is not None:
                headers["X-GPUCall-Output-Validated"] = "true" if result.output_validated else "false"
            return JSONResponse(
                status_code=200,
                headers=headers,
                content=openai_chat_response(
                    "gpucall:auto",
                    result.value or "",
                    result.usage,
                    gpucall=public_plan_summary(plan, runtime.compiler.providers),
                ),
            )
        except GovernanceError as exc:
            status_code = governance_status_code(exc)
            code = "provider_unavailable" if status_code == 503 else exc.code.lower()
            return openai_error_response(status_code, str(exc), code=code)
        except ProviderError as exc:
            headers: dict[str, str] = {}
            if exc.raw_output is not None:
                headers["X-GPUCall-Output-Validated"] = "false"
            return openai_error_response(
                exc.status_code,
                public_provider_error(exc),
                code=exc.code or "provider_error",
                headers=headers,
            )

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
            request = worker_readable_request(request, runtime)
            plan = plan_with_worker_refs(plan, request.input_refs)
            owner_identity = idempotency_identity(http_request)
            job = await runtime.dispatcher.submit_async(plan, owner_identity=owner_identity)
            headers = warning_headers(plan, runtime.compiler.providers)
            content = {
                "job_id": job.job_id,
                "state": job.state,
                "status_url": f"/v2/jobs/{job.job_id}",
                "plan": public_plan_summary(plan, runtime.compiler.providers),
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
                headers=headers,
            )
        except GovernanceError as exc:
            return governance_error_response(exc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v2/tasks/stream")
    async def task_stream(request: TaskRequest, runtime: Runtime = Depends(runtime_dep)) -> StreamingResponse:
        if request.mode is not ExecutionMode.STREAM:
            raise HTTPException(status_code=400, detail="use /v2/tasks/stream with mode=stream")
        try:
            enforce_gateway_owned_routing(request)
            plan = runtime.compiler.compile(request)
            request = worker_readable_request(request, runtime)
            plan = plan_with_worker_refs(plan, request.input_refs)
        except GovernanceError as exc:
            raise HTTPException(status_code=governance_status_code(exc), detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        async def events():
            async for event in runtime.dispatcher.execute_stream(plan):
                yield event

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            status_code=200,
            headers=warning_headers(plan, runtime.compiler.providers),
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
    async def presign_put(request: PresignPutRequest, runtime: Runtime = Depends(runtime_dep)) -> PresignPutResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        return runtime.object_store.presign_put(request)

    @app.post("/v2/objects/presign-get")
    async def presign_get(request: PresignGetRequest, runtime: Runtime = Depends(runtime_dep)) -> PresignGetResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        try:
            return runtime.object_store.presign_get(request)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/v2/results/presign-put")
    async def result_presign_put(request: PresignPutRequest, runtime: Runtime = Depends(runtime_dep)) -> PresignPutResponse:
        if runtime.object_store is None:
            raise HTTPException(status_code=503, detail="object store is not configured")
        return runtime.object_store.presign_put(request)

    return app


async def recover_interrupted_jobs(runtime: Runtime) -> None:
    for job in await runtime.jobs.all():
        if job.state not in {JobState.PENDING, JobState.RUNNING}:
            continue
        await runtime.jobs.update(job.job_id, state=JobState.EXPIRED, error="gateway restarted before job completion")
        runtime.dispatcher.audit.append("job.interrupted", {"job_id": job.job_id, "plan_id": job.plan.plan_id})


def warning_headers(plan, providers=None) -> dict[str, str]:
    warnings = route_warning_tags(plan, providers)
    return {"X-GPUCall-Warning": ", ".join(warnings)} if warnings else {}


def enforce_gateway_owned_routing(request: TaskRequest) -> None:
    if _allow_caller_routing():
        return
    forbidden: list[str] = []
    if request.recipe is not None:
        forbidden.append("recipe")
    if request.requested_provider is not None:
        forbidden.append("requested_provider")
    if request.requested_gpu is not None:
        forbidden.append("requested_gpu")
    if forbidden:
        joined = ", ".join(forbidden)
        raise GovernanceError(f"caller-controlled routing is disabled; omit {joined}")


def _allow_caller_routing() -> bool:
    return os.getenv("GPUCALL_ALLOW_CALLER_ROUTING", "").strip().lower() in {"1", "true", "yes", "on"}


def _public_metrics_enabled() -> bool:
    return os.getenv("GPUCALL_PUBLIC_METRICS", "").strip().lower() in {"1", "true", "yes", "on"}


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
    message = str(exc)
    if "circuit breaker" in message or "no eligible provider after policy, recipe, and circuit constraints" in message:
        return 503
    return 400


def governance_error_response(exc: GovernanceError) -> JSONResponse:
    content: dict[str, Any] = {"detail": str(exc), "code": exc.code}
    if exc.context:
        content["context"] = exc.context
    return JSONResponse(status_code=governance_status_code(exc), content=content)


def worker_readable_request(request: TaskRequest, runtime: Runtime) -> TaskRequest:
    if not request.input_refs:
        return request
    for ref in request.input_refs:
        if not str(ref.uri).startswith("s3://"):
            raise ValueError("input data_ref must be an object-store s3:// reference")
    if runtime.object_store is None:
        raise ValueError("object store is required for data_ref worker access")
    converted = [_worker_readable_ref(ref, runtime) for ref in request.input_refs]
    return request.model_copy(update={"input_refs": converted})


def _worker_readable_ref(ref: DataRef, runtime: Runtime) -> DataRef:
    if not str(ref.uri).startswith("s3://"):
        return ref
    if runtime.object_store is None:
        return ref
    return runtime.object_store.presign_get(PresignGetRequest(data_ref=ref)).data_ref


def plan_with_worker_refs(plan: Any, refs: list[DataRef]) -> Any:
    if not refs:
        return plan
    updated = plan.model_copy(update={"input_refs": refs})
    attestations = dict(getattr(updated, "attestations", {}) or {})
    original_hash = attestations.get("governance_hash")
    if original_hash is not None:
        attestations["caller_governance_hash"] = original_hash
    attestations["governance_hash"] = compiled_plan_hash(updated)
    return updated.model_copy(update={"attestations": attestations})


def compiled_plan_hash(plan: Any) -> str:
    material = plan.model_dump(mode="json", exclude={"attestations"})
    encoded = json.dumps(material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def idempotency_lookup(
    request: TaskRequest,
    cache: OrderedDict[str, tuple[float, str, int, dict[str, Any], dict[str, str]]],
    *,
    request_hash: str,
    identity: str,
    ttl_seconds: float,
    max_entries: int,
):
    if not request.idempotency_key:
        return None
    now = time.monotonic()
    prune_idempotency_cache(cache, now, ttl_seconds=ttl_seconds, max_entries=max_entries)
    recipe_key = request.recipe or "auto"
    key = f"{identity}:{request.task}:{recipe_key}:{request.mode}:{request.idempotency_key}"
    cached = cache.get(key)
    if cached is None:
        return None
    cache.move_to_end(key)
    _, cached_hash, status, content, headers = cached
    if cached_hash != request_hash:
        raise HTTPException(status_code=409, detail="idempotency key reused with different request body")
    return status, content, headers


def idempotency_store(
    request: TaskRequest,
    cache: OrderedDict[str, tuple[float, str, int, dict[str, Any], dict[str, str]]],
    status: int,
    content: dict[str, Any],
    headers: dict[str, str],
    *,
    request_hash: str,
    identity: str,
    max_entries: int,
) -> None:
    if request.idempotency_key:
        recipe_key = request.recipe or "auto"
        key = f"{identity}:{request.task}:{recipe_key}:{request.mode}:{request.idempotency_key}"
        cache[key] = (time.monotonic(), request_hash, status, content, headers)
        cache.move_to_end(key)
        while len(cache) > max_entries:
            cache.popitem(last=False)


def prune_idempotency_cache(
    cache: OrderedDict[str, tuple[float, str, int, dict[str, Any], dict[str, str]]],
    now: float,
    *,
    ttl_seconds: float,
    max_entries: int,
) -> None:
    expired = [key for key, (created, *_rest) in cache.items() if now - created > ttl_seconds]
    for key in expired:
        cache.pop(key, None)
    while len(cache) > max_entries:
        cache.popitem(last=False)


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
    model: str, content: str, usage: dict[str, int], *, gpucall: dict[str, Any] | None = None
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
    return payload


def public_plan_summary(plan: Any, providers: dict[str, Any] | None = None) -> dict[str, Any]:
    chain = list(getattr(plan, "provider_chain", []) or [])
    selected_provider = chain[0] if chain else None
    selected_spec = providers.get(selected_provider) if providers and selected_provider else None
    return {
        "recipe_name": getattr(plan, "recipe_name", None),
        "provider_chain": chain,
        "selected_provider": selected_provider,
        "selected_provider_model": getattr(selected_spec, "model", None) if selected_spec is not None else None,
        "governance_hash": (getattr(plan, "attestations", {}) or {}).get("governance_hash"),
        "output_validation_attempts": getattr(plan, "output_validation_attempts", None),
    }


def public_provider_error(exc: ProviderError) -> str:
    return f"provider execution failed ({exc.code or 'PROVIDER_ERROR'})"


def openai_error_response(
    status_code: int,
    message: str,
    *,
    code: str = "bad_request",
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    error: dict[str, Any] = {"message": message, "type": "gpucall_error", "code": code}
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
