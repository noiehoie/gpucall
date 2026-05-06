from __future__ import annotations

from gpucall.config import ConfigError, GpucallConfig, validate_config
from gpucall.domain import Policy, ProviderSpec, Recipe
from gpucall.provider_catalog import live_provider_catalog_findings


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
    providers = {
        "hyperstack-a100": ProviderSpec(
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

    findings = live_provider_catalog_findings(providers, {"hyperstack": {"api_key": "test"}})

    assert findings
    assert findings[0]["field"] == "image"


def test_runpod_vllm_provider_requires_official_worker_contract() -> None:
    provider = ProviderSpec(
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
        tokenizer_family="qwen",
    )
    config = GpucallConfig(
        policy=Policy(
            version="test",
            inline_bytes_limit=8192,
            default_lease_ttl_seconds=60,
            max_lease_ttl_seconds=120,
            max_timeout_seconds=60,
            providers={"allow": ["runpod-vllm-serverless"], "deny": []},
        ),
        recipes={recipe.name: recipe},
        providers={provider.name: provider},
    )

    try:
        validate_config(config)
    except ConfigError as exc:
        assert "provider_params.worker_env" in str(exc)
    else:
        raise AssertionError("RunPod worker-vLLM provider accepted missing official worker_env contract")


def test_modal_provider_requires_deployed_function_target() -> None:
    provider = ProviderSpec(
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
        tokenizer_family="qwen",
    )
    config = GpucallConfig(
        policy=Policy(
            version="test",
            inline_bytes_limit=8192,
            default_lease_ttl_seconds=60,
            max_lease_ttl_seconds=120,
            max_timeout_seconds=60,
            providers={"allow": ["modal-a10g"], "deny": []},
        ),
        recipes={recipe.name: recipe},
        providers={provider.name: provider},
    )

    try:
        validate_config(config)
    except ConfigError as exc:
        assert "target must be '<modal-app>:<function>'" in str(exc)
    else:
        raise AssertionError("Modal provider accepted a non-deployed-function target")
