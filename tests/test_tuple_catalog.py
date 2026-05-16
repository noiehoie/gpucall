from __future__ import annotations

from gpucall.config import ConfigError, GpucallConfig, validate_config
from gpucall.domain import Policy, ExecutionTupleSpec, Recipe
from gpucall.tuple_catalog import live_tuple_catalog_evidence, live_tuple_catalog_findings


def test_hyperstack_live_catalog_check_rejects_unknown_image(monkeypatch) -> None:
    class FakeResponse:
        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeRequests:
        @staticmethod
        def get(url: str, **_kwargs):
            if url.endswith("/core/images"):
                return FakeResponse(
                    {
                        "images": [
                            {
                                "region_name": "CANADA-1",
                                "images": [{"name": "Ubuntu Server 22.04 LTS R570 CUDA 12.8 with Docker"}],
                            }
                        ]
                    }
                )
            if url.endswith("/core/flavors"):
                return FakeResponse(
                    {"data": [{"region_name": "CANADA-1", "flavors": [{"name": "n3-A100x1"}]}]}
                )
            if url.endswith("/core/environments"):
                return FakeResponse({"environments": [{"name": "default-CANADA-1"}]})
            raise AssertionError(url)

    monkeypatch.setitem(__import__("sys").modules, "requests", FakeRequests)
    tuples = {
        "hyperstack-a100": ExecutionTupleSpec(
            name="hyperstack-a100",
            adapter="hyperstack",
            gpu="A100",
            vram_gb=80,
            max_model_len=32768,
            cost_per_second=0.001,
            target="default-CANADA-1",
            model="Qwen/Qwen2.5-1.5B-Instruct",
            instance="n3-A100x1",
            image="Ubuntu 22.04 LTS",
        )
    }

    findings = live_tuple_catalog_findings(tuples, {"hyperstack": {"api_key": "test"}})

    assert findings
    assert findings[0]["field"] == "image"


def test_runpod_live_catalog_records_price_and_blocks_unavailable_stock(monkeypatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def get(self, url: str, **kwargs):
            calls.append(url)
            if url.endswith("/endpoint-1/health"):
                return FakeResponse({"workers": {"ready": 0, "running": 0, "initializing": 0, "throttled": 1, "unhealthy": 0}})
            if url.endswith("/endpoints"):
                return FakeResponse([{"id": "endpoint-1", "currentPricePerSecond": 0.00042}])
            raise AssertionError(url)

    fake_requests = __import__("types").SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = __import__("types").SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = __import__("types").SimpleNamespace(Retry=lambda **_kwargs: object())
    modules = __import__("sys").modules
    monkeypatch.setitem(modules, "requests", fake_requests)
    monkeypatch.setitem(modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(modules, "urllib3.util.retry", fake_retry)
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        provider_params={"worker_env": {"MODEL_NAME": "Qwen/Qwen2.5-1.5B-Instruct", "MAX_MODEL_LEN": "8192", "GPU_MEMORY_UTILIZATION": "0.9", "MAX_CONCURRENCY": "1"}},
    )

    evidence = live_tuple_catalog_evidence({tuple.name: tuple}, {"runpod": {"api_key": "rk_test"}})

    assert evidence[tuple.name]["status"] == "blocked"
    findings = evidence[tuple.name]["findings"]
    assert any(item.get("live_stock_state") == "unavailable" for item in findings)
    assert any(item.get("live_price_per_second") == 0.00042 for item in findings)
    assert calls == ["https://rest.runpod.io/v1/endpoints", "https://api.runpod.ai/v2/endpoint-1/health"]


def test_runpod_live_catalog_accepts_items_inventory_shape(monkeypatch) -> None:
    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def get(self, url: str, **_kwargs):
            if url.endswith("/endpoints"):
                return FakeResponse({"items": [{"id": "endpoint-1", "currentPricePerSecond": 0.00042}]})
            if url.endswith("/endpoint-1/health"):
                return FakeResponse({"workers": {"ready": 1, "running": 0, "initializing": 0, "throttled": 0, "unhealthy": 0}})
            raise AssertionError(url)

    fake_requests = __import__("types").SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = __import__("types").SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = __import__("types").SimpleNamespace(Retry=lambda **_kwargs: object())
    modules = __import__("sys").modules
    monkeypatch.setitem(modules, "requests", fake_requests)
    monkeypatch.setitem(modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(modules, "urllib3.util.retry", fake_retry)
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    evidence = live_tuple_catalog_evidence({tuple.name: tuple}, {"runpod": {"api_key": "rk_test"}})

    findings = evidence[tuple.name]["findings"]
    assert any(item.get("live_price_per_second") == 0.00042 for item in findings)


def test_runpod_live_catalog_matches_endpoint_id_not_name(monkeypatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def get(self, url: str, **_kwargs):
            calls.append(url)
            if url.endswith("/endpoints"):
                return FakeResponse({"endpoints": [{"id": "different-endpoint", "name": "endpoint-1"}]})
            raise AssertionError("endpoint health must not be queried when inventory id does not match")

    fake_requests = __import__("types").SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = __import__("types").SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = __import__("types").SimpleNamespace(Retry=lambda **_kwargs: object())
    modules = __import__("sys").modules
    monkeypatch.setitem(modules, "requests", fake_requests)
    monkeypatch.setitem(modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(modules, "urllib3.util.retry", fake_retry)
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )

    evidence = live_tuple_catalog_evidence({tuple.name: tuple}, {"runpod": {"api_key": "rk_test"}})

    assert evidence[tuple.name]["status"] == "blocked"
    assert evidence[tuple.name]["findings"][0]["field"] == "runpod_endpoint_inventory"
    assert calls == ["https://rest.runpod.io/v1/endpoints"]


def test_runpod_live_catalog_blocks_positive_workers_min(monkeypatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def get(self, url: str, **kwargs):
            calls.append(url)
            if url.endswith("/endpoint-1/health"):
                return FakeResponse({"workers": {"ready": 1, "running": 0, "initializing": 0, "throttled": 0, "unhealthy": 0}})
            if url.endswith("/endpoints"):
                return FakeResponse(
                    {
                        "endpoints": [
                            {
                                "id": "endpoint-1",
                                "currentPricePerSecond": 0.00042,
                                "workersMin": 1,
                                "workersMax": 1,
                                "activePods": {"running": 1},
                                "workers": [{"id": "worker-1"}],
                            }
                        ]
                    }
                )
            raise AssertionError(url)

    fake_requests = __import__("types").SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = __import__("types").SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = __import__("types").SimpleNamespace(Retry=lambda **_kwargs: object())
    modules = __import__("sys").modules
    monkeypatch.setitem(modules, "requests", fake_requests)
    monkeypatch.setitem(modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(modules, "urllib3.util.retry", fake_retry)
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        provider_params={"worker_env": {"MODEL_NAME": "Qwen/Qwen2.5-1.5B-Instruct", "MAX_MODEL_LEN": "8192", "GPU_MEMORY_UTILIZATION": "0.9", "MAX_CONCURRENCY": "1"}},
    )

    evidence = live_tuple_catalog_evidence({tuple.name: tuple}, {"runpod": {"api_key": "rk_test"}})

    assert evidence[tuple.name]["status"] == "blocked"
    findings = evidence[tuple.name]["findings"]
    billing_findings = [item for item in findings if item.get("field") == "runpod_serverless_billing_guard"]
    assert billing_findings
    assert {item["raw"]["live_reason"] for item in billing_findings} >= {"workers_min_positive", "active_pods_present"}
    assert calls == ["https://rest.runpod.io/v1/endpoints"]


def test_runpod_live_catalog_allows_approved_standing_workers(monkeypatch) -> None:
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeSession:
        def mount(self, *_args, **_kwargs) -> None:
            return None

        def get(self, url: str, **_kwargs):
            calls.append(url)
            if url.endswith("/endpoints"):
                return FakeResponse(
                    {
                        "endpoints": [
                            {
                                "id": "endpoint-1",
                                "currentPricePerSecond": 0.00042,
                                "workersMin": 1,
                                "workersMax": 1,
                                "activePods": {"running": 1},
                            }
                        ]
                    }
                )
            if url.endswith("/endpoint-1/health"):
                return FakeResponse({"workers": {"ready": 1, "running": 0, "initializing": 0, "throttled": 0, "unhealthy": 0}})
            raise AssertionError(url)

    fake_requests = __import__("types").SimpleNamespace(Session=lambda: FakeSession())
    fake_adapters = __import__("types").SimpleNamespace(HTTPAdapter=lambda **_kwargs: object())
    fake_retry = __import__("types").SimpleNamespace(Retry=lambda **_kwargs: object())
    modules = __import__("sys").modules
    monkeypatch.setitem(modules, "requests", fake_requests)
    monkeypatch.setitem(modules, "requests.adapters", fake_adapters)
    monkeypatch.setitem(modules, "urllib3.util.retry", fake_retry)
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        standing_cost_per_second=0.00045,
        standing_cost_window_seconds=3600,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        provider_params={
            "worker_env": {"MODEL_NAME": "Qwen/Qwen2.5-1.5B-Instruct", "MAX_MODEL_LEN": "8192", "GPU_MEMORY_UTILIZATION": "0.9", "MAX_CONCURRENCY": "1"},
            "cost_approval": {
                "standing_workers_approved": True,
                "approved_by": "operator",
                "approved_at": "2026-05-16T00:00:00Z",
                "reason": "bounded production warm pool",
            },
        },
    )

    evidence = live_tuple_catalog_evidence({tuple.name: tuple}, {"runpod": {"api_key": "rk_test"}})

    assert evidence[tuple.name]["status"] == "live_revalidated"
    assert not [item for item in evidence[tuple.name]["findings"] if item.get("field") == "runpod_serverless_billing_guard"]
    assert calls == ["https://rest.runpod.io/v1/endpoints", "https://api.runpod.ai/v2/endpoint-1/health"]


def test_runpod_health_rejection_treats_nonnumeric_worker_counts_as_zero() -> None:
    from gpucall.execution_surfaces.managed_endpoint import runpod_vllm_health_rejection_reason

    reason = runpod_vllm_health_rejection_reason({"workers": {"ready": "bad", "running": 0, "initializing": "1"}})

    assert reason == "workers are still initializing"


def test_runpod_vllm_provider_requires_official_worker_contract() -> None:
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00045,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )
    recipe = Recipe(
        name="text-infer-light",
        task="infer",
        allowed_modes=["sync"],
        min_vram_gb=8,
        max_model_len=8192,
        timeout_seconds=30,
        lease_ttl_seconds=60,
        token_estimation_profile="qwen",
    )
    config = GpucallConfig(
        policy=Policy(
            version="test",
            inline_bytes_limit=8192,
            default_lease_ttl_seconds=60,
            max_lease_ttl_seconds=120,
            max_timeout_seconds=60,
            tuples={"allow": ["runpod-vllm-serverless"], "deny": []},
        ),
        recipes={recipe.name: recipe},
        tuples={tuple.name: tuple},
    )

    try:
        validate_config(config)
    except ConfigError as exc:
        assert "provider_params.worker_env" in str(exc)
    else:
        raise AssertionError("RunPod worker-vLLM tuple accepted missing official worker_env contract")


def test_modal_provider_requires_deployed_function_target() -> None:
    tuple = ExecutionTupleSpec(
        name="modal-a10g",
        adapter="modal",
        gpu="A10G",
        vram_gb=24,
        max_model_len=8192,
        cost_per_second=0.00035,
        modes=["sync"],
        target="gpucall-worker-json",
        endpoint_contract="modal-function",
        input_contracts=["text", "chat_messages"],
        output_contract="plain-text",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
    )
    recipe = Recipe(
        name="text-infer-light",
        task="infer",
        allowed_modes=["sync"],
        min_vram_gb=8,
        max_model_len=8192,
        timeout_seconds=30,
        lease_ttl_seconds=60,
        token_estimation_profile="qwen",
    )
    config = GpucallConfig(
        policy=Policy(
            version="test",
            inline_bytes_limit=8192,
            default_lease_ttl_seconds=60,
            max_lease_ttl_seconds=120,
            max_timeout_seconds=60,
            tuples={"allow": ["modal-a10g"], "deny": []},
        ),
        recipes={recipe.name: recipe},
        tuples={tuple.name: tuple},
    )

    try:
        validate_config(config)
    except ConfigError as exc:
        assert "target must be '<modal-app>:<function>'" in str(exc)
    else:
        raise AssertionError("Modal tuple accepted a non-deployed-function target")


def test_runpod_vllm_requires_model_storage_contract() -> None:
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00016,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        provider_params={
            "worker_env": {
                "MODEL_NAME": "Qwen/Qwen2.5-1.5B-Instruct",
                "OPENAI_SERVED_MODEL_NAME_OVERRIDE": "Qwen/Qwen2.5-1.5B-Instruct",
                "MAX_MODEL_LEN": "8192",
                "BASE_PATH": "/runpod-volume",
                "GPU_MEMORY_UTILIZATION": "0.95",
                "MAX_CONCURRENCY": "30",
            }
        },
    )
    recipe = Recipe(
        name="text-infer-light",
        task="infer",
        allowed_modes=["sync"],
        min_vram_gb=8,
        max_model_len=8192,
        timeout_seconds=30,
        lease_ttl_seconds=60,
        token_estimation_profile="qwen",
    )
    config = GpucallConfig(
        policy=Policy(
            version="test",
            inline_bytes_limit=8192,
            default_lease_ttl_seconds=60,
            max_lease_ttl_seconds=120,
            max_timeout_seconds=60,
            tuples={"allow": ["runpod-vllm-serverless"], "deny": []},
        ),
        recipes={recipe.name: recipe},
        tuples={tuple.name: tuple},
    )

    try:
        validate_config(config)
    except ConfigError as exc:
        assert "provider_params.model_storage" in str(exc)
    else:
        raise AssertionError("RunPod worker-vLLM tuple accepted missing model_storage contract")


def test_runpod_warm_workers_require_explicit_standing_cost_approval() -> None:
    tuple = ExecutionTupleSpec(
        name="runpod-vllm-serverless",
        adapter="runpod-vllm-serverless",
        gpu="AMPERE_16",
        vram_gb=16,
        max_model_len=8192,
        cost_per_second=0.00016,
        modes=["sync"],
        target="endpoint-1",
        image="runpod/worker-v1-vllm:v2.18.1",
        endpoint_contract="openai-chat-completions",
        input_contracts=["chat_messages"],
        output_contract="openai-chat-completions",
        stream_contract="none",
        model="Qwen/Qwen2.5-1.5B-Instruct",
        provider_params={
            "endpoint_runtime": {"workersMin": 1},
            "worker_env": {
                "MODEL_NAME": "Qwen/Qwen2.5-1.5B-Instruct",
                "MAX_MODEL_LEN": "8192",
                "GPU_MEMORY_UTILIZATION": "0.9",
                "MAX_CONCURRENCY": "1",
            },
            "model_storage": {"storage_kind": "container_ephemeral"},
        },
    )
    recipe = Recipe(
        name="text-infer-light",
        task="infer",
        allowed_modes=["sync"],
        min_vram_gb=8,
        max_model_len=8192,
        timeout_seconds=30,
        lease_ttl_seconds=60,
        token_estimation_profile="qwen",
    )
    config = GpucallConfig(
        policy=Policy(
            version="test",
            inline_bytes_limit=8192,
            default_lease_ttl_seconds=60,
            max_lease_ttl_seconds=120,
            max_timeout_seconds=60,
            tuples={"allow": ["runpod-vllm-serverless"], "deny": []},
        ),
        recipes={recipe.name: recipe},
        tuples={tuple.name: tuple},
    )

    try:
        validate_config(config)
    except ConfigError as exc:
        assert "warm RunPod workers require standing_cost_per_second" in str(exc)
        assert "provider_params.cost_approval.standing_workers_approved=true" in str(exc)
    else:
        raise AssertionError("RunPod warm workers accepted without explicit standing cost approval")
