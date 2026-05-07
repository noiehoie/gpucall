from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from gpucall.audit import AuditTrail
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
    ProviderError,
    ProviderResult,
    ResponseFormat,
    ResponseFormatType,
)
from gpucall.execution import EchoProvider, RemoteHandle
from gpucall.registry import ObservedRegistry


class FailingProvider(EchoProvider):
    def __init__(self, name: str, *, retryable: bool) -> None:
        super().__init__(name=name)
        self.retryable = retryable

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        raise ProviderError("failed", retryable=self.retryable, status_code=401 if not self.retryable else 502)


class BuggyProvider(EchoProvider):
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        raise RuntimeError("sdk exploded")


class HangingProvider(EchoProvider):
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        await asyncio.sleep(60)
        return ProviderResult(kind="inline", value="late")


class SequenceProvider(EchoProvider):
    def __init__(self, name: str, values: list[str]) -> None:
        super().__init__(name=name)
        self.values = values
        self.calls = 0

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        value = self.values[min(self.calls, len(self.values) - 1)]
        self.calls += 1
        return ProviderResult(kind="inline", value=value)


class BadStreamProvider(EchoProvider):
    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        yield "not-sse"


class ArtifactProvider(EchoProvider):
    def __init__(self, name: str, manifest: ArtifactManifest) -> None:
        super().__init__(name=name)
        self.manifest = manifest

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        return ProviderResult(kind="artifact_manifest", artifact_manifest=self.manifest)


def plan(chain: list[str]) -> CompiledPlan:
    return CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        provider_chain=chain,
        timeout_seconds=2,
        lease_ttl_seconds=10,
        tokenizer_family="qwen",
        token_budget=None,
        input_refs=[],
        inline_inputs={},
    )


def json_plan(chain: list[str]) -> CompiledPlan:
    return plan(chain).model_copy(
        update={"response_format": ResponseFormat(type=ResponseFormatType.JSON_OBJECT), "output_validation_attempts": 3}
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
        adapters={"bad": FailingProvider("bad", retryable=True), "good": EchoProvider("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(plan(["bad", "good"]))

    assert result.value == "ok:infer:good"


@pytest.mark.asyncio
async def test_dispatcher_audit_uses_redacted_plan_summary(tmp_path) -> None:
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={"good": EchoProvider("good")},
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
        adapters={"good": EchoProvider("good")},
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
        adapters={"bad": FailingProvider("bad", retryable=False), "good": EchoProvider("good")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(ProviderError) as caught:
        await dispatcher.execute_sync(plan(["bad", "good"]))

    assert caught.value.retryable is False
    assert dispatcher.registry.score("good").samples == 0


@pytest.mark.asyncio
async def test_dispatcher_cleanup_runs_after_wait_failure(tmp_path) -> None:
    bad = FailingProvider("bad", retryable=True)
    audit_path = tmp_path / "audit.jsonl"
    dispatcher = Dispatcher(
        adapters={"bad": bad},
        registry=ObservedRegistry(),
        audit=AuditTrail(audit_path),
        jobs=JobStore(),
    )

    with pytest.raises(ProviderError):
        await dispatcher.execute_sync(plan(["bad"]))

    assert bad.cancelled
    events = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
    cleanup = [event for event in events if event["event_type"] == "lease.cleaned_up"][0]["payload"]
    assert cleanup["execution_surface"] == "local_runtime"
    assert cleanup["cleanup_required"] is False


@pytest.mark.asyncio
async def test_dispatcher_records_untyped_provider_exception(tmp_path) -> None:
    bad = BuggyProvider("bad")
    dispatcher = Dispatcher(
        adapters={"bad": bad},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(ProviderError) as caught:
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
        adapters={"artifact": ArtifactProvider("artifact", manifest)},
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
        adapters={"artifact": ArtifactProvider("artifact", manifest)},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(ProviderError, match="artifact manifest"):
        await dispatcher.execute_sync(artifact_plan(["artifact"]))


@pytest.mark.asyncio
async def test_async_cancel_persists_cancelled_state(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"slow": HangingProvider("slow")},
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
        adapters={"echo": EchoProvider("echo")},
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
        adapters={"bad": BadStreamProvider("bad")},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )
    stream_plan = plan(["bad"]).model_copy(update={"mode": ExecutionMode.STREAM})

    with pytest.raises(ProviderError, match="SSE-framed"):
        async for _event in dispatcher.execute_stream(stream_plan):
            pass


@pytest.mark.asyncio
async def test_stream_execution_enforces_security_gate_before_start(tmp_path) -> None:
    provider = BadStreamProvider("bad")
    dispatcher = Dispatcher(
        adapters={"bad": provider},
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

    with pytest.raises(ProviderError, match="key release grant is required"):
        async for _event in dispatcher.execute_stream(stream_plan):
            pass


@pytest.mark.asyncio
async def test_key_release_gate_rejects_policy_mismatch_and_expiry(tmp_path) -> None:
    dispatcher = Dispatcher(
        adapters={"good": EchoProvider("good")},
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

    with pytest.raises(ProviderError, match="policy_hash"):
        await dispatcher.execute_sync(plan(["good"]).model_copy(update={"attestations": base_attestations}))

    expired = {
        **base_attestations,
        "key_release_grant": {
            **base_attestations["key_release_grant"],
            "policy_hash": "c" * 64,
            "expires_at": (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
        },
    }
    with pytest.raises(ProviderError, match="expired"):
        await dispatcher.execute_sync(plan(["good"]).model_copy(update={"attestations": expired}))


@pytest.mark.asyncio
async def test_structured_output_marks_valid_json(tmp_path) -> None:
    provider = SequenceProvider("json", ['{"ok": true}'])
    dispatcher = Dispatcher(
        adapters={"json": provider},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(json_plan(["json"]))

    assert result.output_validated is True
    assert dispatcher.registry.score("json").samples == 1


@pytest.mark.asyncio
async def test_structured_output_retries_same_provider_without_breaker_penalty(tmp_path) -> None:
    provider = SequenceProvider("json", ["not json", '{"ok": true}'])
    dispatcher = Dispatcher(
        adapters={"json": provider, "other": SequenceProvider("other", ['{"wrong": true}'])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(json_plan(["json", "other"]))

    assert result.value == '{"ok": true}'
    assert provider.calls == 2
    assert dispatcher.registry.score("json").samples == 1
    assert dispatcher.registry.score("other").samples == 0


@pytest.mark.asyncio
async def test_structured_output_failure_does_not_open_circuit_or_fail_over(tmp_path) -> None:
    provider = SequenceProvider("json", ["not json", "still not json", "bad again"])
    dispatcher = Dispatcher(
        adapters={"json": provider, "other": SequenceProvider("other", ['{"wrong": true}'])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(json_plan(["json", "other"]))

    assert result.value == '{"wrong": true}'
    assert provider.calls == 3
    assert dispatcher.registry.score("json").samples == 0
    assert dispatcher.registry.score("other").samples == 1


@pytest.mark.asyncio
async def test_structured_output_all_providers_failure_returns_422_without_opening_circuit(tmp_path) -> None:
    provider = SequenceProvider("json", ["not json", "still not json", "bad again"])
    other = SequenceProvider("other", ["also bad", "bad too", "bad finally"])
    dispatcher = Dispatcher(
        adapters={"json": provider, "other": other},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    with pytest.raises(ProviderError) as caught:
        await dispatcher.execute_sync(json_plan(["json", "other"]))

    assert caught.value.code == "MALFORMED_OUTPUT"
    assert caught.value.retryable is False
    assert provider.calls == 3
    assert other.calls == 3
    assert dispatcher.registry.score("json").samples == 0
    assert dispatcher.registry.score("other").samples == 0


@pytest.mark.asyncio
async def test_empty_output_retries_same_provider_once_without_breaker_penalty(tmp_path) -> None:
    provider = SequenceProvider("p1", ["", "ok"])
    dispatcher = Dispatcher(
        adapters={"p1": provider, "other": SequenceProvider("other", ["wrong"])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(checked_plan(["p1", "other"]))

    assert result.value == "ok"
    assert provider.calls == 2
    assert dispatcher.registry.score("p1").samples == 1
    assert dispatcher.registry.score("other").samples == 0


@pytest.mark.asyncio
async def test_empty_output_failure_is_422_and_does_not_open_circuit(tmp_path) -> None:
    provider = SequenceProvider("p1", ["", ""])
    dispatcher = Dispatcher(
        adapters={"p1": provider, "other": SequenceProvider("other", ["wrong"])},
        registry=ObservedRegistry(),
        audit=AuditTrail(tmp_path / "audit.jsonl"),
        jobs=JobStore(),
    )

    result = await dispatcher.execute_sync(checked_plan(["p1", "other"]))

    assert result.value == "wrong"
    assert provider.calls == 2
    assert dispatcher.registry.score("p1").samples == 0
    assert dispatcher.registry.score("other").samples == 1
