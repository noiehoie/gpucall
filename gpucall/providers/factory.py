from __future__ import annotations

from gpucall.credentials import load_credentials
from gpucall.domain import ProviderSpec
from gpucall.providers.base import ProviderAdapter
from gpucall.providers.registry import build_registered_adapter


def build_adapters(providers: dict[str, ProviderSpec]) -> dict[str, ProviderAdapter]:
    credentials = load_credentials()
    return {name: build_adapter(spec, credentials) for name, spec in providers.items()}


def build_adapter(spec: ProviderSpec, credentials: dict[str, dict[str, str]] | None = None) -> ProviderAdapter:
    return build_registered_adapter(spec, credentials)
