from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from types import MappingProxyType
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.config import GpucallConfig, default_state_dir
from gpucall.domain import ExecutionMode, Recipe, recipe_requirements
from gpucall.execution.registry import adapter_descriptor, vendor_family_for_adapter
from gpucall.targeting import has_configured_endpoint_or_target, is_configured_target

NetworkTopologyValue = str | bool | None


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return MappingProxyType(dict(value or {}))


def _freeze_mapping_tuple(values: Any) -> tuple[Mapping[str, Any], ...]:
    return tuple(MappingProxyType(dict(item)) for item in (values or []))


class ProviderAccountSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    account_ref: str
    provider_family: str
    credential_ref: str
    billing_scope: str | None = None
    api_base: str | None = None


class GpuSkuSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sku_ref: str
    normalized_name: str
    vendor: str = "nvidia"
    vram_gb: int
    architecture: str | None = None
    memory_bandwidth_gbps: int | None = None
    source: Literal["builtin", "configured", "candidate"] = "configured"


class ExecutionSurfaceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    surface_ref: str
    execution_surface: str
    lifecycle_kind: str
    isolation_model: str
    cleanup_contract: str
    network_exposure: str
    cold_start_class: str


class ProviderOfferingSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    offering_ref: str
    source: Literal["active_tuple", "tuple_candidate"]
    account_ref: str
    provider_family: str
    resource_ref: str
    surface_ref: str
    gpu_sku_ref: str
    execution_surface: str
    provider_sku: str
    region: str | None = None
    zone: str | None = None
    network_topology: Mapping[str, NetworkTopologyValue] = Field(default_factory=lambda: MappingProxyType({}))

    @field_validator("network_topology")
    @classmethod
    def freeze_network_topology(cls, value: Mapping[str, NetworkTopologyValue]) -> Mapping[str, NetworkTopologyValue]:
        return MappingProxyType(dict(value))

    @field_serializer("network_topology")
    def serialize_network_topology(self, value: Mapping[str, NetworkTopologyValue]) -> dict[str, NetworkTopologyValue]:
        return dict(value)


class CapabilityClaimSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_ref: str
    resource_ref: str
    worker_ref: str
    model_ref: str | None = None
    engine_ref: str | None = None
    claim_source: Literal["configured_binding", "candidate_matrix"] = "configured_binding"
    required_input_contracts: tuple[str, ...] = Field(default_factory=tuple)
    output_contract: str | None = None
    max_model_len: int
    vram_gb: int
    security_tier: str = "shared_gpu"
    sovereign_jurisdiction: str | None = None
    dedicated_gpu: bool = False
    tee_boot_capable: bool = False
    requires_attestation: bool = False
    supports_key_release: bool = False
    attestation_type: str | None = None


class PricingRuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pricing_ref: str
    source: Literal["configured", "candidate", "live_overlay"] = "configured"
    account_ref: str
    resource_ref: str
    price_per_second: float
    configured_price_source: str | None = None
    configured_price_observed_at: str | None = None
    configured_price_ttl_seconds: float | None = None
    billing_granularity_seconds: float | None = None
    min_billable_seconds: float | None = None
    scaledown_window_seconds: float | None = None
    standing_cost_per_second: float | None = None
    endpoint_cost_per_second: float | None = None


class LiveStatusOverlaySpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    overlay_ref: str
    resource_ref: str
    checked: bool
    status: Literal["not_checked", "unknown", "live_revalidated", "blocked"]
    stock_state: Literal["available", "unavailable", "unknown"] = "unknown"
    price_per_second: float | None = None
    price_source: str | None = None
    dimensions: tuple[str, ...] = Field(default_factory=tuple)
    observed_at: str | None = None
    next_revalidate_after: str | None = None
    ttl_seconds: int
    finding_count: int = 0
    finding_hash: str | None = None


class ValidationEvidenceSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_ref: str
    tuple_name: str
    resource_ref: str | None = None
    worker_ref: str | None = None
    artifact_count: int = 0
    passed_count: int = 0
    failed_count: int = 0
    latest_passed: bool = False
    latest_artifact_hash: str | None = None
    observed_wall_seconds_latest: float | None = None
    observed_wall_seconds_p50: float | None = None
    observed_wall_seconds_p99: float | None = None
    observed_wall_seconds_max: float | None = None
    attestation_evidence_hash: str | None = None
    attestation_verified: bool = False
    expires_at: str | None = None


class ResourceCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    resource_ref: str
    source: Literal["active_tuple", "tuple_candidate"]
    account_ref: str
    tuple_name: str
    surface_ref: str
    worker_binding_ref: str
    adapter: str
    execution_surface: str
    gpu: str
    vram_gb: int
    max_model_len: int
    region: str | None = None
    zone: str | None = None
    configured_price_per_second: float
    price_per_second: float
    live_price_per_second: float | None = None
    live_price_source: str | None = None
    stock_state: Literal["configured", "candidate", "unknown"] = "unknown"
    live_stock_state: Literal["available", "unavailable", "unknown"] = "unknown"
    live_catalog_status: Literal["not_checked", "unknown", "live_revalidated", "blocked"] = "not_checked"
    live_catalog_checked: bool = False
    live_catalog_findings: tuple[Mapping[str, Any], ...] = Field(default_factory=tuple)
    validation_evidence: Mapping[str, Any] = Field(default_factory=lambda: MappingProxyType({}))

    @field_validator("live_catalog_findings")
    @classmethod
    def freeze_live_catalog_findings(cls, value: Any) -> tuple[Mapping[str, Any], ...]:
        return _freeze_mapping_tuple(value)

    @field_serializer("live_catalog_findings")
    def serialize_live_catalog_findings(self, value: tuple[Mapping[str, Any], ...]) -> list[dict[str, Any]]:
        return [dict(item) for item in value]

    @field_validator("validation_evidence")
    @classmethod
    def freeze_validation_evidence(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return _freeze_mapping(value)

    @field_serializer("validation_evidence")
    def serialize_validation_evidence(self, value: Mapping[str, Any]) -> dict[str, Any]:
        return dict(value)


class WorkerContractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker_ref: str
    source: Literal["active_tuple", "tuple_candidate"]
    tuple_name: str
    worker_binding_ref: str
    adapter: str
    execution_surface: str
    model_ref: str | None = None
    engine_ref: str | None = None
    modes: tuple[str, ...] = Field(default_factory=tuple)
    input_contracts: tuple[str, ...] = Field(default_factory=tuple)
    output_contract: str | None = None
    stream_contract: str | None = None
    target_configured: bool = False
    endpoint_configured: bool = False
    max_data_classification: str | None = None


class ResourceCatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    snapshot_id: str
    generated_at: str
    config_hash: str
    accounts: tuple[ProviderAccountSpec, ...]
    hardware_catalog: tuple[GpuSkuSpec, ...] = Field(default_factory=tuple)
    execution_surfaces: tuple[ExecutionSurfaceSpec, ...] = Field(default_factory=tuple)
    provider_offerings: tuple[ProviderOfferingSpec, ...] = Field(default_factory=tuple)
    capability_claims: tuple[CapabilityClaimSpec, ...] = Field(default_factory=tuple)
    pricing_rules: tuple[PricingRuleSpec, ...] = Field(default_factory=tuple)
    live_status_overlay: tuple[LiveStatusOverlaySpec, ...] = Field(default_factory=tuple)
    validation_evidence: tuple[ValidationEvidenceSpec, ...] = Field(default_factory=tuple)
    resources: tuple[ResourceCatalogEntry, ...]
    workers: tuple[WorkerContractSpec, ...]


class TupleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tuple_ref: str
    snapshot_id: str
    source: Literal["active_tuple", "tuple_candidate"]
    account_ref: str
    tuple_name: str
    adapter: str
    execution_surface: str
    resource_ref: str
    worker_ref: str
    gpu: str
    vram_gb: int
    max_model_len: int
    configured_price_per_second: float
    price_per_second: float
    live_price_per_second: float | None = None
    live_stock_state: Literal["available", "unavailable", "unknown"] = "unknown"
    model_ref: str | None = None
    engine_ref: str | None = None
    modes: tuple[str, ...] = Field(default_factory=tuple)
    production_state: Literal["production_configured", "candidate_draft"] = "candidate_draft"
    live_catalog_status: Literal["not_checked", "unknown", "live_revalidated", "blocked"] = "not_checked"
    snapshot_pinned: bool = True
    recipe_fit: Mapping[str, Any] | None = None

    @field_validator("recipe_fit")
    @classmethod
    def freeze_recipe_fit(cls, value: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
        if value is None:
            return None
        return _freeze_mapping(value)

    @field_serializer("recipe_fit")
    def serialize_recipe_fit(self, value: Mapping[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return dict(value)


def build_resource_catalog_snapshot(
    config: GpucallConfig,
    *,
    config_dir: Path | None = None,
    live_catalog_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResourceCatalogSnapshot:
    rows = [_active_tuple_payload(tuple) for tuple in sorted(config.tuples.values(), key=lambda item: item.name)]
    if config_dir is not None:
        rows.extend(_candidate_payloads(config_dir))
    validation_evidence = _tuple_validation_evidence(config_dir) if config_dir is not None else {}
    accounts = _accounts_for(rows)
    resources = [
        _resource_entry(row, live_catalog_evidence=live_catalog_evidence, validation_evidence=validation_evidence)
        for row in rows
    ]
    workers = [_worker_contract(row) for row in rows]
    hardware_catalog = _hardware_catalog(rows)
    execution_surfaces = _execution_surfaces(rows)
    provider_offerings = [_provider_offering(row) for row in rows]
    capability_claims = [_capability_claim(row) for row in rows]
    pricing_rules = [_pricing_rule(row) for row in rows]
    live_status_overlay = [_live_status_overlay(resource) for resource in resources]
    validation_rows = [_validation_evidence_row(resource) for resource in resources if resource.validation_evidence]
    config_hash = _config_hash(config=config, candidate_rows=rows)
    content = {
        "schema_version": 1,
        "config_hash": config_hash,
        "accounts": [account.model_dump(mode="json") for account in accounts],
        "hardware_catalog": [item.model_dump(mode="json") for item in hardware_catalog],
        "execution_surfaces": [item.model_dump(mode="json") for item in execution_surfaces],
        "provider_offerings": [item.model_dump(mode="json") for item in provider_offerings],
        "capability_claims": [item.model_dump(mode="json") for item in capability_claims],
        "pricing_rules": [item.model_dump(mode="json") for item in pricing_rules],
        "workers": [worker.model_dump(mode="json") for worker in workers],
    }
    snapshot_id = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return ResourceCatalogSnapshot(
        snapshot_id=snapshot_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        config_hash=config_hash,
        accounts=accounts,
        hardware_catalog=hardware_catalog,
        execution_surfaces=execution_surfaces,
        provider_offerings=provider_offerings,
        capability_claims=capability_claims,
        pricing_rules=pricing_rules,
        live_status_overlay=live_status_overlay,
        validation_evidence=validation_rows,
        resources=resources,
        workers=workers,
    )


def generate_tuple_candidates(snapshot: ResourceCatalogSnapshot, *, recipe: Recipe | None = None) -> list[TupleCandidate]:
    workers = {worker.worker_binding_ref: worker for worker in snapshot.workers}
    candidates: list[TupleCandidate] = []
    for resource in snapshot.resources:
        # Candidate generation is a surface/resource plus worker-contract join.
        # Tuple family is account metadata, not the routing unit.
        worker = workers.get(resource.worker_binding_ref)
        if worker is None:
            continue
        payload = {
            "snapshot_id": snapshot.snapshot_id,
            "source": resource.source,
            "account_ref": resource.account_ref,
            "tuple_name": resource.tuple_name,
            "adapter": resource.adapter,
            "execution_surface": resource.execution_surface,
            "resource_ref": resource.resource_ref,
            "worker_ref": worker.worker_ref,
            "gpu": resource.gpu,
            "vram_gb": resource.vram_gb,
            "max_model_len": resource.max_model_len,
            "configured_price_per_second": resource.configured_price_per_second,
            "price_per_second": resource.price_per_second,
            "live_price_per_second": resource.live_price_per_second,
            "live_stock_state": resource.live_stock_state,
            "live_catalog_status": resource.live_catalog_status,
            "model_ref": worker.model_ref,
            "engine_ref": worker.engine_ref,
            "modes": worker.modes,
            "production_state": "production_configured" if resource.source == "active_tuple" else "candidate_draft",
            "recipe_fit": _recipe_fit(resource, worker, recipe) if recipe is not None else None,
        }
        tuple_ref = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        candidates.append(TupleCandidate(tuple_ref=tuple_ref, **payload))
    return sorted(candidates, key=lambda item: (item.production_state, item.execution_surface, item.price_per_second, item.tuple_name))


def dumps_snapshot(snapshot: ResourceCatalogSnapshot) -> str:
    return json.dumps(snapshot.model_dump(mode="json"), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def dumps_candidates(candidates: list[TupleCandidate]) -> str:
    return json.dumps([item.model_dump(mode="json") for item in candidates], ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _active_tuple_payload(tuple: Any) -> dict[str, Any]:
    payload = tuple.model_dump(mode="json")
    payload["source"] = "active_tuple"
    return payload


def _candidate_payloads(config_dir: Path) -> list[dict[str, Any]]:
    payloads = load_tuple_candidate_payloads(config_dir)
    for payload in payloads:
        payload["source"] = "tuple_candidate"
    return payloads


def _accounts_for(rows: list[Mapping[str, Any]]) -> list[ProviderAccountSpec]:
    accounts: dict[str, ProviderAccountSpec] = {}
    for row in rows:
        adapter = str(row.get("adapter") or "")
        account_ref = _account_ref(row)
        accounts[account_ref] = ProviderAccountSpec(
            account_ref=account_ref,
            provider_family=_provider_family(adapter),
            credential_ref=_provider_family(adapter),
            billing_scope=str(row.get("project_id") or row.get("resource_group") or "") or None,
            api_base=str(row.get("endpoint") or "") or None,
        )
    return [accounts[key] for key in sorted(accounts)]


_GPU_SKU_BUILTINS: dict[str, dict[str, Any]] = {
    "a10g": {"normalized_name": "A10G", "vram_gb": 24, "architecture": "ampere", "memory_bandwidth_gbps": 600},
    "a100": {"normalized_name": "A100", "vram_gb": 80, "architecture": "ampere", "memory_bandwidth_gbps": 2039},
    "h100": {"normalized_name": "H100", "vram_gb": 80, "architecture": "hopper", "memory_bandwidth_gbps": 3350},
    "h200": {"normalized_name": "H200", "vram_gb": 141, "architecture": "hopper", "memory_bandwidth_gbps": 4800},
    "h200x4": {"normalized_name": "H200:4", "vram_gb": 564, "architecture": "hopper", "memory_bandwidth_gbps": 19200},
    "l4": {"normalized_name": "L4", "vram_gb": 24, "architecture": "ada", "memory_bandwidth_gbps": 300},
    "l40": {"normalized_name": "L40", "vram_gb": 48, "architecture": "ada", "memory_bandwidth_gbps": 864},
    "l40s": {"normalized_name": "L40S", "vram_gb": 48, "architecture": "ada", "memory_bandwidth_gbps": 864},
    "rtx_a4000": {"normalized_name": "RTX-A4000", "vram_gb": 16, "architecture": "ampere", "memory_bandwidth_gbps": 448},
    "rtx_a6000": {"normalized_name": "RTX-A6000", "vram_gb": 48, "architecture": "ampere", "memory_bandwidth_gbps": 768},
}


def _gpu_sku_ref(gpu: str) -> str:
    token = gpu.strip().lower().replace(":", "x")
    token = re.sub(r"[^a-z0-9]+", "_", token).strip("_")
    return f"gpu:{token or 'unknown'}"


def _builtin_gpu_sku(gpu: str) -> dict[str, Any]:
    token = _gpu_sku_ref(gpu).removeprefix("gpu:")
    if token in _GPU_SKU_BUILTINS:
        return dict(_GPU_SKU_BUILTINS[token])
    family_match = re.fullmatch(r"(ampere|ada|hopper)_(\d+)", token)
    if family_match:
        architecture, vram = family_match.groups()
        return {
            "normalized_name": gpu,
            "vram_gb": int(vram),
            "architecture": architecture,
            "memory_bandwidth_gbps": None,
        }
    return {}


def _hardware_catalog(rows: list[Mapping[str, Any]]) -> list[GpuSkuSpec]:
    specs: dict[str, GpuSkuSpec] = {}
    for row in rows:
        gpu = str(row.get("gpu") or "unknown")
        sku_ref = _gpu_sku_ref(gpu)
        builtin = _builtin_gpu_sku(gpu)
        specs[sku_ref] = GpuSkuSpec(
            sku_ref=sku_ref,
            normalized_name=builtin.get("normalized_name") or gpu,
            vram_gb=_positive_int(row.get("vram_gb") or builtin.get("vram_gb"), default=1),
            architecture=builtin.get("architecture"),
            memory_bandwidth_gbps=builtin.get("memory_bandwidth_gbps"),
            source="builtin" if builtin else ("candidate" if row.get("source") == "tuple_candidate" else "configured"),
        )
    return [specs[key] for key in sorted(specs)]


def _execution_surfaces(rows: list[Mapping[str, Any]]) -> list[ExecutionSurfaceSpec]:
    surfaces: dict[str, ExecutionSurfaceSpec] = {}
    for row in rows:
        surface = str(row.get("execution_surface") or _surface_for_adapter(str(row.get("adapter") or "")) or "unknown")
        surfaces[surface] = ExecutionSurfaceSpec(
            surface_ref=surface,
            execution_surface=surface,
            lifecycle_kind=_surface_lifecycle_kind(surface),
            isolation_model=_surface_isolation_model(surface),
            cleanup_contract=_surface_cleanup_contract(surface),
            network_exposure=_network_exposure(row),
            cold_start_class=_surface_cold_start_class(surface),
        )
    return [surfaces[key] for key in sorted(surfaces)]


def _surface_lifecycle_kind(surface: str) -> str:
    return {
        "iaas_vm": "leased_vm",
        "managed_endpoint": "standing_or_scale_to_zero_endpoint",
        "function_runtime": "scale_to_zero_function",
        "container_instance": "leased_container",
        "cluster_runtime": "leased_cluster",
        "local_runtime": "local_process",
        "lifecycle_only": "lifecycle_control",
    }.get(surface, "unknown")


def _surface_isolation_model(surface: str) -> str:
    return {
        "iaas_vm": "vm",
        "managed_endpoint": "provider_container_endpoint",
        "function_runtime": "provider_container_function",
        "container_instance": "container",
        "cluster_runtime": "kubernetes",
        "local_runtime": "local_process",
        "lifecycle_only": "control_plane",
    }.get(surface, "unknown")


def _surface_cleanup_contract(surface: str) -> str:
    return {
        "iaas_vm": "resource_lease_destroy_required",
        "managed_endpoint": "provider_endpoint_scale_or_none",
        "function_runtime": "scale_to_zero_no_per_request_cleanup",
        "container_instance": "resource_lease_destroy_required",
        "cluster_runtime": "cluster_lease_destroy_required",
        "local_runtime": "none",
        "lifecycle_only": "provider_resource_manager",
    }.get(surface, "unknown")


def _network_exposure(row: Mapping[str, Any]) -> str:
    surface = str(row.get("execution_surface") or _surface_for_adapter(str(row.get("adapter") or "")) or "")
    if surface == "local_runtime":
        return "local_only"
    if row.get("ssh_remote_cidr"):
        return "public_ssh_restricted"
    if row.get("endpoint") or row.get("target"):
        return "provider_public_endpoint"
    if surface == "managed_endpoint":
        return "provider_public_endpoint"
    if surface == "function_runtime":
        return "provider_sdk_control_plane"
    if surface == "cluster_runtime":
        return "cluster_network"
    return "unknown"


def _surface_cold_start_class(surface: str) -> str:
    return {
        "iaas_vm": "vm_boot_and_worker_bootstrap",
        "managed_endpoint": "endpoint_worker_cold_start",
        "function_runtime": "function_container_cold_start",
        "container_instance": "container_boot",
        "cluster_runtime": "cluster_job_scheduling",
        "local_runtime": "none",
        "lifecycle_only": "none",
    }.get(surface, "unknown")


def _provider_offering(row: Mapping[str, Any]) -> ProviderOfferingSpec:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    adapter = str(row.get("adapter") or "")
    resource_ref = f"{source}:{name}:resource"
    return ProviderOfferingSpec(
        offering_ref=f"{source}:{name}:offering",
        source=source,
        account_ref=_account_ref(row),
        provider_family=_provider_family(adapter),
        resource_ref=resource_ref,
        surface_ref=str(row.get("surface_ref") or name),
        gpu_sku_ref=_gpu_sku_ref(str(row.get("gpu") or "unknown")),
        execution_surface=str(row.get("execution_surface") or _surface_for_adapter(adapter) or "unknown"),
        provider_sku=str(row.get("instance") or row.get("gpu") or "unknown"),
        region=str(row.get("region") or "") or None,
        zone=str(row.get("zone") or "") or None,
        network_topology=_network_topology(row),
    )


def _network_topology(row: Mapping[str, Any]) -> dict[str, NetworkTopologyValue]:
    surface = str(row.get("execution_surface") or _surface_for_adapter(str(row.get("adapter") or "")) or "unknown")
    endpoint_or_target = has_configured_endpoint_or_target(row.get("endpoint"), row.get("target"))
    topology: dict[str, NetworkTopologyValue] = {
        "surface": surface,
        "public_network_required": bool(endpoint_or_target or row.get("ssh_remote_cidr")),
    }
    for key in (
        "region",
        "zone",
        "network",
        "subnet",
        "ssh_remote_cidr",
        "service_account",
        "security_group",
        "vpc",
    ):
        value = row.get(key)
        if value not in (None, ""):
            topology[key] = str(value)
    if endpoint_or_target:
        topology["endpoint_configured"] = True
    return topology


def _capability_claim(row: Mapping[str, Any]) -> CapabilityClaimSpec:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    trust = _trust_profile(row)
    security_tier = str(trust.get("security_tier") or "shared_gpu")
    requires_attestation = bool(trust.get("requires_attestation"))
    return CapabilityClaimSpec(
        claim_ref=f"{source}:{name}:claim",
        resource_ref=f"{source}:{name}:resource",
        worker_ref=f"{source}:{name}:worker",
        model_ref=str(row.get("model_ref") or "") or None,
        engine_ref=str(row.get("engine_ref") or "") or None,
        claim_source="candidate_matrix" if source == "tuple_candidate" else "configured_binding",
        required_input_contracts=[str(item) for item in row.get("input_contracts") or []],
        output_contract=str(row.get("output_contract") or "") or None,
        max_model_len=_positive_int(row.get("max_model_len"), default=1),
        vram_gb=_positive_int(row.get("vram_gb"), default=1),
        security_tier=security_tier,
        sovereign_jurisdiction=str(trust.get("sovereign_jurisdiction") or "") or None,
        dedicated_gpu=bool(trust.get("dedicated_gpu")),
        tee_boot_capable=security_tier == "confidential_tee" or bool(trust.get("attestation_type")),
        requires_attestation=requires_attestation,
        supports_key_release=bool(trust.get("supports_key_release")),
        attestation_type=str(trust.get("attestation_type") or "") or None,
    )


def _pricing_rule(row: Mapping[str, Any]) -> PricingRuleSpec:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    return PricingRuleSpec(
        pricing_ref=f"{source}:{name}:configured-price",
        source="candidate" if source == "tuple_candidate" else "configured",
        account_ref=_account_ref(row),
        resource_ref=f"{source}:{name}:resource",
        price_per_second=float(row.get("cost_per_second") or 0.0),
        configured_price_source=str(row.get("configured_price_source") or row.get("pricing_source") or "") or None,
        configured_price_observed_at=str(row.get("configured_price_observed_at") or "") or None,
        configured_price_ttl_seconds=_optional_float(row.get("configured_price_ttl_seconds")),
        billing_granularity_seconds=_optional_float(row.get("billing_granularity_seconds")),
        min_billable_seconds=_optional_float(row.get("min_billable_seconds")),
        scaledown_window_seconds=_optional_float(row.get("scaledown_window_seconds")),
        standing_cost_per_second=_optional_float(row.get("standing_cost_per_second")),
        endpoint_cost_per_second=_optional_float(row.get("endpoint_cost_per_second")),
    )


def _live_status_overlay(resource: ResourceCatalogEntry) -> LiveStatusOverlaySpec:
    findings = resource.live_catalog_findings
    dimensions = tuple(sorted({str(item.get("dimension")) for item in findings if item.get("dimension")}))
    observed_at = datetime.now(timezone.utc).isoformat() if resource.live_catalog_checked else None
    ttl_seconds = _overlay_ttl_seconds(dimensions)
    return LiveStatusOverlaySpec(
        overlay_ref=f"{resource.resource_ref}:live-overlay",
        resource_ref=resource.resource_ref,
        checked=resource.live_catalog_checked,
        status=resource.live_catalog_status,
        stock_state=resource.live_stock_state,
        price_per_second=resource.live_price_per_second,
        price_source=resource.live_price_source,
        dimensions=dimensions,
        observed_at=observed_at,
        next_revalidate_after=_next_revalidate_after(observed_at, ttl_seconds),
        ttl_seconds=ttl_seconds,
        finding_count=len(findings),
        finding_hash=_stable_hash([dict(item) for item in findings]) if findings else None,
    )


def _validation_evidence_row(resource: ResourceCatalogEntry) -> ValidationEvidenceSpec:
    evidence = resource.validation_evidence
    latest_path = str(evidence.get("latest_path") or "")
    return ValidationEvidenceSpec(
        evidence_ref=f"{resource.resource_ref}:validation",
        tuple_name=resource.tuple_name,
        resource_ref=resource.resource_ref,
        worker_ref=f"{resource.source}:{resource.tuple_name}:worker",
        artifact_count=int(evidence.get("artifact_count") or 0),
        passed_count=int(evidence.get("passed_count") or 0),
        failed_count=int(evidence.get("failed_count") or 0),
        latest_passed=bool(evidence.get("latest_passed")),
        latest_artifact_hash=_file_sha256(Path(latest_path)) if latest_path else None,
        observed_wall_seconds_latest=_optional_float(evidence.get("observed_wall_seconds_latest")),
        observed_wall_seconds_p50=_optional_float(evidence.get("observed_wall_seconds_p50")),
        observed_wall_seconds_p99=_optional_float(evidence.get("observed_wall_seconds_p99")),
        observed_wall_seconds_max=_optional_float(evidence.get("observed_wall_seconds_max")),
        attestation_evidence_hash=str(evidence.get("attestation_evidence_hash") or "") or None,
        attestation_verified=bool(evidence.get("attestation_verified")),
        expires_at=_evidence_expires_at(),
    )


def _optional_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _overlay_ttl_seconds(dimensions: tuple[str, ...]) -> int:
    if not dimensions:
        return 0
    ttl_by_dimension = {
        "stock": 60,
        "endpoint": 60,
        "capacity": 60,
        "health": 60,
        "credential": 300,
        "price": 3600,
        "contract": 86400,
    }
    return min(ttl_by_dimension.get(dimension, 300) for dimension in dimensions)


def _next_revalidate_after(observed_at: str | None, ttl_seconds: int) -> str | None:
    if observed_at is None or ttl_seconds <= 0:
        return None
    try:
        observed = datetime.fromisoformat(observed_at)
    except ValueError:
        return None
    return (observed + timedelta(seconds=ttl_seconds)).isoformat()


def _stable_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_sha256(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _evidence_expires_at() -> str:
    return (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()


def _trust_profile(row: Mapping[str, Any]) -> dict[str, Any]:
    value = row.get("trust_profile")
    if isinstance(value, Mapping):
        return dict(value)
    return {
        "security_tier": row.get("security_tier"),
        "sovereign_jurisdiction": row.get("sovereign_jurisdiction"),
        "dedicated_gpu": row.get("dedicated_gpu"),
        "requires_attestation": row.get("requires_attestation"),
        "supports_key_release": row.get("supports_key_release"),
        "attestation_type": row.get("attestation_type"),
    }


def _resource_entry(
    row: Mapping[str, Any],
    *,
    live_catalog_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    validation_evidence: Mapping[str, Mapping[str, Any]] | None = None,
) -> ResourceCatalogEntry:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    adapter = str(row.get("adapter") or "")
    evidence = dict((live_catalog_evidence or {}).get(name) or {})
    status = str(evidence.get("status") or ("not_checked" if live_catalog_evidence is None else "unknown"))
    if status not in {"not_checked", "unknown", "live_revalidated", "blocked"}:
        status = "unknown"
    findings = [dict(item) for item in evidence.get("findings") or [] if isinstance(item, Mapping)]
    configured_price = float(row.get("cost_per_second") or 0.0)
    live_price, live_price_source = _live_price(findings)
    live_stock = _live_stock(findings)
    return ResourceCatalogEntry(
        resource_ref=f"{source}:{name}:resource",
        source=source,
        account_ref=_account_ref(row),
        tuple_name=name,
        surface_ref=str(row.get("surface_ref") or name),
        worker_binding_ref=str(row.get("worker_ref") or name),
        adapter=adapter,
        execution_surface=str(row.get("execution_surface") or _surface_for_adapter(adapter) or "unknown"),
        gpu=str(row.get("gpu") or "unknown"),
        vram_gb=_positive_int(row.get("vram_gb"), default=1),
        max_model_len=_positive_int(row.get("max_model_len"), default=1),
        region=str(row.get("region") or "") or None,
        zone=str(row.get("zone") or "") or None,
        configured_price_per_second=configured_price,
        price_per_second=live_price if live_price is not None else configured_price,
        live_price_per_second=live_price,
        live_price_source=live_price_source,
        stock_state="candidate" if source == "tuple_candidate" else "configured",
        live_stock_state=live_stock,
        live_catalog_status=status,
        live_catalog_checked=bool(evidence.get("checked")),
        live_catalog_findings=findings,
        validation_evidence=dict((validation_evidence or {}).get(name) or {}),
    )


def _worker_contract(row: Mapping[str, Any]) -> WorkerContractSpec:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    adapter = str(row.get("adapter") or "")
    target_configured = is_configured_target(row.get("target"))
    return WorkerContractSpec(
        worker_ref=f"{source}:{name}:worker",
        source=source,
        tuple_name=name,
        worker_binding_ref=str(row.get("worker_ref") or name),
        adapter=adapter,
        execution_surface=str(row.get("execution_surface") or _surface_for_adapter(adapter) or "unknown"),
        model_ref=str(row.get("model_ref") or "") or None,
        engine_ref=str(row.get("engine_ref") or "") or None,
        modes=[str(item) for item in row.get("modes") or ["sync", "async"]],
        input_contracts=[str(item) for item in row.get("input_contracts") or []],
        output_contract=str(row.get("output_contract") or "") or None,
        stream_contract=str(row.get("stream_contract") or "none"),
        target_configured=target_configured,
        endpoint_configured=has_configured_endpoint_or_target(row.get("endpoint"), row.get("target")),
        max_data_classification=str(row.get("max_data_classification") or "") or None,
    )


def _recipe_fit(resource: ResourceCatalogEntry, worker: WorkerContractSpec, recipe: Recipe | None) -> dict[str, Any]:
    if recipe is None:
        return {"eligible": True, "reasons": []}
    reasons: list[str] = []
    requirements = recipe_requirements(recipe)
    if resource.vram_gb < requirements.minimum_vram_gb:
        reasons.append("resource vram_gb is below derived recipe requirement")
    if resource.max_model_len < requirements.context_budget_tokens:
        reasons.append("resource max_model_len is below recipe requirement")
    recipe_modes = {mode.value if isinstance(mode, ExecutionMode) else str(mode) for mode in recipe.allowed_modes}
    worker_modes = set(worker.modes)
    if recipe_modes and not recipe_modes.intersection(worker_modes):
        reasons.append("worker modes do not intersect recipe allowed_modes")
    input_contracts = set(worker.input_contracts)
    if recipe.task == "vision" and "image" not in input_contracts:
        reasons.append("worker input_contracts do not declare image support")
    if recipe.task == "infer" and input_contracts and not {"text", "chat_messages"}.intersection(input_contracts):
        reasons.append("worker input_contracts do not declare text or chat support")
    if recipe.task == "transcribe" and input_contracts and "audio" not in input_contracts:
        reasons.append("worker input_contracts do not declare audio support")
    if recipe.task == "convert" and input_contracts and "document" not in input_contracts:
        reasons.append("worker input_contracts do not declare document support")
    if recipe.output_contract and worker.output_contract and recipe.output_contract != worker.output_contract:
        reasons.append("worker output_contract does not match recipe output_contract")
    return {"eligible": not reasons, "reasons": reasons}


def _config_hash(*, config: GpucallConfig, candidate_rows: list[Mapping[str, Any]]) -> str:
    payload = {
        "policy": config.policy.model_dump(mode="json"),
        "recipes": {key: value.model_dump(mode="json") for key, value in sorted(config.recipes.items())},
        "tuples": {key: value.model_dump(mode="json") for key, value in sorted(config.tuples.items())},
        "models": {key: value.model_dump(mode="json") for key, value in sorted(config.models.items())},
        "engines": {key: value.model_dump(mode="json") for key, value in sorted(config.engines.items())},
        "tuple_candidates": sorted(candidate_rows, key=lambda row: str(row.get("name") or "")),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _surface_for_adapter(adapter: str) -> str | None:
    descriptor = adapter_descriptor(adapter)
    if descriptor is None or descriptor.execution_surface is None:
        return None
    return descriptor.execution_surface.value


def _account_ref(row: Mapping[str, Any]) -> str:
    explicit = str(row.get("account_ref") or "").strip()
    if explicit:
        return explicit
    return _provider_family(str(row.get("adapter") or ""))


def _provider_family(adapter: str) -> str:
    return vendor_family_for_adapter(adapter)


def _live_price(findings: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    for finding in findings:
        value = finding.get("live_price_per_second")
        try:
            price = float(value)
        except (TypeError, ValueError):
            continue
        if price >= 0:
            return price, str(finding.get("live_price_source") or finding.get("source") or "live_catalog")
    return None, None


def _live_stock(findings: list[dict[str, Any]]) -> Literal["available", "unavailable", "unknown"]:
    for finding in findings:
        state = str(finding.get("live_stock_state") or "").strip().lower()
        if state in {"available", "unavailable"}:
            return state  # type: ignore[return-value]
    return "unknown"


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _tuple_validation_evidence(config_dir: Path) -> dict[str, dict[str, Any]]:
    root = default_state_dir() / "tuple-validation"
    if not root.exists():
        return {}
    expected_hash = _file_config_hash(config_dir)
    expected_commit = _git_commit()
    grouped: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("validation_schema_version") != 1:
            continue
        if data.get("config_hash") != expected_hash:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        tuple_name = str(data.get("tuple") or "")
        if tuple_name:
            grouped.setdefault(tuple_name, []).append({**data, "_path": str(path)})
    return {name: _validation_summary(rows) for name, rows in grouped.items()}


def _validation_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = sorted(rows, key=lambda item: str(item.get("ended_at") or item.get("started_at") or ""))
    seconds = [_observed_seconds(row) for row in rows]
    seconds = [value for value in seconds if value is not None]
    passed = [row for row in rows if row.get("passed") is True]
    latest = rows[-1] if rows else {}
    summary: dict[str, Any] = {
        "artifact_count": len(rows),
        "passed_count": len(passed),
        "failed_count": len(rows) - len(passed),
        "latest_path": latest.get("_path"),
        "latest_passed": bool(latest.get("passed")) if latest else False,
    }
    if seconds:
        summary.update(
            {
                "observed_wall_seconds_latest": seconds[-1],
                "observed_wall_seconds_p50": float(median(seconds)),
                "observed_wall_seconds_p99": _percentile(seconds, 0.99),
                "observed_wall_seconds_max": max(seconds),
            }
        )
    attestation = latest.get("attestation_evidence") or (latest.get("attestations") or {}).get("attestation_evidence")
    if attestation:
        summary["attestation_evidence_hash"] = _stable_hash(attestation)
    if "attestation_verified" in latest:
        summary["attestation_verified"] = bool(latest.get("attestation_verified"))
    elif isinstance(attestation, Mapping):
        summary["attestation_verified"] = bool(attestation.get("verified"))
    return summary


def _observed_seconds(row: Mapping[str, Any]) -> float | None:
    value = row.get("observed_wall_seconds")
    try:
        if value is not None:
            return float(value)
    except (TypeError, ValueError):
        pass
    try:
        started = datetime.fromisoformat(str(row.get("started_at")).replace("Z", "+00:00"))
        ended = datetime.fromisoformat(str(row.get("ended_at")).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (ended - started).total_seconds())


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * percentile)))
    return float(ordered[index])


def _file_config_hash(config_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(config_dir.rglob("*.yml")):
        digest.update(str(path.relative_to(config_dir)).encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _git_commit() -> str | None:
    env_commit = os.getenv("GPUCALL_GIT_COMMIT")
    if env_commit:
        return env_commit
    root = Path(__file__).resolve().parents[1]
    build_commit = root / "BUILD_COMMIT"
    try:
        value = build_commit.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    head = root / ".git" / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
        if value.startswith("ref: "):
            ref = root / ".git" / value.removeprefix("ref: ")
            return ref.read_text(encoding="utf-8").strip()
        return value
    except OSError:
        return None
