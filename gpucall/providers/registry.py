from __future__ import annotations

from collections.abc import Callable

from gpucall.domain import ProviderSpec
from gpucall.plugin_loader import load_entry_point_group
from gpucall.providers.base import ProviderAdapter

AdapterFactory = Callable[[ProviderSpec, dict[str, dict[str, str]]], ProviderAdapter]

_ADAPTER_FACTORIES: dict[str, AdapterFactory] = {}
_ALIASES: dict[str, str] = {}


def register_adapter(
    *names: str,
    aliases: tuple[str, ...] = (),
) -> Callable[[AdapterFactory], AdapterFactory]:
    canonical = names[0] if names else None
    if not canonical:
        raise ValueError("at least one adapter name is required")

    def decorator(factory: AdapterFactory) -> AdapterFactory:
        for name in names:
            _ADAPTER_FACTORIES[_normalize(name)] = factory
        for alias in aliases:
            _ALIASES[_normalize(alias)] = _normalize(canonical)
        return factory

    return decorator


def build_registered_adapter(
    spec: ProviderSpec,
    credentials: dict[str, dict[str, str]] | None = None,
) -> ProviderAdapter:
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
    load_entry_point_group("gpucall.adapters")
    return sorted(_ADAPTER_FACTORIES)


def _normalize(value: str) -> str:
    return value.strip().lower()
