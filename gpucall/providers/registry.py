from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from gpucall.domain import ExecutionSurface, ProviderSpec
from gpucall.plugin_loader import load_entry_point_group
from gpucall.providers.base import ProviderAdapter

AdapterFactory = Callable[[ProviderSpec, dict[str, dict[str, str]]], ProviderAdapter]
ConfigValidator = Callable[[ProviderSpec], list[str]]
CatalogValidator = Callable[[list[ProviderSpec], dict[str, dict[str, str]]], list[dict[str, Any]]]


@dataclass(frozen=True)
class ProviderAdapterDescriptor:
    execution_surface: ExecutionSurface | None = None
    endpoint_contract: str | None = None
    output_contract: str | None = None
    stream_contract: str | None = "none"
    requires_contracts: bool = True
    production_eligible: bool = True
    production_rejection_reason: str | None = None
    local_execution: bool = False
    requires_model_for_auto: bool = True
    required_auto_fields: dict[str, str] = field(default_factory=dict)
    stream_required_fields: dict[str, str] = field(default_factory=dict)
    config_validator: ConfigValidator | None = None
    catalog_validator: CatalogValidator | None = None
    official_sources: tuple[str, ...] = ()

_ADAPTER_FACTORIES: dict[str, AdapterFactory] = {}
_ALIASES: dict[str, str] = {}
_DESCRIPTORS: dict[str, ProviderAdapterDescriptor] = {}
_BUILTINS_LOADED = False
_DEFAULT_EXECUTION_SURFACES = {
    "azure-compute-vm": ExecutionSurface.IAAS_VM,
    "echo": ExecutionSurface.LOCAL_RUNTIME,
    "gcp-confidential-space-vm": ExecutionSurface.IAAS_VM,
    "hyperstack": ExecutionSurface.IAAS_VM,
    "local-ollama": ExecutionSurface.LOCAL_RUNTIME,
    "modal": ExecutionSurface.FUNCTION_RUNTIME,
    "ovhcloud-public-cloud-instance": ExecutionSurface.IAAS_VM,
    "runpod-flash": ExecutionSurface.MANAGED_ENDPOINT,
    "runpod-serverless": ExecutionSurface.MANAGED_ENDPOINT,
    "runpod-vllm-flashboot": ExecutionSurface.FUNCTION_RUNTIME,
    "runpod-vllm-serverless": ExecutionSurface.MANAGED_ENDPOINT,
    "scaleway-instance": ExecutionSurface.IAAS_VM,
}


def register_adapter(
    *names: str,
    aliases: tuple[str, ...] = (),
    descriptor: ProviderAdapterDescriptor | None = None,
) -> Callable[[AdapterFactory], AdapterFactory]:
    canonical = names[0] if names else None
    if not canonical:
        raise ValueError("at least one adapter name is required")

    def decorator(factory: AdapterFactory) -> AdapterFactory:
        for name in names:
            normalized = _normalize(name)
            _ADAPTER_FACTORIES[normalized] = factory
            normalized_descriptor = _descriptor_for(normalized, descriptor)
            if normalized_descriptor is not None:
                _DESCRIPTORS[normalized] = normalized_descriptor
        for alias in aliases:
            normalized_alias = _normalize(alias)
            normalized_canonical = _normalize(canonical)
            _ALIASES[normalized_alias] = normalized_canonical
            normalized_descriptor = _descriptor_for(normalized_canonical, descriptor)
            if normalized_descriptor is not None:
                _DESCRIPTORS[normalized_alias] = normalized_descriptor
        return factory

    return decorator


def ensure_builtin_adapters_loaded() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    import gpucall.providers.azure_compute_vm_adapter  # noqa: F401
    import gpucall.providers.echo  # noqa: F401
    import gpucall.providers.gcp_confidential_space_adapter  # noqa: F401
    import gpucall.providers.hyperstack_adapter  # noqa: F401
    import gpucall.providers.local_adapter  # noqa: F401
    import gpucall.providers.modal_adapter  # noqa: F401
    import gpucall.providers.ovhcloud_public_cloud_adapter  # noqa: F401
    import gpucall.providers.runpod_flash_adapter  # noqa: F401
    import gpucall.providers.runpod_flashboot_adapter  # noqa: F401
    import gpucall.providers.runpod_serverless_adapter  # noqa: F401
    import gpucall.providers.runpod_vllm_adapter  # noqa: F401
    import gpucall.providers.scaleway_instance_adapter  # noqa: F401


def build_registered_adapter(
    spec: ProviderSpec,
    credentials: dict[str, dict[str, str]] | None = None,
) -> ProviderAdapter:
    ensure_builtin_adapters_loaded()
    load_entry_point_group("gpucall.adapters")
    credentials = credentials or {}
    key = _normalize(spec.adapter)
    key = _ALIASES.get(key, key)
    factory = _ADAPTER_FACTORIES.get(key)
    if factory is None:
        known = ", ".join(sorted(_ADAPTER_FACTORIES))
        raise ValueError(f"unknown provider adapter: {spec.adapter} (known: {known})")
    return factory(spec, credentials)


def registered_adapter_names() -> list[str]:
    ensure_builtin_adapters_loaded()
    load_entry_point_group("gpucall.adapters")
    return sorted(_ADAPTER_FACTORIES)


def adapter_descriptor(spec_or_adapter: ProviderSpec | str) -> ProviderAdapterDescriptor | None:
    ensure_builtin_adapters_loaded()
    load_entry_point_group("gpucall.adapters")
    adapter = spec_or_adapter.adapter if isinstance(spec_or_adapter, ProviderSpec) else spec_or_adapter
    key = _normalize(adapter)
    key = _ALIASES.get(key, key)
    return _DESCRIPTORS.get(key)


def registered_adapter_descriptors() -> dict[str, ProviderAdapterDescriptor]:
    ensure_builtin_adapters_loaded()
    load_entry_point_group("gpucall.adapters")
    return dict(sorted(_DESCRIPTORS.items()))


def _normalize(value: str) -> str:
    return value.strip().lower()


def _descriptor_for(adapter: str, descriptor: ProviderAdapterDescriptor | None) -> ProviderAdapterDescriptor | None:
    default_surface = _DEFAULT_EXECUTION_SURFACES.get(adapter)
    if descriptor is None:
        if default_surface is None:
            return None
        return ProviderAdapterDescriptor(execution_surface=default_surface)
    if descriptor.execution_surface is None and default_surface is not None:
        return replace(descriptor, execution_surface=default_surface)
    return descriptor
