from __future__ import annotations

import contextlib
from pathlib import Path
import sys
import types

import pytest

from gpucall.domain import CompiledPlan, ExecutionMode, InlineValue, ProviderSpec
from gpucall.providers.hyperstack_adapter import DEFAULT_HYPERSTACK_IMAGE, HyperstackAdapter
from gpucall.providers import EchoProvider, LocalOllamaAdapter, ModalAdapter, build_adapters
from gpucall.providers.base import RemoteHandle
from gpucall.providers.payloads import gpucall_provider_result, plan_payload
from gpucall.providers.runpod_adapter import (
    RunpodFlashAdapter,
    RunpodServerlessAdapter,
    RunpodVllmFlashBootAdapter,
    RunpodVllmServerlessAdapter,
)


def test_factory_builds_configured_adapter_types() -> None:
    providers = {
        "echo": ProviderSpec(name="echo", adapter="echo", gpu="L4", vram_gb=24, max_model_len=8192, cost_per_second=0),
        "local": ProviderSpec(
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
        "modal": ProviderSpec(
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
        "hyperstack": ProviderSpec(
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
        "runpod-vllm-serverless": ProviderSpec(
            name="runpod-vllm-serverless",
            adapter="runpod-vllm-serverless",
            gpu="AMPERE_16",
            vram_gb=16,
            max_model_len=8192,
            cost_per_second=0,
            target="endpoint-1",
            image="runpod/worker-v1-vllm:v2.18.1",
            endpoint_contract="openai-chat-completions",
            output_contract="openai-chat-completions",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        ),
        "runpod-vllm-flashboot": ProviderSpec(
            name="runpod-vllm-flashboot",
            adapter="runpod-vllm-flashboot",
            gpu="AMPERE_16",
            vram_gb=16,
            max_model_len=8192,
            cost_per_second=0,
            target="endpoint-2",
            image="runpod/worker-v1-vllm:v2.18.1",
            endpoint_contract="openai-chat-completions",
            output_contract="gpucall-provider-result",
            model="Qwen/Qwen2.5-1.5B-Instruct",
        ),
    }

    adapters = build_adapters(providers)

    assert isinstance(adapters["echo"], EchoProvider)
    assert isinstance(adapters["local"], LocalOllamaAdapter)
    assert isinstance(adapters["modal"], ModalAdapter)
    assert isinstance(adapters["hyperstack"], HyperstackAdapter)
    assert isinstance(adapters["runpod-vllm-serverless"], RunpodVllmServerlessAdapter)
    assert isinstance(adapters["runpod-vllm-flashboot"], RunpodVllmFlashBootAdapter)
    assert adapters["hyperstack"].environment_name == "default-CANADA-1"
    assert adapters["hyperstack"].model == "Qwen/Qwen2.5-1.5B-Instruct"
    assert adapters["modal"].stream_function_name == "stream"


def test_provider_payload_contains_refs_not_dereferenced_data() -> None:
    plan = CompiledPlan(
        policy_version="test",
        recipe_name="r1",
        task="infer",
        mode=ExecutionMode.SYNC,
        provider_chain=["p1"],
        timeout_seconds=2,
        lease_ttl_seconds=10,
        tokenizer_family="qwen",
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


async def test_runpod_flash_cancel_without_owned_resource_is_noop(monkeypatch) -> None:
    called = False

    def cleanup(resource_id: str, resource_name: str | None = None) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("gpucall.providers.runpod_adapter._runpod_flash_cleanup_resource_sync", cleanup)
    adapter = RunpodFlashAdapter(api_key="test")
    handle = RemoteHandle(provider="runpod-flash", remote_id="job", expires_at=plan_payload_plan().expires_at())

    await adapter.cancel_remote(handle)

    assert called is False


async def test_runpod_flash_stream_is_explicitly_unsupported() -> None:
    adapter = RunpodFlashAdapter(api_key="rk_test", endpoint_id="endpoint-1")
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

    adapter = RunpodFlashAdapter(
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


def test_runpod_flash_starts_deployed_jobs_with_run_not_runsync(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {"id": "job-1", "status": "IN_QUEUE"}

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

    adapter = RunpodFlashAdapter(api_key="rk_test", endpoint_id="endpoint-1")
    job_id = adapter._start_endpoint_job_sync(plan_payload_plan(), "resource-1")

    assert job_id == "job-1"
    assert calls[0][1] == "https://api.runpod.ai/v2/endpoint-1/run"
    assert calls[0][2]["input"]["resource_name"] == "resource-1"


async def test_runpod_flash_official_vllm_uses_openai_chat_route(monkeypatch) -> None:
    calls: list[tuple[str, str, dict[str, object] | None]] = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self) -> dict[str, object]:
            return {
                "choices": [{"message": {"content": "flash llm ok"}}],
                "usage": {"completion_tokens": 3, "prompt_tokens_details": None},
            }

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

    adapter = RunpodFlashAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    plan = plan_payload_plan().model_copy(update={"messages": [{"role": "user", "content": "hello"}]})
    handle = await adapter.start(plan)
    result = await adapter.wait(handle, plan)

    assert result.value == "flash llm ok"
    assert result.usage == {"completion_tokens": 3}
    assert calls[0][1] == "https://api.runpod.ai/v2/endpoint-1/openai/v1/chat/completions"
    assert calls[0][2]["model"] == "Qwen/Qwen2.5-1.5B-Instruct"
    assert calls[0][2]["messages"] == [{"role": "user", "content": "hello"}]
    assert calls[0][2]["stream"] is False


async def test_runpod_flash_official_vllm_rejects_data_refs_for_failover() -> None:
    plan = plan_payload_plan().model_copy(
        update={"input_refs": [{"uri": "https://example.com/input.txt", "sha256": "a" * 64, "bytes": 100}]}
    )
    adapter = RunpodFlashAdapter(
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


async def test_runpod_flash_requires_official_worker_vllm_unless_experimental_enabled() -> None:
    adapter = RunpodFlashAdapter(
        api_key="rk_test",
        endpoint_id="endpoint-1",
        image="custom/runpod-worker:latest",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    try:
        await adapter.start(plan_payload_plan())
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 400
        assert "official worker-vLLM" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("RunPod Flash accepted non-official production worker")


def test_runpod_flash_worker_is_self_contained() -> None:
    source = (Path(__file__).resolve().parents[1] / "gpucall" / "providers" / "runpod_flash_worker.py").read_text(
        encoding="utf-8"
    )

    assert "from gpucall.providers" not in source
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
        RemoteHandle(provider="runpod", remote_id=sync_request["job_id"], expires_at=plan.expires_at(), meta=sync_request),
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


def test_gpucall_provider_result_rejects_heuristic_output_shapes() -> None:
    from gpucall.domain import ProviderError

    with pytest.raises(ProviderError, match="ProviderResult contract"):
        gpucall_provider_result({"output": "ok"})


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
        provider_chain=["p1"],
        timeout_seconds=2,
        lease_ttl_seconds=10,
        tokenizer_family="qwen",
        token_budget=100,
        input_refs=[],
        inline_inputs={},
    )


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

    adapter._record_lease({"event": "destroyed", "vm_id": "vm-1"})

    assert adapter._active_manifest_leases() == []


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
    assert "security_rules" not in posted


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
        text = "flavor not found"

        def json(self) -> dict[str, object]:
            return {}

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
    else:  # pragma: no cover
        raise AssertionError("Hyperstack 404 unexpectedly provisioned")


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
    assert "vllm==0.6.3" in commands[0]
    assert "GPUCALL_WORKER_MODEL" in commands[0]
    assert "[HYPERSTACK]" not in commands[0]




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

    monkeypatch.setattr("gpucall.providers.hyperstack_adapter.time.sleep", lambda _seconds: None)
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
        def exec_command(self, _cmd: str):
            return None, FakeStdout(), None

    return FakeSSH()
