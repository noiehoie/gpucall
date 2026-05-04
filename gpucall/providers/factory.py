from __future__ import annotations

from gpucall.domain import ProviderSpec
from gpucall.credentials import load_credentials
from gpucall.providers.base import ProviderAdapter
from gpucall.providers.echo import EchoProvider
from gpucall.providers.hyperstack_adapter import HyperstackAdapter
from gpucall.providers.hyperstack_adapter import DEFAULT_HYPERSTACK_IMAGE
from gpucall.providers.local_adapter import LocalOllamaAdapter
from gpucall.providers.modal_adapter import ModalAdapter
from gpucall.providers.registry import build_registered_adapter, register_adapter
from gpucall.providers.runpod_adapter import (
    RunpodFlashAdapter,
    RunpodServerlessAdapter,
    RunpodVllmFlashBootAdapter,
    RunpodVllmServerlessAdapter,
)


def build_adapters(providers: dict[str, ProviderSpec]) -> dict[str, ProviderAdapter]:
    credentials = load_credentials()
    adapters: dict[str, ProviderAdapter] = {}
    for name, spec in providers.items():
        adapters[name] = build_adapter(spec, credentials)
    return adapters


def build_adapter(spec: ProviderSpec, credentials: dict[str, dict[str, str]] | None = None) -> ProviderAdapter:
    return build_registered_adapter(spec, credentials)


def _split_modal_target(target: str | None) -> tuple[str | None, str | None]:
    if not target:
        return None, None
    if ":" not in target:
        return target, None
    app_name, function_name = target.split(":", 1)
    return app_name or None, function_name or None


@register_adapter("echo")
def _build_echo(spec: ProviderSpec, _credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    return EchoProvider(name=spec.name)


@register_adapter("local-ollama", aliases=("local", "ollama"))
def _build_local_ollama(spec: ProviderSpec, _credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    if not spec.endpoint or not spec.model:
        raise ValueError("local-ollama provider requires explicit endpoint and model")
    _require_contract(spec, endpoint="ollama-generate", output="ollama-generate", stream="none")
    return LocalOllamaAdapter(
        name=spec.name,
        base_url=str(spec.endpoint),
        model=spec.model,
    )


@register_adapter("modal")
def _build_modal(spec: ProviderSpec, _credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    _require_contract(spec, endpoint="modal-function", output="plain-text", stream="none")
    app_name, function_name = _split_modal_target(spec.target)
    stream_app_name, stream_function_name = _split_modal_target(spec.stream_target)
    if stream_app_name and app_name and stream_app_name != app_name:
        raise ValueError("modal stream_target must use the same app as target")
    return ModalAdapter(
        name=spec.name,
        app_name=app_name,
        function_name=function_name,
        stream_function_name=stream_function_name,
        model=spec.model,
        max_model_len=spec.max_model_len,
        allow_ephemeral=False,
    )


@register_adapter("runpod-serverless", aliases=("runpod",))
def _build_runpod_serverless(spec: ProviderSpec, credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    _require_contract(spec, endpoint="runpod-serverless", output="gpucall-provider-result", stream="none")
    runpod = credentials.get("runpod", {})
    return RunpodServerlessAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
    )


@register_adapter("runpod-vllm-serverless")
def _build_runpod_vllm_serverless(spec: ProviderSpec, credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    _require_contract(spec, endpoint="openai-chat-completions", output="openai-chat-completions", stream="none")
    runpod = credentials.get("runpod", {})
    return RunpodVllmServerlessAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
        image=spec.image,
        endpoint_contract=spec.endpoint_contract,
    )


@register_adapter("runpod-vllm-flashboot")
def _build_runpod_vllm_flashboot(spec: ProviderSpec, credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    _require_contract(spec, endpoint="openai-chat-completions", output="openai-chat-completions", stream="none")
    runpod = credentials.get("runpod", {})
    return RunpodVllmFlashBootAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
        image=spec.image,
        endpoint_contract=spec.endpoint_contract,
    )


@register_adapter("runpod-flash", aliases=("flash",))
def _build_runpod_flash(spec: ProviderSpec, credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    _require_contract(spec, endpoint="openai-chat-completions", output="openai-chat-completions", stream="none")
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


@register_adapter("hyperstack")
def _build_hyperstack(spec: ProviderSpec, credentials: dict[str, dict[str, str]]) -> ProviderAdapter:
    _require_contract(spec, endpoint="hyperstack-vm", output="plain-text", stream="none")
    missing = [
        field
        for field, value in {
            "target": spec.target,
            "instance": spec.instance,
            "image": spec.image,
            "key_name": spec.key_name,
            "model": spec.model,
            "max_model_len": spec.max_model_len,
        }.items()
        if value in {None, ""}
    ]
    if missing:
        raise ValueError(f"hyperstack provider requires explicit fields: {', '.join(missing)}")
    hyperstack = credentials.get("hyperstack", {})
    return HyperstackAdapter(
        name=spec.name,
        api_key=hyperstack.get("api_key"),
        base_url=str(spec.endpoint) if spec.endpoint else None,
        ssh_key_path=hyperstack.get("ssh_key_path"),
        environment_name=str(spec.target),
        flavor_name=str(spec.instance),
        image_name=str(spec.image),
        key_name=str(spec.key_name),
        ssh_remote_cidr=spec.ssh_remote_cidr,
        lease_manifest_path=spec.lease_manifest_path,
        model=spec.model,
        max_model_len=spec.max_model_len,
    )


def _require_contract(spec: ProviderSpec, *, endpoint: str, output: str, stream: str) -> None:
    expected = {
        "endpoint_contract": endpoint,
        "output_contract": output,
        "stream_contract": stream,
    }
    mismatches = [f"{field}={getattr(spec, field)!r} expected {value!r}" for field, value in expected.items() if getattr(spec, field) != value]
    if mismatches:
        raise ValueError(f"provider {spec.name!r} contract mismatch: " + "; ".join(mismatches))
