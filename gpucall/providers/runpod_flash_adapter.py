from __future__ import annotations

from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter
from gpucall.providers.runpod_vllm_adapter import RunpodVllmServerlessAdapter


class RunpodFlashAdapter(RunpodVllmServerlessAdapter):
    """Deprecated compatibility adapter name for the official worker-vLLM route."""


@register_adapter(
    "runpod-flash",
    aliases=("flash",),
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        official_sources=(
            "https://docs.runpod.io/serverless/vllm/openai-compatibility",
            "https://docs.runpod.io/serverless/endpoints/send-requests",
        ),
    ),
)
def build_runpod_flash_adapter(spec, credentials):
    runpod = credentials.get("runpod", {})
    return RunpodFlashAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
        image=spec.image,
        endpoint_contract=spec.endpoint_contract,
    )
