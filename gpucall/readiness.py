from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gpucall.config import ConfigError, default_state_dir, load_config
from gpucall.credentials import load_credentials
from gpucall.domain import ExecutionMode, Recipe, recipe_requirements
from gpucall.panopticon import default_panopticon_path, load_panopticon_evidence, store_panopticon_evidence
from gpucall.price_freshness import tuple_configured_price_freshness
from gpucall.routing import tuple_route_rejection_reason
from gpucall.tuple_catalog import live_tuple_catalog_evidence
from gpucall.validation_evidence import (
    RouteValidationEvidence,
    RouteValidationStatus,
    load_route_validation_evidence,
    load_route_validation_statuses,
    route_validation_key,
    route_validation_required_for_tuple,
)


def build_readiness_report(
    *,
    config_dir: str | Path,
    source: str | None = None,
    intent: str | None = None,
    recipe: str | None = None,
    validation_dir: str | Path | None = None,
    config: Any | None = None,
    live: bool = False,
    panopticon_path: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(config_dir)
    config = config or load_config(root)
    recipes = _selected_recipes(config.recipes, intent=intent, recipe=recipe)
    live_scope = _live_catalog_scope_for_recipes(recipes, config=config)
    panopticon = Path(panopticon_path) if panopticon_path is not None else default_panopticon_path()
    live_evidence: Mapping[str, Mapping[str, Any]] = load_panopticon_evidence(panopticon)
    live_source = "panopticon_snapshot"
    if live:
        credentials = load_credentials()
        try:
            observed = live_tuple_catalog_evidence(live_scope, credentials) if live_scope else {}
            if observed:
                store_panopticon_evidence(observed, panopticon)
            live_evidence = load_panopticon_evidence(panopticon)
            live_source = "live_refresh"
        except Exception as exc:
            live_evidence = {
                name: {
                    "tuple": name,
                    "status": "blocked",
                    "checked": True,
                    "findings": [
                        {
                            "severity": "error",
                            "dimension": "panopticon",
                            "reason": f"live catalog lookup failed: {exc}",
                            "raw": {"live_reason": "panopticon_live_refresh_failed"},
                        }
                    ],
                }
                for name in live_scope
            }
            live_source = "live_refresh_failed"
    route_validation_evidence = load_route_validation_evidence(config_dir=root, validation_dir=validation_dir)
    route_validation_statuses = load_route_validation_statuses(config_dir=root, validation_dir=validation_dir)
    report = {
        "schema_version": 1,
        "phase": "readiness",
        "source": source,
        "config_dir": str(root),
        "validation_dir": str(Path(validation_dir) if validation_dir else default_state_dir() / "tuple-validation"),
        "panopticon": {
            "path": str(panopticon),
            "source": live_source,
            "live_refresh": live,
            "tuple_count": len(live_evidence),
        },
        "recipes": [],
    }
    if not recipes and intent:
        report["recipes"].append(
            {
                "recipe": None,
                "intent": intent,
                "recipe_exists": False,
                "static_config_valid": True,
                "eligible_tuple_count": 0,
                "eligible_tuples": [],
                "production_activated": False,
                "sync_eligible": False,
                "async_only_recommended": False,
                "next_actions": ["submit preflight with gpucall-recipe-draft", "materialize reviewed recipe in the admin inbox"],
            }
        )
        return report
    for item in recipes:
        report["recipes"].append(
            _recipe_readiness(
                item,
                config=config,
                config_dir=root,
                validation_dir=validation_dir,
                live_evidence=live_evidence,
                route_validation_evidence=route_validation_evidence,
                route_validation_statuses=route_validation_statuses,
            )
        )
    return report


def dumps_readiness(report: Mapping[str, Any]) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _selected_recipes(recipes: Mapping[str, Recipe], *, intent: str | None, recipe: str | None) -> list[Recipe]:
    if recipe:
        found = recipes.get(recipe)
        if found is None:
            raise ConfigError(f"unknown recipe: {recipe}")
        return [found]
    values = sorted(recipes.values(), key=lambda item: item.name)
    if intent:
        values = [item for item in values if item.intent == intent or item.name == intent]
    return values


def _live_catalog_scope_for_recipes(recipes: list[Recipe], *, config: Any) -> dict[str, Any]:
    scoped: dict[str, Any] = {}
    for recipe in recipes:
        requirements = recipe_requirements(recipe)
        required_inputs = _required_input_contracts(recipe)
        for tuple in config.tuples.values():
            for mode in _allowed_modes(recipe):
                reason = tuple_route_rejection_reason(
                    policy=config.policy,
                    recipe=recipe,
                    tuple=tuple,
                    model=config.models.get(tuple.model_ref) if tuple.model_ref else None,
                    engine=config.engines.get(tuple.engine_ref) if tuple.engine_ref else None,
                    mode=mode,
                    required_len=requirements.context_budget_tokens,
                    required_input_contracts=required_inputs,
                    auto_selected=True,
                )
                if reason is None:
                    scoped[tuple.name] = tuple
                    break
    return scoped


def _recipe_readiness(
    recipe: Recipe,
    *,
    config: Any,
    config_dir: Path,
    validation_dir: str | Path | None,
    live_evidence: Mapping[str, Mapping[str, Any]] | None = None,
    route_validation_evidence: Mapping[tuple[str, str, str], RouteValidationEvidence] | None = None,
    route_validation_statuses: Mapping[tuple[str, str, str], RouteValidationStatus] | None = None,
) -> dict[str, Any]:
    requirements = recipe_requirements(recipe)
    modes = _allowed_modes(recipe)
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    required_inputs = _required_input_contracts(recipe)
    for tuple in sorted(config.tuples.values(), key=lambda item: item.name):
        for mode in modes:
            reason = tuple_route_rejection_reason(
                policy=config.policy,
                recipe=recipe,
                tuple=tuple,
                model=config.models.get(tuple.model_ref) if tuple.model_ref else None,
                engine=config.engines.get(tuple.engine_ref) if tuple.engine_ref else None,
                mode=mode,
                required_len=requirements.context_budget_tokens,
                required_input_contracts=required_inputs,
                auto_selected=True,
            )
            validation_key = route_validation_key(tuple.name, recipe.name, mode.value)
            validation = (route_validation_evidence or {}).get(validation_key)
            validation_status = (route_validation_statuses or {}).get(validation_key)
            row = {
                "tuple": tuple.name,
                "mode": mode.value,
                "vram_gb": tuple.vram_gb,
                "max_model_len": tuple.max_model_len,
                "price_freshness": tuple_configured_price_freshness(tuple).value,
                "live_validation_artifact": validation.path if validation else None,
                "route_validation_required": route_validation_required_for_tuple(tuple),
            }
            if validation_status is not None:
                row["latest_route_validation_artifact"] = validation_status.path
                row["latest_route_validation_mtime"] = validation_status.mtime
                row["route_validation_status"] = "accepted" if validation_status.accepted else "rejected"
                if validation_status.reason:
                    row["route_validation_reason"] = validation_status.reason
            live = (live_evidence or {}).get(tuple.name)
            if isinstance(live, Mapping):
                row["live_catalog_checked"] = bool(live.get("checked"))
                row["live_catalog_status"] = live.get("status")
                for key in (
                    "panopticon_observed_at",
                    "panopticon_expires_at",
                    "panopticon_ttl_seconds",
                    "panopticon_age_seconds",
                    "panopticon_stale",
                ):
                    if key in live:
                        row[key] = live[key]
                findings = live.get("findings")
                row["live_catalog_findings"] = findings if isinstance(findings, list) else []
                if live.get("status") == "blocked":
                    row["live_blocked"] = True
                    row["live_reason"] = _live_block_reason(row["live_catalog_findings"])
            if reason is None:
                if row["route_validation_required"] and validation is None and row.get("live_blocked") is not True:
                    row["live_blocked"] = True
                    row["live_reason"] = _route_validation_block_reason(validation_status)
                eligible.append(row)
            else:
                row["reason"] = reason
                rejected.append(row)
    live_blocked = [item for item in eligible if item.get("live_blocked") is True]
    live_ready = [item for item in eligible if item.get("live_blocked") is not True]
    policy_blocked_candidates = _policy_blocked_candidate_tuples(rejected, min_model_len=requirements.context_budget_tokens)
    selected_mode = _selected_mode(modes, live_ready, eligible)
    sync_live_ready = any(item.get("mode") == ExecutionMode.SYNC.value for item in live_ready)
    return {
        "recipe": recipe.name,
        "intent": recipe.intent,
        "task": recipe.task,
        "auto_select": recipe.auto_select,
        "allowed_modes": [mode.value for mode in modes],
        "selected_mode": selected_mode,
        "mode_readiness": _mode_readiness(modes, eligible),
        "context_budget_tokens": requirements.context_budget_tokens,
        "max_input_bytes": requirements.max_input_bytes,
        "recipe_exists": True,
        "static_config_valid": True,
        "eligible_tuple_count": len(eligible),
        "eligible_tuples": eligible,
        "rejected_tuple_count": len(rejected),
        "rejected_tuple_reasons": _rejection_summary(rejected),
        "policy_blocked_candidate_tuples": policy_blocked_candidates,
        "live_ready_tuple_count": len(live_ready),
        "live_ready_tuples": live_ready,
        "live_blocked_tuples": live_blocked,
        "production_activated": bool(live_ready and recipe.auto_select),
        "sync_eligible": sync_live_ready,
        "async_only_recommended": bool(live_ready and not sync_live_ready),
        "current_caller_action": "send_request" if live_ready else "retry_later_or_contact_gpucall_admin",
        "next_actions": _next_actions(recipe, eligible, policy_blocked_candidates),
    }


def _allowed_modes(recipe: Recipe) -> list[ExecutionMode]:
    return list(recipe.allowed_modes) or [ExecutionMode.SYNC]


def _selected_mode(
    modes: list[ExecutionMode],
    live_ready: list[Mapping[str, Any]],
    eligible: list[Mapping[str, Any]],
) -> str:
    for mode in modes:
        if any(item.get("mode") == mode.value for item in live_ready):
            return mode.value
    for mode in modes:
        if any(item.get("mode") == mode.value for item in eligible):
            return mode.value
    return modes[0].value


def _mode_readiness(modes: list[ExecutionMode], eligible: list[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for mode in modes:
        rows = [item for item in eligible if item.get("mode") == mode.value]
        live_ready = [item for item in rows if item.get("live_blocked") is not True]
        live_blocked = [item for item in rows if item.get("live_blocked") is True]
        result[mode.value] = {
            "eligible_tuple_count": len(rows),
            "live_ready_tuple_count": len(live_ready),
            "live_blocked_tuple_count": len(live_blocked),
        }
    return result


def _rejection_summary(rejected: list[Mapping[str, Any]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for item in rejected:
        reason = str(item.get("reason") or "unknown")
        summary[reason] = summary.get(reason, 0) + 1
    return dict(sorted(summary.items()))


def _policy_blocked_candidate_tuples(rejected: list[Mapping[str, Any]], *, min_model_len: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in rejected:
        if item.get("reason") != "tuple is not in policy allowlist":
            continue
        try:
            max_model_len = int(item.get("max_model_len") or 0)
        except (TypeError, ValueError):
            max_model_len = 0
        if max_model_len < min_model_len:
            continue
        key = (str(item.get("tuple") or ""), str(item.get("mode") or ""))
        if key in seen:
            continue
        seen.add(key)
        rows.append(
            {
                "tuple": item.get("tuple"),
                "mode": item.get("mode"),
                "vram_gb": item.get("vram_gb"),
                "max_model_len": item.get("max_model_len"),
                "price_freshness": item.get("price_freshness"),
                "reason": item.get("reason"),
            }
        )
        if len(rows) >= 20:
            break
    return rows


def _live_block_reason(findings: object) -> str:
    if isinstance(findings, list):
        for finding in findings:
            if not isinstance(finding, Mapping):
                continue
            raw = finding.get("raw")
            if isinstance(raw, Mapping) and raw.get("live_reason"):
                return str(raw["live_reason"])
            if finding.get("field"):
                return str(finding["field"])
            if finding.get("live_stock_state") == "unavailable":
                return "live_stock_unavailable"
            if finding.get("reason"):
                return str(finding["reason"])
    return "live_catalog_blocked"


def _route_validation_block_reason(status: RouteValidationStatus | None) -> str:
    if status is None:
        return "missing_route_validation_evidence"
    if status.accepted:
        return "missing_route_validation_evidence"
    return status.reason or "route_validation_rejected"


def _next_actions(recipe: Recipe, eligible: list[Mapping[str, Any]], policy_blocked_candidates: list[Mapping[str, Any]]) -> list[str]:
    actions = ["run gpucall validate-config"]
    if policy_blocked_candidates:
        actions.append("add validated replacement tuple to policy allowlist or remove stale allowed tuple")
    if not eligible:
        actions.append("submit or materialize tuple/provider capability for this recipe")
        return actions
    live_reasons = {str(item.get("live_reason") or "") for item in eligible}
    if "endpoint_missing_from_inventory" in live_reasons or "runpod_endpoint_inventory" in live_reasons:
        actions.append("update or remove stale RunPod endpoint tuples from production policy before validation")
    if any(reason.startswith("latest_route_validation_failed") for reason in live_reasons):
        actions.append("rerun explicit tuple validation after the provider endpoint is stable")
    if "validation_config_hash_mismatch" in live_reasons or "validation_commit_mismatch" in live_reasons:
        actions.append("refresh route validation evidence for the current config and commit")
    if not any(item.get("live_validation_artifact") for item in eligible):
        actions.append("run explicit billable validation with gpucall-recipe-admin promote --run-validation")
    if not recipe.auto_select:
        actions.append("activate only after validation evidence is accepted")
    return actions


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
