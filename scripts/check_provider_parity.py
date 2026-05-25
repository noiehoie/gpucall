#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import yaml

from gpucall.credential_registry import configured_probes, env_overrides
import gpucall.credential_targets  # noqa: F401 - registers built-in credential contracts
from gpucall.execution.registry import adapter_descriptor, registered_adapter_names, vendor_family_for_adapter
from gpucall.panopticon_provisioning import (
    SUPPORTED_SUPPLY_PROVISIONING_PROVIDERS,
    UNSUPPORTED_SUPPLY_PROVISIONING_PROVIDERS,
)
from gpucall.panopticon_remediation import (
    SUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS,
    UNSUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS,
)
from gpucall.provider_contracts import CLOUD_PROVIDER_FAMILIES, PROVIDER_SETUP_CONTRACTS


ROOT = Path(__file__).resolve().parents[1]
REQUIRED = set(CLOUD_PROVIDER_FAMILIES)


def main() -> int:
    errors: list[str] = []
    report: dict[str, Any] = {"providers": {}, "required": sorted(REQUIRED)}

    _check_setup_contracts(errors, report)
    _check_accounts(errors, report)
    _check_candidate_sources(errors, report)
    _check_surface_catalog(errors, report)
    _check_adapter_registry(errors, report)
    _check_credential_registry(errors, report)
    _check_control_plane_boundaries(errors, report)

    if errors:
        print(json.dumps({"ok": False, "errors": errors, "report": report}, indent=2, sort_keys=True))
        return 1
    print(json.dumps({"ok": True, "report": report}, indent=2, sort_keys=True))
    print("provider parity guard ok")
    return 0


def _check_setup_contracts(errors: list[str], report: dict[str, Any]) -> None:
    contract_names = set(PROVIDER_SETUP_CONTRACTS)
    if contract_names != REQUIRED:
        errors.append(f"setup provider contracts mismatch: expected={sorted(REQUIRED)} actual={sorted(contract_names)}")
    for name, contract in PROVIDER_SETUP_CONTRACTS.items():
        _provider(report, name)["setup_contract"] = {
            "display_name": contract.display_name,
            "credential_probe_sets": [sorted(probes) for probes in contract.credential_probe_sets],
            "gpucall_credentials_required": sorted(contract.gpucall_credentials_required),
            "official_cli": contract.official_cli,
            "endpoint_id_supported": contract.endpoint_id_supported,
        }
        if contract.family != name:
            errors.append(f"{name}: setup contract family field is {contract.family!r}")
        if not contract.credential_probe_sets:
            errors.append(f"{name}: setup contract has no credential probe set")


def _check_accounts(errors: list[str], report: dict[str, Any]) -> None:
    for base in ("config/accounts", "gpucall/config_templates/accounts"):
        for provider in REQUIRED:
            path = ROOT / base / f"{provider}.yml"
            payload = _yaml(path, errors)
            _provider(report, provider).setdefault("accounts", {})[base] = path.exists()
            if not payload:
                continue
            if payload.get("provider_family") != provider:
                errors.append(f"{path}: provider_family must be {provider!r}")
            if payload.get("credential_ref") != provider:
                errors.append(f"{path}: credential_ref must be {provider!r}")


def _check_candidate_sources(errors: list[str], report: dict[str, Any]) -> None:
    for base in ("config/candidate_sources", "gpucall/config_templates/candidate_sources"):
        families = _families_from_yamls(ROOT / base, errors)
        report.setdefault("candidate_sources", {})[base] = sorted(families)
        missing = REQUIRED.difference(families)
        if missing:
            errors.append(f"{base}: missing candidate source families: {sorted(missing)}")


def _check_surface_catalog(errors: list[str], report: dict[str, Any]) -> None:
    for base in ("config/surfaces", "gpucall/config_templates/surfaces"):
        families = _families_from_yamls(ROOT / base, errors)
        report.setdefault("surface_catalog", {})[base] = sorted(families)
        missing = REQUIRED.difference(families)
        if missing:
            errors.append(f"{base}: missing surface families: {sorted(missing)}")


def _check_adapter_registry(errors: list[str], report: dict[str, Any]) -> None:
    adapters_by_family: dict[str, list[str]] = {provider: [] for provider in REQUIRED}
    adapters_without_sources: list[str] = []
    adapters_without_endpoint_contract: list[str] = []
    for adapter in registered_adapter_names():
        family = vendor_family_for_adapter(adapter)
        if family not in REQUIRED:
            continue
        adapters_by_family[family].append(adapter)
        descriptor = adapter_descriptor(adapter)
        if descriptor is None or not descriptor.official_sources:
            adapters_without_sources.append(adapter)
        if descriptor is None or not descriptor.endpoint_contract:
            adapters_without_endpoint_contract.append(adapter)
    report["adapter_registry"] = {name: sorted(adapters) for name, adapters in adapters_by_family.items()}
    for provider, adapters in adapters_by_family.items():
        if not adapters:
            errors.append(f"{provider}: no registered adapter")
    if adapters_without_sources:
        errors.append(f"provider adapters missing official_sources: {sorted(adapters_without_sources)}")
    if adapters_without_endpoint_contract:
        errors.append(f"provider adapters missing endpoint_contract: {sorted(adapters_without_endpoint_contract)}")


def _check_credential_registry(errors: list[str], report: dict[str, Any]) -> None:
    probe_names = {probe.contract for probe in configured_probes()}
    env_providers = {override.provider for override in env_overrides()}
    report["credential_registry"] = {
        "probe_count": len(probe_names),
        "env_override_providers": sorted(provider for provider in env_providers if provider in REQUIRED),
    }
    for provider, contract in PROVIDER_SETUP_CONTRACTS.items():
        required_probes = set().union(*contract.credential_probe_sets, contract.gpucall_credentials_required)
        missing_probes = sorted(required_probes.difference(probe_names))
        if missing_probes:
            errors.append(f"{provider}: missing credential probes: {missing_probes}")
        if provider not in env_providers:
            errors.append(f"{provider}: missing credential env override")


def _check_control_plane_boundaries(errors: list[str], report: dict[str, Any]) -> None:
    supply_supported = set(SUPPORTED_SUPPLY_PROVISIONING_PROVIDERS)
    supply_unsupported = set(UNSUPPORTED_SUPPLY_PROVISIONING_PROVIDERS)
    remediation_supported = set(SUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS)
    remediation_unsupported = set(UNSUPPORTED_PROVIDER_MUTATION_REMEDIATION_PROVIDERS)
    report["control_plane_boundaries"] = {
        "supply_provisioning_supported": sorted(supply_supported),
        "supply_provisioning_unsupported_explicit": sorted(supply_unsupported),
        "provider_mutation_remediation_supported": sorted(remediation_supported),
        "provider_mutation_remediation_unsupported_explicit": sorted(remediation_unsupported),
    }
    if supply_supported | supply_unsupported != REQUIRED:
        errors.append("provider supply provisioning support matrix does not cover all cloud provider families")
    if remediation_supported | remediation_unsupported != REQUIRED:
        errors.append("provider mutation remediation support matrix does not cover all cloud provider families")
    if not supply_supported.issubset(REQUIRED):
        errors.append(f"unknown supply provisioning providers: {sorted(supply_supported.difference(REQUIRED))}")
    if not remediation_supported.issubset(REQUIRED):
        errors.append(f"unknown provider mutation remediation providers: {sorted(remediation_supported.difference(REQUIRED))}")


def _families_from_yamls(directory: Path, errors: list[str]) -> set[str]:
    families: set[str] = set()
    if not directory.exists():
        errors.append(f"{directory}: missing")
        return families
    for path in sorted(directory.glob("*.yml")):
        payload = _yaml(path, errors)
        families.update(_families_from_payload(payload))
    return families


def _families_from_payload(value: Any) -> set[str]:
    families: set[str] = set()
    if isinstance(value, dict):
        adapter = value.get("adapter")
        provider_family = value.get("provider_family")
        if isinstance(adapter, str):
            families.add(vendor_family_for_adapter(adapter))
        if isinstance(provider_family, str):
            families.add(provider_family)
        for child in value.values():
            families.update(_families_from_payload(child))
    elif isinstance(value, list):
        for child in value:
            families.update(_families_from_payload(child))
    return families


def _provider(report: dict[str, Any], name: str) -> dict[str, Any]:
    return report["providers"].setdefault(name, {})


def _yaml(path: Path, errors: list[str]) -> Any:
    if not path.exists():
        errors.append(f"{path}: missing")
        return None
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        errors.append(f"{path}: invalid YAML: {exc}")
        return None


if __name__ == "__main__":
    raise SystemExit(main())
