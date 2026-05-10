from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from gpucall.domain import ExecutionMode
from gpucall.recipe_intents import capabilities_for, normalize_intent

TEXT_STOP_TOKENS = ["<|im_end|>", "<|endoftext|>"]
ASYNC_ONLY_RESOURCE_CLASSES = {"large", "exlarge", "ultralong"}
ASYNC_ONLY_LATENCY_CLASSES = {"batch", "long_running"}
HIGH_COLD_START_SECONDS = 300


def canonical_recipe_from_artifact(artifact: Mapping[str, Any], *, catalog: Any | None = None) -> dict[str, Any]:
    proposed = _proposed_recipe_from_artifact(artifact)
    task = str(proposed.get("task") or "infer")
    name = _canonical_name(str(proposed.get("name") or f"{task}-draft"))
    context_budget_tokens = _positive_int(proposed.get("context_budget_tokens") or proposed.get("max_model_len"), default=32768)
    resource_class = str(proposed.get("resource_class") or _resource_class_for(task, context_budget_tokens))
    latency_class = str(
        proposed.get("latency_class")
        or ("long_running" if context_budget_tokens >= 524288 else ("batch" if context_budget_tokens >= 65536 else "standard"))
    )
    intent = normalize_intent(str(proposed.get("intent") or f"{task}_draft")) or f"{task}_draft"
    recipe: dict[str, Any] = {
        "name": name,
        "recipe_schema_version": 3,
        "task": task,
        "intent": intent,
        "auto_select": bool(proposed.get("auto_select", False)),
        "data_classification": str(proposed.get("data_classification") or "confidential"),
        "allowed_modes": _allowed_modes(
            proposed,
            task=task,
            context_budget_tokens=context_budget_tokens,
            resource_class=resource_class,
            latency_class=latency_class,
            catalog=catalog,
        ),
        "context_budget_tokens": context_budget_tokens,
        "resource_class": resource_class,
        "latency_class": latency_class,
        "quality_floor": "draft",
        "timeout_seconds": _timeout_for(task, context_budget_tokens),
        "lease_ttl_seconds": _lease_for(task, context_budget_tokens),
        "token_estimation_profile": str(proposed.get("token_estimation_profile") or "generic_utf8"),
        "max_input_bytes": _max_input_bytes(task, context_budget_tokens),
        "allowed_mime_prefixes": _allowed_mime_prefixes(task, proposed),
        "default_temperature": 0.2 if task == "vision" else 0.7,
        "structured_temperature": 0.0,
        "structured_system_prompt": "Return only valid JSON when response_format requests JSON. Do not include markdown fences or prose.",
        "system_prompt": _system_prompt_for(task),
        "stop_tokens": TEXT_STOP_TOKENS,
        "repetition_penalty": 1.05,
        "guided_decoding": True,
        "output_validation_attempts": 1,
        "required_model_capabilities": [str(item) for item in proposed.get("required_model_capabilities") or []],
        "output_contract": _route_output_contract(proposed),
    }
    if task == "vision":
        recipe["allowed_inline_mime_prefixes"] = ["text/"]
    return recipe


def materialization_report(artifact: Mapping[str, Any], recipe: Mapping[str, Any], *, catalog: Any | None = None) -> dict[str, Any]:
    proposed = _proposed_recipe_from_artifact(artifact)
    catalog_policy = _catalog_mode_policy(
        task=str(recipe.get("task") or proposed.get("task") or "infer"),
        context_budget_tokens=_positive_int(recipe.get("context_budget_tokens") or proposed.get("context_budget_tokens"), default=32768),
        resource_class=str(recipe.get("resource_class") or proposed.get("resource_class") or ""),
        latency_class=str(recipe.get("latency_class") or proposed.get("latency_class") or ""),
        catalog=catalog,
    )
    return {
        "schema_version": 1,
        "phase": "admin-materialization",
        "policy": "accept-all",
        "human_review_bypassed": True,
        "canonical_recipe": dict(recipe),
        "catalog_policy": catalog_policy,
        "discarded_draft_fields": sorted(set(proposed) - set(recipe)),
        "warnings": [
            "accept-all materialization writes a recipe candidate; it does not create a capable tuple.",
            "recipe mode policy is derived deterministically from request shape and catalog cold-start metadata.",
            "run gpucall validate-config after copying the recipe into a real config directory.",
            "if validate-config reports no satisfying tuple, add or enable a tuple before production use.",
        ],
    }


def write_recipe_yaml(recipe: Mapping[str, Any], output_dir: str | Path, *, force: bool = False) -> Path:
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{recipe['name']}.yml"
    if path.exists() and not force:
        raise FileExistsError(f"recipe already exists: {path}")
    path.write_text(to_yaml(recipe), encoding="utf-8")
    return path


def to_yaml(value: Mapping[str, Any]) -> str:
    return yaml.safe_dump(dict(value), allow_unicode=True, sort_keys=False)


def _proposed_recipe_from_artifact(artifact: Mapping[str, Any]) -> Mapping[str, Any]:
    if "proposed_recipe" in artifact:
        return _mapping(artifact.get("proposed_recipe"))
    sanitized = _mapping(artifact.get("sanitized_request"))
    if sanitized:
        return _proposed_recipe_from_sanitized(sanitized)
    raise ValueError("artifact must be a gpucall-recipe-draft intake or draft JSON object")


def _proposed_recipe_from_sanitized(sanitized: Mapping[str, Any]) -> dict[str, Any]:
    task = str(sanitized.get("task") or "infer")
    intent = normalize_intent(str(sanitized.get("intent") or task)) or task
    capabilities = sanitized.get("desired_capabilities")
    if not isinstance(capabilities, list) or not capabilities:
        capabilities = capabilities_for(task=task, intent=intent)
    context_budget_tokens = _round_context_budget(_context_budget_from_context(_mapping(_mapping(sanitized.get("error")).get("context"))))
    return {
        "name": _recipe_name(task, intent),
        "recipe_schema_version": 3,
        "task": task,
        "intent": intent,
        "auto_select": False,
        "data_classification": str(sanitized.get("classification") or "confidential"),
        "allowed_modes": [str(sanitized.get("mode") or "sync")],
        "required_model_capabilities": [str(item) for item in capabilities],
        "context_budget_tokens": context_budget_tokens,
        "resource_class": _resource_class_for(task, context_budget_tokens),
        "latency_class": "long_running" if context_budget_tokens >= 524288 else ("batch" if context_budget_tokens >= 65536 else "standard"),
        "token_estimation_profile": "generic_utf8",
        "allowed_mime_prefixes": _mime_prefixes_for(task),
        "output_contract": sanitized.get("expected_output") or "plain_text",
    }


def _route_output_contract(proposed: Mapping[str, Any]) -> str:
    raw = str(proposed.get("output_contract") or "").strip().lower().replace("_", "-")
    if raw in {"json_object", "json-schema"}:
        return raw.replace("-", "_")
    if raw in {"plain-text", "text", "plain"}:
        return "plain-text"
    return "plain-text"


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _canonical_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return cleaned or "recipe-draft"


def _recipe_name(task: str, intent: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", intent.lower()).strip("-")
    return f"{task}-{cleaned or 'standard'}-draft"


def _positive_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, number)


def _round_context_budget(value: Any) -> int:
    try:
        required = int(value)
    except (TypeError, ValueError):
        required = 8192
    for candidate in (8192, 32768, 65536, 131072, 262144, 524288, 1010000):
        if required <= candidate:
            return candidate
    return required


def _context_budget_from_context(context: Mapping[str, Any]) -> int | None:
    for key in ("context_budget_tokens", "required_model_len"):
        value = context.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _allowed_modes(
    proposed: Mapping[str, Any],
    *,
    task: str,
    context_budget_tokens: int,
    resource_class: str,
    latency_class: str,
    catalog: Any | None,
) -> list[str]:
    policy = _catalog_mode_policy(
        task=task,
        context_budget_tokens=context_budget_tokens,
        resource_class=resource_class,
        latency_class=latency_class,
        catalog=catalog,
    )
    if policy["requires_async"]:
        return ["async"]
    raw = proposed.get("allowed_modes")
    if isinstance(raw, list) and raw:
        return _dedupe_modes([str(item) for item in raw if str(item)])
    return ["sync", "async"]


def _dedupe_modes(values: list[str]) -> list[str]:
    seen: set[str] = set()
    modes: list[str] = []
    for value in values:
        mode = str(value).strip()
        if not mode or mode in seen:
            continue
        seen.add(mode)
        modes.append(mode)
    return modes or ["sync", "async"]


def _catalog_mode_policy(
    *,
    task: str,
    context_budget_tokens: int,
    resource_class: str,
    latency_class: str,
    catalog: Any | None,
) -> dict[str, Any]:
    reasons: list[str] = []
    if task == "infer" and context_budget_tokens > 32768:
        reasons.append("infer context_budget_tokens above sync-safe threshold")
    if resource_class in ASYNC_ONLY_RESOURCE_CLASSES:
        reasons.append(f"resource_class {resource_class} is async-preferred")
    if latency_class in ASYNC_ONLY_LATENCY_CLASSES:
        reasons.append(f"latency_class {latency_class} is async-preferred")
    tuple_latency = _tuple_latency_buckets(task=task, context_budget_tokens=context_budget_tokens, catalog=catalog)
    high_cold_start_tuples = tuple_latency["high_cold_start_tuples"]
    sync_safe_tuples = tuple_latency["sync_safe_tuples"]
    if high_cold_start_tuples and not sync_safe_tuples:
        reasons.append("all catalog candidate tuples exceed sync-safe cold-start threshold")
    return {
        "catalog_consulted": catalog is not None,
        "requires_async": bool(reasons),
        "sync_safe_context_budget_tokens": 32768,
        "high_cold_start_threshold_seconds": HIGH_COLD_START_SECONDS,
        "high_cold_start_tuples": high_cold_start_tuples,
        "sync_safe_tuples": sync_safe_tuples,
        "reasons": reasons,
    }


def _tuple_latency_buckets(*, task: str, context_budget_tokens: int, catalog: Any | None) -> dict[str, list[str]]:
    if catalog is None:
        return {"high_cold_start_tuples": [], "sync_safe_tuples": []}
    tuples = getattr(catalog, "tuples", {})
    if not isinstance(tuples, Mapping):
        return {"high_cold_start_tuples": [], "sync_safe_tuples": []}
    high_cold_start: list[str] = []
    sync_safe: list[str] = []
    for name, tuple_spec in tuples.items():
        max_model_len = _positive_int(getattr(tuple_spec, "max_model_len", None), default=0)
        if max_model_len < context_budget_tokens:
            continue
        modes = [str(item) for item in getattr(tuple_spec, "modes", [])]
        if "sync" not in modes:
            continue
        if task == "vision" and not bool(getattr(tuple_spec, "supports_vision", False)):
            continue
        if task != "vision" and bool(getattr(tuple_spec, "supports_vision", False)) and "text" not in [str(item) for item in getattr(tuple_spec, "input_contracts", [])]:
            continue
        cold_start = _positive_int(getattr(tuple_spec, "expected_cold_start_seconds", None), default=0)
        if cold_start > HIGH_COLD_START_SECONDS:
            high_cold_start.append(str(name))
        else:
            sync_safe.append(str(name))
    return {"high_cold_start_tuples": sorted(high_cold_start), "sync_safe_tuples": sorted(sync_safe)}


def _resource_class_for(task: str, context_budget_tokens: int) -> str:
    if task == "vision":
        return "document_vision" if context_budget_tokens >= 8192 else "standard"
    if context_budget_tokens <= 8192:
        return "light"
    if context_budget_tokens <= 32768:
        return "standard"
    if context_budget_tokens <= 65536:
        return "large"
    if context_budget_tokens <= 131072:
        return "exlarge"
    return "ultralong"


def _timeout_for(task: str, max_model_len: int) -> int:
    if task == "vision":
        return 1800
    if max_model_len >= 131072:
        return 600
    return 180


def _lease_for(task: str, max_model_len: int) -> int:
    if task == "vision":
        return 2100
    if max_model_len >= 131072:
        return 900
    return 240


def _max_input_bytes(task: str, max_model_len: int) -> int:
    if task == "vision":
        return 16 * 1024 * 1024
    return max(16 * 1024 * 1024, min(1024 * 1024 * 1024, max_model_len * 1024))


def _allowed_mime_prefixes(task: str, proposed: Mapping[str, Any]) -> list[str]:
    raw = proposed.get("allowed_mime_prefixes")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    return _mime_prefixes_for(task)


def _mime_prefixes_for(task: str) -> list[str]:
    if task == "vision":
        return ["image/"]
    if task == "transcribe":
        return ["audio/"]
    if task == "video":
        return ["video/"]
    return ["text/"]


def _system_prompt_for(task: str) -> str:
    if task == "vision":
        return "Answer the user's vision request directly from the supplied image and prompt."
    return "Answer the user's request directly."
