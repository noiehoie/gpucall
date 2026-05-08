from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from gpucall.candidate_sources import load_tuple_candidate_payloads
from gpucall.config import default_state_dir
from gpucall.credentials import credentials_path, load_credentials
from gpucall.domain import ExecutionMode, ExecutionTupleSpec, Recipe, recipe_requirements
from gpucall.execution.contracts import artifact_tuple_evidence_key, official_contract, official_contract_hash, tuple_evidence_key
from gpucall.tuple_catalog import live_tuple_catalog_findings
from gpucall.execution.registry import adapter_descriptor
from gpucall.routing import tuple_route_rejection_reason


def tuple_audit_report(config: Any, *, config_dir: Path, recipe_name: str | None = None, live: bool = False) -> dict[str, Any]:
    recipes = _selected_recipes(config.recipes, recipe_name)
    candidates = _load_tuple_candidates(config_dir)
    creds = load_credentials()
    report: dict[str, Any] = {
        "schema_version": 1,
        "phase": "execution-tuple-governance-audit",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "config_dir": str(config_dir),
        "credentials_path": str(credentials_path()),
        "recipe_scope": [recipe.name for recipe in recipes],
        "ideal_contract": {
            "recipe_is_authority": True,
            "production_tuple_requires": [
                "recipe fit",
                "model catalog fit",
                "engine catalog fit",
                "resource catalog fit",
                "official execution contract",
                "cost metadata",
                "endpoint or lifecycle configuration",
                "billable live validation for the exact recipe/resource/model/engine/contract tuple",
                "cleanup audit compatibility",
            ],
            "unvalidated_candidates_enter_routing": False,
        },
        "live_catalog": {"checked": live, "findings": []},
        "recipes": {},
    }
    if live:
        report["live_catalog"]["findings"] = live_tuple_catalog_findings(config.tuples, creds)
    for recipe in recipes:
        report["recipes"][recipe.name] = _recipe_audit(config, config_dir=config_dir, recipe=recipe, candidates=candidates)
    return report


def _selected_recipes(recipes: Mapping[str, Recipe], recipe_name: str | None) -> list[Recipe]:
    if recipe_name:
        recipe = recipes.get(recipe_name)
        if recipe is None:
            raise ValueError(f"unknown recipe: {recipe_name}")
        return [recipe]
    return sorted([recipe for recipe in recipes.values() if recipe.auto_select], key=lambda item: item.name)


def _recipe_audit(config: Any, *, config_dir: Path, recipe: Recipe, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    required_inputs = _required_input_contracts(recipe)
    active_rows = [
        _active_tuple_row(config, config_dir=config_dir, recipe=recipe, tuple=tuple, required_inputs=required_inputs)
        for tuple in sorted(config.tuples.values(), key=lambda item: item.name)
    ]
    candidate_rows = [
        _candidate_row(config, config_dir=config_dir, recipe=recipe, candidate=candidate, required_inputs=required_inputs)
        for candidate in candidates
    ]
    active_fit = [row for row in active_rows if row["recipe_fit"]["eligible"]]
    production_ready = [row for row in active_rows if row["production_decision"] == "PRODUCTION_READY"]
    validation_ready = [row for row in active_rows if row["production_decision"] == "READY_FOR_BILLABLE_VALIDATION"]
    candidate_fit = [row for row in candidate_rows if row["recipe_fit"]["eligible"]]
    return {
        "task": recipe.task,
        "required_input_contracts": sorted(required_inputs),
        "active_fit_count": len(active_fit),
        "production_ready_count": len(production_ready),
        "ready_for_validation_count": len(validation_ready),
        "candidate_fit_count": len(candidate_fit),
        "surfaces": _surface_summary(active_rows, candidate_rows),
        "routing_decision": _routing_decision(production_ready, validation_ready, candidate_fit),
        "active_tuples": active_rows,
        "candidate_tuples": candidate_rows,
    }


def _active_tuple_row(
    config: Any,
    *,
    config_dir: Path,
    recipe: Recipe,
    tuple: ExecutionTupleSpec,
    required_inputs: set[str],
) -> dict[str, Any]:
    model = config.models.get(tuple.model_ref) if tuple.model_ref else None
    engine = config.engines.get(tuple.engine_ref) if tuple.engine_ref else None
    reason = tuple_route_rejection_reason(
        policy=config.policy,
        recipe=recipe,
        tuple=tuple,
        model=model,
        engine=engine,
        mode=_first_mode(recipe),
        required_len=recipe_requirements(recipe).context_budget_tokens,
        required_input_contracts=required_inputs,
        auto_selected=True,
    )
    validation = _matching_validation(tuple=tuple, recipe=recipe, config_dir=config_dir)
    contract = _official_contract(tuple)
    config_findings = _adapter_config_findings(tuple)
    decision = "REJECTED_BY_RECIPE_OR_POLICY"
    if reason is None:
        if config_findings:
            decision = "REJECTED_BY_ADAPTER_CONFIG"
        elif validation["matched"]:
            decision = "PRODUCTION_READY"
        else:
            decision = "READY_FOR_BILLABLE_VALIDATION"
    return {
        "source": "tuples",
        "name": tuple.name,
        "tuple": _tuple_summary(tuple),
        "recipe_fit": {"eligible": reason is None, "reason": reason},
        "official_contract": contract,
        "adapter_config_findings": config_findings,
        "live_validation": validation,
        "production_decision": decision,
    }


def _candidate_row(
    config: Any,
    *,
    config_dir: Path,
    recipe: Recipe,
    candidate: Mapping[str, Any],
    required_inputs: set[str],
) -> dict[str, Any]:
    tuple_error: str | None = None
    tuple: ExecutionTupleSpec | None = None
    try:
        tuple = _tuple_from_candidate(candidate, config)
    except Exception as exc:
        tuple_error = str(exc)
    reason = tuple_error
    config_findings: list[str] = []
    validation: dict[str, Any] = {"matched": []}
    contract: dict[str, Any] = {}
    if tuple is not None:
        model = config.models.get(tuple.model_ref) if tuple.model_ref else None
        engine = config.engines.get(tuple.engine_ref) if tuple.engine_ref else None
        reason = tuple_route_rejection_reason(
            policy=config.policy,
            recipe=recipe,
            tuple=tuple,
            model=model,
            engine=engine,
            mode=_first_mode(recipe),
            required_len=recipe_requirements(recipe).context_budget_tokens,
            required_input_contracts=required_inputs,
            auto_selected=False,
        )
        config_findings = _adapter_config_findings(tuple)
        validation = _matching_validation(tuple=tuple, recipe=recipe, config_dir=config_dir)
        contract = _official_contract(tuple)
    decision = _candidate_decision(reason=reason, config_findings=config_findings, validation=validation)
    return {
        "source": "candidate_catalog",
        "name": candidate.get("name"),
        "path": candidate.get("_path"),
        "tuple": _tuple_summary(tuple) if tuple is not None else _candidate_tuple_summary(candidate),
        "recipe_fit": {"eligible": reason is None, "reason": reason},
        "official_contract": contract,
        "adapter_config_findings": config_findings,
        "live_validation": validation,
        "production_decision": decision,
    }


def _candidate_decision(*, reason: str | None, config_findings: list[str], validation: Mapping[str, Any]) -> str:
    if reason is not None:
        return "REJECTED_BY_RECIPE_OR_POLICY"
    if config_findings:
        return "READY_FOR_ENDPOINT_CONFIGURATION"
    if validation.get("matched"):
        return "VALIDATED_READY_TO_ACTIVATE"
    return "READY_FOR_BILLABLE_VALIDATION"


def _routing_decision(production_ready: list[dict[str, Any]], validation_ready: list[dict[str, Any]], candidate_fit: list[dict[str, Any]]) -> dict[str, Any]:
    if production_ready:
        return {
            "decision": "ROUTABLE",
            "reason": "at least one active execution tuple has exact live validation evidence",
            "tuples": [row["name"] for row in production_ready],
        }
    if validation_ready:
        return {
            "decision": "READY_FOR_VALIDATION",
            "reason": "active execution tuples fit the recipe but lack exact live validation evidence",
            "tuples": [row["name"] for row in validation_ready],
        }
    if candidate_fit:
        return {
            "decision": "CANDIDATE_ONLY",
            "reason": "candidate tuples fit the recipe but are not active production routes",
            "tuples": [row["name"] for row in candidate_fit],
        }
    return {"decision": "FAIL_CLOSED", "reason": "no active or candidate tuple satisfies the recipe"}


def _tuple_from_candidate(candidate: Mapping[str, Any], config: Any) -> ExecutionTupleSpec:
    name = str(candidate.get("name") or "")
    model_ref = str(candidate.get("model_ref") or "")
    engine_ref = str(candidate.get("engine_ref") or "")
    model = config.models.get(model_ref)
    if model is None:
        raise ValueError(f"candidate references unknown model_ref {model_ref!r}")
    payload = {
        "name": name,
        "adapter": str(candidate.get("adapter") or ""),
        "execution_surface": candidate.get("execution_surface") or _surface_for_adapter(str(candidate.get("adapter") or "")),
        "max_data_classification": str(candidate.get("max_data_classification") or "confidential"),
        "trust_profile": {
            "security_tier": str(candidate.get("security_tier") or "encrypted_capsule"),
            "sovereign_jurisdiction": candidate.get("sovereign_jurisdiction") or "unknown",
            "dedicated_gpu": bool(candidate.get("dedicated_gpu", False)),
            "requires_attestation": bool(candidate.get("requires_attestation", False)),
            "supports_key_release": bool(candidate.get("supports_key_release", False)),
            "allows_worker_s3_credentials": bool(candidate.get("allows_worker_s3_credentials", False)),
        },
        "gpu": str(candidate.get("gpu") or "unknown"),
        "vram_gb": _positive_int(candidate.get("vram_gb"), default=1),
        "max_model_len": _positive_int(candidate.get("max_model_len"), default=model.max_model_len),
        "cost_per_second": float(candidate.get("cost_per_second") or 0.0),
        "modes": _strings(candidate.get("modes") or ["sync", "async"]),
        "endpoint": candidate.get("endpoint"),
        "endpoint_contract": candidate.get("endpoint_contract"),
        "input_contracts": _strings(candidate.get("input_contracts")),
        "output_contract": candidate.get("output_contract"),
        "stream_contract": candidate.get("stream_contract") or "none",
        "supports_vision": bool(model.supports_vision),
        "target": candidate.get("target") or "",
        "stream_target": candidate.get("stream_target"),
        "model": model.provider_model_id,
        "declared_model_max_len": model.max_model_len,
        "model_ref": model_ref,
        "engine_ref": engine_ref,
        "provider_params": dict(candidate.get("provider_params") or {}),
    }
    for key in (
        "project_id",
        "region",
        "zone",
        "resource_group",
        "network",
        "subnet",
        "service_account",
        "instance",
        "image",
        "key_name",
        "ssh_remote_cidr",
        "lease_manifest_path",
    ):
        if key in candidate:
            payload[key] = candidate.get(key)
    return ExecutionTupleSpec.model_validate(payload)


def _adapter_config_findings(tuple: ExecutionTupleSpec) -> list[str]:
    descriptor = adapter_descriptor(tuple)
    if descriptor is None or descriptor.config_validator is None:
        return []
    return descriptor.config_validator(tuple)


def _official_contract(tuple: ExecutionTupleSpec) -> dict[str, Any]:
    return official_contract(tuple)


def _matching_validation(*, tuple: ExecutionTupleSpec, recipe: Recipe, config_dir: Path) -> dict[str, Any]:
    root = default_state_dir() / "tuple-validation"
    result: dict[str, Any] = {"dir": str(root), "matched": [], "checked": 0}
    if not root.exists():
        result["reason"] = "validation artifact directory does not exist"
        return result
    expected_hash = _config_hash(config_dir)
    expected_commit = _git_commit()
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        result["checked"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("validation_schema_version") != 1 or data.get("passed") is not True:
            continue
        if data.get("recipe") != recipe.name:
            continue
        if artifact_tuple_evidence_key(data, tuple) != tuple_evidence_key(tuple):
            continue
        if data.get("config_hash") != expected_hash:
            continue
        if expected_commit and data.get("commit") != expected_commit:
            continue
        contract = data.get("official_contract") if isinstance(data.get("official_contract"), dict) else {}
        if not _official_contract_hash_valid(data, contract):
            continue
        result["matched"].append({"path": str(path), "tuple": data.get("tuple"), "recipe": recipe.name, "tuple_key": tuple_evidence_key(tuple)})
    return result


def _official_contract_hash_valid(data: Mapping[str, Any], contract: Mapping[str, Any]) -> bool:
    if not contract:
        return False
    return data.get("official_contract_hash") == official_contract_hash(contract)


def _load_tuple_candidates(config_dir: Path) -> list[dict[str, Any]]:
    return load_tuple_candidate_payloads(config_dir)


def _required_input_contracts(recipe: Recipe) -> set[str]:
    if recipe.task == "vision":
        return {"image", "text", "data_refs"}
    if recipe.task == "transcribe":
        return {"audio", "data_refs"}
    if recipe.task == "convert":
        return {"document", "data_refs"}
    if recipe.task in {"train", "fine-tune"}:
        return {"data_refs", "artifact_refs"}
    if recipe.task == "split-infer":
        return {"activation_refs"}
    return {"chat_messages"}


def _first_mode(recipe: Recipe) -> ExecutionMode | None:
    return recipe.allowed_modes[0] if recipe.allowed_modes else None


def _tuple_summary(tuple: ExecutionTupleSpec | None) -> dict[str, Any]:
    if tuple is None:
        return {}
    return {
        "tuple": tuple.name,
        "adapter": tuple.adapter,
        "execution_surface": tuple.execution_surface.value if tuple.execution_surface else None,
        "gpu": tuple.gpu,
        "vram_gb": tuple.vram_gb,
        "model_ref": tuple.model_ref,
        "engine_ref": tuple.engine_ref,
        "max_model_len": tuple.max_model_len,
        "cost_per_second": float(tuple.cost_per_second),
        "modes": [mode.value for mode in tuple.modes],
        "target_configured": bool(tuple.target),
    }


def _candidate_tuple_summary(candidate: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "tuple": candidate.get("name"),
        "adapter": candidate.get("adapter"),
        "execution_surface": candidate.get("execution_surface") or _surface_for_adapter(str(candidate.get("adapter") or "")),
        "gpu": candidate.get("gpu"),
        "vram_gb": candidate.get("vram_gb"),
        "model_ref": candidate.get("model_ref"),
        "engine_ref": candidate.get("engine_ref"),
        "max_model_len": candidate.get("max_model_len"),
        "cost_per_second": float(candidate.get("cost_per_second") or 0.0),
        "modes": _strings(candidate.get("modes") or ["sync", "async"]),
        "target_configured": bool(candidate.get("target")),
    }


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _strings(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if value is None:
        return []
    return [str(value)]


def _surface_for_adapter(adapter: str) -> str | None:
    descriptor = adapter_descriptor(adapter)
    if descriptor is None or descriptor.execution_surface is None:
        return None
    return descriptor.execution_surface.value


def _surface_summary(active_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"active": {}, "candidate": {}}
    for bucket, rows in (("active", active_rows), ("candidate", candidate_rows)):
        counts: dict[str, int] = {}
        for row in rows:
            tuple_data = row.get("tuple") if isinstance(row.get("tuple"), dict) else {}
            surface = str(tuple_data.get("execution_surface") or "unknown")
            counts[surface] = counts.get(surface, 0) + 1
        summary[bucket] = dict(sorted(counts.items()))
    return summary


def _config_hash(config_dir: Path) -> str:
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
