from __future__ import annotations

from typing import Any

from gpucall.domain import ProviderSpec
from gpucall.execution.registry import adapter_descriptor


def live_provider_catalog_findings(providers: dict[str, ProviderSpec], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[ProviderSpec]] = {}
    for provider in providers.values():
        grouped.setdefault(provider.adapter.strip().lower(), []).append(provider)

    findings: list[dict[str, Any]] = []
    for providers_for_adapter in grouped.values():
        descriptor = adapter_descriptor(providers_for_adapter[0])
        if descriptor is None or descriptor.catalog_validator is None:
            continue
        findings.extend(descriptor.catalog_validator(providers_for_adapter, credentials))
    return findings
