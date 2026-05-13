from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from gpucall.domain import ChatMessage, ExecutionMode, ResponseFormat, TaskRequest
from gpucall.openai_contract import (
    OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS,
    OPENAI_CHAT_COMPLETIONS_FIELDS,
    OPENAI_CHAT_COMPLETIONS_FEATURE_GATED_FIELDS,
    OPENAI_CHAT_COMPLETIONS_REQUEST_SCHEMA,
)


GPUCALL_OPENAI_EXTENSION_FIELDS = frozenset({"intent", "task_family"})
_REQUEST_VALIDATOR = Draft202012Validator(OPENAI_CHAT_COMPLETIONS_REQUEST_SCHEMA)


class OpenAIProtocolError(ValueError):
    def __init__(self, message: str, *, code: str = "invalid_request_error", status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class OpenAIChatAdmission:
    task_request: TaskRequest
    requested_model: str
    stream: bool
    raw_supported_payload: dict[str, Any]
    gpucall_extensions: dict[str, Any]


def admit_openai_chat_completion(payload: Mapping[str, Any], *, inline_bytes_limit: int) -> OpenAIChatAdmission:
    raw = dict(payload)
    extensions = {key: raw.pop(key) for key in list(raw) if key in GPUCALL_OPENAI_EXTENSION_FIELDS}
    _validate_official_request_schema(raw)
    _reject_unsupported_fields(raw)

    messages = [openai_message_to_chat_message(message) for message in raw.get("messages") or []]
    if _openai_chat_message_bytes(messages) > inline_bytes_limit:
        raise OpenAIProtocolError(
            "OpenAI facade inline prompt exceeds policy limit; use the gpucall SDK DataRef upload path for large inputs",
            code="payload_too_large",
            status_code=413,
        )
    response_format = ResponseFormat.model_validate(raw["response_format"]) if raw.get("response_format") is not None else None
    task_request = TaskRequest(
        task="infer",
        mode=ExecutionMode.STREAM if raw.get("stream") else ExecutionMode.SYNC,
        intent=_openai_request_intent(raw, extensions),
        messages=messages,
        max_tokens=raw.get("max_tokens") or raw.get("max_completion_tokens"),
        temperature=raw.get("temperature"),
        top_p=raw.get("top_p"),
        stop=raw.get("stop"),
        seed=raw.get("seed"),
        presence_penalty=raw.get("presence_penalty"),
        frequency_penalty=raw.get("frequency_penalty"),
        tools=raw.get("tools"),
        tool_choice=raw.get("tool_choice"),
        functions=raw.get("functions"),
        function_call=raw.get("function_call"),
        stream_options=raw.get("stream_options"),
        response_format=response_format,
        metadata=_openai_request_metadata(raw, extensions),
    )
    return OpenAIChatAdmission(
        task_request=task_request,
        requested_model=str(raw.get("model") or "gpucall:auto"),
        stream=bool(raw.get("stream")),
        raw_supported_payload=raw,
        gpucall_extensions=extensions,
    )


def _validate_official_request_schema(payload: Mapping[str, Any]) -> None:
    errors = sorted(_REQUEST_VALIDATOR.iter_errors(payload), key=lambda error: list(error.path))
    if not errors:
        return
    error = errors[0]
    field = ".".join(str(part) for part in error.path) or "<request>"
    raise OpenAIProtocolError(
        f"OpenAI chat.completions request does not match vendored OpenAI schema at {field}: {_safe_jsonschema_message(error)}",
        code="invalid_request_error",
        status_code=400,
    )


def _safe_jsonschema_message(error: ValidationError) -> str:
    if error.validator in {"oneOf", "anyOf", "allOf"}:
        return "value does not match the required OpenAI schema"
    return str(error.message).replace("\n", " ")


def _reject_unsupported_fields(payload: Mapping[str, Any]) -> None:
    unsupported: list[str] = []
    for key in sorted(str(key) for key in payload):
        if key in OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS:
            unsupported.append(key)
        elif key not in OPENAI_CHAT_COMPLETIONS_FIELDS:
            unsupported.append(f"unknown.{key}")
    if payload.get("n") not in (None, 1):
        unsupported.append("n > 1")
    if (
        payload.get("max_tokens") is not None
        and payload.get("max_completion_tokens") is not None
        and payload.get("max_tokens") != payload.get("max_completion_tokens")
    ):
        unsupported.append("max_tokens and max_completion_tokens conflict")
    stream_options = payload.get("stream_options")
    if isinstance(stream_options, Mapping):
        allowed = {
            str(field).removeprefix("stream_options.")
            for field in OPENAI_CHAT_COMPLETIONS_FEATURE_GATED_FIELDS
            if str(field).startswith("stream_options.")
        }
        for key in sorted(str(key) for key in stream_options if str(key) not in allowed):
            unsupported.append(f"stream_options.{key}")
        if not payload.get("stream"):
            unsupported.append("stream_options_without_stream")
        if "include_usage" in stream_options and not isinstance(stream_options.get("include_usage"), bool):
            unsupported.append("stream_options.include_usage")
        if stream_options.get("include_usage") is True:
            unsupported.append("stream_options.include_usage")
        if "include_obfuscation" in stream_options and not isinstance(stream_options.get("include_obfuscation"), bool):
            unsupported.append("stream_options.include_obfuscation")
        if stream_options.get("include_obfuscation") is True:
            unsupported.append("stream_options.include_obfuscation")
    if payload.get("stream") is True and payload.get("response_format") is not None:
        unsupported.append("stream.response_format")
    if not unsupported:
        return
    fields = ", ".join(sorted(set(unsupported)))
    raise OpenAIProtocolError(
        f"OpenAI facade does not support these fields yet: {fields}",
        code="unsupported_openai_field",
        status_code=400,
    )


def openai_message_to_chat_message(message: Mapping[str, Any]) -> ChatMessage:
    payload = dict(message)
    if payload.get("content") is not None:
        payload["content"] = _message_content_to_text(payload.get("content"))
    return ChatMessage.model_validate(payload)


def _message_content_to_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise OpenAIProtocolError("OpenAI message content must be a string or text-only content parts")
    parts: list[str] = []
    unsupported: list[str] = []
    for item in content:
        if not isinstance(item, Mapping):
            unsupported.append("<non-object>")
            continue
        kind = str(item.get("type") or "")
        if kind in {"text", "input_text"} and isinstance(item.get("text"), str):
            parts.append(item["text"])
            continue
        unsupported.append(kind or "<missing>")
    if parts and not unsupported:
        return "".join(parts)
    raise OpenAIProtocolError(
        "OpenAI facade accepts string or text-only content parts; use gpucall DataRef APIs for image/file inputs"
    )


def _openai_chat_message_bytes(messages: list[ChatMessage]) -> int:
    total = 0
    for message in messages:
        payload = message.model_dump(mode="json", exclude_none=True)
        total += len(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return total


def _openai_request_intent(payload: Mapping[str, Any], extensions: Mapping[str, Any]) -> str | None:
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    for value in (
        extensions.get("intent"),
        extensions.get("task_family"),
        metadata.get("intent"),
        metadata.get("task_family"),
        metadata.get("gpucall_intent"),
        metadata.get("gpucall_task_family"),
    ):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _openai_request_metadata(payload: Mapping[str, Any], extensions: Mapping[str, Any]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    raw_metadata = payload.get("metadata") if isinstance(payload.get("metadata"), Mapping) else {}
    for key, value in raw_metadata.items():
        if value is not None:
            metadata[str(key)] = _metadata_value(value)
    metadata["openai.model"] = str(payload.get("model") or "gpucall:auto")
    optional_fields = {
        "openai.user": payload.get("user"),
        "openai.top_p": payload.get("top_p"),
        "openai.stop": payload.get("stop"),
        "openai.seed": payload.get("seed"),
        "openai.tools": payload.get("tools"),
        "openai.tool_choice": payload.get("tool_choice"),
        "openai.functions": payload.get("functions"),
        "openai.function_call": payload.get("function_call"),
        "openai.presence_penalty": payload.get("presence_penalty"),
        "openai.frequency_penalty": payload.get("frequency_penalty"),
        "openai.n": payload.get("n"),
        "openai.max_completion_tokens": payload.get("max_completion_tokens"),
        "openai.stream_options": payload.get("stream_options"),
        "gpucall.intent": extensions.get("intent"),
        "gpucall.task_family": extensions.get("task_family"),
    }
    for key, value in optional_fields.items():
        if value is not None:
            metadata[key] = _metadata_value(value)
    intent = _openai_request_intent(payload, extensions)
    if intent:
        metadata.setdefault("intent", intent)
    return metadata


def _metadata_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
