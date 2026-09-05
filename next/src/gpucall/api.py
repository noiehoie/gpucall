"""Authenticated owner API; adapters never see caller routing or policy overrides."""
from __future__ import annotations

import hmac
import threading
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Request as HTTPRequest
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from .kernel import Rejected, compile_plan
from .models import Bundle, Request
from .runtime import Runtime, public_job
from .store import MAX_ARTIFACT_BYTES, Store


def wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def create_app(store: Store, bundle: Bundle, runtime: Runtime, *, token: str,
               clock=wall_clock_ms, run_worker=False) -> FastAPI:
    if len(token) < 32:
        raise ValueError("owner token must be at least 32 characters")

    @asynccontextmanager
    async def lifespan(app):
        stopped = threading.Event()
        def worker():
            while not stopped.is_set():
                try:
                    runtime.tick(clock())
                except Exception:
                    # Durable state survives a failed cycle; no provider payload is logged.
                    pass
                stopped.wait(0.25)
        thread = threading.Thread(target=worker, daemon=True)
        if run_worker:
            thread.start()
        yield
        stopped.set()
        if run_worker:
            thread.join(timeout=2)

    app = FastAPI(title="gpucall", version="3.0.0.dev0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def bound_and_protect(request: HTTPRequest, call_next):
        if request.url.path != "/healthz":
            if not hmac.compare_digest(request.headers.get("authorization", "").encode(), ("Bearer " + token).encode()):
                return JSONResponse(status_code=401, content={"error": {"code": "unauthorized"}})
        if request.method == "POST" and request.url.path != "/v1/artifacts":
            size = 0
            chunks = []
            async for part in request.stream():
                size += len(part)
                if size > 128*1024:
                    return JSONResponse(status_code=413, content={"error": {"code": "request_too_large"}})
                chunks.append(part)
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    async def authorized(authorization: str = Header(default="")):
        if not hmac.compare_digest(authorization.encode(), ("Bearer " + token).encode()):
            raise HTTPException(401, "unauthorized")

    @app.exception_handler(Rejected)
    async def rejected(_, exc):
        status = 409 if exc.code in {"idempotency_conflict", "account_budget_exceeded"} else 422
        return JSONResponse(status_code=status, content={"error": {"code": exc.code, "details": exc.details}})

    @app.exception_handler(KeyError)
    @app.exception_handler(FileNotFoundError)
    async def missing(_, exc):
        return JSONResponse(status_code=404, content={"error": {"code": "not_found"}})

    @app.exception_handler(RequestValidationError)
    async def invalid(_, exc):
        return JSONResponse(status_code=422, content={"error": {"code": "invalid_request", "fields":
            [{"location": list(e["loc"]), "type": e["type"]} for e in exc.errors()]}})

    @app.get("/healthz")
    def health():
        return {"status": "ok", "version": "3.0.0.dev0"}

    @app.post("/v1/estimate", dependencies=[Depends(authorized)])
    def estimate(request: Request):
        profile = next((p for p in bundle.profiles if p.id == request.profile), None)
        if profile is None:
            raise Rejected("unknown_profile")
        snapshot = store.enrich(bundle).snapshot.model_copy(update={"at_ms": clock()})
        return compile_plan(request, profile, snapshot, allow_fixture=bundle.allow_fixture)

    @app.get("/v1/schema", dependencies=[Depends(authorized)])
    def schema():
        return Request.model_json_schema()

    @app.get("/v1/profiles", dependencies=[Depends(authorized)])
    def profiles():
        return {"profiles": [p.model_dump(mode="json") for p in bundle.profiles]}

    @app.post("/v1/jobs", status_code=202, dependencies=[Depends(authorized)])
    def submit(request: Request):
        return public_job(store.submit(request, bundle, clock()))

    @app.get("/v1/jobs", dependencies=[Depends(authorized)])
    def jobs():
        return [public_job(j) for j in store.jobs()]

    @app.get("/v1/jobs/{job_id}", dependencies=[Depends(authorized)])
    def get(job_id: str):
        return public_job(store.get(job_id))

    @app.post("/v1/jobs/{job_id}/cancel", dependencies=[Depends(authorized)])
    def cancel(job_id: str):
        return public_job(store.cancel(job_id, clock()))

    @app.get("/v1/jobs/{job_id}/replay", dependencies=[Depends(authorized)])
    def replay(job_id: str):
        return store.replay(job_id)

    @app.get("/v1/jobs/{job_id}/result", dependencies=[Depends(authorized)])
    def result(job_id: str):
        job = store.get(job_id)
        if job["output"] is None:
            raise HTTPException(409, "result_not_available")
        return Response(store.artifacts.get(job["output"]), media_type="text/plain; charset=utf-8",
                        headers={"X-Gpucall-Cleanup": "complete" if job["status"] in {"succeeded", "failed", "cancelled"} else "pending"})

    @app.post("/v1/artifacts", dependencies=[Depends(authorized)])
    async def put_artifact(request: HTTPRequest):
        data = bytearray()
        async for part in request.stream():
            if len(data) + len(part) > MAX_ARTIFACT_BYTES:
                raise HTTPException(413, "artifact_too_large")
            data.extend(part)
        sha = store.artifacts.put(bytes(data))
        return {"sha256": sha, "size": len(data)}

    @app.get("/v1/artifacts/{sha}", dependencies=[Depends(authorized)])
    def get_artifact(sha: str):
        return Response(store.artifacts.get(sha), media_type="application/octet-stream")

    @app.get("/v1/account", dependencies=[Depends(authorized)])
    def account():
        return store.account()

    @app.get("/v1/audit", dependencies=[Depends(authorized)])
    def audit():
        return store.audit()

    return app
