from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from gpucall.admission import AdmissionController
from gpucall.audit import AuditTrail
from gpucall.dispatcher import Dispatcher, JobStore
from gpucall.domain import CompiledPlan, ExecutionMode, JobState, ProviderErrorCode, TupleError, TupleResult
from gpucall.execution import EchoTuple, RemoteHandle
from gpucall.openai_facade.chat_completions import OpenAIProtocolError, admit_openai_chat_completion
from gpucall.provider_errors import should_suppress_provider_family
from gpucall.registry import ObservedRegistry
from gpucall.tenant import TenantUsageLedger


@dataclass
class AcceptanceCheck:
    id: str
    name: str
    passed: bool
    details: dict[str, Any] = field(default_factory=dict)


class ProviderCodeFailingTuple(EchoTuple):
    def __init__(self, name: str, code: ProviderErrorCode) -> None:
        super().__init__(name=name, latency_seconds=0.0)
        self.code = code
        self.cancelled_handles: list[str] = []

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        raise TupleError("provider temporarily unavailable", retryable=True, status_code=503, code=self.code)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        self.cancelled_handles.append(handle.remote_id)


class SlowTuple(EchoTuple):
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        await asyncio.sleep(0.05)
        return TupleResult(kind="inline", value=f"ok:{plan.task}:{self.name}")


def run_production_acceptance() -> dict[str, Any]:
    return asyncio.run(run_production_acceptance_async())


async def run_production_acceptance_async() -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="gpucall-acceptance-") as raw_root:
        root = Path(raw_root)
        checks = [
            await _check_f2_f6_live_executability(root),
            await _check_f4_provider_failure_suppression(root),
            await _check_f5_fallback_storm_bound(root),
            _check_f7_openai_chat_contract(),
            await _check_f9_budget_lifecycle(root),
            await _check_f10_async_lifecycle(root),
        ]
    passed = all(item.passed for item in checks)
    return {
        "schema_version": 1,
        "phase": "production_acceptance",
        "passed": passed,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "checks": [
            {"id": item.id, "name": item.name, "passed": item.passed, "details": item.details}
            for item in checks
        ],
    }


def dumps_acceptance_report(report: dict[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


async def _check_f2_f6_live_executability(root: Path) -> AcceptanceCheck:
    with _temporary_env(
        GPUCALL_TUPLE_CONCURRENCY_LIMIT="10",
        GPUCALL_PROVIDER_FAMILY_CONCURRENCY_LIMIT="10",
        GPUCALL_WORKLOAD_SCOPE_CONCURRENCY_LIMIT="1",
        GPUCALL_ADMISSION_WAIT_SECONDS="0",
    ):
        dispatcher = Dispatcher(
            adapters={"a": SlowTuple("a"), "b": SlowTuple("b")},
            registry=ObservedRegistry(),
            audit=AuditTrail(root / "f2-f6-audit.jsonl"),
            jobs=JobStore(),
        )

        async def run(item: CompiledPlan) -> TupleResult | TupleError:
            try:
                return await dispatcher.execute_sync(item)
            except TupleError as exc:
                return exc

        results = await asyncio.gather(run(_plan(["a"])), run(_plan(["b"])))
        failures = [item for item in results if isinstance(item, TupleError)]
        admission = dispatcher.admission.snapshot()
        passed = (
            len(failures) == 1
            and failures[0].code == ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE
            and admission["workload_scope_limit"] == 1
        )
        return AcceptanceCheck(
            id="F2/F6",
            name="static eligibility is separated from live executability",
            passed=passed,
            details={
                "result_types": [type(item).__name__ for item in results],
                "failure_codes": [item.code for item in failures],
                "runtime_admission": admission,
            },
        )


async def _check_f4_provider_failure_suppression(root: Path) -> AcceptanceCheck:
    with _temporary_env(GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS="60"):
        bad = ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED)
        dispatcher = Dispatcher(
            adapters={"bad": bad, "good": EchoTuple("good", latency_seconds=0.0)},
            registry=ObservedRegistry(),
            audit=AuditTrail(root / "f4-audit.jsonl"),
            jobs=JobStore(),
        )
        first = await dispatcher.execute_sync(_plan(["bad", "good"]))
        second = await dispatcher.execute_sync(_plan(["bad", "good"]))
        audit = (root / "f4-audit.jsonl").read_text(encoding="utf-8")
        snapshot = dispatcher.admission.snapshot()
        passed = (
            first.value == "ok:infer:good"
            and second.value == "ok:infer:good"
            and len(bad.cancelled_handles) == 1
            and "bad" in snapshot["suppressed_tuples"]
            and '"reason":"tuple_suppressed"' in audit
        )
        return AcceptanceCheck(
            id="F4",
            name="provider temporary failure becomes routing state",
            passed=passed,
            details={
                "suppressed_tuples": snapshot["suppressed_tuples"],
                "suppressed_provider_families": snapshot["suppressed_provider_families"],
                "bad_tuple_started_count": len(bad.cancelled_handles),
            },
        )


async def _check_f5_fallback_storm_bound(root: Path) -> AcceptanceCheck:
    with _temporary_env(GPUCALL_MAX_FALLBACK_ATTEMPTS="1"):
        bad = ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED)
        dispatcher = Dispatcher(
            adapters={"bad": bad, "good": EchoTuple("good", latency_seconds=0.0)},
            registry=ObservedRegistry(),
            audit=AuditTrail(root / "f5-audit.jsonl"),
            jobs=JobStore(),
        )
        caught: TupleError | None = None
        try:
            await dispatcher.execute_sync(_plan(["bad", "good"]))
        except TupleError as exc:
            caught = exc
        audit = (root / "f5-audit.jsonl").read_text(encoding="utf-8")
        passed = (
            caught is not None
            and caught.code == ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED
            and '"event_type":"plan.fallback_exhausted"' in audit
            and '"event_type":"plan.completed"' not in audit
        )
        return AcceptanceCheck(
            id="F5",
            name="fallback storm is bounded",
            passed=passed,
            details={"error_code": caught.code if caught else None, "bad_tuple_started_count": len(bad.cancelled_handles)},
        )


def _check_f7_openai_chat_contract() -> AcceptanceCheck:
    accepted = admit_openai_chat_completion(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
            "n": 2,
            "stream": True,
            "stream_options": {"include_usage": True},
            "response_format": {"type": "json_object"},
        },
        inline_bytes_limit=10_000,
    )
    rejected = False
    try:
        admit_openai_chat_completion(
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hello"}],
                "stream": True,
                "stream_options": {"include_obfuscation": True},
            },
            inline_bytes_limit=10_000,
        )
    except OpenAIProtocolError:
        rejected = True
    passed = accepted.stream is True and accepted.task_request.n == 2 and rejected
    return AcceptanceCheck(
        id="F7",
        name="OpenAI chat completion contract is accepted only inside declared support",
        passed=passed,
        details={
            "accepted_n": accepted.task_request.n,
            "accepted_stream_options": accepted.task_request.stream_options,
            "unsupported_obfuscation_rejected": rejected,
        },
    )


async def _check_f9_budget_lifecycle(root: Path) -> AcceptanceCheck:
    ledger = TenantUsageLedger(root / "tenant_usage.db")
    success_plan = _plan(["good"])
    failed_plan = _plan(["bad"])
    ledger.reserve("tenant-a", 1.0, tuple="good", recipe="r1", plan_id=success_plan.plan_id)
    ledger.reserve("tenant-a", 2.0, tuple="bad", recipe="r1", plan_id=failed_plan.plan_id)

    dispatcher = Dispatcher(
        adapters={"good": EchoTuple("good", latency_seconds=0.0), "bad": ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE)},
        registry=ObservedRegistry(),
        audit=AuditTrail(root / "f9-audit.jsonl"),
        jobs=JobStore(),
        on_async_success=lambda plan: ledger.commit_plan(plan.plan_id),
        on_async_terminal_failure=lambda plan: ledger.release_plan(plan.plan_id),
    )
    success = await dispatcher.submit_async(success_plan)
    success_task = dispatcher._job_tasks[success.job_id]
    failed = await dispatcher.submit_async(failed_plan)
    failed_task = dispatcher._job_tasks[failed.job_id]
    await success_task
    await failed_task

    with sqlite3.connect(root / "tenant_usage.db") as conn:
        rows = conn.execute("SELECT plan_id, status FROM tenant_usage ORDER BY estimated_cost_usd").fetchall()
    passed = rows == [(success_plan.plan_id, "committed"), (failed_plan.plan_id, "released")]
    return AcceptanceCheck(
        id="F9",
        name="budget lifecycle separates reserve, commit, and release",
        passed=passed,
        details={"statuses": [{"plan_id": plan_id, "status": status} for plan_id, status in rows]},
    )


async def _check_f10_async_lifecycle(root: Path) -> AcceptanceCheck:
    dispatcher = Dispatcher(
        adapters={
            "good": EchoTuple("good", latency_seconds=0.0),
            "bad": ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE),
            "slow": SlowTuple("slow"),
        },
        registry=ObservedRegistry(),
        audit=AuditTrail(root / "f10-audit.jsonl"),
        jobs=JobStore(),
    )
    completed = await dispatcher.submit_async(_plan(["good"]))
    completed_task = dispatcher._job_tasks[completed.job_id]
    failed = await dispatcher.submit_async(_plan(["bad"]))
    failed_task = dispatcher._job_tasks[failed.job_id]
    cancelled = await dispatcher.submit_async(_plan(["slow"]))
    cancelled_task = dispatcher._job_tasks[cancelled.job_id]
    for _ in range(20):
        stored = await dispatcher.jobs.get(cancelled.job_id)
        if stored is not None and stored.state is JobState.RUNNING:
            break
        await asyncio.sleep(0)
    dispatcher.cancel_job(cancelled.job_id)

    await completed_task
    await failed_task
    try:
        await cancelled_task
    except asyncio.CancelledError:
        pass

    completed_record = await dispatcher.jobs.get(completed.job_id)
    failed_record = await dispatcher.jobs.get(failed.job_id)
    cancelled_record = await dispatcher.jobs.get(cancelled.job_id)
    states = {
        "completed": completed_record.state if completed_record else None,
        "failed": failed_record.state if failed_record else None,
        "cancelled": cancelled_record.state if cancelled_record else None,
    }
    passed = states == {
        "completed": JobState.COMPLETED,
        "failed": JobState.FAILED,
        "cancelled": JobState.CANCELLED,
    }
    return AcceptanceCheck(
        id="F10",
        name="async lifecycle exposes terminal states deterministically",
        passed=passed,
        details={key: str(value) for key, value in states.items()},
    )


def _plan(chain: list[str], *, mode: ExecutionMode = ExecutionMode.SYNC) -> CompiledPlan:
    return CompiledPlan(
        policy_version="acceptance",
        recipe_name="r1",
        task="infer",
        mode=mode,
        tuple_chain=chain,
        timeout_seconds=2,
        lease_ttl_seconds=10,
        token_estimation_profile="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
        metadata={"intent": "standard_text_inference"},
    )


@contextmanager
def _temporary_env(**values: str) -> Iterator[None]:
    previous = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
