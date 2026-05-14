from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from time import monotonic
from typing import Callable
from uuid import uuid4

from gpucall.admission import AdmissionController, AdmissionLease
from gpucall.audit import AuditTrail, redacted_plan_for_audit
from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.domain import (
    ArtifactManifest,
    CompiledPlan,
    JobRecord,
    JobState,
    ProviderErrorCode,
    TupleError,
    TupleObservation,
    TupleResult,
    ResponseFormatType,
)
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.provider_errors import is_provider_temporary_unavailable, provider_error_class, should_suppress_provider_family
from gpucall.registry import ObservedRegistry


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    async def create(self, plan: CompiledPlan, *, owner_identity: str | None = None) -> JobRecord:
        job = JobRecord(job_id=uuid4().hex, state=JobState.QUEUED, plan=plan, owner_identity=owner_identity)
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
        adapters: dict[str, TupleAdapter],
        registry: ObservedRegistry,
        audit: AuditTrail,
        jobs: JobStore,
        tuple_costs: dict[str, float] | None = None,
        artifact_registry: SQLiteArtifactRegistry | None = None,
        admission: AdmissionController | None = None,
        on_async_success: Callable[[CompiledPlan], None] | None = None,
        on_async_terminal_failure: Callable[[CompiledPlan], None] | None = None,
    ) -> None:
        self.adapters = adapters
        self.registry = registry
        self.audit = audit
        self.jobs = jobs
        self.tuple_costs = tuple_costs or {}
        self.artifact_registry = artifact_registry
        self.admission = admission or AdmissionController()
        self.on_async_success = on_async_success
        self.on_async_terminal_failure = on_async_terminal_failure
        self._job_tasks: dict[str, asyncio.Task[None]] = {}
        self._job_plans: dict[str, CompiledPlan] = {}

    async def execute_sync(self, plan: CompiledPlan) -> TupleResult:
        self.audit.append("plan.accepted", redacted_plan_for_audit(plan))
        last_error: TupleError | None = None
        workload_scope = _admission_workload_scope(plan)
        started_attempts = 0
        family_attempts: dict[str, int] = {}
        max_fallback_attempts = _max_fallback_attempts()
        max_family_attempts = _max_provider_family_attempts()
        for tuple in plan.tuple_chain:
            if not self.registry.is_available(tuple):
                continue
            adapter = self.adapters.get(tuple)
            if adapter is None:
                continue
            attempts = _output_validation_attempts(plan)
            for attempt in range(1, attempts + 1):
                if max_fallback_attempts > 0 and started_attempts >= max_fallback_attempts:
                    self.audit.append(
                        "plan.fallback_exhausted",
                        {"plan_id": plan.plan_id, "max_attempts": max_fallback_attempts},
                    )
                    break
                family = self.admission.family_for(tuple)
                if max_family_attempts > 0 and family_attempts.get(family, 0) >= max_family_attempts:
                    self.audit.append(
                        "tuple.admission_rejected",
                        {
                            "plan_id": plan.plan_id,
                            "tuple": tuple,
                            "reason": "provider_family_attempt_limit",
                            "provider_family": family,
                        },
                    )
                    last_error = TupleError(
                        "provider family fallback attempt limit reached",
                        retryable=True,
                        status_code=503,
                        code=ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE,
                    )
                    break
                started = monotonic()
                handle: RemoteHandle | None = None
                admission_lease: AdmissionLease | None = None
                try:
                    decision = await self.admission.acquire_with_wait(
                        tuple,
                        workload_scope=workload_scope,
                        wait_seconds=_admission_wait_seconds(plan),
                    )
                    if not decision.allowed:
                        self.audit.append(
                            "tuple.admission_rejected",
                            {
                                "plan_id": plan.plan_id,
                                "tuple": tuple,
                                "reason": decision.reason,
                                "workload_scope": workload_scope,
                                "suppressed_until_seconds": decision.suppressed_until_seconds,
                            },
                        )
                        last_error = TupleError(
                            "tuple is temporarily unavailable due to runtime admission constraints",
                            retryable=True,
                            status_code=503,
                            code=ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE,
                        )
                        break
                    admission_lease = decision.lease
                    started_attempts += 1
                    family_attempts[family] = family_attempts.get(family, 0) + 1
                    _enforce_pre_execution_security_gate(plan)
                    handle = await adapter.start(plan)
                    self.audit.append(
                        "lease.started",
                        {**_lease_audit(handle), "plan_id": plan.plan_id, "tuple": tuple, "attempt": attempt},
                    )
                    result = await asyncio.wait_for(adapter.wait(handle, plan), timeout=plan.timeout_seconds)
                    result = _validate_and_register_tuple_output(self, plan, result)
                    self.registry.record(
                        self._observation(tuple, started, success=True)
                    )
                    self.audit.append("plan.completed", {"plan_id": plan.plan_id, "tuple": tuple, "attempt": attempt})
                    return result
                except TupleError as exc:
                    last_error = exc
                    if exc.code in {"EMPTY_OUTPUT", "MALFORMED_OUTPUT"}:
                        final_attempt = attempt >= attempts
                        self.audit.append(
                            "tuple.output_rejected",
                            {
                                "plan_id": plan.plan_id,
                                "tuple": tuple,
                                "attempt": attempt,
                                "code": exc.code,
                                "retryable": not final_attempt,
                            },
                        )
                        if _structured_failure_should_switch_tuple(plan, exc):
                            self.registry.record_quality_failure(
                                tuple,
                                recipe=plan.recipe_name,
                                task=plan.task,
                                mode=plan.mode.value,
                                code=exc.code,
                            )
                            last_error = TupleError(
                                _job_error_message(exc),
                                retryable=False,
                                status_code=422,
                                code=exc.code,
                                raw_output=exc.raw_output,
                            )
                            break
                        if not final_attempt:
                            continue
                        last_error = TupleError(
                            _job_error_message(exc),
                            retryable=False,
                            status_code=422,
                            code=exc.code,
                            raw_output=exc.raw_output,
                        )
                        break
                    self.registry.record(
                        self._observation(tuple, started, success=False)
                    )
                    is_provider_error = is_provider_temporary_unavailable(exc.code)
                    if is_provider_error:
                        await self.admission.suppress(
                            tuple,
                            code=exc.code,
                            suppress_family=should_suppress_provider_family(exc.code),
                        )

                    event_type = "tuple.provider_temporary_failure" if is_provider_error else "tuple.failed"
                    self.audit.append(
                        event_type,
                        {"plan_id": plan.plan_id, "tuple": tuple, "error": _tuple_error_audit(exc)},
                    )

                    if not _provider_error_fallback_eligible(exc):
                        raise
                    break
                except asyncio.TimeoutError as exc:
                    last_error = TupleError(
                        "tuple timed out",
                        retryable=True,
                        status_code=504,
                        code=ProviderErrorCode.PROVIDER_POLL_TIMEOUT,
                    )
                    self.registry.record(
                        self._observation(tuple, started, success=False)
                    )
                    await self.admission.suppress(
                        tuple,
                        code=last_error.code,
                        suppress_family=should_suppress_provider_family(last_error.code),
                    )
                    self.audit.append("tuple.timeout", {"plan_id": plan.plan_id, "tuple": tuple})
                    break
                except Exception as exc:
                    last_error = TupleError(
                        "tuple raised unexpected exception",
                        retryable=True,
                        status_code=502,
                        code=ProviderErrorCode.PROVIDER_ERROR,
                    )
                    self.registry.record(
                        self._observation(tuple, started, success=False)
                    )
                    self.audit.append(
                        "tuple.failed",
                        {
                            "plan_id": plan.plan_id,
                            "tuple": tuple,
                            "error": _exception_audit(exc, retryable=True),
                        },
                    )
                    break
                finally:
                    if handle is not None:
                        await self._cleanup_remote(adapter, handle, plan_id=plan.plan_id, tuple=tuple, attempt=attempt)
                    await self.admission.release(admission_lease)
        if last_error is not None:
            raise last_error
        raise TupleError("no tuple adapter available", retryable=False, status_code=503)

    async def execute_stream(self, plan: CompiledPlan):
        self.audit.append("plan.accepted", redacted_plan_for_audit(plan))
        last_error: TupleError | None = None
        workload_scope = _admission_workload_scope(plan)
        started_attempts = 0
        family_attempts: dict[str, int] = {}
        max_fallback_attempts = _max_fallback_attempts()
        max_family_attempts = _max_provider_family_attempts()
        for tuple in plan.tuple_chain:
            if not self.registry.is_available(tuple):
                continue
            adapter = self.adapters.get(tuple)
            if adapter is None:
                continue
            if max_fallback_attempts > 0 and started_attempts >= max_fallback_attempts:
                self.audit.append(
                    "plan.fallback_exhausted",
                    {"plan_id": plan.plan_id, "max_attempts": max_fallback_attempts},
                )
                break
            family = self.admission.family_for(tuple)
            if max_family_attempts > 0 and family_attempts.get(family, 0) >= max_family_attempts:
                self.audit.append(
                    "tuple.admission_rejected",
                    {
                        "plan_id": plan.plan_id,
                        "tuple": tuple,
                        "reason": "provider_family_attempt_limit",
                        "provider_family": family,
                    },
                )
                last_error = TupleError(
                    "provider family fallback attempt limit reached",
                    retryable=True,
                    status_code=503,
                    code=ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE,
                )
                continue
            started = monotonic()
            handle: RemoteHandle | None = None
            admission_lease: AdmissionLease | None = None
            try:
                decision = await self.admission.acquire_with_wait(
                    tuple,
                    workload_scope=workload_scope,
                    wait_seconds=_admission_wait_seconds(plan),
                )
                if not decision.allowed:
                    self.audit.append(
                        "tuple.admission_rejected",
                        {
                            "plan_id": plan.plan_id,
                            "tuple": tuple,
                            "reason": decision.reason,
                            "workload_scope": workload_scope,
                            "suppressed_until_seconds": decision.suppressed_until_seconds,
                        },
                    )
                    last_error = TupleError(
                        "tuple is temporarily unavailable due to runtime admission constraints",
                        retryable=True,
                        status_code=503,
                        code=ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE,
                    )
                    continue
                admission_lease = decision.lease
                started_attempts += 1
                family_attempts[family] = family_attempts.get(family, 0) + 1
                _enforce_pre_execution_security_gate(plan)
                handle = await adapter.start(plan)
                self.audit.append("lease.started", {**_lease_audit(handle), "plan_id": plan.plan_id, "tuple": tuple})
                async for event in adapter.stream(handle, plan):
                    yield _validate_stream_event(plan, event)
                self.registry.record(
                    self._observation(tuple, started, success=True)
                )
                self.audit.append("plan.completed", {"plan_id": plan.plan_id, "tuple": tuple})
                return
            except TupleError as exc:
                last_error = exc
                self.registry.record(
                    self._observation(tuple, started, success=False)
                )
                is_provider_error = is_provider_temporary_unavailable(exc.code)
                if is_provider_error:
                    await self.admission.suppress(
                        tuple,
                        code=exc.code,
                        suppress_family=should_suppress_provider_family(exc.code),
                    )

                event_type = "tuple.provider_temporary_failure" if is_provider_error else "tuple.failed"
                self.audit.append(
                    event_type,
                    {"plan_id": plan.plan_id, "tuple": tuple, "error": _tuple_error_audit(exc)},
                )

                if not _provider_error_fallback_eligible(exc):
                    raise
            except Exception as exc:
                last_error = TupleError(
                    "tuple raised unexpected exception",
                    retryable=True,
                    status_code=502,
                    code=ProviderErrorCode.PROVIDER_ERROR,
                )
                self.registry.record(
                    self._observation(tuple, started, success=False)
                )
                self.audit.append(
                    "tuple.failed",
                    {"plan_id": plan.plan_id, "tuple": tuple, "error": _exception_audit(exc, retryable=True)},
                )
            finally:
                if handle is not None:
                    await self._cleanup_remote(adapter, handle, plan_id=plan.plan_id, tuple=tuple)
                await self.admission.release(admission_lease)
        if last_error is not None:
            raise last_error
        raise TupleError("no tuple adapter available", retryable=False, status_code=503)

    def _observation(self, tuple: str, started: float, *, success: bool) -> TupleObservation:
        latency_ms = (monotonic() - started) * 1000
        cost = max(latency_ms / 1000.0 * float(self.tuple_costs.get(tuple, 0.0)), 0.0)
        return TupleObservation(tuple=tuple, latency_ms=latency_ms, success=success, cost=cost)

    async def _cleanup_remote(
        self,
        adapter: TupleAdapter,
        handle: RemoteHandle,
        *,
        plan_id: str,
        tuple: str,
        attempt: int | None = None,
    ) -> None:
        payload: dict[str, object] = {**_lease_audit(handle), "plan_id": plan_id, "tuple": tuple}
        if attempt is not None:
            payload["attempt"] = attempt
        try:
            await asyncio.wait_for(adapter.cancel_remote(handle), timeout=_remote_cleanup_timeout_seconds())
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
            self._commit_async_budget(plan)
        except asyncio.CancelledError:
            current = await self.jobs.get(job_id)
            if current is not None and not is_terminal_job_state(current.state):
                await self.jobs.update(job_id, state=JobState.CANCELLED, error="job cancelled")
            self.audit.append("job.cancelled", {"job_id": job_id})
            self._release_async_budget(plan)
            raise
        except TupleError as exc:
            provider_error_code = None
            if exc.code:
                try:
                    provider_error_code = ProviderErrorCode(exc.code)
                except ValueError:
                    pass
            await self.jobs.update(
                job_id,
                state=JobState.FAILED,
                error=_job_error_message(exc),
                result=None,
                provider_error_code=provider_error_code,
            )
            self.audit.append("job.failed", {"job_id": job_id, "error": _tuple_error_audit(exc)})
            self._release_async_budget(plan)
        finally:
            self._job_tasks.pop(job_id, None)
            self._job_plans.pop(job_id, None)

    def _release_async_budget(self, plan: CompiledPlan) -> None:
        if self.on_async_terminal_failure is None:
            return
        try:
            self.on_async_terminal_failure(plan)
        except Exception as exc:
            self.audit.append("job.budget_release_failed", {"plan_id": plan.plan_id, "error": _exception_audit(exc, retryable=True)})

    def _commit_async_budget(self, plan: CompiledPlan) -> None:
        if self.on_async_success is None:
            return
        try:
            self.on_async_success(plan)
        except Exception as exc:
            self.audit.append("job.budget_commit_failed", {"plan_id": plan.plan_id, "error": _exception_audit(exc, retryable=True)})


def _storage_safe_plan(plan: CompiledPlan) -> CompiledPlan:
    attestations = dict(plan.attestations)
    attestations["storage_safe_plan"] = True
    return plan.model_copy(
        update={"input_refs": [], "inline_inputs": {}, "messages": [], "system_prompt": None, "attestations": attestations}
    )


def is_terminal_job_state(state: JobState) -> bool:
    return state in {
        JobState.SUCCEEDED,
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.EXPIRED,
        JobState.COMPLETED_AFTER_CALLER_TIMEOUT,
    }


def _lease_audit(handle: RemoteHandle) -> dict[str, object]:
    return {
        "remote_id": handle.remote_id,
        "account_ref": handle.account_ref,
        "execution_surface": handle.execution_surface,
        "resource_kind": handle.resource_kind,
        "cleanup_required": handle.cleanup_required,
        "reaper_eligible": handle.reaper_eligible,
    }


def _job_error_message(exc: TupleError) -> str:
    return f"tuple execution failed ({exc.code or 'PROVIDER_ERROR'})"


def _provider_error_fallback_eligible(exc: TupleError) -> bool:
    if not is_provider_temporary_unavailable(exc.code):
        return bool(exc.retryable)
    provider_class = provider_error_class(exc.code)
    return bool(provider_class and provider_class.fallback_eligible)


def _validate_stream_event(plan: CompiledPlan, event: str) -> str:
    if not isinstance(event, str):
        raise TupleError("stream event must be text", retryable=True, status_code=502)
    if not event.endswith("\n\n"):
        raise TupleError("stream event must be SSE-framed", retryable=True, status_code=502)
    if event.startswith(": ") or event.startswith(":\n") or event.startswith("data: "):
        return event
    raise TupleError(
        f"stream event does not match tuple stream contract {plan.mode.value}",
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


def _structured_failure_should_switch_tuple(plan: CompiledPlan, exc: TupleError) -> bool:
    return (
        exc.code == "MALFORMED_OUTPUT"
        and plan.response_format is not None
        and plan.response_format.type is ResponseFormatType.JSON_SCHEMA
        and plan.response_format.strict
    )


def _admission_workload_scope(plan: CompiledPlan) -> str:
    intent = (
        plan.metadata.get("intent")
        or plan.metadata.get("task_family")
        or plan.metadata.get("gpucall_intent")
        or plan.recipe_name
    )
    return f"{plan.task}:{intent}:{plan.mode.value}"


def _max_fallback_attempts() -> int:
    return _env_non_negative_int("GPUCALL_MAX_FALLBACK_ATTEMPTS", 16)


def _max_provider_family_attempts() -> int:
    return _env_non_negative_int("GPUCALL_MAX_PROVIDER_FAMILY_ATTEMPTS", 4)


def _admission_wait_seconds(plan: CompiledPlan) -> float:
    configured = _env_non_negative_float("GPUCALL_ADMISSION_WAIT_SECONDS", 0.0)
    if configured <= 0:
        return 0.0
    return min(configured, max(float(plan.timeout_seconds) - 1.0, 0.0))


def _env_non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(int(raw), 0)
    except ValueError:
        return default


def _env_non_negative_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return default


def _enforce_pre_execution_security_gate(plan: CompiledPlan) -> None:
    attestations = plan.attestations or {}
    security_gate = attestations.get("security_gate")
    if isinstance(security_gate, dict) and security_gate.get("attestation_required") is True:
        evidence = attestations.get("attestation_evidence")
        if not isinstance(evidence, dict) or evidence.get("verified") is not True:
            raise TupleError("verified attestation evidence is required before execution", retryable=False, status_code=412, code="ATTESTATION_REQUIRED")
    key_release = attestations.get("key_release_requirement")
    if isinstance(key_release, dict) and key_release.get("required") is True:
        grant = attestations.get("key_release_grant")
        if not isinstance(grant, dict):
            raise TupleError("key release grant is required before execution", retryable=False, status_code=412, code="KEY_RELEASE_REQUIRED")
        if grant.get("key_id") != key_release.get("key_id"):
            raise TupleError("key release grant does not match required key_id", retryable=False, status_code=412, code="KEY_RELEASE_REQUIRED")
        if not hmac.compare_digest(str(grant.get("policy_hash") or ""), str(key_release.get("policy_hash") or "")):
            raise TupleError("key release grant does not match required policy_hash", retryable=False, status_code=412, code="KEY_RELEASE_REQUIRED")
        expires_at = _parse_datetime(grant.get("expires_at"))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            raise TupleError("key release grant is expired or missing expires_at", retryable=False, status_code=412, code="KEY_RELEASE_REQUIRED")


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _validate_tuple_output(plan: CompiledPlan, result: TupleResult) -> TupleResult:
    if _requires_checked_inline_output(plan) and result.kind == "inline":
        if result.value is None or not result.value.strip():
            raise TupleError("empty tuple output", retryable=True, code="EMPTY_OUTPUT", raw_output=result.value or "")
    if not _requires_json_output(plan):
        return result
    if result.openai_choices:
        for choice in result.openai_choices:
            content = _openai_choice_content(choice)
            _validate_structured_text(plan, content)
        return result.model_copy(update={"output_validated": True})
    if result.kind != "inline" or result.value is None:
        raise TupleError("structured output must be inline text", retryable=True, code="MALFORMED_OUTPUT")
    _validate_structured_text(plan, result.value)
    return result.model_copy(update={"output_validated": True})


def _openai_choice_content(choice: dict[str, object]) -> str:
    message = choice.get("message")
    if not isinstance(message, dict):
        raise TupleError("OpenAI-compatible structured choice missing message", retryable=True, code="MALFORMED_OUTPUT")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise TupleError("OpenAI-compatible structured choice missing text content", retryable=True, code="MALFORMED_OUTPUT")
    return content


def _validate_structured_text(plan: CompiledPlan, value: str) -> None:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise TupleError("malformed structured output", retryable=True, code="MALFORMED_OUTPUT", raw_output=value) from exc
    if plan.response_format is not None and plan.response_format.type is ResponseFormatType.JSON_OBJECT:
        if not isinstance(parsed, dict):
            raise TupleError("structured output must be a JSON object", retryable=True, code="MALFORMED_OUTPUT", raw_output=value)
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
            raise TupleError("structured output does not match JSON schema", retryable=True, code="MALFORMED_OUTPUT", raw_output=value) from exc


def _validate_and_register_tuple_output(dispatcher: Dispatcher, plan: CompiledPlan, result: TupleResult) -> TupleResult:
    result = _validate_tuple_output(plan, result)
    if result.kind == "artifact_manifest":
        manifest = _artifact_manifest_from_result(result)
        _validate_artifact_manifest(plan, manifest)
        if dispatcher.artifact_registry is not None:
            dispatcher.artifact_registry.append(manifest)
            dispatcher.audit.append(
                "artifact.registered",
                {
                    "plan_id": plan.plan_id,
                    "artifact_id": manifest.artifact_id,
                    "artifact_chain_id": manifest.artifact_chain_id,
                    "version": manifest.version,
                    "classification": manifest.classification.value,
                },
            )
        return result.model_copy(update={"artifact_manifest": manifest, "output_validated": True})
    return result


def _artifact_manifest_from_result(result: TupleResult) -> ArtifactManifest:
    if result.artifact_manifest is not None:
        return result.artifact_manifest
    if result.value is not None:
        try:
            return ArtifactManifest.model_validate_json(result.value)
        except Exception as exc:
            raise TupleError("artifact manifest output is malformed", retryable=True, status_code=502, code="MALFORMED_ARTIFACT") from exc
    raise TupleError("artifact manifest result is missing manifest", retryable=True, status_code=502, code="MALFORMED_ARTIFACT")


def _validate_artifact_manifest(plan: CompiledPlan, manifest: ArtifactManifest) -> None:
    expected_hash = (plan.attestations or {}).get("governance_hash")
    if expected_hash and manifest.producer_plan_hash != expected_hash:
        raise TupleError("artifact manifest producer_plan_hash does not match plan", retryable=False, status_code=502, code="ARTIFACT_POLICY_VIOLATION")
    if plan.artifact_export is not None:
        export = plan.artifact_export
        if manifest.artifact_chain_id != export.artifact_chain_id or manifest.version != export.version:
            raise TupleError("artifact manifest does not match requested artifact export", retryable=False, status_code=502, code="ARTIFACT_POLICY_VIOLATION")
        if manifest.key_id != export.key_id:
            raise TupleError("artifact manifest key_id does not match key release requirement", retryable=False, status_code=502, code="ARTIFACT_POLICY_VIOLATION")
    if manifest.classification != plan.data_classification:
        raise TupleError("artifact manifest classification must inherit task classification", retryable=False, status_code=502, code="ARTIFACT_POLICY_VIOLATION")


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
                if is_terminal_job_state(job.state):
                    continue
                if job.created_at.timestamp() + job.plan.lease_ttl_seconds <= now.timestamp():
                    await self.jobs.update(job.job_id, state=JobState.EXPIRED, error="lease expired")
                    if self.cancel_job is not None:
                        self.cancel_job(job.job_id)
                    self.audit.append("job.expired", {"job_id": job.job_id, "plan_id": job.plan.plan_id})


class TupleReconciler:
    def __init__(
        self,
        adapters: dict[str, TupleAdapter],
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
                self.audit.append("tuple.reconciled", {"tuple": name})
            except Exception as exc:
                self.audit.append("tuple.reconcile_failed", {"tuple": name, "error": _exception_audit(exc, retryable=True)})


def _tuple_error_audit(exc: TupleError) -> dict[str, object]:
    detail = str(exc)
    payload: dict[str, object] = {
        "code": exc.code or "PROVIDER_ERROR",
        "status_code": exc.status_code,
        "retryable": exc.retryable,
        "message_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        "message_bytes": len(detail.encode("utf-8")),
    }
    if exc.raw_output is not None:
        payload["raw_output_sha256"] = hashlib.sha256(exc.raw_output.encode("utf-8")).hexdigest()
        payload["raw_output_bytes"] = len(exc.raw_output.encode("utf-8"))
    return payload


def _remote_cleanup_timeout_seconds() -> float:
    raw = os.getenv("GPUCALL_REMOTE_CLEANUP_TIMEOUT_SECONDS", "30")
    try:
        return max(float(raw), 0.001)
    except ValueError:
        return 30.0


def _exception_audit(exc: Exception, *, retryable: bool) -> dict[str, object]:
    detail = f"{type(exc).__module__}.{type(exc).__qualname__}:{exc}"
    return {
        "code": "PROVIDER_EXCEPTION",
        "retryable": retryable,
        "exception_type": f"{type(exc).__module__}.{type(exc).__qualname__}",
        "message_sha256": hashlib.sha256(detail.encode("utf-8")).hexdigest(),
        "message_bytes": len(detail.encode("utf-8")),
    }
