from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.config import GpucallConfig
from gpucall.domain import ExecutionMode, Recipe
from gpucall.execution.registry import adapter_descriptor, vendor_family_for_adapter


class ProviderAccountSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ref: str
    provider_family: str
    credential_ref: str
    billing_scope: str | None = None
    api_base: str | None = None


class ResourceCatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    price_per_second: float
    stock_state: Literal["configured", "candidate", "unknown"] = "unknown"


class WorkerContractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    worker_ref: str
    source: Literal["active_tuple", "tuple_candidate"]
    tuple_name: str
    worker_binding_ref: str
    adapter: str
    execution_surface: str
    model_ref: str | None = None
    engine_ref: str | None = None
    modes: list[str] = Field(default_factory=list)
    input_contracts: list[str] = Field(default_factory=list)
    output_contract: str | None = None
    stream_contract: str | None = None
    target_configured: bool = False
    endpoint_configured: bool = False
    max_data_classification: str | None = None


class ResourceCatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    snapshot_id: str
    generated_at: str
    config_hash: str
    accounts: list[ProviderAccountSpec]
    resources: list[ResourceCatalogEntry]
    workers: list[WorkerContractSpec]


class TupleCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

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
    price_per_second: float
    model_ref: str | None = None
    engine_ref: str | None = None
    modes: list[str] = Field(default_factory=list)
    production_state: Literal["production_configured", "candidate_draft"] = "candidate_draft"
    snapshot_pinned: bool = True
    recipe_fit: dict[str, Any] | None = None


def build_resource_catalog_snapshot(config: GpucallConfig, *, config_dir: Path | None = None) -> ResourceCatalogSnapshot:
    rows = [_active_tuple_payload(tuple) for tuple in sorted(config.tuples.values(), key=lambda item: item.name)]
    if config_dir is not None:
        rows.extend(_candidate_payloads(config_dir))
    accounts = _accounts_for(rows)
    resources = [_resource_entry(row) for row in rows]
    workers = [_worker_contract(row) for row in rows]
    config_hash = _config_hash(config=config, candidate_rows=rows)
    content = {
        "schema_version": 1,
        "config_hash": config_hash,
        "accounts": [account.model_dump(mode="json") for account in accounts],
        "resources": [resource.model_dump(mode="json") for resource in resources],
        "workers": [worker.model_dump(mode="json") for worker in workers],
    }
    snapshot_id = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return ResourceCatalogSnapshot(
        snapshot_id=snapshot_id,
        generated_at=datetime.now(timezone.utc).isoformat(),
        config_hash=config_hash,
        accounts=accounts,
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
            "price_per_second": resource.price_per_second,
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


def _resource_entry(row: Mapping[str, Any]) -> ResourceCatalogEntry:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    adapter = str(row.get("adapter") or "")
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
        price_per_second=float(row.get("cost_per_second") or 0.0),
        stock_state="candidate" if source == "tuple_candidate" else "configured",
    )


def _worker_contract(row: Mapping[str, Any]) -> WorkerContractSpec:
    source = "tuple_candidate" if row.get("source") == "tuple_candidate" else "active_tuple"
    name = str(row.get("name") or "")
    adapter = str(row.get("adapter") or "")
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
        target_configured=bool(row.get("target")),
        endpoint_configured=bool(row.get("endpoint") or row.get("target")),
        max_data_classification=str(row.get("max_data_classification") or "") or None,
    )


def _recipe_fit(resource: ResourceCatalogEntry, worker: WorkerContractSpec, recipe: Recipe | None) -> dict[str, Any]:
    if recipe is None:
        return {"eligible": True, "reasons": []}
    reasons: list[str] = []
    if resource.vram_gb < int(recipe.min_vram_gb):
        reasons.append("resource vram_gb is below recipe requirement")
    if resource.max_model_len < int(recipe.max_model_len):
        reasons.append("resource max_model_len is below recipe requirement")
    recipe_modes = {mode.value if isinstance(mode, ExecutionMode) else str(mode) for mode in recipe.allowed_modes}
    worker_modes = set(worker.modes)
    if recipe_modes and not recipe_modes.intersection(worker_modes):
        reasons.append("worker modes do not intersect recipe allowed_modes")
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


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)
