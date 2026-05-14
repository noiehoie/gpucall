from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from gpucall.audit import AuditTrail
from gpucall.admission import AdmissionController
from gpucall.artifacts import SQLiteArtifactRegistry
from gpucall.dispatcher import Dispatcher, JobStore, _storage_safe_plan
from gpucall.domain import (
    ArtifactManifest,
    ArtifactExportSpec,
    CompiledPlan,
    DataClassification,
    DataRef,
    ExecutionMode,
    InlineValue,
    ProviderErrorCode,
    TupleError,
    TupleResult,
    ResponseFormat,
    ResponseFormatType,
)
from gpucall.execution import EchoTuple, RemoteHandle
from gpucall.provider_errors import provider_error_class, should_suppress_provider_family
from gpucall.registry import ObservedRegistry


class FailingTuple(EchoTuple):
    def __init__(self, name: str, *, retryable: bool) -> None:
        super().__init__(name=name)
        self.retryable = retryable

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        raise TupleError("failed", retryable=self.retryable, status_code=401 if not self.retryable else 502)


class ProviderCodeFailingTuple(EchoTuple):
    def __init__(self, name: str, code: ProviderErrorCode) -> None:
        super().__init__(name=name)
        self.code = code
        self.cancelled_handles: list[str] = []

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        raise TupleError("provider temporarily unavailable", retryable=False, status_code=503, code=self.code)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        self.cancelled_handles.append(handle.remote_id)


class HangingCleanupProviderTuple(ProviderCodeFailingTuple):
    async def cancel_remote(self, handle: RemoteHandle) -> None:
        await asyncio.sleep(60)


class BuggyTuple(EchoTuple):
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        raise RuntimeError("sdk exploded")


class HangingTuple(EchoTuple):
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        await asyncio.sleep(60)
        return TupleResult(kind="inline", value="late")


class SlowTuple(EchoTuple):
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        await asyncio.sleep(0.05)
        return TupleResult(kind="inline", value=f"ok:{plan.task}:{self.name}")


class SequenceTuple(EchoTuple):
    def __init__(self, name: str, values: list[str]) -> None:
        super().__init__(name=name)
        self.values = values
        self.calls = 0

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return TupleResult(kind="inline", value=value)


class BadStreamTuple(EchoTuple):
    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        yield "not-sse"


class ArtifactTuple(EchoTuple):
    def __init__(self, name: str, manifest: ArtifactManifest) -> None:
        super().__init__(name=name)
        self.manifest = manifest

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        return TupleResult(kind="artifact_manifest", artifact_manifest=self.manifest)


def plan(chain: list[str]) -> CompiledPlan:
    return CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        tuple_chain=chain,
        timeout_seconds=2,
        lease_ttl_seconds=10,
        token_estimation_profile="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
    )


def json_plan(chain: list[str]) -> CompiledPlan:
    return plan(chain).model_copy(
        update={"response_format": ResponseFormat(type=ResponseFormatType.JSON_OBJECT), "output_validation_attempts": 3}
    )


def strict_json_schema_plan(chain: list[str]) -> CompiledPlan:
    return plan(chain).model_copy(
        update={
            "response_format": ResponseFormat(
                type=ResponseFormatType.JSON_SCHEMA,
                json_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                strict=True,
            ),
            "output_validation_attempts": 3,
        }
    )


def checked_plan(chain: list[str]) -> CompiledPlan:
    return plan(chain).model_copy(update={"output_validation_attempts": 2})


def artifact_plan(chain: list[str]) -> CompiledPlan:
    return plan(chain).model_copy(
        update={
            "task": "fine-tune",
            "data_classification": DataClassification.RESTRICTED,
            "artifact_export": ArtifactExportSpec(artifact_chain_id="chain-1", version="0001", key_id="tenant-key"),
            "attestations": {
                "governance_hash": "c" * 64,
                "key_release_requirement": {
                    "required": True,
                    "key_id": "tenant-key",
                    "policy_hash": "p" * 64,
                },
                "key_release_grant": {
                    "key_id": "tenant-key",
                    "policy_hash": "p" * 64,
                    "attestation_evidence_ref": "attestation-1",
                    "recipient": "worker",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
                },
            },
        }
    )


def sensitive_plan(chain: list[str]) -> CompiledPlan:
    return plan(chain).model_copy(
        update={
            "input_refs": [
                DataRef(
                    uri="https://r2.example/input.txt?X-Amz-Signature=secret",
                    sha256="b" * 64,
                    bytes=42,
                    content_type="text/plain",
                    endpoint_url="https://r2.example",
                )
            ],
            "inline_inputs": {"prompt": InlineValue(value="secret prompt", content_type="text/plain")},
        }
    )


@pytest.mark.asyncio
async def test_dispatcher_fails_over_retryable_provider(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"bad": FailingTuple("bad", retryable=True), "good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(plan(["bad", "good"]))

    assert result.value == "ok:infer:good"


@pytest.mark.asyncio
async def test_dispatcher_fails_over_all_provider_temporary_unavailable_codes(tmp_path) -> None:
    for code in ProviderErrorCode:
        bad = ProviderCodeFailingTuple("bad", code)
        dispatcher = Dispatcher(
            adapters={"bad": bad, "good": EchoTuple("good")},
            registry=ObservedRegistry(),
            audit=AuditTrail(tmp_path / f"audit-{code.value}.jsonl"),
            jobs=JobStore(),
        )

        provider_class = provider_error_class(code)
        if provider_class is not None and not provider_class.fallback_eligible:
            with pytest.raises(TupleError) as caught:
                await dispatcher.execute_sync(plan(["bad", "good"]))
            assert caught.value.code == code
            continue

        result = await dispatcher.execute_sync(plan(["bad", "good"]))
        assert result.value == "ok:infer:good"
        assert bad.cancelled_handles


@pytest.mark.asyncio
async def test_dispatcher_admission_prevents_tuple_stampede(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_TUPLE_CONCURRENCY_LIMIT", "1")
    dispatcher = Dispatcher(
        adapters={"busy": SlowTuple("busy"), "fallback": EchoTuple("fallback")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    first, second = await asyncio.gather(
        dispatcher.execute_sync(plan(["busy", "fallback"])),
        dispatcher.execute_sync(plan(["busy", "fallback"])),
    )

    values = {first.value, second.value}
    assert values == {"ok:infer:busy", "ok:infer:fallback"}
    assert '"event_type":"tuple.admission_rejected"' in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_provider_temporary_failure_suppresses_tuple_for_later_plans(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS", "60")
    bad = ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED)
    dispatcher = Dispatcher(
        adapters={"bad": bad, "good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    first = await dispatcher.execute_sync(plan(["bad", "good"]))
    second = await dispatcher.execute_sync(plan(["bad", "good"]))

    assert first.value == "ok:infer:good"
    assert second.value == "ok:infer:good"
    assert len(bad.cancelled_handles) == 1
    assert '"reason":"tuple_suppressed"' in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_resource_exhaustion_suppresses_only_observed_tuple(monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS", "60")
    admission = AdmissionController(cooldown_seconds=60)
    admission.tuple_families = {"modal-a": "modal:function_runtime:modal", "modal-b": "modal:function_runtime:modal"}

    await admission.suppress(
        "modal-a",
        code="PROVIDER_RESOURCE_EXHAUSTED",
        suppress_family=should_suppress_provider_family("PROVIDER_RESOURCE_EXHAUSTED"),
    )

    first = await admission.acquire("modal-a")
    second = await admission.acquire("modal-b")

    assert first.allowed is False
    assert first.reason == "tuple_suppressed"
    assert second.allowed is True
    await admission.release(second.lease)
    assert 0 < first.suppressed_until_seconds <= 60


@pytest.mark.asyncio
async def test_endpoint_capacity_failure_only_suppresses_tuple(monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS", "60")
    admission = AdmissionController(cooldown_seconds=60)
    admission.tuple_families = {"runpod-a": "runpod:serverless:runpod", "runpod-b": "runpod:serverless:runpod"}

    await admission.suppress(
        "runpod-a",
        code="PROVIDER_CAPACITY_UNAVAILABLE",
        suppress_family=should_suppress_provider_family("PROVIDER_CAPACITY_UNAVAILABLE"),
    )

    first = await admission.acquire("runpod-a")
    second = await admission.acquire("runpod-b")

    assert first.allowed is False
    assert first.reason == "tuple_suppressed"
    assert second.allowed is True
    await admission.release(second.lease)


@pytest.mark.asyncio
async def test_upstream_failure_suppresses_provider_family(monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS", "60")
    admission = AdmissionController(cooldown_seconds=60)
    admission.tuple_families = {"modal-a": "modal:function_runtime:modal", "modal-b": "modal:function_runtime:modal"}

    await admission.suppress("modal-a", code="PROVIDER_UPSTREAM_UNAVAILABLE", suppress_family=True)

    first = await admission.acquire("modal-a")
    second = await admission.acquire("modal-b")

    assert first.allowed is False
    assert first.reason == "tuple_suppressed"
    assert second.allowed is False
    assert second.reason == "provider_family_suppressed"


@pytest.mark.asyncio
async def test_dispatcher_admission_limits_same_workload_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_TUPLE_CONCURRENCY_LIMIT", "10")
    monkeypatch.setenv("GPUCALL_PROVIDER_FAMILY_CONCURRENCY_LIMIT", "10")
    monkeypatch.setenv("GPUCALL_WORKLOAD_SCOPE_CONCURRENCY_LIMIT", "1")
    dispatcher = Dispatcher(
        adapters={"a": SlowTuple("a"), "b": SlowTuple("b")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    async def run(item):
        try:
            return await dispatcher.execute_sync(item)
        except TupleError as exc:
            return exc

    results = await asyncio.gather(run(plan(["a"])), run(plan(["b"])))

    assert sum(isinstance(item, TupleResult) for item in results) == 1
    failures = [item for item in results if isinstance(item, TupleError)]
    assert len(failures) == 1
    assert failures[0].code == ProviderErrorCode.PROVIDER_CAPACITY_UNAVAILABLE
    assert '"reason":"workload_scope_inflight_limit"' in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dispatcher_waits_for_workload_scope_slot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_TUPLE_CONCURRENCY_LIMIT", "10")
    monkeypatch.setenv("GPUCALL_PROVIDER_FAMILY_CONCURRENCY_LIMIT", "10")
    monkeypatch.setenv("GPUCALL_WORKLOAD_SCOPE_CONCURRENCY_LIMIT", "1")
    monkeypatch.setenv("GPUCALL_ADMISSION_WAIT_SECONDS", "1")
    dispatcher = Dispatcher(
        adapters={"a": SlowTuple("a"), "b": SlowTuple("b")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    results = await asyncio.gather(dispatcher.execute_sync(plan(["a"])), dispatcher.execute_sync(plan(["b"])))

    assert {item.value for item in results} == {"ok:infer:a", "ok:infer:b"}
    assert '"reason":"workload_scope_inflight_limit"' not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dispatcher_stops_fallback_after_attempt_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_MAX_FALLBACK_ATTEMPTS", "1")
    bad = ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED)
    dispatcher = Dispatcher(
        adapters={"bad": bad, "good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError) as caught:
        await dispatcher.execute_sync(plan(["bad", "good"]))

    assert caught.value.code == ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event_type":"plan.fallback_exhausted"' in raw
    assert '"event_type":"plan.completed"' not in raw


@pytest.mark.asyncio
async def test_fallback_ineligible_provider_code_does_not_blindly_fallback(tmp_path) -> None:
    bad = ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_QUOTA_EXCEEDED)
    dispatcher = Dispatcher(
        adapters={"bad": bad, "good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError) as caught:
        await dispatcher.execute_sync(plan(["bad", "good"]))

    assert caught.value.code == ProviderErrorCode.PROVIDER_QUOTA_EXCEEDED
    raw = (tmp_path / "audit.jsonl").read_text(encoding="utf-8")
    assert '"event_type":"plan.completed"' not in raw


@pytest.mark.asyncio
async def test_timeout_suppresses_tuple_for_later_plans(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_PROVIDER_TEMPORARY_COOLDOWN_SECONDS", "60")
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={"slow": HangingTuple("slow"), "good": EchoTuple("good", latency_seconds=0.0)},
        registry=ObservedRegistry(),
        audit=AuditTrail(audit_path),
        jobs=JobStore(),
    )
    fast_timeout = plan(["slow", "good"]).model_copy(update={"timeout_seconds": 0.01})

    first = await dispatcher.execute_sync(fast_timeout)
    second = await dispatcher.execute_sync(fast_timeout)

    assert first.value == "ok:infer:good"
    assert second.value == "ok:infer:good"
    raw = audit_path.read_text(encoding="utf-8")
    assert '"event_type":"tuple.timeout"' in raw
    assert '"reason":"tuple_suppressed"' in raw


@pytest.mark.asyncio
async def test_async_failed_job_releases_reserved_budget_callback(tmp_path) -> None:
    released: list[str] = []
    dispatcher = Dispatcher(
        adapters={"bad": ProviderCodeFailingTuple("bad", ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED)},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
        on_async_terminal_failure=lambda p: released.append(p.plan_id),
    )

    job = await dispatcher.submit_async(plan(["bad"]))
    await dispatcher._job_tasks[job.job_id]

    stored = await dispatcher.jobs.get(job.job_id)
    assert stored.state == "FAILED"
    assert released == [stored.plan.plan_id]


@pytest.mark.asyncio
async def test_dispatcher_cleanup_timeout_does_not_block_failover(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_REMOTE_CLEANUP_TIMEOUT_SECONDS", "0.001")
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={
            "bad": HangingCleanupProviderTuple("bad", ProviderErrorCode.PROVIDER_RESOURCE_EXHAUSTED),
            "good": EchoTuple("good"),
        },
        registry=ObservedRegistry(),
        audit=AuditTrail(audit_path),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(plan(["bad", "good"]))

    assert result.value == "ok:infer:good"
    raw = audit_path.read_text(encoding="utf-8")
    assert '"event_type":"lease.cleanup_failed"' in raw
    assert '"event_type":"plan.completed"' in raw


@pytest.mark.asyncio
async def test_dispatcher_audit_uses_redacted_plan_summary(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={"good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(audit_path),
        jobs=JobStore(),
    )

    await dispatcher.execute_sync(sensitive_plan(["good"]))
    raw = audit_path.read_text(encoding="utf-8")
    accepted = next(json.loads(line) for line in raw.splitlines() if '"event_type":"plan.accepted"' in line)
    plan_payload = accepted["payload"]

    assert plan_payload["inline_inputs"]["prompt"]["redacted"] is True
    assert plan_payload["inline_inputs"]["prompt"]["bytes"] == len("secret prompt")
    assert plan_payload["input_refs"][0]["redacted"] is True
    assert plan_payload["input_refs"][0]["bytes"] == 42
    assert "secret prompt" not in raw
    assert "X-Amz" not in raw
    assert "Signature" not in raw
    assert "r2.example" not in raw


@pytest.mark.asyncio
async def test_async_job_created_audit_uses_redacted_plan_summary(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={"good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(audit_path),
        jobs=JobStore(),
    )

    job = await dispatcher.submit_async(sensitive_plan(["good"]))
    await dispatcher._job_tasks[job.job_id]
    raw = audit_path.read_text(encoding="utf-8")
    created = next(json.loads(line) for line in raw.splitlines() if '"event_type":"job.created"' in line)

    assert created["payload"]["plan"]["inline_inputs"]["prompt"]["redacted"] is True
    assert created["payload"]["plan"]["input_refs"][0]["redacted"] is True
    assert "secret prompt" not in raw
    assert "X-Amz" not in raw
    assert "Signature" not in raw
    assert "r2.example" not in raw
    assert AuditTrail(audit_path).verify()


def test_storage_safe_plan_removes_secret_execution_fields() -> None:
    original = sensitive_plan(["good"]).model_copy(update={"system_prompt": "private system prompt"})

    safe = _storage_safe_plan(original)

    assert safe.input_refs == []
    assert safe.inline_inputs == {}
    assert safe.messages == []
    assert safe.system_prompt is None
    assert safe.attestations["storage_safe_plan"] is True


@pytest.mark.asyncio
async def test_dispatcher_does_not_fail_over_non_retryable_provider(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"bad": FailingTuple("bad", retryable=False), "good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError) as caught:
        await dispatcher.execute_sync(plan(["bad", "good"]))

    assert caught.value.retryable is False
    assert dispatcher.registry.score("good").samples == 0


@pytest.mark.asyncio
async def test_dispatcher_cleanup_runs_after_wait_failure(tmp_path) -> None:
    bad = FailingTuple("bad", retryable=True)
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={"bad": bad},
        registry=ObservedRegistry(),
        audit=AuditTrail(audit_path),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError):
        await dispatcher.execute_sync(plan(["bad"]))

    assert bad.cancelled
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    cleanup = [event for event in events if event["event_type"] == "lease.cleaned_up"][0]["payload"]
    assert cleanup["execution_surface"] == "local_runtime"
    assert cleanup["cleanup_required"] is False


@pytest.mark.asyncio
async def test_dispatcher_records_untyped_provider_exception(tmp_path) -> None:
    bad = BuggyTuple("bad")
    dispatcher = Dispatcher(
        adapters={"bad": bad},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError) as caught:
        await dispatcher.execute_sync(plan(["bad"]))

    assert "sdk exploded" not in str(caught.value)
    assert "unexpected exception" in str(caught.value)
    assert dispatcher.registry.score("bad").samples == 1
    assert bad.cancelled
    assert "sdk exploded" not in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dispatcher_registers_valid_artifact_manifest(tmp_path) -> None:
    manifest = ArtifactManifest(
        artifact_id="artifact-1",
        artifact_chain_id="chain-1",
        version="0001",
        classification=DataClassification.RESTRICTED,
        ciphertext_uri="s3://bucket/artifact-1",
        ciphertext_sha256="a" * 64,
        key_id="tenant-key",
        producer_plan_hash="c" * 64,
    )
    registry = SQLiteArtifactRegistry(tmp_path / "artifacts.db")
    dispatcher = Dispatcher(
        adapters={"artifact": ArtifactTuple("artifact", manifest)},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
        artifact_registry=registry,
    )

    result = await dispatcher.execute_sync(artifact_plan(["artifact"]))

    assert result.output_validated is True
    assert registry.get("artifact-1") == manifest
    assert "artifact.registered" in (tmp_path / "audit.jsonl").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_dispatcher_rejects_artifact_manifest_that_breaks_lineage(tmp_path) -> None:
    manifest = ArtifactManifest(
        artifact_id="artifact-1",
        artifact_chain_id="chain-1",
        version="0002",
        classification=DataClassification.RESTRICTED,
        ciphertext_uri="s3://bucket/artifact-1",
        ciphertext_sha256="a" * 64,
        key_id="tenant-key",
        producer_plan_hash="c" * 64,
    )
    dispatcher = Dispatcher(
        adapters={"artifact": ArtifactTuple("artifact", manifest)},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError, match="artifact manifest"):
        await dispatcher.execute_sync(artifact_plan(["artifact"]))


@pytest.mark.asyncio
async def test_async_cancel_persists_cancelled_state(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"slow": HangingTuple("slow")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    job = await dispatcher.submit_async(plan(["slow"]))
    for _ in range(20):
        stored = await dispatcher.jobs.get(job.job_id)
        if stored.state == "RUNNING":
            break
        await asyncio.sleep(0)

    dispatcher.cancel_job(job.job_id)
    task = dispatcher._job_tasks[job.job_id]
    with pytest.raises(asyncio.CancelledError):
        await task

    stored = await dispatcher.jobs.get(job.job_id)
    assert stored.state == "CANCELLED"
    assert stored.error == "job cancelled"


@pytest.mark.asyncio
async def test_async_storage_safe_plan_is_not_executed_after_restart(tmp_path) -> None:
    jobs = JobStore()
    job = await jobs.create(_storage_safe_plan(plan(["echo"])))

    restarted = Dispatcher(
        adapters={"echo": EchoTuple("echo")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit2.jsonl"),
        jobs=jobs,
    )
    await restarted._run_job(job.job_id)

    stored = await jobs.get(job.job_id)
    assert stored.state == "EXPIRED"
    assert stored.error == "gateway restarted before job dispatch"


@pytest.mark.asyncio
async def test_stream_events_must_match_sse_contract(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"bad": BadStreamTuple("bad")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )
    stream_plan = plan(["bad"]).model_copy(update={"mode": ExecutionMode.STREAM})

    with pytest.raises(TupleError, match="SSE-framed"):
        async for _event in dispatcher.execute_stream(stream_plan):
            pass


@pytest.mark.asyncio
async def test_stream_execution_enforces_security_gate_before_start(tmp_path) -> None:
    tuple = BadStreamTuple("bad")
    dispatcher = Dispatcher(
        adapters={"bad": tuple},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )
    stream_plan = plan(["bad"]).model_copy(
        update={
            "mode": ExecutionMode.STREAM,
            "attestations": {
                "key_release_requirement": {
                    "required": True,
                    "key_id": "tenant-key",
                    "policy_hash": "c" * 64,
                }
            },
        }
    )

    with pytest.raises(TupleError, match="key release grant is required"):
        async for _event in dispatcher.execute_stream(stream_plan):
            pass


@pytest.mark.asyncio
async def test_key_release_gate_rejects_policy_mismatch_and_expiry(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"good": EchoTuple("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )
    base_attestations = {
        "key_release_requirement": {
            "required": True,
            "key_id": "tenant-key",
            "policy_hash": "c" * 64,
        },
        "key_release_grant": {
            "key_id": "tenant-key",
            "policy_hash": "d" * 64,
            "attestation_evidence_ref": "attestation-1",
            "recipient": "worker",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        },
    }

    with pytest.raises(TupleError, match="policy_hash"):
        await dispatcher.execute_sync(plan(["good"]).model_copy(update={"attestations": base_attestations}))

    expired = {
        **base_attestations,
        "key_release_grant": {
            **base_attestations["key_release_grant"],
            "policy_hash": "c" * 64,
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        },
    }
    with pytest.raises(TupleError, match="expired"):
        await dispatcher.execute_sync(plan(["good"]).model_copy(update={"attestations": expired}))


@pytest.mark.asyncio
async def test_structured_output_marks_valid_json(tmp_path) -> None:
    tuple = SequenceTuple("json", ['{"ok": true}'])
    dispatcher = Dispatcher(
        adapters={"json": tuple},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(json_plan(["json"]))

    assert result.output_validated is True
    assert dispatcher.registry.score("json").samples == 1


@pytest.mark.asyncio
async def test_structured_output_retries_same_provider_without_breaker_penalty(tmp_path) -> None:
    tuple = SequenceTuple("json", ["not json", '{"ok": true}'])
    dispatcher = Dispatcher(
        adapters={"json": tuple, "other": SequenceTuple("other", ['{"wrong": true}'])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(json_plan(["json", "other"]))

    assert result.value == '{"ok": true}'
    assert tuple.calls == 2
    assert dispatcher.registry.score("json").samples == 1
    assert dispatcher.registry.score("other").samples == 0


@pytest.mark.asyncio
async def test_structured_output_failure_does_not_open_circuit_or_fail_over(tmp_path) -> None:
    tuple = SequenceTuple("json", ["not json", "still not json", "bad again"])
    dispatcher = Dispatcher(
        adapters={"json": tuple, "other": SequenceTuple("other", ['{"wrong": true}'])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(json_plan(["json", "other"]))

    assert result.value == '{"wrong": true}'
    assert tuple.calls == 3
    assert dispatcher.registry.score("json").samples == 0
    assert dispatcher.registry.score("other").samples == 1


@pytest.mark.asyncio
async def test_strict_json_schema_failure_switches_tuple_without_same_tuple_retry(tmp_path) -> None:
    tuple = SequenceTuple("weak", ['{"wrong": true}', '{"ok": true}'])
    other = SequenceTuple("strong", ['{"ok": true}'])
    dispatcher = Dispatcher(
        adapters={"weak": tuple, "strong": other},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(strict_json_schema_plan(["weak", "strong"]))

    assert result.value == '{"ok": true}'
    assert tuple.calls == 1
    assert other.calls == 1
    assert dispatcher.registry.quality_failure_count(
        "weak",
        recipe="r1",
        task="infer",
        mode="sync",
        code="MALFORMED_OUTPUT",
    ) == 1
    assert dispatcher.registry.score("weak").samples == 0
    assert dispatcher.registry.score("strong").samples == 1


@pytest.mark.asyncio
async def test_structured_output_all_providers_failure_returns_422_without_opening_circuit(tmp_path) -> None:
    tuple = SequenceTuple("json", ["not json", "still not json", "bad again"])
    other = SequenceTuple("other", ["also bad", "bad too", "bad finally"])
    dispatcher = Dispatcher(
        adapters={"json": tuple, "other": other},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(TupleError) as caught:
        await dispatcher.execute_sync(json_plan(["json", "other"]))

    assert caught.value.code == "MALFORMED_OUTPUT"
    assert caught.value.retryable is False
    assert tuple.calls == 3
    assert other.calls == 3
    assert dispatcher.registry.score("json").samples == 0
    assert dispatcher.registry.score("other").samples == 0


@pytest.mark.asyncio
async def test_empty_output_retries_same_provider_once_without_breaker_penalty(tmp_path) -> None:
    tuple = SequenceTuple("p1", ["", "ok"])
    dispatcher = Dispatcher(
        adapters={"p1": tuple, "other": SequenceTuple("other", ["wrong"])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(checked_plan(["p1", "other"]))

    assert result.value == "ok"
    assert tuple.calls == 2
    assert dispatcher.registry.score("p1").samples == 1
    assert dispatcher.registry.score("other").samples == 0


@pytest.mark.asyncio
async def test_empty_output_failure_is_422_and_does_not_open_circuit(tmp_path) -> None:
    tuple = SequenceTuple("p1", ["", ""])
    dispatcher = Dispatcher(
        adapters={"p1": tuple, "other": SequenceTuple("other", ["wrong"])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(checked_plan(["p1", "other"]))

    assert result.value == "wrong"
    assert tuple.calls == 2
    assert dispatcher.registry.score("p1").samples == 0
    assert dispatcher.registry.score("other").samples == 1
