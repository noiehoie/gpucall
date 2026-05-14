from __future__ import annotations

import re
from typing import Any

from gpucall.domain import CompiledPlan, ResponseFormatType, TupleResult
from gpucall.domain import TupleError


def plan_payload(plan: CompiledPlan) -> dict[str, Any]:
    """Build a tuple payload without dereferencing sensitive object data."""
    trust_policy = plan.attestations.get("model_trust_policy") if isinstance(plan.attestations, dict) else None
    return {
        "plan_id": plan.plan_id,
        "task": plan.task,
        "recipe": plan.recipe_name,
        "mode": plan.mode.value,
        "data_classification": plan.data_classification.value,
        "token_estimation_profile": plan.token_estimation_profile,
        "token_budget": plan.token_budget,
        "max_tokens": plan.max_tokens,
        "temperature": plan.temperature,
        "top_p": plan.top_p,
        "seed": plan.seed,
        "presence_penalty": plan.presence_penalty,
        "frequency_penalty": plan.frequency_penalty,
        "tools": plan.tools,
        "tool_choice": plan.tool_choice,
        "functions": plan.functions,
        "function_call": plan.function_call,
        "stream_options": plan.stream_options,
        "n": plan.n,
        "input_refs": [ref.model_dump(mode="json") for ref in plan.input_refs],
        "inline_inputs": {key: value.model_dump(mode="json") for key, value in plan.inline_inputs.items()},
        "messages": [message.model_dump(mode="json") for message in plan.messages],
        "response_format": plan.response_format.model_dump(mode="json") if plan.response_format is not None else None,
        "metadata": dict(plan.metadata),
        "artifact_export": plan.artifact_export.model_dump(mode="json") if plan.artifact_export is not None else None,
        "split_learning": plan.split_learning.model_dump(mode="json") if plan.split_learning is not None else None,
        "system_prompt": plan.system_prompt,
        "stop_tokens": plan.stop_tokens,
        "repetition_penalty": plan.repetition_penalty,
        "guided_decoding": plan.guided_decoding,
        "trust_remote_code": bool(trust_policy.get("trust_remote_code")) if isinstance(trust_policy, dict) else False,
        "attestations": plan.attestations,
    }


def openai_chat_payload_from_plan(
    plan: CompiledPlan,
    *,
    model: str,
    stream: bool,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the OpenAI-compatible worker request from a compiled plan.

    Generation is delegated to vLLM/OpenAI-compatible workers; gateway code only
    forwards the official fields it accepted at the facade boundary plus
    gpucall-selected model/route information.
    """
    payload: dict[str, Any] = {
        "model": model,
        "messages": messages if messages is not None else _openai_messages_from_plan(plan),
        "stream": stream,
    }
    optional = {
        "temperature": plan.temperature,
        "max_tokens": plan.max_tokens,
        "top_p": plan.top_p,
        "seed": plan.seed,
        "presence_penalty": plan.presence_penalty,
        "frequency_penalty": plan.frequency_penalty,
        "stop": plan.stop_tokens if plan.stop_tokens else None,
        "tools": plan.tools,
        "tool_choice": plan.tool_choice,
        "functions": plan.functions,
        "function_call": plan.function_call,
        "stream_options": plan.stream_options,
        "n": plan.n,
        "response_format": _openai_response_format(plan) if plan.response_format is not None else None,
    }
    for key, value in optional.items():
        if value is not None:
            payload[key] = value
    return payload


def _openai_response_format(plan: CompiledPlan) -> dict[str, Any]:
    response_format = plan.response_format
    if response_format is None:
        raise TupleError("OpenAI response_format requested without response format", retryable=False, status_code=400)
    if response_format.type is not ResponseFormatType.JSON_SCHEMA:
        return response_format.model_dump(mode="json")
    if response_format.json_schema is None:
        raise TupleError("json_schema response_format requires schema", retryable=False, status_code=400)
    return {
        "type": "json_schema",
        "json_schema": {
            "name": _schema_name(plan.recipe_name),
            "strict": bool(response_format.strict),
            "schema": response_format.json_schema,
        },
    }


def _schema_name(recipe_name: str) -> str:
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", recipe_name).strip("_")
    return name[:64] or "gpucall_response"


def _openai_messages_from_plan(plan: CompiledPlan) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    has_system_message = any(message.role == "system" for message in plan.messages)
    if plan.system_prompt and not has_system_message:
        messages.append({"role": "system", "content": plan.system_prompt})
    for message in plan.messages:
        item = message.model_dump(mode="json", exclude_none=True)
        messages.append(item)
    if plan.messages:
        return messages
    if "prompt" in plan.inline_inputs:
        return [*messages, {"role": "user", "content": plan.inline_inputs["prompt"].value}]
    if plan.inline_inputs:
        return [*messages, {"role": "user", "content": "\n".join(value.value for value in plan.inline_inputs.values())}]
    return messages or [{"role": "user", "content": ""}]


def gpucall_tuple_result(value: Any) -> TupleResult:
    if isinstance(value, TupleResult):
        return value
    if isinstance(value, dict):
        if value.get("kind") in {"inline", "ref", "artifact_manifest"}:
            return TupleResult.model_validate(value)
    raise TupleError(f"tuple output does not match gpucall TupleResult contract: {type(value).__name__}", retryable=True, status_code=502)


def plain_text_result(value: Any) -> TupleResult:
    if isinstance(value, str):
        return TupleResult(kind="inline", value=value)
    raise TupleError(f"tuple output does not match plain text contract: {type(value).__name__}", retryable=True, status_code=502)


def ollama_generate_result(value: Any) -> TupleResult:
    if not isinstance(value, dict) or not isinstance(value.get("response"), str):
        raise TupleError("Ollama response missing string 'response' field", retryable=True, status_code=502)
    return TupleResult(kind="inline", value=value["response"])


def openai_chat_completion_result(value: Any) -> TupleResult:
    if not isinstance(value, dict):
        raise TupleError("OpenAI-compatible response must be an object", retryable=True, status_code=502)
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise TupleError("OpenAI-compatible response missing choices", retryable=True, status_code=502)
    normalized_choices: list[dict[str, Any]] = []
    first_content: str | None = None
    first_tool_calls: list[dict[str, Any]] | None = None
    first_function_call: dict[str, Any] | None = None
    first_finish_reason: str | None = None
    for index, raw_choice in enumerate(choices):
        if not isinstance(raw_choice, dict):
            raise TupleError("OpenAI-compatible response has invalid choice", retryable=True, status_code=502)
        choice = dict(raw_choice)
        choice.setdefault("index", index)
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            raise TupleError("OpenAI-compatible response has invalid finish_reason", retryable=True, status_code=502)
        message = choice.get("message")
        content: Any = None
        tool_calls: Any = None
        function_call: Any = None
        if isinstance(message, dict):
            content = message.get("content")
            tool_calls = message.get("tool_calls")
            function_call = message.get("function_call")
        if content is not None and not isinstance(content, str):
            raise TupleError("OpenAI-compatible response has non-string assistant content", retryable=True, status_code=502)
        if tool_calls is not None and not isinstance(tool_calls, list):
            raise TupleError("OpenAI-compatible response has invalid tool_calls", retryable=True, status_code=502)
        if isinstance(tool_calls, list):
            for item in tool_calls:
                if not _is_openai_tool_call(item):
                    raise TupleError("OpenAI-compatible response has invalid tool_calls", retryable=True, status_code=502)
        if function_call is not None and not isinstance(function_call, dict):
            raise TupleError("OpenAI-compatible response has invalid function_call", retryable=True, status_code=502)
        if content is None and not tool_calls and not function_call:
            raise TupleError("OpenAI-compatible response missing assistant content, tool_calls, or function_call", retryable=True, status_code=502)
        normalized_choices.append(choice)
        if index == 0:
            first_content = content
            first_tool_calls = tool_calls
            first_function_call = function_call
            first_finish_reason = finish_reason

    usage: dict[str, int] = {}
    raw_usage = value.get("usage")
    if isinstance(raw_usage, dict):
        usage = {str(k): v for k, v in raw_usage.items() if isinstance(v, int) and not isinstance(v, bool)}
    return TupleResult(
        kind="inline",
        value=first_content,
        usage=usage,
        tool_calls=first_tool_calls,
        function_call=first_function_call,
        finish_reason=first_finish_reason,
        openai_choices=normalized_choices,
    )


def _is_openai_tool_call(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("id"), str) or not isinstance(value.get("type"), str):
        return False
    if value.get("type") == "function":
        function = value.get("function")
        if not isinstance(function, dict):
            return False
        return isinstance(function.get("name"), str) and isinstance(function.get("arguments"), str)
    if value.get("type") == "custom":
        custom = value.get("custom")
        if not isinstance(custom, dict):
            return False
        return isinstance(custom.get("name"), str) and isinstance(custom.get("input"), str)
    return False
