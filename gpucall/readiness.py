from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from gpucall.config import ConfigError, default_state_dir, load_config
from gpucall.domain import ExecutionMode, Recipe, recipe_requirements
from gpucall.price_freshness import tuple_configured_price_freshness
from gpucall.routing import tuple_route_rejection_reason


def build_readiness_report(
    *,
    config_dir: str | Path,
    source: str | None = None,
    intent: str | None = None,
    recipe: str | None = None,
    validation_dir: str | Path | None = None,
) -> dict[str, Any]:
    root = Path(config_dir)
    config = load_config(root)
    recipes = _selected_recipes(config.recipes, intent=intent, recipe=recipe)
    report = {
        "schema_version": 1,
        "phase": "readiness",
        "source": source,
        "config_dir": str(root),
        "validation_dir": str(Path(validation_dir) if validation_dir else default_state_dir() / "tuple-validation"),
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
        report["recipes"].append(_recipe_readiness(item, config=config, validation_dir=validation_dir))
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


def _recipe_readiness(recipe: Recipe, *, config: Any, validation_dir: str | Path | None) -> dict[str, Any]:
    requirements = recipe_requirements(recipe)
    mode = recipe.allowed_modes[0] if recipe.allowed_modes else ExecutionMode.SYNC
    eligible: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    required_inputs = _required_input_contracts(recipe)
    for tuple in sorted(config.tuples.values(), key=lambda item: item.name):
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
        row = {
            "tuple": tuple.name,
            "mode": mode.value,
            "vram_gb": tuple.vram_gb,
            "max_model_len": tuple.max_model_len,
            "price_freshness": tuple_configured_price_freshness(tuple).value,
            "live_validation_artifact": _validation_artifact_path(tuple.name, recipe.name, validation_dir),
        }
        if reason is None:
            eligible.append(row)
        else:
            row["reason"] = reason
            rejected.append(row)
    sync_eligible = bool(eligible and ExecutionMode.SYNC in recipe.allowed_modes)
    return {
        "recipe": recipe.name,
        "intent": recipe.intent,
        "task": recipe.task,
        "auto_select": recipe.auto_select,
        "recipe_exists": True,
        "static_config_valid": True,
        "eligible_tuple_count": len(eligible),
        "eligible_tuples": eligible,
        "rejected_tuple_count": len(rejected),
        "production_activated": bool(eligible and recipe.auto_select),
        "sync_eligible": sync_eligible,
        "async_only_recommended": bool(eligible and not sync_eligible),
        "next_actions": _next_actions(recipe, eligible),
    }


def _validation_artifact_path(tuple_name: str, recipe_name: str, validation_dir: str | Path | None) -> str | None:
    root = Path(validation_dir) if validation_dir else default_state_dir() / "tuple-validation"
    if not root.exists():
        return None
    for path in sorted(root.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if data.get("tuple") == tuple_name and data.get("recipe") == recipe_name and data.get("passed") is True:
            return str(path)
    return None


def _next_actions(recipe: Recipe, eligible: list[Mapping[str, Any]]) -> list[str]:
    actions = ["run gpucall validate-config"]
    if not eligible:
        actions.append("submit or materialize tuple/provider capability for this recipe")
        return actions
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
