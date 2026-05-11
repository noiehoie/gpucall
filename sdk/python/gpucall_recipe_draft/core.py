from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from gpucall_recipe_draft.recipe_intents import TASK_DEFAULT_CAPABILITIES, capabilities_for, normalize_intent


SENSITIVE_KEYS = {
    "api_key",
    "authorization",
    "content",
    "download_url",
    "messages",
    "prompt",
    "upload_url",
    "uri",
    "url",
    "value",
}

@dataclass(frozen=True)
class DraftInputs:
    error_payload: Mapping[str, Any]
    task: str | None = None
    mode: str | None = None
    intent: str | None = None
    business_need: str | None = None
    classification: str = "confidential"
    expected_output: str | None = None


@dataclass(frozen=True)
class PreflightInputs:
    task: str
    mode: str = "sync"
    intent: str | None = None
    business_need: str | None = None
    classification: str = "confidential"
    expected_output: str = "plain_text"
    content_types: tuple[str, ...] = ()
    byte_values: tuple[int, ...] = ()
    context_budget_tokens: int | None = None
    required_model_len: int | None = None


@dataclass(frozen=True)
class QualityFeedbackInputs:
    task: str
    mode: str = "sync"
    intent: str | None = None
    business_need: str | None = None
    classification: str = "confidential"
    expected_output: str = "plain_text"
    content_types: tuple[str, ...] = ()
    byte_values: tuple[int, ...] = ()
    dimensions: tuple[str, ...] = ()
    context_budget_tokens: int | None = None
    required_model_len: int | None = None
    observed_recipe: str | None = None
    observed_tuple: str | None = None
    observed_tuple_model: str | None = None
    output_validated: bool | None = None
    quality_failure_kind: str = "low_quality_success"
    quality_failure_reason: str = ""
    observed_output_kind: str | None = None
    response_format: str | None = None
    expected_json_schema: Mapping[str, Any] | None = None
    observed_json_schema: Mapping[str, Any] | None = None
    schema_success_count: int | None = None
    schema_failure_count: int | None = None


def intake_from_error(inputs: DraftInputs) -> dict[str, Any]:
    error = dict(inputs.error_payload)
    failure_artifact = _failure_artifact(error)
    safe_summary = _as_mapping(failure_artifact.get("safe_request_summary"))
    code = _first_present(error, "code", ("error", "code")) or _infer_code_from_detail(error)
    if code is None:
        code = _str_or_none(failure_artifact.get("code"))
    context = _as_mapping(error.get("context")) or _as_mapping(failure_artifact.get("context"))
    context_budget = _context_budget_from_context(context)
    largest_auto_context_budget = _first_int(context, "largest_auto_recipe_context_budget_tokens", "largest_auto_recipe_model_len")
    task = inputs.task or _str_or_none(context.get("task")) or _str_or_none(safe_summary.get("task")) or _task_from_detail(error.get("detail")) or "infer"
    mode = inputs.mode or _str_or_none(context.get("mode")) or _str_or_none(safe_summary.get("mode")) or "sync"
    rejections = _extract_rejections(error, context)
    input_summary = _input_summary_from_failure_artifact(safe_summary) or _input_summary(error)
    llm_safe_business_need = _sanitize_free_text(inputs.business_need or "")
    intent = normalize_intent(inputs.intent)
    desired_capabilities = _capabilities_for(task=task, intent=intent)
    sanitized_request = {
        "task": task,
        "mode": mode,
        "intent": intent,
        "business_need": llm_safe_business_need,
        "classification": _str_or_none(safe_summary.get("classification")) or inputs.classification,
        "expected_output": inputs.expected_output or _expected_output_from_error(error),
        "error": {
            "code": code,
            "detail_kind": _detail_kind(error.get("detail")),
            "failure_id": failure_artifact.get("failure_id"),
            "failure_kind": failure_artifact.get("failure_kind"),
            "caller_action": failure_artifact.get("caller_action"),
            "capability_gap": failure_artifact.get("capability_gap"),
            "context": {
                "context_budget_tokens": context_budget,
                "largest_auto_recipe_context_budget_tokens": largest_auto_context_budget,
            },
            "rejections": rejections,
        },
        "input_summary": input_summary,
        "desired_capabilities": desired_capabilities,
    }
    redacted = _redact(error)
    removed = sorted(_removed_paths(error))
    return {
        "schema_version": 1,
        "phase": "deterministic-intake",
        "llm_safe": True,
        "sanitized_request": sanitized_request,
        "redaction_report": {
            "removed_fields": removed,
            "sensitive_keys": sorted(SENSITIVE_KEYS),
            "prompt_body_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
        },
        "redacted_error_payload": redacted,
    }


def intake_from_preflight(inputs: PreflightInputs) -> dict[str, Any]:
    intent = normalize_intent(inputs.intent)
    desired_capabilities = _capabilities_for(task=inputs.task, intent=intent)
    max_bytes = max(inputs.byte_values) if inputs.byte_values else None
    context_budget = inputs.context_budget_tokens if inputs.context_budget_tokens is not None else inputs.required_model_len
    return {
        "schema_version": 1,
        "phase": "deterministic-preflight-intake",
        "llm_safe": True,
        "sanitized_request": {
            "task": inputs.task,
            "mode": inputs.mode,
            "intent": intent,
            "business_need": _sanitize_free_text(inputs.business_need or ""),
            "classification": inputs.classification,
            "expected_output": inputs.expected_output,
            "error": {
                "code": None,
                "detail_kind": "preflight",
                "context": {
                    "context_budget_tokens": context_budget,
                    "largest_auto_recipe_context_budget_tokens": None,
                },
                "rejections": [],
            },
            "input_summary": {
                "content_types": sorted(set(inputs.content_types)),
                "max_bytes": max_bytes,
                "input_count": len(inputs.content_types) or len(inputs.byte_values),
                "prompt_lengths": [],
            },
            "desired_capabilities": desired_capabilities,
        },
        "redaction_report": {
            "removed_fields": [],
            "sensitive_keys": sorted(SENSITIVE_KEYS),
            "prompt_body_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
        },
        "redacted_error_payload": {},
    }


def intake_from_quality_feedback(inputs: QualityFeedbackInputs) -> dict[str, Any]:
    intent = normalize_intent(inputs.intent)
    desired_capabilities = _capabilities_for(task=inputs.task, intent=intent)
    max_bytes = max(inputs.byte_values) if inputs.byte_values else None
    context_budget = inputs.context_budget_tokens if inputs.context_budget_tokens is not None else inputs.required_model_len
    return {
        "schema_version": 1,
        "phase": "deterministic-quality-feedback-intake",
        "llm_safe": True,
        "sanitized_request": {
            "task": inputs.task,
            "mode": inputs.mode,
            "intent": intent,
            "business_need": _sanitize_free_text(inputs.business_need or ""),
            "classification": inputs.classification,
            "expected_output": inputs.expected_output,
            "error": {
                "code": "LOW_QUALITY_SUCCESS",
                "detail_kind": "quality_feedback",
                "failure_kind": "low_quality_success",
                "caller_action": "submit_quality_feedback_to_gpucall_admin",
                "capability_gap": _quality_capability_gap(inputs.quality_failure_kind),
                "context": {
                    "context_budget_tokens": context_budget,
                    "largest_auto_recipe_context_budget_tokens": None,
                },
                "rejections": [],
            },
            "input_summary": {
                "content_types": sorted(set(inputs.content_types)),
                "max_bytes": max_bytes,
                "input_count": len(inputs.content_types) or len(inputs.byte_values),
                "prompt_lengths": [],
                "dimensions": sorted(set(inputs.dimensions)),
            },
            "runtime_selection": {
                "observed_recipe": inputs.observed_recipe,
                "observed_tuple": inputs.observed_tuple,
                "observed_tuple_model": inputs.observed_tuple_model,
                "output_validated": inputs.output_validated,
            },
            "quality_feedback": {
                "kind": _sanitize_quality_kind(inputs.quality_failure_kind),
                "reason": _sanitize_free_text(inputs.quality_failure_reason),
                "observed_output_kind": _sanitize_free_text(inputs.observed_output_kind or ""),
                "output_contract_feedback": _output_contract_feedback(inputs),
            },
            "desired_capabilities": desired_capabilities,
        },
        "redaction_report": {
            "removed_fields": [],
            "sensitive_keys": sorted(SENSITIVE_KEYS),
            "prompt_body_forwarded": False,
            "message_content_forwarded": False,
            "data_ref_uri_forwarded": False,
            "presigned_url_forwarded": False,
            "output_body_forwarded": False,
        },
        "redacted_error_payload": {},
    }


def compare_preflight_to_failure(preflight: Mapping[str, Any], failure_intake: Mapping[str, Any]) -> dict[str, Any]:
    before = _as_mapping(preflight.get("sanitized_request"))
    after = _as_mapping(failure_intake.get("sanitized_request"))
    checks = [
        ("task", before.get("task"), after.get("task")),
        ("mode", before.get("mode"), after.get("mode")),
        ("intent", before.get("intent"), after.get("intent")),
        ("classification", before.get("classification"), after.get("classification")),
        ("expected_output", before.get("expected_output"), after.get("expected_output")),
        (
            "context_budget_tokens",
            _context_budget_from_context(_as_mapping(_as_mapping(before.get("error")).get("context"))),
            _context_budget_from_context(_as_mapping(_as_mapping(after.get("error")).get("context"))),
        ),
        ("content_types", _as_mapping(before.get("input_summary")).get("content_types"), _as_mapping(after.get("input_summary")).get("content_types")),
        ("max_bytes", _as_mapping(before.get("input_summary")).get("max_bytes"), _as_mapping(after.get("input_summary")).get("max_bytes")),
        ("desired_capabilities", before.get("desired_capabilities"), after.get("desired_capabilities")),
    ]
    differences = [
        {"field": field, "preflight": expected, "actual": actual}
        for field, expected, actual in checks
        if _normalized(expected) != _normalized(actual)
    ]
    if not differences:
        classification = "preflight_matched_runtime_failure"
        action = "check admin status and catalog/runtime failures"
    elif any(item["field"] in {"task", "context_budget_tokens", "content_types", "desired_capabilities"} for item in differences):
        classification = "workload_drift"
        action = "submit updated intake"
    else:
        classification = "metadata_drift"
        action = "review caller preflight metadata"
    return {
        "schema_version": 1,
        "phase": "preflight-failure-compare",
        "preflight_matched_actual": not differences,
        "classification": classification,
        "differences": differences,
        "recommended_action": action,
    }


def draft_from_intake(intake: Mapping[str, Any]) -> dict[str, Any]:
    sanitized = _as_mapping(intake.get("sanitized_request"))
    task = _str_or_none(sanitized.get("task")) or "infer"
    intent = normalize_intent(_str_or_none(sanitized.get("intent"))) or task
    capabilities = [str(item) for item in sanitized.get("desired_capabilities") or TASK_DEFAULT_CAPABILITIES.get(task, [])]
    classification = _str_or_none(sanitized.get("classification")) or "confidential"
    context_budget_tokens = _round_context_budget(_context_budget_from_context(_as_mapping(_as_mapping(sanitized.get("error")).get("context"))))
    recipe_name = _recipe_name(task, intent)
    return {
        "schema_version": 1,
        "phase": "draft",
        "source": "sanitized_request_only",
        "human_review_required": True,
        "proposed_recipe": {
            "recipe_schema_version": 3,
            "name": recipe_name,
            "task": task,
            "intent": intent,
            "auto_select": True,
            "data_classification": classification,
            "allowed_modes": [_str_or_none(sanitized.get("mode")) or "sync"],
            "required_model_capabilities": capabilities,
            "context_budget_tokens": context_budget_tokens,
            "resource_class": _resource_class_for(task, context_budget_tokens),
            "latency_class": _latency_class_for(context_budget_tokens),
            "quality_floor": "draft",
            "token_estimation_profile": "generic_utf8",
            "allowed_mime_prefixes": _mime_prefixes_for(task),
            "output_contract": sanitized.get("expected_output") or "plain_text",
        },
        "workload_contract": {
            "required_capabilities": capabilities,
            "context_budget_tokens": context_budget_tokens,
            "input_contracts": _input_contracts_for(task),
            "output_contract": sanitized.get("expected_output") or "plain_text",
        },
        "operator_notes": [
            "This draft was produced from sanitized metadata only.",
            "Do not commit this draft directly; materialize it through the gpucall admin workflow and run validation.",
            "If the caller's intent is wrong or too broad, revise the intent before adding a production recipe.",
        ],
    }


def _capabilities_for(*, task: str, intent: str | None) -> list[str]:
    return capabilities_for(task=task, intent=intent)


def _quality_capability_gap(kind: str) -> str:
    normalized = _sanitize_quality_kind(kind)
    if normalized in {"weak_model", "wrong_capability", "insufficient_ocr", "insufficient_document_understanding"}:
        return "model_or_recipe_capability_mismatch"
    if normalized in {"insufficient_context", "truncated_output"}:
        return "context_or_output_budget_insufficient"
    if normalized in {"insufficient_structured_output", "malformed_business_output", "schema_mismatch", "missing_required_json_field"}:
        return "output_contract_insufficient"
    return "quality_expectation_not_met"


def _sanitize_quality_kind(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
    return cleaned[:80] or "low_quality_success"


def _output_contract_feedback(inputs: QualityFeedbackInputs) -> dict[str, Any]:
    expected_schema = _sanitize_schema(inputs.expected_json_schema)
    observed_schema = _sanitize_schema(inputs.observed_json_schema)
    return {
        "response_format": _sanitize_response_format(inputs.response_format),
        "expected_json_schema": expected_schema,
        "observed_json_schema": observed_schema,
        "schema_success_count": _non_negative_int(inputs.schema_success_count),
        "schema_failure_count": _non_negative_int(inputs.schema_failure_count),
        "raw_output_forwarded": False,
    }


def _sanitize_response_format(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = re.sub(r"[^a-z0-9_]+", "_", value.strip().lower()).strip("_")
    if cleaned in {"text", "json_object", "json_schema"}:
        return cleaned
    return cleaned[:40] or None


def _non_negative_int(value: int | None) -> int | None:
    if value is None:
        return None
    return max(int(value), 0)


_SCHEMA_KEYS = {
    "$schema",
    "additionalItems",
    "additionalProperties",
    "allOf",
    "anyOf",
    "items",
    "maxItems",
    "maxLength",
    "maximum",
    "minItems",
    "minLength",
    "minimum",
    "oneOf",
    "pattern",
    "properties",
    "required",
    "type",
}


def _sanitize_schema(value: Mapping[str, Any] | None, *, depth: int = 0) -> dict[str, Any] | None:
    if value is None:
        return None
    if depth > 8:
        return {"truncated": True}
    cleaned: dict[str, Any] = {}
    for key in sorted(str(item) for item in value):
        if key not in _SCHEMA_KEYS:
            continue
        item = value.get(key)
        if key == "properties" and isinstance(item, Mapping):
            cleaned[key] = {
                _sanitize_schema_name(str(prop_key)): _sanitize_schema(_as_mapping(prop_value), depth=depth + 1) or {}
                for prop_key, prop_value in sorted(item.items(), key=lambda pair: str(pair[0]))
                if _sanitize_schema_name(str(prop_key))
            }
            continue
        if key == "required" and isinstance(item, list):
            cleaned[key] = [_sanitize_schema_name(str(entry)) for entry in item if _sanitize_schema_name(str(entry))][:100]
            continue
        if key in {"items", "additionalProperties"} and isinstance(item, Mapping):
            cleaned[key] = _sanitize_schema(item, depth=depth + 1)
            continue
        if key in {"allOf", "anyOf", "oneOf"} and isinstance(item, list):
            cleaned[key] = [_sanitize_schema(_as_mapping(entry), depth=depth + 1) or {} for entry in item[:20]]
            continue
        if isinstance(item, bool | int | float):
            cleaned[key] = item
            continue
        if isinstance(item, str):
            cleaned[key] = _sanitize_schema_text(item)
    return cleaned or None


def _sanitize_schema_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:-]+", "_", value.strip()).strip("_")
    return cleaned[:120]


def _sanitize_schema_text(value: str) -> str:
    cleaned = re.sub(r"\s+", " ", value.strip())
    return cleaned[:120]


def _extract_rejections(error: Mapping[str, Any], context: Mapping[str, Any]) -> list[str]:
    failure_artifact = _failure_artifact(error)
    matrix = _as_mapping(failure_artifact.get("rejection_matrix"))
    recipe_matrix = _as_mapping(matrix.get("recipes"))
    tuple_matrix = _as_mapping(matrix.get("tuples"))
    if recipe_matrix or tuple_matrix:
        return [f"{name}: {reason}" for name, reason in sorted({**recipe_matrix, **tuple_matrix}.items())]
    raw = context.get("rejections")
    if isinstance(raw, list):
        return [str(item) for item in raw]
    detail = error.get("detail")
    if not isinstance(detail, str) or ":" not in detail:
        return []
    _, _, tail = detail.partition(":")
    return [part.strip() for part in tail.split(";") if part.strip()]


def _failure_artifact(error: Mapping[str, Any]) -> Mapping[str, Any]:
    direct = _as_mapping(error.get("failure_artifact"))
    if direct:
        return direct
    return _as_mapping(_as_mapping(error.get("error")).get("gpucall_failure_artifact"))


def _input_summary_from_failure_artifact(safe_summary: Mapping[str, Any]) -> dict[str, Any] | None:
    if not safe_summary:
        return None
    content_types = sorted(
        {
            str(value)
            for key in ("input_ref_content_types", "inline_input_content_types")
            for value in _list_or_empty(safe_summary.get(key))
        }
    )
    byte_values = [
        value
        for value in (
            safe_summary.get("input_ref_max_bytes"),
            safe_summary.get("message_max_bytes"),
        )
        if isinstance(value, int)
    ]
    input_count = 0
    for key in ("input_ref_count", "inline_input_count", "message_count"):
        value = safe_summary.get(key)
        if isinstance(value, int):
            input_count += value
    return {
        "content_types": content_types,
        "max_bytes": max(byte_values) if byte_values else None,
        "input_count": input_count,
        "prompt_lengths": [safe_summary["message_max_bytes"]] if isinstance(safe_summary.get("message_max_bytes"), int) else [],
    }


def _list_or_empty(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _input_summary(payload: Any) -> dict[str, Any]:
    refs = _find_dicts_with_any_key(payload, {"content_type", "bytes"})
    content_types = sorted({str(ref["content_type"]) for ref in refs if ref.get("content_type")})
    byte_values = [int(ref["bytes"]) for ref in refs if isinstance(ref.get("bytes"), int)]
    prompt_lengths = _prompt_lengths(payload)
    return {
        "content_types": content_types,
        "max_bytes": max(byte_values) if byte_values else None,
        "input_count": len(refs),
        "prompt_lengths": prompt_lengths,
    }


def _prompt_lengths(payload: Any) -> list[int]:
    lengths: list[int] = []
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            if lowered in {"prompt", "content", "value"} and isinstance(value, str):
                lengths.append(len(value.encode("utf-8")))
            else:
                lengths.extend(_prompt_lengths(value))
    elif isinstance(payload, list):
        for item in payload:
            lengths.extend(_prompt_lengths(item))
    return lengths


def _find_dicts_with_any_key(payload: Any, keys: set[str]) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    if isinstance(payload, Mapping):
        if any(key in payload for key in keys):
            found.append(payload)
        for value in payload.values():
            found.extend(_find_dicts_with_any_key(value, keys))
    elif isinstance(payload, list):
        for item in payload:
            found.extend(_find_dicts_with_any_key(item, keys))
    return found


def _redact(payload: Any) -> Any:
    if isinstance(payload, Mapping):
        redacted: dict[str, Any] = {}
        for key, value in payload.items():
            if str(key).lower() in SENSITIVE_KEYS:
                redacted[key] = _redacted_value(value)
            else:
                redacted[key] = _redact(value)
        return redacted
    if isinstance(payload, list):
        return [_redact(item) for item in payload]
    return payload


def _redacted_value(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"redacted": True, "type": "str", "utf8_bytes": len(value.encode("utf-8"))}
    if isinstance(value, list):
        return {"redacted": True, "type": "list", "items": len(value)}
    if isinstance(value, Mapping):
        return {"redacted": True, "type": "object", "keys": sorted(str(key) for key in value)}
    return {"redacted": True, "type": type(value).__name__}


def _removed_paths(payload: Any, prefix: str = "$") -> set[str]:
    removed: set[str] = set()
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            path = f"{prefix}.{key}"
            if str(key).lower() in SENSITIVE_KEYS:
                removed.add(path)
            else:
                removed.update(_removed_paths(value, path))
    elif isinstance(payload, list):
        for index, item in enumerate(payload):
            removed.update(_removed_paths(item, f"{prefix}[{index}]"))
    return removed


def _first_present(payload: Mapping[str, Any], key: str, nested: tuple[str, str]) -> str | None:
    value = payload.get(key)
    if value:
        return str(value)
    parent = payload.get(nested[0])
    if isinstance(parent, Mapping) and parent.get(nested[1]):
        return str(parent[nested[1]])
    return None


def _infer_code_from_detail(error: Mapping[str, Any]) -> str | None:
    detail = error.get("detail")
    if isinstance(detail, str) and "no auto-selectable recipe" in detail:
        return "NO_AUTO_SELECTABLE_RECIPE"
    return None


def _task_from_detail(detail: Any) -> str | None:
    if not isinstance(detail, str):
        return None
    match = re.search(r"task '([^']+)'", detail)
    return match.group(1) if match else None


def _expected_output_from_error(error: Mapping[str, Any]) -> str:
    response_format = error.get("response_format")
    if isinstance(response_format, Mapping) and response_format.get("type") in {"json_object", "json_schema"}:
        return "json"
    return "plain_text"


def _detail_kind(detail: Any) -> str:
    if isinstance(detail, str) and "no auto-selectable recipe" in detail:
        return "recipe_selection_failure"
    if isinstance(detail, str) and "no eligible tuple" in detail:
        return "tuple_selection_failure"
    return "unknown"


def _context_budget_from_context(context: Mapping[str, Any]) -> int | None:
    return _first_int(context, "context_budget_tokens", "required_model_len")


def _first_int(payload: Mapping[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _round_context_budget(value: Any) -> int:
    try:
        required = int(value)
    except (TypeError, ValueError):
        required = 8192
    for candidate in (8192, 32768, 65536, 131072, 262144, 524288, 1048576):
        if required <= candidate:
            return candidate
    return required


def _recipe_name(task: str, intent: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", intent.lower()).strip("-")
    return f"{task}-{cleaned or 'standard'}-draft"


def _mime_prefixes_for(task: str) -> list[str]:
    if task == "vision":
        return ["image/"]
    if task == "transcribe":
        return ["audio/"]
    if task == "video":
        return ["video/"]
    return ["text/", "application/json"]


def _input_contracts_for(task: str) -> list[str]:
    if task == "vision":
        return ["image", "data_refs", "text"]
    if task == "transcribe":
        return ["data_refs"]
    if task == "video":
        return ["data_refs"]
    return ["text", "chat_messages", "data_refs"]


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


def _latency_class_for(context_budget_tokens: int) -> str:
    if context_budget_tokens >= 524288:
        return "long_running"
    if context_budget_tokens >= 65536:
        return "batch"
    return "standard"


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _str_or_none(value: Any) -> str | None:
    return str(value) if value is not None else None


def _normalized(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(_normalized(item) for item in value)
    return value


def _sanitize_free_text(value: str) -> str:
    value = value.strip()
    if not value:
        return ""
    value = re.sub(r"https?://\S+", "[redacted-url]", value)
    value = re.sub(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b", "[redacted-email]", value)
    return value[:1000]


def dumps_json(data: Mapping[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
