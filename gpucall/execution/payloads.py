from __future__ import annotations

from typing import Any

from gpucall.domain import CompiledPlan, TupleResult
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
    first = choices[0]
    if not isinstance(first, dict):
        raise TupleError("OpenAI-compatible response has invalid choice", retryable=True, status_code=502)
    finish_reason = first.get("finish_reason")
    if finish_reason is not None and not isinstance(finish_reason, str):
        raise TupleError("OpenAI-compatible response has invalid finish_reason", retryable=True, status_code=502)
    message = first.get("message")
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

    usage: dict[str, int] = {}
    raw_usage = value.get("usage")
    if isinstance(raw_usage, dict):
        usage = {str(k): v for k, v in raw_usage.items() if isinstance(v, int) and not isinstance(v, bool)}
    return TupleResult(kind="inline", value=content, usage=usage, tool_calls=tool_calls, function_call=function_call, finish_reason=finish_reason)


def _is_openai_tool_call(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if not isinstance(value.get("id"), str) or not isinstance(value.get("type"), str):
        return False
    function = value.get("function")
    if not isinstance(function, dict):
        return False
    return isinstance(function.get("name"), str) and isinstance(function.get("arguments"), str)
