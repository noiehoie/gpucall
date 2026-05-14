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

from gpucall.acceptance_invariants import (
    OrthogonalRouteState,
    ProviderRuntimeState,
    TenantBudgetState,
    TupleQualityState,
    WorkloadAdmissionState,
    dataref_lifecycle_summary,
    production_route_gate,
    semantic_to_wire_transform_evidence,
    state_axes_are_orthogonal,
    validate_failure_artifact_boundary,
    validate_openai_interaction_contract,
)
from gpucall.acceptance_replay import load_anonymous_replay_fixture, replay_workload_classes
from gpucall.admission import AdmissionController
from gpucall.app_helpers import build_provider_failure_artifact
from gpucall.audit import AuditTrail
from gpucall.config import ConfigError, load_config
from gpucall.dispatcher import Dispatcher, JobStore
from gpucall.domain import ChatMessage, CompiledPlan, DataRef, ExecutionMode, JobState, ProviderErrorCode, RecipeQualityFloor, TaskRequest, TupleError, TupleResult
from gpucall.execution import EchoTuple, RemoteHandle
from gpucall.openai_facade.chat_completions import OpenAIProtocolError, admit_openai_chat_completion
from gpucall.provider_errors import should_suppress_provider_family
from gpucall.readiness import build_readiness_report
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


def run_production_acceptance(config_dir: Path | None = None) -> dict[str, Any]:
    return asyncio.run(run_production_acceptance_async(config_dir=config_dir))


async def run_production_acceptance_async(config_dir: Path | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="gpucall-acceptance-") as raw_root:
        root = Path(raw_root)
        checks = [
            _check_f1_semantic_wire_contract(),
            await _check_f2_f6_live_executability(root),
            _check_f3_production_route_gate(),
            await _check_f4_provider_failure_suppression(root),
            await _check_f5_fallback_storm_bound(root),
            _check_f7_openai_chat_contract(),
            _check_f8_failure_artifact_boundary(),
            await _check_f9_budget_lifecycle(root),
            await _check_f10_async_lifecycle(root),
            _check_f12_orthogonal_route_state(),
            _check_f13_anonymous_replay_fixture(),
            _check_f14_dataref_lifecycle_contract(),
        ]
        if config_dir is not None:
            checks.append(_check_f11_config_route_resilience(Path(config_dir)))
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


def _check_f1_semantic_wire_contract() -> AcceptanceCheck:
    evidence = semantic_to_wire_transform_evidence(
        task="vision",
        intent="understand_document_image",
        mode="sync",
        input_contract="data_refs:image",
        output_contract="json_schema:articles",
    )
    repeated = semantic_to_wire_transform_evidence(
        task="vision",
        intent="understand_document_image",
        mode="sync",
        input_contract="data_refs:image",
        output_contract="json_schema:articles",
    )
    passed = (
        evidence["evidence_sha256"] == repeated["evidence_sha256"]
        and evidence["payload"]["input_contract"] == "data_refs:image"
        and evidence["payload"]["output_contract"] == "json_schema:articles"
    )
    return AcceptanceCheck(
        id="F1",
        name="semantic contract lowers to worker wire contract with evidence",
        passed=passed,
        details=evidence,
    )


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


def _check_f3_production_route_gate() -> AcceptanceCheck:
    blocked = [
        production_route_gate({"candidate": True, "endpoint_configured": False, "validation_evidence": True, "production_activated": False}),
        production_route_gate({"candidate": False, "endpoint_configured": True, "validation_evidence": False, "production_activated": True}),
        production_route_gate({"candidate": False, "endpoint_configured": True, "validation_evidence": True, "placeholder": True, "production_activated": True}),
        production_route_gate({"candidate": False, "endpoint_configured": True, "validation_evidence": True, "quality_floor": "smoke", "production_activated": True}),
    ]
    allowed = production_route_gate(
        {
            "candidate": False,
            "endpoint_configured": True,
            "validation_evidence": True,
            "placeholder": False,
            "quality_floor": "production",
            "production_activated": True,
        }
    )
    passed = all(not item["allowed"] and item["missing"] for item in blocked) and allowed == {"allowed": True, "missing": []}
    return AcceptanceCheck(
        id="F3",
        name="candidate and unvalidated tuples cannot enter production routing",
        passed=passed,
        details={"blocked": blocked, "allowed": allowed},
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
            and not snapshot["suppressed_provider_families"]
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
    strict_schema = admit_openai_chat_completion(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "return json"}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "answer",
                    "strict": True,
                    "schema": {"type": "object", "properties": {"answer": {"type": "string"}}, "required": ["answer"]},
                },
            },
        },
        inline_bytes_limit=10_000,
    )
    tools = admit_openai_chat_completion(
        {
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "call tool"}],
            "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            "tool_choice": "auto",
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
    parallel_rejected = False
    try:
        admit_openai_chat_completion(
            {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "call tool"}],
                "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
                "parallel_tool_calls": True,
            },
            inline_bytes_limit=10_000,
        )
    except OpenAIProtocolError:
        parallel_rejected = True
    interaction = validate_openai_interaction_contract(accepted.raw_supported_payload)
    passed = (
        accepted.stream is True
        and accepted.task_request.n == 2
        and interaction["include_usage"] is True
        and strict_schema.task_request.response_format is not None
        and strict_schema.task_request.response_format.json_schema is not None
        and tools.task_request.tools is not None
        and tools.task_request.tool_choice == "auto"
        and rejected
        and parallel_rejected
    )
    return AcceptanceCheck(
        id="F7",
        name="OpenAI chat completion contract is accepted only inside declared support",
        passed=passed,
        details={
            "accepted_n": accepted.task_request.n,
            "accepted_stream_options": accepted.task_request.stream_options,
            "accepted_interaction": interaction,
            "strict_json_schema": strict_schema.task_request.response_format.json_schema,
            "tools_preserved": bool(tools.task_request.tools),
            "unsupported_obfuscation_rejected": rejected,
            "parallel_tool_calls_fail_closed": parallel_rejected,
        },
        )


def _check_f8_failure_artifact_boundary() -> AcceptanceCheck:
    request = TaskRequest(
        task="infer",
        mode=ExecutionMode.SYNC,
        intent="summarize_text",
        messages=[ChatMessage(role="user", content="redacted by summary")],
    )
    artifact = build_provider_failure_artifact(
        TupleError(
            "tuple execution failed",
            retryable=True,
            status_code=503,
            code=ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE,
            raw_output="provider said capacity is unavailable",
        ),
        request,
    )
    boundary = validate_failure_artifact_boundary(artifact)
    passed = (
        boundary["valid"]
        and artifact["retryable"] is True
        and artifact["caller_action"] == "retry_later_or_wait_for_gpucall_fallback"
        and artifact["redaction_guarantee"]["prompt_body_included"] is False
        and "tuple_error_body_redacted" not in artifact
    )
    return AcceptanceCheck(
        id="F8",
        name="failure artifacts expose caller action and redaction guarantees",
        passed=passed,
        details={"artifact": artifact, "boundary": boundary},
    )


def _check_f11_config_route_resilience(config_dir: Path) -> AcceptanceCheck:
    try:
        config = load_config(config_dir)
        report = build_readiness_report(config_dir=config_dir, config=config)
    except ConfigError as exc:
        return AcceptanceCheck(
            id="F11",
            name="production config routes have cross-family executable breadth",
            passed=False,
            details={"config_error": str(exc)},
        )

    admission = AdmissionController(config.tuples)
    issues: list[dict[str, Any]] = []
    by_recipe = {item.name: item for item in config.recipes.values()}
    for recipe_report in report.get("recipes", []):
        if not recipe_report.get("auto_select"):
            continue
        if recipe_report.get("task") not in {"infer", "vision"}:
            continue
        recipe = by_recipe.get(str(recipe_report.get("recipe")))
        if recipe is None or recipe.quality_floor is RecipeQualityFloor.SMOKE:
            continue
        eligible_names = [str(item.get("tuple")) for item in recipe_report.get("eligible_tuples", []) if item.get("tuple")]
        families = sorted({admission.family_for(name) for name in eligible_names})
        surfaces = sorted(
            {
                config.tuples[name].execution_surface.value
                for name in eligible_names
                if name in config.tuples and config.tuples[name].execution_surface is not None
            }
        )
        if not eligible_names:
            issues.append(
                {
                    "recipe": recipe.name,
                    "intent": recipe.intent,
                    "reason": "no_eligible_production_tuple",
                }
            )
            continue
        if len(families) < 2:
            issues.append(
                {
                    "recipe": recipe.name,
                    "intent": recipe.intent,
                    "reason": "single_provider_family",
                    "eligible_tuple_count": len(eligible_names),
                    "provider_families": families,
                    "execution_surfaces": surfaces,
                }
            )

    return AcceptanceCheck(
        id="F11",
        name="production config routes have cross-family executable breadth",
        passed=not issues,
        details={
            "config_dir": str(config_dir),
            "issue_count": len(issues),
            "issues": issues[:50],
        },
    )


def _check_f12_orthogonal_route_state() -> AcceptanceCheck:
    provider_exhausted = OrthogonalRouteState(
        provider=ProviderRuntimeState.EXHAUSTED,
        tenant=TenantBudgetState.OK,
        workload=WorkloadAdmissionState.SYNC_SAFE,
        tuple_quality=TupleQualityState.PASSED,
    )
    tenant_exhausted = OrthogonalRouteState(
        provider=ProviderRuntimeState.LIVE_READY,
        tenant=TenantBudgetState.EXHAUSTED,
        workload=WorkloadAdmissionState.SYNC_SAFE,
        tuple_quality=TupleQualityState.PASSED,
    )
    strict_schema_failed = OrthogonalRouteState(
        provider=ProviderRuntimeState.LIVE_READY,
        tenant=TenantBudgetState.RELEASED,
        workload=WorkloadAdmissionState.SYNC_SAFE,
        tuple_quality=TupleQualityState.STRICT_SCHEMA_FAILED,
    )
    states = [provider_exhausted.as_dict(), tenant_exhausted.as_dict(), strict_schema_failed.as_dict()]
    passed = all(state_axes_are_orthogonal(state) for state in states) and len({tuple(sorted(item.items())) for item in states}) == 3
    return AcceptanceCheck(
        id="F12",
        name="provider, tenant, workload, and tuple quality states are orthogonal",
        passed=passed,
        details={"states": states},
    )


def _check_f13_anonymous_replay_fixture() -> AcceptanceCheck:
    fixture_path = Path(__file__).resolve().parent / "fixtures" / "anonymous_synthetic_replay.json"
    fixture = load_anonymous_replay_fixture(fixture_path)
    classes = replay_workload_classes(fixture)
    expected = {
        "document_vision_burst",
        "long_async_rank",
        "sync_text_batch",
        "strict_schema_business_object",
        "capacity_unavailable_burst",
        "cold_start_late_completion",
    }
    state_ok = all(state_axes_are_orthogonal(item.get("expected_state", {})) for item in fixture.get("workloads", []))
    passed = expected <= classes and state_ok
    return AcceptanceCheck(
        id="F13",
        name="anonymous synthetic replay fixture covers product workload classes",
        passed=passed,
        details={
            "classes": sorted(classes),
            "fixture": "gpucall/fixtures/anonymous_synthetic_replay.json",
            "workload_count": len(fixture.get("workloads", [])),
        },
    )


def _check_f14_dataref_lifecycle_contract() -> AcceptanceCheck:
    ref = DataRef(
        uri="s3://bucket/gpucall/tenants/tenant-a/object.bin",
        sha256="a" * 64,
        bytes=4096,
        content_type="image/jpeg",
    ).model_dump(mode="json")
    summary = dataref_lifecycle_summary({**ref, "expiry_seconds": 900})
    passed = (
        summary["has_uri"]
        and summary["has_sha256"]
        and summary["bytes"] == 4096
        and summary["content_type"] == "image/jpeg"
        and summary["expiry_seconds"] == 900
        and summary["body_included"] is False
    )
    return AcceptanceCheck(
        id="F14",
        name="DataRef lifecycle acceptance uses metadata without payload bytes",
        passed=passed,
        details=summary,
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
