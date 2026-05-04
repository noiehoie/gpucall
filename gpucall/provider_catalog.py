from __future__ import annotations

from typing import Any

from gpucall.domain import ProviderSpec
from gpucall.providers.hyperstack_adapter import HYPERSTACK_API_BASE, region_from_hyperstack_environment


def live_provider_catalog_findings(providers: dict[str, ProviderSpec], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    hyperstack_key = credentials.get("hyperstack", {}).get("api_key")
    hyperstack_providers = [provider for provider in providers.values() if provider.adapter == "hyperstack"]
    if hyperstack_providers and not hyperstack_key:
        for provider in hyperstack_providers:
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": provider.adapter,
                    "severity": "error",
                    "reason": "missing Hyperstack API key; cannot verify official provider catalog",
                }
            )
    if hyperstack_providers and hyperstack_key:
        findings.extend(_hyperstack_catalog_findings(hyperstack_providers, hyperstack_key))
    return findings


def _hyperstack_catalog_findings(providers: list[ProviderSpec], api_key: str) -> list[dict[str, Any]]:
    try:
        import requests
    except ImportError as exc:
        return [
            {
                "provider": provider.name,
                "adapter": "hyperstack",
                "severity": "error",
                "reason": f"requests is unavailable; cannot verify official provider catalog: {exc}",
            }
            for provider in providers
        ]

    headers = {"api_key": api_key, "Accept": "application/json", "Content-Type": "application/json"}
    findings: list[dict[str, Any]] = []
    try:
        base_urls = {str(provider.endpoint or HYPERSTACK_API_BASE).rstrip("/") for provider in providers}
        if len(base_urls) != 1:
            raise ValueError("all Hyperstack providers must use the same official endpoint during catalog validation")
        base_url = next(iter(base_urls))
        images = _hyperstack_region_images(requests.get(f"{base_url}/core/images", headers=headers, timeout=20).json())
        flavors = _hyperstack_region_flavors(requests.get(f"{base_url}/core/flavors", headers=headers, timeout=20).json())
        environments = _hyperstack_environment_names(
            requests.get(f"{base_url}/core/environments", headers=headers, timeout=20).json()
        )
    except Exception as exc:
        return [
            {
                "provider": provider.name,
                "adapter": "hyperstack",
                "severity": "error",
                "reason": f"official Hyperstack catalog lookup failed: {exc}",
            }
            for provider in providers
        ]

    for provider in providers:
        region = region_from_hyperstack_environment(provider.target or "")
        if provider.target not in environments:
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": "hyperstack",
                    "severity": "error",
                    "field": "target",
                    "configured": provider.target,
                    "reason": "environment_name is not present in official Hyperstack /core/environments catalog",
                }
            )
        if provider.image not in images.get(region, set()):
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": "hyperstack",
                    "severity": "error",
                    "field": "image",
                    "configured": provider.image,
                    "region": region,
                    "reason": "image_name is not present in official Hyperstack /core/images catalog for provider region",
                }
            )
        if provider.instance not in flavors.get(region, set()):
            findings.append(
                {
                    "provider": provider.name,
                    "adapter": "hyperstack",
                    "severity": "error",
                    "field": "instance",
                    "configured": provider.instance,
                    "region": region,
                    "reason": "flavor_name is not present in official Hyperstack /core/flavors catalog for provider region",
                }
            )
    return findings


def _hyperstack_region_images(payload: dict[str, Any]) -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for group in payload.get("images") or []:
        if not isinstance(group, dict):
            continue
        region = str(group.get("region_name") or "")
        if not region:
            continue
        rows.setdefault(region, set())
        for image in group.get("images") or []:
            if isinstance(image, dict) and image.get("name"):
                rows[region].add(str(image["name"]))
    return rows


def _hyperstack_region_flavors(payload: dict[str, Any]) -> dict[str, set[str]]:
    rows: dict[str, set[str]] = {}
    for group in payload.get("data") or []:
        if not isinstance(group, dict):
            continue
        region = str(group.get("region_name") or "")
        if not region:
            continue
        rows.setdefault(region, set())
        for flavor in group.get("flavors") or []:
            if isinstance(flavor, dict) and flavor.get("name"):
                rows[region].add(str(flavor["name"]))
    return rows


def _hyperstack_environment_names(payload: dict[str, Any]) -> set[str]:
    return {str(row["name"]) for row in payload.get("environments") or [] if isinstance(row, dict) and row.get("name")}
