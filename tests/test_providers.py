from __future__ import annotations

import contextlib
import json
from pathlib import Path
import sys
import types

import pytest
import yaml

from gpucall.domain import ChatMessage, CompiledPlan, ExecutionMode, InlineValue, ExecutionTupleSpec
from gpucall.domain import ArtifactExportSpec, DataClassification
from gpucall.execution_surfaces.iaas_vm import DEFAULT_HYPERSTACK_IMAGE, HyperstackAdapter
from gpucall.execution import (
    AzureComputeVMAdapter,
    EchoTuple,
    GCPConfidentialSpaceVMAdapter,
    LocalOllamaAdapter,
    ModalAdapter,
    OVHCloudPublicCloudInstanceAdapter,
    ScalewayInstanceAdapter,
    build_adapters,
)
from gpucall.execution.base import RemoteHandle
from gpucall.execution.payloads import gpucall_tuple_result, plan_payload
from gpucall.execution_surfaces.function_runtime import RunpodVllmFlashBootAdapter
from gpucall.execution_surfaces.managed_endpoint import RunpodServerlessAdapter
from gpucall.execution_surfaces.managed_endpoint import RunpodVllmServerlessAdapter, runpod_vllm_health_rejection_reason


def test_router_core_does_not_hardcode_builtin_tuple_implementations() -> None:
    root = Path(__file__).resolve().parents[1]
    core_files = [
        root / "gpucall" / "config.py",
        root / "gpucall" / "routing.py",
        root / "gpucall" / "tuple_catalog.py",
        root / "gpucall" / "execution" / "factory.py",
        root / "gpucall" / "compiler.py",
        root / "gpucall" / "dispatcher.py",
    ]
    provider_tokens = [
        "azure-compute-vm",
        "gcp-confidential-space-vm",
        "hyperstack",
        "local-ollama",
        "modal",
        "ovhcloud",
        "runpod",
        "scaleway",
    ]

    offenders: list[str] = []
    for path in core_files:
        text = path.read_text(encoding="utf-8")
        for token in provider_tokens:
            if token in text:
                offenders.append(f"{path.relative_to(root)}:{token}")

    assert offenders == []


def test_provider_contract_modules_are_separated_and_sourced() -> None:
    from gpucall.execution.registry import adapter_descriptor

    root = Path(__file__).resolve().parents[1]
    registry = (root / "gpucall" / "execution" / "registry.py").read_text(encoding="utf-8")

    for removed in (
        "runpod_adapter.py",
        "cloud_vm_adapters.py",
        "hyperstack_adapter.py",
        "modal_adapter.py",
        "runpod_vllm_adapter.py",
    ):
        assert not (root / "gpucall" / "tuples" / removed).exists()
    for module in ("iaas_vm", "managed_endpoint", "function_runtime"):
        assert f"gpucall.execution_surfaces.{module}" in registry

    expected = {
        "local-ollama": "https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-completion",
        "modal": "https://modal.com/docs/reference/modal.Function#from_name",
        "runpod-serverless": "https://docs.runpod.io/serverless/endpoints/send-requests",
        "runpod-vllm-serverless": "https://docs.runpod.io/serverless/vllm/openai-compatibility",
        "hyperstack": "https://portal.hyperstack.cloud/knowledge/api-documentation",
        "azure-compute-vm": "https://learn.microsoft.com/en-us/python/api/azure-mgmt-compute/azure.mgmt.compute.operations.virtualmachinesoperations",
        "gcp-confidential-space-vm": "https://cloud.google.com/python/docs/reference/compute/latest/google.cloud.compute_v1.services.instances.InstancesClient",
        "scaleway-instance": "https://www.scaleway.com/en/developer-api/",
        "ovhcloud-public-cloud-instance": "https://github.com/ovh/python-ovh",
    }
    for adapter, source in expected.items():
        descriptor = adapter_descriptor(adapter)
        assert descriptor is not None
        assert source in descriptor.official_sources

    flashboot = adapter_descriptor("runpod-vllm-flashboot")
    assert flashboot is not None
    assert flashboot.endpoint_contract == "runpod-flash-sdk"
    assert flashboot.output_contract == "gpucall-tuple-result"
    assert flashboot.production_eligible is True
    assert flashboot.required_auto_fields["target"] == "RunPod endpoint target is not configured"

    for adapter in ("azure-compute-vm", "gcp-confidential-space-vm", "scaleway-instance", "ovhcloud-public-cloud-instance"):
        descriptor = adapter_descriptor(adapter)
        assert descriptor is not None
        assert descriptor.production_eligible is False
        assert descriptor.production_rejection_reason


def test_launch_validation_is_tuple_contract_based() -> None:
    root = Path(__file__).resolve().parents[1]
    cli = (root / "gpucall" / "cli.py").read_text(encoding="utf-8")

    assert "required_live_adapters" not in cli
    assert "missing_adapters" not in cli
    assert "artifacts_by_adapter" not in cli
    assert 'adapter == "modal"' not in cli
    assert 'adapter == "runpod-vllm-serverless"' not in cli
    assert 'adapter == "hyperstack"' not in cli
    assert "required_live_tuples" in cli
    assert "tuple_evidence_key" in cli


def test_provider_descriptor_conformance_invariants() -> None:
    from gpucall.execution.registry import registered_adapter_descriptors

    descriptors = registered_adapter_descriptors()

    for name, descriptor in descriptors.items():
        if descriptor.production_eligible and not descriptor.local_execution:
            assert descriptor.official_sources, f"{name} is production-eligible without official sources"

    assert descriptors["echo"].production_eligible is False
    assert descriptors["runpod-serverless"].production_eligible is False
    assert "custom" in str(descriptors["runpod-serverless"].production_rejection_reason)
    for name in ("azure-compute-vm", "gcp-confidential-space-vm", "scaleway-instance", "ovhcloud-public-cloud-instance"):
        assert descriptors[name].production_eligible is False
        assert "lifecycle-only" in str(descriptors[name].production_rejection_reason)


def test_factory_builds_configured_adapter_types() -> None:
    tuples = {
        "echo": ExecutionTupleSpec(name="echo", adapter="echo", gpu="L4", vram_gb=24, max_model_len=8192, cost_per_second=0),
        "local": ExecutionTupleSpec(
            name="local",
            adapter="local-ollama",
            gpu="local",
            vram_gb=1,
            max_model_len=8192,
            cost_per_second=0,
            modes=[ExecutionMode.SYNC, ExecutionMode.ASYNC],
                endpoint="http://127.0.0.1:11434",
                endpoint_contract="ollama-generate",
                output_contract="ollama-generate",
                model="qwen2.5",
        ),
        "modal": ExecutionTupleSpec(
            name="modal",
            adapter="modal",
            gpu="A10G",
            vram_gb=24,
            max_model_len=32768,
            cost_per_second=0,
            target="app:run",
            stream_target="app:stream",
            endpoint_contract="modal-function",
            output_contract="plain-text",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        ),
        "hyperstack": ExecutionTupleSpec(
            name="hyperstack",
            adapter="hyperstack",
            gpu="A100",
            vram_gb=80,
            max_model_len=32768,
            cost_per_second=0,
            target="default-CANADA-1",
            endpoint_contract="hyperstack-vm",
            output_contract="plain-text",
            model="Qwen/Qwen2.5-1.5B-Instruct",
            instance="n3-A100x1",
            image=DEFAULT_HYPERSTACK_IMAGE,
            key_name="gpucall-key",
            ssh_remote_cidr="203.0.113.0/24",
        ),
        "runpod-vllm-serverless": ExecutionTupleSpec(
            name="runpod-vllm-serverless",
            adapter="runpod-vllm-serverless",
            gpu="AMPERE_16",
            vram_gb=16,
            max_model_len=8192,
            cost_per_second=0,
            target="endpoint-1",
            image="runpod/worker-v1-vllm:v2.18.1",
            endpoint_contract="runpod-flash-sdk",
            output_contract="openai-chat-completions",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        ),
        "runpod-vllm-flashboot": ExecutionTupleSpec(
            name="runpod-vllm-flashboot",
            adapter="runpod-vllm-flashboot",
            gpu="AMPERE_16",
            vram_gb=16,
            max_model_len=8192,
            cost_per_second=0,
            target="endpoint-2",
            image="runpod/worker-v1-vllm:v2.18.1",
            endpoint_contract="openai-chat-completions",
            output_contract="gpucall-tuple-result",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        ),
        "azure": ExecutionTupleSpec(
            name="azure",
            adapter="azure-compute-vm",
            gpu="H100",
            vram_gb=80,
            max_model_len=32768,
            cost_per_second=0,
            resource_group="rg",
            region="eastus",
            network="/subscriptions/s/resourceGroups/rg/tuples/Microsoft.Network/networkInterfaces/nic",
            endpoint_contract="azure-compute-vm",
            output_contract="gpucall-tuple-result",
            instance="Standard_NC40ads_H100_v5",
            provider_params={
                "image_reference": {"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"},
                "admin_username": "azureuser",
                "ssh_public_key": "ssh-ed25519 AAAA test",
            },
        ),
        "gcp": ExecutionTupleSpec(
            name="gcp",
            adapter="gcp-confidential-space-vm",
            gpu="H100",
            vram_gb=80,
            max_model_len=32768,
            cost_per_second=0,
            project_id="project",
            zone="us-central1-a",
            network="global/networks/default",
            endpoint_contract="gcp-confidential-space-vm",
            output_contract="gpucall-tuple-result",
            instance="zones/us-central1-a/machineTypes/a3-highgpu-1g",
            image="projects/confidential-space-images/global/images/family/confidential-space",
        ),
        "scaleway": ExecutionTupleSpec(
            name="scaleway",
            adapter="scaleway-instance",
            gpu="L40S",
            vram_gb=48,
            max_model_len=32768,
            cost_per_second=0,
            endpoint_contract="scaleway-instance",
            output_contract="gpucall-tuple-result",
            project_id="project",
            zone="fr-par-1",
            instance="GPU-L40S",
            image="ubuntu_noble",
        ),
        "ovhcloud": ExecutionTupleSpec(
            name="ovhcloud",
            adapter="ovhcloud-public-cloud-instance",
            gpu="L40S",
            vram_gb=48,
            max_model_len=32768,
            cost_per_second=0,
            endpoint_contract="ovhcloud-public-cloud-instance",
            output_contract="gpucall-tuple-result",
            project_id="service",
            region="GRA11",
            instance="flavor-id",
            image="image-id",
            key_name="ssh-key-id",
        ),
    }

    adapters = build_adapters(tuples)

    assert isinstance(adapters["echo"], EchoTuple)
    assert isinstance(adapters["local"], LocalOllamaAdapter)
    assert isinstance(adapters["modal"], ModalAdapter)
    assert isinstance(adapters["hyperstack"], HyperstackAdapter)
    assert isinstance(adapters["runpod-vllm-serverless"], RunpodVllmServerlessAdapter)
    assert isinstance(adapters["runpod-vllm-flashboot"], RunpodVllmFlashBootAdapter)
    assert isinstance(adapters["azure"], AzureComputeVMAdapter)
    assert isinstance(adapters["gcp"], GCPConfidentialSpaceVMAdapter)
    assert isinstance(adapters["scaleway"], ScalewayInstanceAdapter)
    assert isinstance(adapters["ovhcloud"], OVHCloudPublicCloudInstanceAdapter)
    assert adapters["hyperstack"].environment_name == "default-CANADA-1"
    assert adapters["hyperstack"].model == "Qwen/Qwen2.5-1.5B-Instruct"
    assert adapters["modal"].stream_function_name == "stream"


def test_provider_payload_contains_refs_not_dereferenced_data() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        tuple_chain=["p1"],
        timeout_seconds=2,
        lease_ttl_seconds=10,
        token_estimation_profile="qwen",
        token_budget=100,
        max_tokens=64,
        temperature=0.0,
        input_refs=[{"uri": "https://example.com/object?signature=abc", "sha256": "a" * 64, "bytes": 123}],
        inline_inputs={"prompt": InlineValue(value="small prompt")},
    )

    payload = plan_payload(plan)

    assert payload["input_refs"][0]["uri"].startswith("https://example.com/object")
    assert payload["inline_inputs"]["prompt"]["value"] == "small prompt"
    assert payload["max_tokens"] == 64
    assert payload["temperature"] == 0.0
    assert "bytes_payload" not in payload


def test_modal_stream_uses_explicit_deployed_remote_gen(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeFunction:
        @staticmethod
        def from_name(app_name: str, function_name: str):
            calls["app_name"] = app_name
            calls["function_name"] = function_name
            return FakeFunction()

        def remote_gen(self, *args, **kwargs):
            calls["args"] = args
            calls["kwargs"] = kwargs
            yield "ok"

    fake_modal = types.SimpleNamespace(
        Function=FakeFunction,
        enable_output=lambda **_: contextlib.nullcontext(),
    )
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    adapter = ModalAdapter(
        name="modal",
        app_name="gpucall-worker",
        function_name="run_inference_on_modal",
        stream_function_name="stream_inference_on_modal",
        max_model_len=100,
    )
    chunks = list(adapter._stream_sync(plan_payload_plan(), timeout=3, remote_id="stream-1"))

    assert chunks == ["ok"]
    assert calls["app_name"] == "gpucall-worker"
    assert calls["function_name"] == "stream_inference_on_modal"
    assert calls["args"][1] == "infer"
    assert calls["kwargs"]["max_model_len"] == 100


def test_modal_scaledown_metadata_matches_worker_defaults() -> None:
    root = Path(__file__).resolve().parents[1]
    worker = (root / "gpucall" / "worker_contracts" / "modal.py").read_text(encoding="utf-8")
    surfaces = {
        path.name: yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in [
            root / "config" / "surfaces" / "modal-a10g.yml",
            root / "config" / "surfaces" / "modal-vision-a10g.yml",
            root / "config" / "surfaces" / "modal-h200x4-qwen25-14b-1m.yml",
        ]
    }

    assert 'GPUCALL_MODAL_A10G_SCALEDOWN_WINDOW", 60' in worker
    assert 'GPUCALL_MODAL_VISION_H100_SCALEDOWN_WINDOW", 60' in worker
    assert 'GPUCALL_MODAL_H200X4_SCALEDOWN_WINDOW", 300' in worker
    assert surfaces["modal-a10g.yml"]["scaledown_window_seconds"] == 60
    assert surfaces["modal-vision-a10g.yml"]["scaledown_window_seconds"] == 60
    assert surfaces["modal-h200x4-qwen25-14b-1m.yml"]["scaledown_window_seconds"] == 300


async def test_runpod_flash_cancel_without_owned_resource_is_noop(monkeypatch) -> None:
    called = False

    def cleanup(resource_id: str, resource_name: str | None = None) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("gpucall.execution_surfaces.function_runtime.runpod_flash_cleanup_resource_sync", cleanup)
    adapter = RunpodVllmFlashBootAdapter(api_key="test", model="Qwen/Qwen2.5-1.5B-Instruct")
    handle = RemoteHandle(tuple="runpod-vllm-flashboot", remote_id="job", expires_at=plan_payload_plan().expires_at())

    await adapter.cancel_remote(handle)

    assert called is False


async def test_runpod_vllm_stream_is_explicitly_unsupported() -> None:
    adapter = RunpodVllmServerlessAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        endpoint_contract="openai-chat-completions",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )
    plan = plan_payload_plan().model_copy(update={"mode": ExecutionMode.STREAM})

    try:
        await adapter.start(plan)
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "streaming is not supported" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("RunPod Flash stream unexpectedly started")


async def test_runpod_flash_uses_deployed_runsync_rest_endpoint(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"status": "COMPLETED", "output": {"kind": "inline", "value": "flash ok"}}

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, url: str, **kwargs):
            calls.append(("POST", url, kwargs.get("json")))
            return FakeResponse()

    fake_requests = types.SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = types.SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = types.SimpleNamespace(Retry=lambda **_kwargs: object())
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(sys.modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(sys.modules, "urllib3.util.retry", fake_retry)

    adapter = RunpodVllmFlashBootAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        max_model_len=16384,
    )

    result = await adapter._run_flash(plan_payload_plan())

    assert result["kind"] == "inline"
    assert result["value"] == "flash ok"
    assert calls[0][1] == "https://api.runpod.ai/v2/endpoint-1/runsync"
    assert calls[0][2]["input"]["model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert calls[0][2]["input"]["max_model_len"] == 16384


def test_runpod_flashboot_declares_non_openai_contract() -> None:
    from gpucall.execution.registry import adapter_descriptor

    descriptor = adapter_descriptor("runpod-vllm-flashboot")

    assert descriptor is not None
    assert descriptor.endpoint_contract == "runpod-flash-sdk"
    assert descriptor.output_contract == "gpucall-tuple-result"
    assert descriptor.production_eligible is True
    assert descriptor.required_auto_fields["target"] == "RunPod endpoint target is not configured"


async def test_runpod_vllm_official_route_uses_openai_chat_route(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {
                "choices": [{"message": {"content": "flash llm ok"}}],
                "usage": {"completion_tokens": 3, "prompt_tokens_details": None},
            }

    class FakeHealthResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"workers": {"ready": 1, "running": 0, "initializing": 0, "throttled": 0, "unhealthy": 0}}

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def get(self, url: str, **_kwargs):
            calls.append(("GET", url, None))
            return FakeHealthResponse()

        def post(self, url: str, **kwargs):
            calls.append(("POST", url, kwargs.get("json")))
            return FakeResponse()

    fake_requests = types.SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = types.SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = types.SimpleNamespace(Retry=lambda **_kwargs: object())
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(sys.modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(sys.modules, "urllib3.util.retry", fake_retry)

    adapter = RunpodVllmServerlessAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    plan = plan_payload_plan().model_copy(update={"messages": [ChatMessage(role="user", content="hello")]})
    handle = await adapter.start(plan)
    result = await adapter.wait(handle, plan)

    assert result.value == "flash llm ok"
    assert result.usage == {"completion_tokens": 3}
    assert calls[0][1] == "https://api.runpod.ai/v2/endpoint-1/health"
    assert calls[1][1] == "https://api.runpod.ai/v2/endpoint-1/openai/v1/chat/completions"
    assert calls[1][2]["model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert calls[1][2]["messages"] == [{"role": "user", "content": "hello"}]
    assert calls[1][2]["stream"] is False


def test_runpod_worker_vllm_health_rejects_throttled_endpoint() -> None:
    health = {"workers": {"idle": 0, "initializing": 0, "ready": 0, "running": 0, "throttled": 1, "unhealthy": 0}}

    assert runpod_vllm_health_rejection_reason(health) == "workers are throttled and no ready worker is available"


async def test_runpod_vllm_official_route_rejects_data_refs_for_failover() -> None:
    plan = plan_payload_plan().model_copy(
        update={"input_refs": [{"uri": "https://example.com/input.txt", "sha256": "a" * 64, "bytes": 100}]}
    )
    adapter = RunpodVllmServerlessAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )
    handle = await adapter.start(plan)

    try:
        await adapter.wait(handle, plan)
    except Exception as exc:
        assert getattr(exc, "retryable", None) is True
        assert "does not fetch DataRef" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("RunPod Flash official worker-vLLM unexpectedly accepted DataRef input")


async def test_runpod_vllm_requires_official_worker_vllm_unless_experimental_enabled() -> None:
    adapter = RunpodVllmServerlessAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        image="custom/runpod-worker:latest",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    try:
        await adapter.start(plan_payload_plan())
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "endpoint_contract=openai-chat-completions" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("RunPod Flash accepted non-official production worker")


def test_runpod_flash_worker_is_self_contained() -> None:
    source = (Path(__file__).resolve().parents[1] / "gpucall" / "worker_contracts" / "runpod_flash.py").read_text(
        encoding="utf-8"
    )

    assert "from gpucall.execution" not in source
    assert "import gpucall" not in source


def test_runpod_serverless_uses_rest_policy_and_cancel(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        def __init__(self, status_code: int, data: dict[str, object]) -> None:
            self.status_code = status_code
            self._data = data
            self.text = str(data)

        def json(self) -> dict[str, object]:
            return self._data

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, url: str, **kwargs):
            calls.append(("POST", url, kwargs.get("json")))
            if url.endswith("/run"):
                return FakeResponse(200, {"id": "job-1"})
            if url.endswith("/runsync"):
                return FakeResponse(200, {"id": "job-1", "status": "COMPLETED", "output": {"kind": "inline", "value": "ok"}})
            return FakeResponse(200, {"status": "CANCELLED"})

        def get(self, url: str, **kwargs):
            calls.append(("GET", url, None))
            return FakeResponse(200, {"status": "COMPLETED", "output": {"kind": "inline", "value": "ok"}})

    fake_requests = types.SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = types.SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = types.SimpleNamespace(Retry=lambda **_kwargs: object())
    monkeypatch.setitem(sys.modules, "requests", fake_requests)
    monkeypatch.setitem(sys.modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(sys.modules, "urllib3.util.retry", fake_retry)

    adapter = RunpodServerlessAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        max_model_len=32768,
    )
    plan = plan_payload_plan()
    run_request = adapter._start_sync(plan)
    sync_request = adapter._runsync_sync(plan)
    result = adapter._wait_sync(
        RemoteHandle(tuple="runpod", remote_id=sync_request["job_id"], expires_at=plan.expires_at(), meta=sync_request),
        plan,
    )
    adapter._cancel_sync("job-1")

    assert result.value == "ok"
    assert calls[0][1] == "https://api.runpod.ai/v2/endpoint-1/run"
    assert calls[0][2]["input"]["model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert calls[0][2]["input"]["max_model_len"] == 32768
    assert calls[0][2]["policy"] == {"executionTimeout": 5000, "ttl": 10000}
    assert calls[1][1] == "https://api.runpod.ai/v2/endpoint-1/runsync"
    assert calls[1][2]["policy"] == {"executionTimeout": 5000, "ttl": 10000}
    assert calls[2][1] == "https://api.runpod.ai/v2/endpoint-1/cancel/job-1"


def test_gpucall_tuple_result_rejects_heuristic_output_shapes() -> None:
    from gpucall.domain import TupleError

    with pytest.raises(TupleError, match="TupleResult contract"):
        gpucall_tuple_result({"output": "ok"})


def test_local_ollama_rejects_data_refs_without_leaking_uri() -> None:
    plan = plan_payload_plan().model_copy(
        update={
            "input_refs": [
                {
                    "uri": "https://storage.example/prompt.txt?X-Amz-Signature=secret",
                    "content_type": "text/plain",
                }
            ]
        }
    )
    adapter = LocalOllamaAdapter()

    with pytest.raises(Exception) as exc_info:
        adapter._prompt_from_plan(plan)

    assert "does not support data_refs" in str(exc_info.value)
    assert "X-Amz-Signature" not in str(exc_info.value)


def plan_payload_plan() -> CompiledPlan:
    return CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        tuple_chain=["p1"],
        timeout_seconds=2,
        lease_ttl_seconds=10,
        token_estimation_profile="qwen",
        token_budget=100,
        input_refs=[],
        inline_inputs={},
    )


async def test_azure_adapter_uses_compute_management_client(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakePoller:
        def result(self) -> None:
            calls["create_waited"] = True

    class FakeVirtualMachines:
        def begin_create_or_update(self, resource_group: str, vm_name: str, parameters: dict[str, object]) -> FakePoller:
            calls["resource_group"] = resource_group
            calls["vm_name"] = vm_name
            calls["parameters"] = parameters
            return FakePoller()

        def begin_delete(self, resource_group: str, vm_name: str) -> FakePoller:
            calls["delete"] = (resource_group, vm_name)
            return FakePoller()

    class FakeComputeClient:
        def __init__(self, credential: object, subscription_id: str) -> None:
            calls["subscription_id"] = subscription_id
            self.virtual_machines = FakeVirtualMachines()

    fake_identity = types.ModuleType("azure.identity")
    fake_identity.DefaultAzureCredential = lambda: object()
    fake_compute = types.ModuleType("azure.mgmt.compute")
    fake_compute.ComputeManagementClient = FakeComputeClient
    monkeypatch.setitem(sys.modules, "azure", types.ModuleType("azure"))
    monkeypatch.setitem(sys.modules, "azure.identity", fake_identity)
    monkeypatch.setitem(sys.modules, "azure.mgmt", types.ModuleType("azure.mgmt"))
    monkeypatch.setitem(sys.modules, "azure.mgmt.compute", fake_compute)

    adapter = AzureComputeVMAdapter(
        name="azure",
        subscription_id="sub",
        resource_group="rg",
        location="eastus",
        vm_size="Standard_NC40ads_H100_v5",
        image_reference={"publisher": "Canonical", "offer": "ubuntu-24_04-lts", "sku": "server", "version": "latest"},
        network_interface_id="/subscriptions/s/resourceGroups/rg/tuples/Microsoft.Network/networkInterfaces/nic",
        admin_username="azureuser",
        ssh_public_key="ssh-ed25519 AAAA test",
        params={"vm_name": "gpucall-test"},
    )

    plan = plan_payload_plan()
    handle = await adapter.start(plan)
    await adapter.cancel_remote(handle)

    parameters = calls["parameters"]
    assert calls["subscription_id"] == "sub"
    assert calls["resource_group"] == "rg"
    assert calls["vm_name"] == "gpucall-test"
    assert parameters["security_profile"]["security_type"] == "ConfidentialVM"
    assert calls["delete"] == ("rg", "gpucall-test")


async def test_gcp_adapter_uses_instances_client(monkeypatch) -> None:
    calls: dict[str, object] = {}

    class FakeInstancesClient:
        def insert(self, **kwargs):
            calls["insert"] = kwargs
            return object()

        def delete(self, **kwargs):
            calls["delete"] = kwargs
            return object()

    fake_compute_v1 = types.ModuleType("google.cloud.compute_v1")
    fake_compute_v1.InstancesClient = FakeInstancesClient
    monkeypatch.setitem(sys.modules, "google", types.ModuleType("google"))
    monkeypatch.setitem(sys.modules, "google.cloud", types.ModuleType("google.cloud"))
    monkeypatch.setitem(sys.modules, "google.cloud.compute_v1", fake_compute_v1)

    adapter = GCPConfidentialSpaceVMAdapter(
        name="gcp",
        project_id="project",
        zone="us-central1-a",
        machine_type="zones/us-central1-a/machineTypes/a3-highgpu-1g",
        source_image="projects/confidential-space-images/global/images/family/confidential-space",
        network="global/networks/default",
        params={"instance_name": "gpucall-test"},
    )

    plan = plan_payload_plan()
    handle = await adapter.start(plan)
    await adapter.cancel_remote(handle)

    instance_resource = calls["insert"]["instance_resource"]
    assert calls["insert"]["project"] == "project"
    assert calls["insert"]["zone"] == "us-central1-a"
    assert instance_resource["name"] == "gpucall-test"
    assert instance_resource["confidential_instance_config"]["enable_confidential_compute"] is True
    assert calls["delete"] == {"project": "project", "zone": "us-central1-a", "instance": "gpucall-test"}


async def test_scaleway_adapter_uses_official_instance_rest_paths(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeCreateResponse:
        status_code = 201

        def json(self) -> dict[str, object]:
            return {"server": {"id": "server-1"}}

    class FakeDeleteResponse:
        status_code = 204

    class FakeSession:
        def __init__(self) -> None:
            self.headers: dict[str, str] = {}

        def post(self, url: str, **kwargs):
            calls.append(("POST", url, kwargs.get("json")))
            return FakeCreateResponse()

        def delete(self, url: str, **_kwargs):
            calls.append(("DELETE", url, None))
            return FakeDeleteResponse()

    fake_requests = types.ModuleType("requests")
    fake_requests.Session = FakeSession
    monkeypatch.setitem(sys.modules, "requests", fake_requests)

    adapter = ScalewayInstanceAdapter(
        name="scaleway",
        secret_key="secret",
        project_id="project",
        zone="fr-par-1",
        commercial_type="GPU-L40S",
        image="ubuntu_noble",
        params={"server_name": "gpucall-test"},
    )

    plan = plan_payload_plan()
    handle = await adapter.start(plan)
    await adapter.cancel_remote(handle)

    assert calls[0] == (
        "POST",
        "https://api.scaleway.com/instance/v1/zones/fr-par-1/servers",
        {
            "name": "gpucall-test",
            "project": "project",
            "commercial_type": "GPU-L40S",
            "image": "ubuntu_noble",
            "enable_ipv6": False,
            "tags": ["gpucall-managed", f"gpucall-plan-{plan.plan_id[:12]}"],
        },
    )
    assert calls[1] == ("DELETE", "https://api.scaleway.com/instance/v1/zones/fr-par-1/servers/server-1", None)


async def test_ovhcloud_adapter_uses_official_sdk_paths(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object]]] = []

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            calls.append(("CLIENT", "", kwargs))

        def post(self, path: str, **kwargs):
            calls.append(("POST", path, kwargs))
            return {"id": "instance-1"}

        def delete(self, path: str):
            calls.append(("DELETE", path, {}))

    fake_ovh = types.ModuleType("ovh")
    fake_ovh.Client = FakeClient
    monkeypatch.setitem(sys.modules, "ovh", fake_ovh)

    adapter = OVHCloudPublicCloudInstanceAdapter(
        name="ovhcloud",
        endpoint="ovh-eu",
        service_name="service",
        region="GRA11",
        flavor_id="flavor-id",
        image_id="image-id",
        ssh_key_id="ssh-key-id",
        application_key="ak",
        application_secret="as",
        consumer_key="ck",
        params={"instance_name": "gpucall-test"},
    )

    handle = await adapter.start(plan_payload_plan())
    await adapter.cancel_remote(handle)

    assert calls[0] == ("CLIENT", "", {"endpoint": "ovh-eu", "application_key": "ak", "application_secret": "as", "consumer_key": "ck"})
    assert calls[1] == (
        "POST",
        "/cloud/project/service/instance",
        {
            "name": "gpucall-test",
            "region": "GRA11",
            "flavorId": "flavor-id",
            "imageId": "image-id",
            "sshKeyId": "ssh-key-id",
        },
    )
    assert calls[3] == ("DELETE", "/cloud/project/service/instance/instance-1", {})


async def test_lifecycle_only_adapters_do_not_fake_provider_success() -> None:
    adapter = ScalewayInstanceAdapter(secret_key="secret", project_id="project", zone="fr-par-1", commercial_type="GPU-L40S", image="ubuntu")
    handle = RemoteHandle(tuple="scaleway", remote_id="server-1", expires_at=plan_payload_plan().expires_at())

    with pytest.raises(Exception) as exc_info:
        await adapter.wait(handle, plan_payload_plan())

    assert getattr(exc_info.value, "status_code", None) == 501
    assert getattr(exc_info.value, "code", None) == "PROVIDER_WORKER_BOOTSTRAP_NOT_CONFIGURED"


def test_hyperstack_manifest_tracks_active_and_destroyed_leases(tmp_path) -> None:
    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )

    adapter._record_lease(
        {
            "event": "provision.created",
            "vm_name": "gpucall-managed-plan-vm",
            "vm_id": "vm-1",
            "plan_id": "plan",
            "expires_at": "2000-01-01T00:00:00+00:00",
        }
    )
    assert adapter._active_manifest_leases()[0]["vm_id"] == "vm-1"
    adapter._record_lease({"event": "destroy.pending", "vm_id": "vm-1"})
    pending = adapter._active_manifest_leases()[0]
    assert pending["event"] == "destroy.pending"
    assert pending["vm_name"] == "gpucall-managed-plan-vm"

    adapter._record_lease({"event": "destroyed", "vm_id": "vm-1"})

    assert adapter._active_manifest_leases() == []


def test_hyperstack_destroy_records_pending_after_accepted_delete(monkeypatch, tmp_path) -> None:
    events: list[dict[str, object]] = []

    class FakeDeleteResponse:
        status_code = 200

    class FakeSession:
        def delete(self, *_args, **_kwargs):
            return FakeDeleteResponse()

    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())
    monkeypatch.setattr(adapter, "_wait_vm_absent", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(adapter, "_record_lease", lambda event: events.append(event))

    adapter._destroy_sync({"vm_id": "vm-1"})

    assert events[0]["event"] == "destroy.requested"
    assert events[1]["event"] == "destroy.pending"


def test_hyperstack_create_payload_uses_official_fields(monkeypatch, tmp_path) -> None:
    posted: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"instances": [{"id": 123}]}

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, _url: str, **kwargs):
            if _url.endswith("/core/virtual-machines"):
                posted.update(kwargs["json"])
            return FakeResponse()

    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())
    monkeypatch.setattr(adapter, "_wait_active", lambda _session, _vm_id: "203.0.113.10")
    monkeypatch.setattr(adapter, "_connect_ssh", lambda _ip: _fake_ssh())

    plan = plan_payload_plan()
    adapter._provision_and_start(plan)

    assert "metadata" not in posted
    assert posted["assign_floating_ip"] is True
    assert posted["enable_port_randomization"] is False
    assert posted["labels"] == ["gpucall-managed", f"gpucall-plan-{plan.plan_id[:12]}"]
    assert posted["security_rules"] == [
        {
            "direction": "ingress",
            "ethertype": "IPv4",
            "protocol": "tcp",
            "remote_ip_prefix": "203.0.113.0/24",
            "port_range_min": 22,
            "port_range_max": 22,
        }
    ]


def test_hyperstack_create_payload_uses_configured_image_without_aliasing(monkeypatch, tmp_path) -> None:
    posted: dict[str, object] = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"instances": [{"id": 123}]}

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, _url: str, **kwargs):
            posted.update(kwargs["json"])
            return FakeResponse()

    adapter = HyperstackAdapter(
        api_key="test",
        image_name="Ubuntu 22.04 LTS",
        lease_manifest_path=str(tmp_path / "leases.jsonl"),
        ssh_remote_cidr="203.0.113.0/24",
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())
    monkeypatch.setattr(adapter, "_wait_active", lambda _session, _vm_id: "203.0.113.10")
    monkeypatch.setattr(adapter, "_connect_ssh", lambda _ip: _fake_ssh())

    adapter._provision_and_start(plan_payload_plan())

    assert posted["image_name"] == "Ubuntu 22.04 LTS"


def test_hyperstack_provision_404_is_retryable_for_fallback(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        status_code = 404
        text = '{"status":false,"message":"flavor not found","error_reason":"not_found"}'

        def json(self) -> dict[str, object]:
            return {"status": False, "message": "flavor not found", "error_reason": "not_found"}

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, _url: str, **_kwargs):
            return FakeResponse()

    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())

    try:
        adapter._provision_and_start(plan_payload_plan())
    except Exception as exc:
        assert getattr(exc, "retryable", None) is True
        assert getattr(exc, "status_code", None) == 503
        assert getattr(exc, "code", None) == "PROVIDER_PROVISION_UNAVAILABLE"
        assert "not_found" in str(exc)
        assert '"message":"flavor not found"' in getattr(exc, "raw_output", "")
    else:  # pragma: no cover
        raise AssertionError("Hyperstack 404 unexpectedly provisioned")


def test_hyperstack_provision_400_preserves_redacted_error_body(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        status_code = 400
        text = "{}"

        def json(self) -> dict[str, object]:
            return {
                "status": False,
                "message": "Image does not exist",
                "error_reason": "not_found",
                "api_key": "secret",
            }

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, _url: str, **_kwargs):
            return FakeResponse()

    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())

    try:
        adapter._provision_and_start(plan_payload_plan())
    except Exception as exc:
        assert getattr(exc, "retryable", None) is False
        assert getattr(exc, "status_code", None) == 502
        assert getattr(exc, "code", None) == "PROVIDER_PROVISION_FAILED"
        assert "not_found" in str(exc)
        raw = getattr(exc, "raw_output", "")
        assert '"message":"Image does not exist"' in raw
        assert '"api_key":"<redacted>"' in raw
        assert "secret" not in raw
    else:  # pragma: no cover
        raise AssertionError("Hyperstack 400 unexpectedly provisioned")


def test_hyperstack_stock_400_is_retryable_capacity_unavailable(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        status_code = 400
        text = "{}"

        def json(self) -> dict[str, object]:
            return {
                "status": False,
                "message": "Not Enough Stock of A100-80G-PCIe. Unable to launch virtual-machines.",
                "error_reason": "bad_request",
            }

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, _url: str, **_kwargs):
            return FakeResponse()

    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())

    try:
        adapter._provision_and_start(plan_payload_plan())
    except Exception as exc:
        assert getattr(exc, "retryable", None) is True
        assert getattr(exc, "status_code", None) == 503
        assert getattr(exc, "code", None) == "PROVIDER_PROVISION_UNAVAILABLE"
        assert "Not Enough Stock" in getattr(exc, "raw_output", "")
    else:  # pragma: no cover
        raise AssertionError("Hyperstack stock failure unexpectedly provisioned")


def test_hyperstack_worker_script_invokes_vllm_not_smoke_output(monkeypatch, tmp_path) -> None:
    commands: list[str] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"instances": [{"id": 123}]}

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def post(self, _url: str, **kwargs):
            return FakeResponse()

    class FakeStdout:
        class channel:
            @staticmethod
            def exit_status_ready() -> bool:
                return True

            @staticmethod
            def recv_exit_status() -> int:
                return 0

    class FakeSSH:
        uploaded: dict[str, str] = {}

        def open_sftp(self):
            parent = self

            class FakeHandle:
                def __init__(self, path: str) -> None:
                    self.path = path

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def write(self, value: str) -> None:
                    parent.uploaded[self.path] = value

            class FakeSFTP:
                def mkdir(self, _path: str) -> None:
                    return None

                def file(self, path: str, _mode: str):
                    return FakeHandle(path)

                def close(self) -> None:
                    return None

            return FakeSFTP()

        def exec_command(self, cmd: str):
            commands.append(cmd)
            return None, FakeStdout(), None

    adapter = HyperstackAdapter(
        api_key="test",
        lease_manifest_path=str(tmp_path / "leases.jsonl"),
        ssh_remote_cidr="203.0.113.0/24",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        max_model_len=32768,
    )
    monkeypatch.setattr(adapter, "_session", lambda: FakeSession())
    monkeypatch.setattr(adapter, "_wait_active", lambda _session, _vm_id: "203.0.113.10")
    monkeypatch.setattr(adapter, "_connect_ssh", lambda _ip: FakeSSH())

    adapter._provision_and_start(plan_payload_plan())

    assert commands
    assert "GPUCALL_HYPERSTACK_VLLM_PACKAGE" in commands[0]
    assert "GPUCALL_WORKER_MODEL" in commands[0]
    assert "cat > /tmp/gpucall/input.json" not in commands[0]
    assert "cat > /tmp/gpucall/worker.py" not in commands[0]
    assert "[HYPERSTACK]" not in commands[0]


def test_hyperstack_wait_parses_artifact_manifest(tmp_path) -> None:
    manifest = {
        "artifact_id": "a" * 64,
        "artifact_chain_id": "chain-1",
        "version": "0001",
        "classification": "restricted",
        "ciphertext_uri": "s3://bucket/artifact.bin",
        "ciphertext_sha256": "b" * 64,
        "key_id": "tenant-key",
        "producer_plan_hash": "c" * 64,
    }

    class FakeChannel:
        def exit_status_ready(self) -> bool:
            return True

        def recv_exit_status(self) -> int:
            return 0

    class FakeStdout:
        channel = FakeChannel()

        def read(self) -> bytes:
            return json.dumps(manifest).encode("utf-8")

    class FakeSSH:
        def exec_command(self, _cmd: str):
            return None, FakeStdout(), None

    adapter = HyperstackAdapter(api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24")
    plan = plan_payload_plan().model_copy(
        update={
            "task": "fine-tune",
            "data_classification": DataClassification.RESTRICTED,
            "artifact_export": ArtifactExportSpec(artifact_chain_id="chain-1", version="0001", key_id="tenant-key"),
        }
    )
    handle = RemoteHandle(
        tuple="hyperstack",
        remote_id="vm-1",
        expires_at=plan.expires_at(),
        meta={"ssh_channel": FakeChannel(), "ssh_client": FakeSSH()},
    )

    result = adapter._wait_sync(handle, plan)

    assert result.kind == "artifact_manifest"
    assert result.artifact_manifest is not None
    assert result.artifact_manifest.artifact_chain_id == "chain-1"




def test_hyperstack_adds_ssh_security_rule_after_active(tmp_path) -> None:
    posted: dict[str, object] = {}

    class FakeResponse:
        status_code = 201
        text = "{}"

    class FakeSession:
        def post(self, url: str, **kwargs):
            posted["url"] = url
            posted["json"] = kwargs["json"]
            return FakeResponse()

    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )
    adapter._ensure_ssh_rule(FakeSession(), "123")

    assert posted["url"].endswith("/core/virtual-machines/123/sg-rules")
    assert posted["json"] == {
        "direction": "ingress",
        "ethertype": "IPv4",
        "protocol": "tcp",
        "remote_ip_prefix": "203.0.113.0/24",
        "port_range_min": 22,
        "port_range_max": 22,
    }


def test_hyperstack_adapter_rejects_all_open_ssh_cidr(tmp_path) -> None:
    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="0.0.0.0/0"
    )

    with pytest.raises(Exception) as exc_info:
        adapter._provision_and_start(plan_payload_plan())

    assert "must not allow all addresses" in str(exc_info.value)


def test_hyperstack_wait_active_ignores_private_fixed_ip(monkeypatch, tmp_path) -> None:
    calls = 0

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"instance": {"status": "ACTIVE", "fixed_ip": "10.0.0.10"}}
            return {"instance": {"status": "ACTIVE", "fixed_ip": "10.0.0.10", "floating_ip": "203.0.113.10"}}

    class FakeSession:
        def get(self, *_args, **_kwargs):
            return FakeResponse()

    monkeypatch.setattr("gpucall.execution_surfaces.hyperstack_vm.time.sleep", lambda _seconds: None)
    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )

    assert adapter._wait_active(FakeSession(), "vm-1") == "203.0.113.10"
    assert calls == 2


def test_hyperstack_wait_active_retries_transient_api_timeout(monkeypatch, tmp_path) -> None:
    calls = 0

    class FakeResponse:
        status_code = 200

        def json(self) -> dict[str, object]:
            return {"instance": {"status": "ACTIVE", "floating_ip": "203.0.113.10"}}

    class FakeSession:
        def get(self, *_args, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("slow official API")
            return FakeResponse()

    monkeypatch.setattr("gpucall.execution_surfaces.hyperstack_vm.time.sleep", lambda _seconds: None)
    adapter = HyperstackAdapter(
        api_key="test", lease_manifest_path=str(tmp_path / "leases.jsonl"), ssh_remote_cidr="203.0.113.0/24"
    )

    assert adapter._wait_active(FakeSession(), "vm-1") == "203.0.113.10"
    assert calls == 2


def _fake_ssh():
    class FakeChannel:
        def exit_status_ready(self) -> bool:
            return True

        def recv_exit_status(self) -> int:
            return 0

    class FakeStdout:
        channel = FakeChannel()

        def read(self) -> bytes:
            return b"ok"

    class FakeSSH:
        uploaded: dict[str, str] = {}

        def open_sftp(self):
            parent = self

            class FakeHandle:
                def __init__(self, path: str) -> None:
                    self.path = path

                def __enter__(self):
                    return self

                def __exit__(self, *_args):
                    return None

                def write(self, value: str) -> None:
                    parent.uploaded[self.path] = value

            class FakeSFTP:
                def mkdir(self, _path: str) -> None:
                    return None

                def file(self, path: str, _mode: str):
                    return FakeHandle(path)

                def close(self) -> None:
                    return None

            return FakeSFTP()

        def exec_command(self, _cmd: str):
            return None, FakeStdout(), None

    return FakeSSH()
