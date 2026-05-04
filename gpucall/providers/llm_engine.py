from __future__ import annotations

import json
import os
import sys
import types
import inspect
from typing import Any

from gpucall.providers.worker_io import prompt_from_payload


DEFAULT_ALLOWED_MODELS = {
    "Qwen/Qwen2.5-0.5B-Instruct",
    "Qwen/Qwen2.5-1.5B-Instruct",
    "Qwen/Qwen2.5-7B-Instruct",
    "Qwen/Qwen2-0.5B-Instruct",
    "Qwen/Qwen2-1.5B-Instruct",
    "facebook/opt-125m",
    "meta-llama/Llama-3.1-8B-Instruct",
}

_LLM: Any = None
_LOADED_MODEL: str | None = None


def generate_text(payload: dict[str, Any], *, model: str, max_model_len: int) -> str:
    llm = _load_vllm(model, max_model_len)
    prompt = format_prompt_for_model(llm, model, payload)
    outputs = llm.generate([prompt], sampling_params(payload), use_tqdm=False)
    return outputs[0].outputs[0].text.strip()


def format_prompt_for_model(llm: Any, model_id: str, payload: dict[str, Any]) -> str:
    raw_prompt = prompt_from_payload(payload).strip()
    if not _should_apply_chat_template(model_id, llm):
        return raw_prompt
    messages = messages_from_payload(payload, raw_prompt)
    tokenizer = _get_tokenizer(llm)
    template = getattr(tokenizer, "apply_chat_template", None)
    if callable(template):
        try:
            return template(messages, tokenize=False, add_generation_prompt=True)
        except Exception:
            pass
    if model_id.startswith("Qwen/"):
        rendered = []
        for message in messages:
            role = message.get("role", "user")
            if role not in {"system", "user", "assistant", "tool"}:
                raise ValueError(f"unsupported chat role for Qwen template: {role}")
            rendered.append(f"<|im_start|>{role}\n{message.get('content', '')}<|im_end|>")
        rendered.append("<|im_start|>assistant\n")
        return "\n".join(rendered)
    return raw_prompt


def system_prompt_for_payload(payload: dict[str, Any]) -> str:
    return str(payload.get("system_prompt") or "")


def messages_from_payload(payload: dict[str, Any], raw_prompt: str | None = None) -> list[dict[str, str]]:
    raw_messages = payload.get("messages") or []
    if raw_messages:
        return [
            {"role": str(message.get("role", "user")), "content": str(message.get("content", ""))}
            for message in raw_messages
            if str(message.get("content", ""))
        ]
    messages: list[dict[str, str]] = []
    system_prompt = system_prompt_for_payload(payload)
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
        if raw_prompt:
            messages.append({"role": "user", "content": raw_prompt})
    if messages:
        return messages
    prompt = raw_prompt if raw_prompt is not None else prompt_from_payload(payload).strip()
    return [{"role": "user", "content": prompt}] if prompt else []


def sampling_params(payload: dict[str, Any]) -> Any:
    from vllm import SamplingParams

    response_format = payload.get("response_format") or {}
    max_tokens = int(payload.get("max_tokens") or os.getenv("GPUCALL_WORKER_MAX_TOKENS", "256"))
    kwargs: dict[str, Any] = {
        "temperature": float(payload["temperature"]) if payload.get("temperature") is not None else 0.0,
        "max_tokens": max_tokens,
    }
    if payload.get("repetition_penalty") is not None:
        kwargs["repetition_penalty"] = float(payload["repetition_penalty"])
    if payload.get("stop_tokens"):
        kwargs["stop"] = list(payload["stop_tokens"])
    guided = guided_decoding_params(response_format) if payload.get("guided_decoding") else None
    if guided is not None:
        kwargs["guided_decoding"] = guided
    return SamplingParams(**kwargs)


def guided_decoding_params(response_format: dict[str, Any]) -> Any | None:
    if response_format.get("type") not in {"json_object", "json_schema"}:
        return None
    _install_pyairports_stub()
    try:
        from vllm.sampling_params import GuidedDecodingParams
    except Exception:
        return None
    if response_format.get("type") == "json_object":
        try:
            params = inspect.signature(GuidedDecodingParams).parameters
            if "json_object" in params:
                return GuidedDecodingParams(json_object=True)
        except Exception:
            pass
        return GuidedDecodingParams(json={})
    return GuidedDecodingParams(json=response_format.get("json_schema") or {})


def is_structured_payload(payload: dict[str, Any]) -> bool:
    response_format = payload.get("response_format") or {}
    return response_format.get("type") in {"json_object", "json_schema"}


def _load_vllm(model_id: str, max_model_len: int) -> Any:
    global _LLM, _LOADED_MODEL
    _assert_model_allowed(model_id)
    if _LOADED_MODEL == model_id and _LLM is not None:
        return _LLM
    if _LLM is not None:
        _LLM = None
        _LOADED_MODEL = None
        try:
            import gc
            import torch

            gc.collect()
            torch.cuda.empty_cache()
        except Exception:
            pass
    from vllm import LLM

    _LLM = LLM(
        model=model_id,
        max_model_len=bounded_model_len(model_id, max_model_len),
        gpu_memory_utilization=float(os.getenv("GPUCALL_WORKER_GPU_MEMORY_UTILIZATION", "0.90")),
        trust_remote_code=True,
        tensor_parallel_size=int(os.getenv("GPUCALL_WORKER_TENSOR_PARALLEL_SIZE", "1")),
        disable_log_stats=True,
    )
    _LOADED_MODEL = model_id
    return _LLM


def bounded_model_len(model_id: str, max_model_len: int) -> int:
    if model_id == "facebook/opt-125m":
        return min(max_model_len, 2048)
    if model_id.startswith("Qwen/"):
        return min(max_model_len, 32768)
    return min(max_model_len, 8192)


def _assert_model_allowed(model_id: str) -> None:
    configured = os.getenv("GPUCALL_WORKER_ALLOWED_MODELS")
    allowed = {item.strip() for item in configured.split(",") if item.strip()} if configured else DEFAULT_ALLOWED_MODELS
    if model_id not in allowed:
        raise ValueError(f"model {model_id} is not allowed")


def _should_apply_chat_template(model_id: str, llm: Any) -> bool:
    if "Instruct" in model_id or model_id.startswith("Qwen/"):
        return True
    tokenizer = _get_tokenizer(llm)
    return bool(getattr(tokenizer, "chat_template", None))


def _get_tokenizer(llm: Any) -> Any:
    getter = getattr(llm, "get_tokenizer", None)
    if callable(getter):
        return getter()
    return getattr(llm, "tokenizer", None)


def _install_pyairports_stub() -> None:
    if "pyairports.airports" in sys.modules:
        return
    package = sys.modules.get("pyairports") or types.ModuleType("pyairports")
    airports = types.ModuleType("pyairports.airports")
    airports.AIRPORT_LIST = []
    package.airports = airports
    sys.modules["pyairports"] = package
    sys.modules["pyairports.airports"] = airports
