from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from time import monotonic
from typing import Callable
from uuid import uuid4

from gpucall.audit import AuditTrail, redacted_plan_for_audit
from gpucall.domain import (
    CompiledPlan,
    JobRecord,
    JobState,
    ProviderError,
    ProviderObservation,
    ProviderResult,
    ResponseFormatType,
)
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.registry import ObservedRegistry


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, plan: CompiledPlan, *, owner_identity: str | None = None) -> JobRecord:
        job = JobRecord(job_id=uuid4().hex, state=JobState.PENDING, plan=plan, owner_identity=owner_identity)
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> JobRecord | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def update(self, job_id: str, **changes: object) -> JobRecord:
        async with self._lock:
            job = self._jobs[job_id].model_copy(update={**changes, "updated_at": datetime.now(timezone.utc)})
            self._jobs[job_id] = job
            return job

    async def all(self) -> list[JobRecord]:
        async with self._lock:
            return list(self._jobs.values())


class Dispatcher:
    def __init__(
        self,
        *,
        adapters: dict[str, ProviderAdapter],
        registry: ObservedRegistry,
        audit: AuditTrail,
        jobs: JobStore,
        provider_costs: dict[str, float] | None = None,
    ) -> None:
        self.adapters = adapters
        self.registry = registry
        self.audit = audit
        self.jobs = jobs
        self.provider_costs = provider_costs or {}
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._job_plans: dict[str, CompiledPlan] = {}

    async def execute_sync(self, plan: CompiledPlan) -> ProviderResult:
        self.audit.append("plan.accepted", redacted_plan_for_audit(plan))
        last_error: ProviderError | None = None
        for provider in plan.provider_chain:
            if not self.registry.is_available(provider):
                continue
            adapter = self.adapters.get(provider)
            if adapter is None:
                continue
            attempts = _output_validation_attempts(plan)
            for attempt in range(1, attempts + 1):
                started = monotonic()
                handle: RemoteHandle | None = None
                try:
                    handle = await adapter.start(plan)
                    self.audit.append(
                        "lease.started",
                        {"plan_id": plan.plan_id, "provider": provider, "remote_id": handle.remote_id, "attempt": attempt},
                    )
                    result = await asyncio.wait_for(adapter.wait(handle, plan), timeout=plan.timeout_seconds)
                    result = _validate_provider_output(plan, result)
                    self.registry.record(
                        self._observation(provider, started, success=True)
                    )
                    self.audit.append("plan.completed", {"plan_id": plan.plan_id, "provider": provider, "attempt": attempt})
                    return result
                except ProviderError as exc:
                    last_error = exc
                    if exc.code in {"EMPTY_OUTPUT", "MALFORMED_OUTPUT"}:
                        final_attempt = attempt >= attempts
                        self.audit.append(
                            "provider.output_rejected",
                            {
                                "plan_id": plan.plan_id,
                                "provider": provider,
                                "attempt": attempt,
                                "code": exc.code,
                                "retryable": not final_attempt,
                            },
                        )
                        if not final_attempt:
                            continue
                        last_error = ProviderError(
                            _job_error_message(exc),
                            retryable=False,
                            status_code=422,
                            code=exc.code,
                            raw_output=exc.raw_output,
                        )
                        break
                    self.registry.record(
                        self._observation(provider, started, success=False)
                    )
                    self.audit.append(
                        "provider.failed",
                        {"plan_id": plan.plan_id, "provider": provider, "error": _provider_error_audit(exc)},
                    )
                    if not exc.retryable:
                        raise
                    break
                except asyncio.TimeoutError as exc:
                    last_error = ProviderError("provider timed out", retryable=True, status_code=504)
                    self.registry.record(
                        self._observation(provider, started, success=False)
                    )
                    self.audit.append("provider.timeout", {"plan_id": plan.plan_id, "provider": provider})
                    break
                except Exception as exc:
                    last_error = ProviderError("provider raised unexpected exception", retryable=True, status_code=502)
                    self.registry.record(
                        self._observation(provider, started, success=False)
                    )
                    self.audit.append(
                        "provider.failed",
                        {
                            "plan_id": plan.plan_id,
                            "provider": provider,
                            "error": _exception_audit(exc, retryable=True),
                        },
                    )
                    break
                finally:
                    if handle is not None:
                        await self._cleanup_remote(adapter, handle, plan_id=plan.plan_id, provider=provider, attempt=attempt)
        if last_error is not None:
            raise last_error
        raise ProviderError("no provider adapter available", retryable=False, status_code=503)

    async def execute_stream(self, plan: CompiledPlan):
        self.audit.append("plan.accepted", redacted_plan_for_audit(plan))
        last_error: ProviderError | None = None
        for provider in plan.provider_chain:
            if not self.registry.is_available(provider):
                continue
            adapter = self.adapters.get(provider)
            if adapter is None:
                continue
            started = monotonic()
            handle: RemoteHandle | None = None
            try:
                handle = await adapter.start(plan)
                self.audit.append("lease.started", {"plan_id": plan.plan_id, "provider": provider, "remote_id": handle.remote_id})
                async for event in adapter.stream(handle, plan):
                    yield _validate_stream_event(plan, event)
                self.registry.record(
                    self._observation(provider, started, success=True)
                )
                self.audit.append("plan.completed", {"plan_id": plan.plan_id, "provider": provider})
                return
            except ProviderError as exc:
                last_error = exc
                self.registry.record(
                    self._observation(provider, started, success=False)
                )
                self.audit.append(
                    "provider.failed",
                    {"plan_id": plan.plan_id, "provider": provider, "error": _provider_error_audit(exc)},
                )
                if not exc.retryable:
                    raise
            except Exception as exc:
                last_error = ProviderError("provider raised unexpected exception", retryable=True, status_code=502)
                self.registry.record(
                    self._observation(provider, started, success=False)
                )
                self.audit.append(
                    "provider.failed",
                    {"plan_id": plan.plan_id, "provider": provider, "error": _exception_audit(exc, retryable=True)},
                )
            finally:
                if handle is not None:
                    await self._cleanup_remote(adapter, handle, plan_id=plan.plan_id, provider=provider)
        if last_error is not None:
            raise last_error
        raise ProviderError("no provider adapter available", retryable=False, status_code=503)

    def _observation(self, provider: str, started: float, *, success: bool) -> ProviderObservation:
        latency_ms = (monotonic() - started) * 1000
        cost = max(latency_ms / 1000.0 * float(self.provider_costs.get(provider, 0.0)), 0.0)
        return ProviderObservation(provider=provider, latency_ms=latency_ms, success=success, cost=cost)

    async def _cleanup_remote(
        self,
        adapter: ProviderAdapter,
        handle: RemoteHandle,
        *,
        plan_id: str,
        provider: str,
        attempt: int | None = None,
    ) -> None:
        payload: dict[str, object] = {"plan_id": plan_id, "provider": provider, "remote_id": handle.remote_id}
        if attempt is not None:
            payload["attempt"] = attempt
        try:
            await adapter.cancel_remote(handle)
        except Exception as exc:
            self.audit.append("lease.cleanup_failed", {**payload, "error": _exception_audit(exc, retryable=True)})
            return
        self.audit.append("lease.cleaned_up", payload)

    async def submit_async(self, plan: CompiledPlan, *, owner_identity: str | None = None) -> JobRecord:
        stored_plan = _storage_safe_plan(plan)
        job = await self.jobs.create(stored_plan, owner_identity=owner_identity)
        self._job_plans[job.job_id] = plan
        self.audit.append("job.created", {"job_id": job.job_id, "plan": redacted_plan_for_audit(plan)})
        self._job_tasks[job.job_id] = asyncio.create_task(self._run_job(job.job_id))
        return job

    def cancel_job(self, job_id: str) -> None:
        task = self._job_tasks.get(job_id)
        if task is not None:
            task.cancel()

    async def _run_job(self, job_id: str) -> None:
        job = await self.jobs.get(job_id)
        if job is None:
            return
        await self.jobs.update(job_id, state=JobState.RUNNING)
        plan = self._job_plans.get(job_id, job.plan)
        if plan.attestations.get("storage_safe_plan") is True:
            await self.jobs.update(job_id, state=JobState.EXPIRED, error="gateway restarted before job dispatch")
            self.audit.append("job.expired", {"job_id": job_id, "reason": "gateway restarted before job dispatch"})
            return
        try:
            result = await self.execute_sync(plan)
            result_ref = result.ref
            await self.jobs.update(job_id, state=JobState.COMPLETED, result_ref=result_ref, result=result)
            self.audit.append("job.completed", {"job_id": job_id, "result_kind": result.kind})
        except asyncio.CancelledError:
            current = await self.jobs.get(job_id)
            if current is not None and current.state not in {
                JobState.COMPLETED,
                JobState.FAILED,
                JobState.CANCELLED,
                JobState.EXPIRED,
            }:
                await self.jobs.update(job_id, state=JobState.CANCELLED, error="job cancelled")
            self.audit.append("job.cancelled", {"job_id": job_id})
            raise
        except ProviderError as exc:
            await self.jobs.update(job_id, state=JobState.FAILED, error=_job_error_message(exc), result=None)
            self.audit.append("job.failed", {"job_id": job_id, "error": _provider_error_audit(exc)})
        finally:
            self._job_tasks.pop(job_id, None)
            self._job_plans.pop(job_id, None)


def _storage_safe_plan(plan: CompiledPlan) -> CompiledPlan:
    attestations = dict(plan.attestations)
    attestations["storage_safe_plan"] = True
    return plan.model_copy(
        update={"input_refs": [], "inline_inputs": {}, "messages": [], "system_prompt": None, "attestations": attestations}
    )


def _job_error_message(exc: ProviderError) -> str:
    return f"provider execution failed ({exc.code or 'PROVIDER_ERROR'})"


def _validate_stream_event(plan: CompiledPlan, event: str) -> str:
    if not isinstance(event, str):
        raise ProviderError("stream event must be text", retryable=True, status_code=502)
    if not event.endswith("\n\n"):
        raise ProviderError("stream event must be SSE-framed", retryable=True, status_code=502)
    if event.startswith(": ") or event.startswith(":\n") or event.startswith("data: "):
        return event
    raise ProviderError(
        f"stream event does not match provider stream contract {plan.mode.value}",
        retryable=True,
        status_code=502,
    )


def _requires_json_output(plan: CompiledPlan) -> bool:
    return plan.response_format is not None and plan.response_format.type in {
        ResponseFormatType.JSON_OBJECT,
        ResponseFormatType.JSON_SCHEMA,
    }


def _requires_checked_inline_output(plan: CompiledPlan) -> bool:
    return plan.task in {"infer", "vision"} or _requires_json_output(plan)


def _output_validation_attempts(plan: CompiledPlan) -> int:
    return max(int(getattr(plan, "output_validation_attempts", 1) or 1), 1)


def _validate_provider_output(plan: CompiledPlan, result: ProviderResult) -> ProviderResult:
    if _requires_checked_inline_output(plan) and result.kind == "inline":
        if result.value is None or not result.value.strip():
            raise ProviderError("empty provider output", retryable=True, code="EMPTY_OUTPUT", raw_output=result.value or "")
    if not _requires_json_output(plan):
        return result
    if result.kind != "inline" or result.value is None:
        raise ProviderError("structured output must be inline text", retryable=True, code="MALFORMED_OUTPUT")
    try:
        parsed = json.loads(result.value)
    except json.JSONDecodeError as exc:
        raise ProviderError("malformed structured output", retryable=True, code="MALFORMED_OUTPUT", raw_output=result.value) from exc
    if plan.response_format is not None and plan.response_format.type is ResponseFormatType.JSON_OBJECT:
        if not isinstance(parsed, dict):
            raise ProviderError("structured output must be a JSON object", retryable=True, code="MALFORMED_OUTPUT", raw_output=result.value)
    if (
        plan.response_format is not None
        and plan.response_format.type is ResponseFormatType.JSON_SCHEMA
        and plan.response_format.strict
        and plan.response_format.json_schema is not None
    ):
        try:
            import jsonschema

            jsonschema.validate(parsed, plan.response_format.json_schema)
        except Exception as exc:
            raise ProviderError("structured output does not match JSON schema", retryable=True, code="MALFORMED_OUTPUT", raw_output=result.value) from exc
    return result.model_copy(update={"output_validated": True})


class LeaseReaper:
    def __init__(
        self,
        jobs: JobStore,
        audit: AuditTrail,
        interval_seconds: float = 5.0,
        cancel_job: Callable[[str], None] | None = None,
    ) -> None:
        self.jobs = jobs
        self.audit = audit
        self.interval_seconds = interval_seconds
        self.cancel_job = cancel_job
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval_seconds)
            now = datetime.now(timezone.utc)
            for job in await self.jobs.all():
                if job.state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED, JobState.EXPIRED}:
                    continue
                if job.created_at.timestamp() + job.plan.lease_ttl_seconds <= now.timestamp():
                    await self.jobs.update(job.job_id, state=JobState.EXPIRED, error="lease expired")
                    if self.cancel_job is not None:
                        self.cancel_job(job.job_id)
                    self.audit.append("job.expired", {"job_id": job.job_id, "plan_id": job.plan.plan_id})


class ProviderReconciler:
    def __init__(
        self,
        adapters: dict[str, ProviderAdapter],
        audit: AuditTrail,
        interval_seconds: float = 300.0,
    ) -> None:
        self.adapters = adapters
        self.audit = audit
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        await self._reconcile_once()
        while True:
            await asyncio.sleep(self.interval_seconds)
            await self._reconcile_once()

    async def _reconcile_once(self) -> None:
        for name, adapter in self.adapters.items():
            reconcile = getattr(adapter, "reconcile_orphans", None)
            if not callable(reconcile):
                continue
            try:
                await reconcile()
                self.audit.append("provider.reconciled", {"provider": name})
            except Exception as exc:
                self.audit.append("provider.reconcile_failed", {"provider": name, "error": _exception_audit(exc, retryable=True)})


def _provider_error_audit(exc: ProviderError) -> dict[str, object]:
    detail = str(exc)
    return {
        "code": exc.code or "PROVIDER_ERROR",
        "status_code": exc.status_code,
        "retryable": exc.retryable,
        "message_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        "message_bytes": len(detail.encode("utf-8")),
    }


def _exception_audit(exc: Exception, *, retryable: bool) -> dict[str, object]:
    detail = f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}"
    return {
        "code": "PROVIDER_EXCEPTION",
        "retryable": retryable,
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        "message_bytes": len(detail.encode("utf-8")),
    }
