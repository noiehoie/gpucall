from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any


AUTHORING_SYSTEM_PROMPT = """You are gpucall's administrator-side recipe authoring assistant.
Use only the sanitized intake, catalog review, readiness, validation, and error evidence provided.
Do not request raw prompts, raw files, secrets, provider credentials, or presigned URLs.
Return only a JSON object matching the requested proposal contract.
The proposal is not production config; deterministic validation and administrator approval happen later."""


PATCHABLE_RECIPE_FIELDS = {
    "/allowed_inline_mime_prefixes",
    "/allowed_mime_prefixes",
    "/allowed_modes",
    "/artifact_export",
    "/context_budget_tokens",
    "/cost_policy",
    "/data_classification",
    "/default_temperature",
    "/expected_cold_start_seconds",
    "/guided_decoding",
    "/intent",
    "/latency_class",
    "/lease_ttl_seconds",
    "/max_input_bytes",
    "/output_contract",
    "/output_validation_attempts",
    "/quality_floor",
    "/recipe_schema_version",
    "/repetition_penalty",
    "/required_model_capabilities",
    "/requires_key_release",
    "/resource_class",
    "/stop_tokens",
    "/structured_system_prompt",
    "/structured_temperature",
    "/system_prompt",
    "/timeout_seconds",
    "/token_estimation_profile",
}


def build_authoring_bundle(report: Mapping[str, Any], *, config_summary: Mapping[str, Any] | None = None) -> dict[str, Any]:
    review = _mapping(report.get("admin_review"))
    existing = _mapping(report.get("existing_tuple_activation"))
    promotion = _mapping(report.get("promotion"))
    materialized = _mapping(report.get("canonical_recipe"))
    if not materialized:
        materialized = _mapping(report.get("admin_review", {})).get("canonical_recipe") or {}
    bundle = {
        "schema_version": 1,
        "phase": "recipe-authoring-input",
        "materialization": {
            "recipe_path": report.get("recipe_path"),
            "processing_action": report.get("processing_action"),
            "canonical_recipe": _redact_recipe(_mapping(materialized)),
            "catalog_policy": report.get("catalog_policy"),
        },
        "admin_review": {
            "decision": review.get("decision"),
            "production_ready": review.get("production_ready"),
            "auto_select_safe": review.get("auto_select_safe"),
            "canonical_recipe": _redact_recipe(_mapping(review.get("canonical_recipe"))),
            "required_execution_contract": review.get("required_execution_contract"),
            "eligible_tuples": review.get("eligible_tuples"),
            "live_validation": _redact_live_validation(_mapping(review.get("live_validation"))),
            "capability_review": review.get("capability_review"),
            "auto_select_review": review.get("auto_select_review"),
            "warnings": review.get("warnings"),
            "blockers": review.get("blockers"),
        },
        "existing_tuple_activation": _redact_activation(existing),
        "promotion": {
            "decision": promotion.get("decision"),
            "run_billable_validation": promotion.get("run_billable_validation"),
            "activate_validated": promotion.get("activate_validated"),
        },
        "config_summary": dict(config_summary or {}),
    }
    return _drop_empty(bundle)


def authoring_prompt(bundle: Mapping[str, Any]) -> str:
    contract = {
        "proposal_kind": "recipe_patch",
        "target_recipe": "recipe name from the bundle",
        "summary": "short operator-facing summary",
        "patch": [{"op": "add|replace|remove", "path": "one of the allowed recipe JSON pointer paths", "value": "JSON value when op is add or replace"}],
        "validation_plan": ["deterministic validation or smoke command to run"],
        "risk_notes": ["risk or reason this proposal may be unsafe"],
    }
    return (
        "Create a recipe improvement proposal from this gpucall admin evidence.\n"
        "Do not include raw caller data. Do not choose providers or GPUs unless the evidence explicitly names eligible tuples.\n"
        "Do not include patch entries for guarded fields: /auto_select, /name, /task.\n"
        "A patch containing /auto_select, /name, or /task is invalid and will be rejected.\n"
        "Use only these patch paths: "
        + ", ".join(sorted(PATCHABLE_RECIPE_FIELDS))
        + ".\n"
        "If a guarded field should change, mention it only in risk_notes for deterministic administrator review.\n"
        "Return only JSON matching this contract:\n"
        + json.dumps(contract, ensure_ascii=False, sort_keys=True)
        + "\nEvidence bundle:\n"
        + json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    )


def parse_authoring_proposal(raw: str, *, target_recipe: str | None = None) -> dict[str, Any]:
    data = _parse_json_object(raw)
    if data.get("proposal_kind") != "recipe_patch":
        raise ValueError("authoring proposal must have proposal_kind='recipe_patch'")
    if not isinstance(data.get("target_recipe"), str) or not data["target_recipe"].strip():
        raise ValueError("authoring proposal requires target_recipe")
    if target_recipe is not None and data["target_recipe"] != target_recipe:
        raise ValueError(f"authoring proposal target_recipe {data['target_recipe']!r} does not match {target_recipe!r}")
    patch = data.get("patch")
    if not isinstance(patch, list):
        raise ValueError("authoring proposal requires patch list")
    for item in patch:
        if not isinstance(item, Mapping):
            raise ValueError("authoring proposal patch entries must be objects")
        if item.get("op") not in {"add", "replace", "remove"}:
            raise ValueError("authoring proposal patch op must be add, replace, or remove")
        path = item.get("path")
        if not isinstance(path, str) or not path.startswith("/"):
            raise ValueError("authoring proposal patch path must be a JSON pointer")
        if path in {"/auto_select", "/name", "/task"}:
            raise ValueError(f"authoring proposal cannot patch guarded field {path}")
        if path not in PATCHABLE_RECIPE_FIELDS:
            raise ValueError(f"authoring proposal cannot patch unknown recipe field {path}")
        if item.get("op") in {"add", "replace"} and "value" not in item:
            raise ValueError("authoring proposal add/replace patch entries require value")
    for key in ("validation_plan", "risk_notes"):
        if not isinstance(data.get(key), list) or not all(isinstance(item, str) for item in data[key]):
            raise ValueError(f"authoring proposal requires string list {key}")
    return dict(data)


def authoring_artifact(
    *,
    report_path: str | Path,
    authoring_recipe: str,
    bundle: Mapping[str, Any],
    raw_output: str,
    proposal: Mapping[str, Any],
) -> dict[str, Any]:
    bundle_json = json.dumps(bundle, ensure_ascii=False, sort_keys=True)
    return {
        "schema_version": 1,
        "phase": "recipe-authoring-proposal",
        "report_path": str(report_path),
        "authoring_recipe": authoring_recipe,
        "bundle_sha256": hashlib.sha256(bundle_json.encode("utf-8")).hexdigest(),
        "raw_output_sha256": hashlib.sha256(raw_output.encode("utf-8")).hexdigest(),
        "proposal": dict(proposal),
        "production_config_written": False,
    }


def _parse_json_object(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if not match:
            raise
        data = json.loads(match.group(0))
    if not isinstance(data, dict):
        raise ValueError("authoring proposal must be a JSON object")
    return data


def _redact_recipe(recipe: Mapping[str, Any]) -> dict[str, Any]:
    if not recipe:
        return {}
    allowed = {
        "name",
        "recipe_schema_version",
        "task",
        "intent",
        "auto_select",
        "data_classification",
        "allowed_modes",
        "context_budget_tokens",
        "resource_class",
        "latency_class",
        "quality_floor",
        "timeout_seconds",
        "lease_ttl_seconds",
        "token_estimation_profile",
        "max_input_bytes",
        "allowed_mime_prefixes",
        "allowed_inline_mime_prefixes",
        "default_temperature",
        "structured_temperature",
        "stop_tokens",
        "repetition_penalty",
        "guided_decoding",
        "output_validation_attempts",
        "artifact_export",
        "requires_key_release",
        "required_model_capabilities",
        "output_contract",
        "expected_cold_start_seconds",
        "cost_policy",
    }
    result = {key: value for key, value in recipe.items() if key in allowed}
    for prompt_key in ("system_prompt", "structured_system_prompt"):
        value = recipe.get(prompt_key)
        if isinstance(value, str):
            result[prompt_key] = {"sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(), "chars": len(value)}
    return result


def _redact_live_validation(value: Mapping[str, Any]) -> dict[str, Any]:
    matched = []
    for item in value.get("matched", []) if isinstance(value.get("matched"), list) else []:
        if isinstance(item, Mapping):
            matched.append({"tuple": item.get("tuple"), "recipe": item.get("recipe"), "path": item.get("path")})
    return {"matched": matched}


def _redact_activation(value: Mapping[str, Any]) -> dict[str, Any]:
    if not value:
        return {}
    attempts = []
    for item in value.get("validation_attempts", []) if isinstance(value.get("validation_attempts"), list) else []:
        validation = _mapping(item.get("validation"))
        attempts.append(
            {
                "tuple": item.get("tuple"),
                "returncode": validation.get("returncode"),
                "passed": validation.get("passed"),
                "artifact_path": validation.get("artifact_path"),
                "stderr_sha256": _optional_sha256(validation.get("stderr")),
                "stdout_sha256": _optional_sha256(validation.get("stdout")),
            }
        )
    return {
        "decision": value.get("decision"),
        "activated": value.get("activated"),
        "reason": value.get("reason"),
        "eligible_tuples": value.get("eligible_tuples"),
        "matched_validation": value.get("matched_validation"),
        "validation_attempts": attempts,
        "auto_select_review": value.get("auto_select_review"),
    }


def _optional_sha256(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _drop_empty(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: cleaned for key, item in value.items() if (cleaned := _drop_empty(item)) not in ({}, [], None)}
    if isinstance(value, list):
        return [cleaned for item in value if (cleaned := _drop_empty(item)) not in ({}, [], None)]
    return value
