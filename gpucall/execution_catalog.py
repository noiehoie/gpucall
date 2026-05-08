from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.config import GpucallConfig, default_state_dir
from gpucall.domain import ExecutionMode, Recipe, recipe_requirements
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
    configured_price_per_second: float
    price_per_second: float
    live_price_per_second: float | None = None
    live_price_source: str | None = None
    stock_state: Literal["configured", "candidate", "unknown"] = "unknown"
    live_stock_state: Literal["available", "unavailable", "unknown"] = "unknown"
    live_catalog_status: Literal["not_checked", "unknown", "live_revalidated", "blocked"] = "not_checked"
    live_catalog_checked: bool = False
    live_catalog_findings: list[dict[str, Any]] = Field(default_factory=list)
    validation_evidence: dict[str, Any] = Field(default_factory=dict)


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
    configured_price_per_second: float
    price_per_second: float
    live_price_per_second: float | None = None
    live_stock_state: Literal["available", "unavailable", "unknown"] = "unknown"
    model_ref: str | None = None
    engine_ref: str | None = None
    modes: list[str] = Field(default_factory=list)
    production_state: Literal["production_configured", "candidate_draft"] = "candidate_draft"
    live_catalog_status: Literal["not_checked", "unknown", "live_revalidated", "blocked"] = "not_checked"
    snapshot_pinned: bool = True
    recipe_fit: dict[str, Any] | None = None


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
    requirements = recipe_requirements(recipe)
    if resource.vram_gb < int(requirements.minimum_vram_gb):
        reasons.append("resource vram_gb is below derived recipe requirement")
    if resource.max_model_len < int(requirements.context_budget_tokens):
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
                "observed_wall_seconds_max": max(seconds),
            }
        )
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
