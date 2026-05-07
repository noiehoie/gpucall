from __future__ import annotations

from typing import Any

from gpucall.domain import CompiledPlan, ProviderResult
from gpucall.domain import ProviderError


def plan_payload(plan: CompiledPlan) -> dict[str, Any]:
    """Build a provider payload without dereferencing sensitive object data."""
    return {
        "plan_id": plan.plan_id,
        "task": plan.task,
        "recipe": plan.recipe_name,
        "mode": plan.mode.value,
        "data_classification": plan.data_classification.value,
        "tokenizer_family": plan.tokenizer_family,
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
        "attestations": plan.attestations,
    }


def gpucall_provider_result(value: Any) -> ProviderResult:
    if isinstance(value, ProviderResult):
        return value
    if isinstance(value, dict):
        if value.get("kind") in {"inline", "ref", "artifact_manifest"}:
            return ProviderResult.model_validate(value)
    raise ProviderError(f"provider output does not match gpucall ProviderResult contract: {type(value).__name__}", retryable=True, status_code=502)


def plain_text_result(value: Any) -> ProviderResult:
    if isinstance(value, str):
        return ProviderResult(kind="inline", value=value)
    raise ProviderError(f"provider output does not match plain text contract: {type(value).__name__}", retryable=True, status_code=502)


def ollama_generate_result(value: Any) -> ProviderResult:
    if not isinstance(value, dict) or not isinstance(value.get("response"), str):
        raise ProviderError("Ollama response missing string 'response' field", retryable=True, status_code=502)
    return ProviderResult(kind="inline", value=value["response"])


def openai_chat_completion_result(value: Any) -> ProviderResult:
    if not isinstance(value, dict):
        raise ProviderError("OpenAI-compatible response must be an object", retryable=True, status_code=502)
    choices = value.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ProviderError("OpenAI-compatible response missing choices", retryable=True, status_code=502)
    first = choices[0]
    if not isinstance(first, dict):
        raise ProviderError("OpenAI-compatible response has invalid choice", retryable=True, status_code=502)
    message = first.get("message")
    content: Any = None
    if isinstance(message, dict):
        content = message.get("content")
    if not isinstance(content, str):
        raise ProviderError("OpenAI-compatible response missing assistant content", retryable=True, status_code=502)
    usage: dict[str, int] = {}
    raw_usage = value.get("usage")
    if isinstance(raw_usage, dict):
        usage = {str(k): v for k, v in raw_usage.items() if isinstance(v, int) and not isinstance(v, bool)}
    return ProviderResult(kind="inline", value=content, usage=usage)
