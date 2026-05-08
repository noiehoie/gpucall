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
        "input_refs": [ref.model_dump(mode="json") for ref in plan.input_refs],
        "inline_inputs": {key: value.model_dump(mode="json") for key, value in plan.inline_inputs.items()},
        "messages": [message.model_dump(mode="json") for message in plan.messages],
        "response_format": plan.response_format.model_dump(mode="json") if plan.response_format is not None else None,
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
    message = first.get("message")
    content: Any = None
    if isinstance(message, dict):
        content = message.get("content")
    if not isinstance(content, str):
        raise TupleError("OpenAI-compatible response missing assistant content", retryable=True, status_code=502)
    usage: dict[str, int] = {}
    raw_usage = value.get("usage")
    if isinstance(raw_usage, dict):
        usage = {str(k): v for k, v in raw_usage.items() if isinstance(v, int) and not isinstance(v, bool)}
    return TupleResult(kind="inline", value=content, usage=usage)
