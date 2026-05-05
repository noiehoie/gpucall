from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


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

CAPABILITY_BY_INTENT = {
    "answer_question_about_image": ["visual_question_answering", "instruction_following"],
    "caption_image": ["image_captioning"],
    "understand_document_image": ["document_understanding", "visual_question_answering", "instruction_following"],
    "transcribe_audio": ["speech_to_text"],
    "summarize_audio": ["speech_to_text", "summarization"],
    "summarize_video": ["video_understanding", "summarization"],
    "translate_text": ["translation"],
    "summarize_text": ["summarization"],
    "extract_json": ["structured_output"],
}

TASK_DEFAULT_CAPABILITIES = {
    "infer": ["instruction_following"],
    "vision": ["visual_question_answering", "instruction_following"],
    "transcribe": ["speech_to_text"],
    "video": ["video_understanding"],
}

VRAM_BY_TASK = {
    "infer": 24,
    "vision": 80,
    "transcribe": 24,
    "video": 80,
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
    required_model_len: int | None = None


def intake_from_error(inputs: DraftInputs) -> dict[str, Any]:
    error = dict(inputs.error_payload)
    failure_artifact = _failure_artifact(error)
    safe_summary = _as_mapping(failure_artifact.get("safe_request_summary"))
    code = _first_present(error, "code", ("error", "code")) or _infer_code_from_detail(error)
    if code is None:
        code = _str_or_none(failure_artifact.get("code"))
    context = _as_mapping(error.get("context")) or _as_mapping(failure_artifact.get("context"))
    task = inputs.task or _str_or_none(context.get("task")) or _str_or_none(safe_summary.get("task")) or _task_from_detail(error.get("detail")) or "infer"
    mode = inputs.mode or _str_or_none(context.get("mode")) or _str_or_none(safe_summary.get("mode")) or "sync"
    rejections = _extract_rejections(error, context)
    input_summary = _input_summary_from_failure_artifact(safe_summary) or _input_summary(error)
    llm_safe_business_need = _sanitize_free_text(inputs.business_need or "")
    desired_capabilities = _capabilities_for(task=task, intent=inputs.intent)
    sanitized_request = {
        "task": task,
        "mode": mode,
        "intent": inputs.intent,
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
                "required_model_len": context.get("required_model_len"),
                "largest_auto_recipe_model_len": context.get("largest_auto_recipe_model_len"),
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
    desired_capabilities = _capabilities_for(task=inputs.task, intent=inputs.intent)
    max_bytes = max(inputs.byte_values) if inputs.byte_values else None
    return {
        "schema_version": 1,
        "phase": "deterministic-preflight-intake",
        "llm_safe": True,
        "sanitized_request": {
            "task": inputs.task,
            "mode": inputs.mode,
            "intent": inputs.intent,
            "business_need": _sanitize_free_text(inputs.business_need or ""),
            "classification": inputs.classification,
            "expected_output": inputs.expected_output,
            "error": {
                "code": None,
                "detail_kind": "preflight",
                "context": {
                    "required_model_len": inputs.required_model_len,
                    "largest_auto_recipe_model_len": None,
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
            "required_model_len",
            _as_mapping(_as_mapping(before.get("error")).get("context")).get("required_model_len"),
            _as_mapping(_as_mapping(after.get("error")).get("context")).get("required_model_len"),
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
        action = "check admin status and provider/runtime failures"
    elif any(item["field"] in {"task", "required_model_len", "content_types", "desired_capabilities"} for item in differences):
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
    intent = _str_or_none(sanitized.get("intent")) or task
    capabilities = [str(item) for item in sanitized.get("desired_capabilities") or TASK_DEFAULT_CAPABILITIES.get(task, [])]
    classification = _str_or_none(sanitized.get("classification")) or "confidential"
    required_model_len = _as_mapping(_as_mapping(sanitized.get("error")).get("context")).get("required_model_len")
    max_model_len = _round_model_len(required_model_len)
    recipe_name = _recipe_name(task, intent)
    min_vram = _vram_for(task, capabilities, max_model_len)
    return {
        "schema_version": 1,
        "phase": "draft",
        "source": "sanitized_request_only",
        "human_review_required": True,
        "proposed_recipe": {
            "name": recipe_name,
            "task": task,
            "auto_select": True,
            "data_classification": classification,
            "allowed_modes": [_str_or_none(sanitized.get("mode")) or "sync"],
            "required_model_capabilities": capabilities,
            "min_vram_gb": min_vram,
            "max_model_len": max_model_len,
            "allowed_mime_prefixes": _mime_prefixes_for(task),
            "output_contract": sanitized.get("expected_output") or "plain_text",
        },
        "provider_requirements": {
            "model_capabilities": capabilities,
            "instruction_tuned": "instruction_following" in capabilities,
            "min_vram_gb": min_vram,
            "min_model_len": max_model_len,
            "input_contracts": _input_contracts_for(task),
        },
        "operator_notes": [
            "This draft was produced from sanitized metadata only.",
            "Do not commit this draft directly; map it to gpucall's canonical recipe/provider schema and run validation.",
            "If the caller's intent is wrong or too broad, revise the intent before adding a production recipe.",
        ],
    }


def _capabilities_for(*, task: str, intent: str | None) -> list[str]:
    if intent and intent in CAPABILITY_BY_INTENT:
        return CAPABILITY_BY_INTENT[intent]
    return TASK_DEFAULT_CAPABILITIES.get(task, ["instruction_following"])


def _extract_rejections(error: Mapping[str, Any], context: Mapping[str, Any]) -> list[str]:
    failure_artifact = _failure_artifact(error)
    matrix = _as_mapping(failure_artifact.get("rejection_matrix"))
    recipe_matrix = _as_mapping(matrix.get("recipes"))
    provider_matrix = _as_mapping(matrix.get("providers"))
    if recipe_matrix or provider_matrix:
        return [f"{name}: {reason}" for name, reason in sorted({**recipe_matrix, **provider_matrix}.items())]
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
    if isinstance(detail, str) and "no eligible provider" in detail:
        return "provider_selection_failure"
    return "unknown"


def _round_model_len(value: Any) -> int:
    try:
        required = int(value)
    except (TypeError, ValueError):
        required = 8192
    for candidate in (8192, 32768, 65536, 131072, 262144, 524288, 1048576):
        if required <= candidate:
            return candidate
    return required


def _vram_for(task: str, capabilities: list[str], max_model_len: int) -> int:
    base = VRAM_BY_TASK.get(task, 24)
    if max_model_len > 131072:
        base = max(base, 80)
    if any(capability in capabilities for capability in {"document_understanding", "video_understanding"}):
        base = max(base, 80)
    return base


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
