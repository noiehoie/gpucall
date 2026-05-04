from __future__ import annotations

import json
import os
import sys
import types
import urllib.request

os.environ.setdefault("HOME", "/tmp")

try:
    from runpod_flash import Endpoint, GpuGroup  # type: ignore
except ImportError as exc:  # pragma: no cover - imported only when Flash is installed
    Endpoint = None
    GpuGroup = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


if Endpoint is not None and GpuGroup is not None:
    _GPU_CHOICES = [
        gpu
        for gpu in (
            getattr(GpuGroup, "AMPERE_16", None),
            getattr(GpuGroup, "AMPERE_24", None),
            getattr(GpuGroup, "ADA_24", None),
        )
        if gpu is not None
    ]

    @Endpoint(
        name="gpucall-flash-worker",
        gpu=_GPU_CHOICES,
        workers=(0, 1),
        idle_timeout=300,
        dependencies=[
            "boto3>=1.34",
            "transformers==4.45.2",
            "torch>=2.4",
            "accelerate>=0.34",
            "huggingface-hub==0.25.2",
            "hf-transfer==0.1.8",
        ],
        env={
            "HF_HUB_ENABLE_HF_TRANSFER": "1",
            "GPUCALL_WORKER_ALLOWED_MODELS": os.getenv(
                "GPUCALL_WORKER_ALLOWED_MODELS",
                "Qwen/Qwen2.5-0.5B-Instruct,Qwen/Qwen2.5-1.5B-Instruct,Qwen/Qwen2.5-7B-Instruct",
            ),
        },
        execution_timeout_ms=600_000,
    )
    def run_inference_on_flash(data):
        import hashlib
        import json as _json
        import os as _os
        import urllib.request as _urlrequest

        def _is_structured(payload):
            response_format = payload.get("response_format") or {}
            return response_format.get("type") in {"json_object", "json_schema"}

        def _max_new_tokens(payload):
            return int(payload.get("max_tokens") or _os.getenv("GPUCALL_WORKER_MAX_TOKENS", "256"))

        def _temperature(payload):
            temperature = payload.get("temperature")
            if temperature is None:
                temperature = 0.0
            return float(temperature)

        def _fetch_ref_bytes(ref):
            max_bytes = int(_os.getenv("GPUCALL_WORKER_MAX_FETCH_BYTES", "16777216"))
            uri = str(ref.get("uri") or "")
            if not uri:
                raise ValueError("input_ref uri is required")
            if uri.startswith("s3://"):
                try:
                    import boto3
                except ImportError as exc:
                    raise RuntimeError("boto3 is required for s3 input_refs") from exc
                bucket_key = uri.removeprefix("s3://")
                bucket, _, key = bucket_key.partition("/")
                if not bucket or not key:
                    raise ValueError("s3 input_ref must be s3://bucket/key")
                client = boto3.client("s3", endpoint_url=_os.getenv("AWS_ENDPOINT_URL_S3") or _os.getenv("S3_ENDPOINT_URL"))
                response = client.get_object(Bucket=bucket, Key=key)
                chunks = []
                total = 0
                for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
                    total += len(chunk)
                    if total > max_bytes:
                        raise ValueError("input_ref exceeds worker fetch limit")
                    chunks.append(chunk)
                data_bytes = b"".join(chunks)
            elif uri.startswith("http://") or uri.startswith("https://"):
                if ref.get("gateway_presigned") is not True:
                    raise ValueError("http(s) input_refs must be gateway-presigned")
                with _urlrequest.urlopen(uri, timeout=30) as response:
                    data_bytes = response.read(max_bytes + 1)
            else:
                raise ValueError(f"unsupported input_ref uri scheme: {uri.split(':', 1)[0]}")
            if len(data_bytes) > max_bytes:
                raise ValueError("input_ref exceeds worker fetch limit")
            expected_sha256 = ref.get("sha256")
            if expected_sha256 and hashlib.sha256(data_bytes).hexdigest() != expected_sha256:
                raise ValueError("input_ref sha256 mismatch")
            return data_bytes

        def _prompt_from_payload(payload):
            messages = payload.get("messages") or []
            if messages:
                return "\n\n".join(str(message.get("content", "")) for message in messages if str(message.get("content", "")))
            inline_inputs = payload.get("inline_inputs") or {}
            parts = []
            for key in sorted(inline_inputs):
                item = inline_inputs[key] or {}
                value = item.get("value")
                if value:
                    parts.append(str(value))
            refs = payload.get("input_refs") or []
            for ref in refs:
                if not isinstance(ref, dict):
                    continue
                content_type = str(ref.get("content_type") or "")
                if content_type and not content_type.startswith("text/"):
                    parts.append(f"[non-text input_ref: {content_type}]")
                    continue
                parts.append(_fetch_ref_bytes(ref).decode("utf-8", errors="replace"))
            if parts:
                return "\n\n".join(parts)
            return str(payload.get("prompt") or "")

        def _system_prompt(payload):
            return str(payload.get("system_prompt") or "")

        def _format_prompt(tokenizer, model_id, payload):
            raw_prompt = _prompt_from_payload(payload).strip()
            if "Instruct" not in model_id and not model_id.startswith("Qwen/"):
                return raw_prompt
            messages = [
                {"role": "system", "content": _system_prompt(payload)},
                {"role": "user", "content": raw_prompt},
            ]
            template = getattr(tokenizer, "apply_chat_template", None)
            if callable(template):
                try:
                    return template(messages, tokenize=False, add_generation_prompt=True)
                except Exception:
                    pass
            if model_id.startswith("Qwen/"):
                return (
                    "<|im_start|>system\n"
                    f"{messages[0]['content']}<|im_end|>\n"
                    "<|im_start|>user\n"
                    f"{messages[1]['content']}<|im_end|>\n"
                    "<|im_start|>assistant\n"
                )
            return raw_prompt

        def _assert_model_allowed(model_id):
            configured = _os.getenv("GPUCALL_WORKER_ALLOWED_MODELS")
            allowed = {item.strip() for item in configured.split(",") if item.strip()} if configured else {
                "Qwen/Qwen2.5-0.5B-Instruct",
                "Qwen/Qwen2.5-1.5B-Instruct",
                "Qwen/Qwen2.5-7B-Instruct",
            }
            if model_id not in allowed:
                raise ValueError(f"model {model_id} is not allowed")

        model_id = str(data.get("model") or _os.getenv("GPUCALL_RUNPOD_FLASH_MODEL") or "Qwen/Qwen2.5-1.5B-Instruct")
        max_model_len = int(data.get("max_model_len") or _os.getenv("GPUCALL_RUNPOD_FLASH_MAX_MODEL_LEN", "16384"))
        _assert_model_allowed(model_id)

        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        cache = globals().setdefault("_GPUCALL_FLASH_MODEL_CACHE", {})
        if cache.get("model_id") == model_id and cache.get("model") is not None and cache.get("tokenizer") is not None:
            model_obj = cache["model"]
            tokenizer = cache["tokenizer"]
        else:
            tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
            model_obj = AutoModelForCausalLM.from_pretrained(
                model_id,
                torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
                device_map="auto",
                trust_remote_code=True,
            )
            model_obj.eval()
            cache.clear()
            cache.update({"model_id": model_id, "model": model_obj, "tokenizer": tokenizer})

        prompt = _format_prompt(tokenizer, model_id, data)
        inputs = tokenizer(
            prompt,
            return_tensors="pt",
            truncation=True,
            max_length=max(1, max_model_len - _max_new_tokens(data)),
        )
        device = getattr(model_obj, "device", None)
        if device is not None:
            inputs = {key: value.to(device) for key, value in inputs.items()}
        do_sample = not _is_structured(data)
        generation_kwargs = {
            "max_new_tokens": _max_new_tokens(data),
            "do_sample": do_sample,
            "pad_token_id": tokenizer.eos_token_id,
            "eos_token_id": tokenizer.eos_token_id,
        }
        if data.get("repetition_penalty") is not None:
            generation_kwargs["repetition_penalty"] = float(data["repetition_penalty"])
        if do_sample:
            generation_kwargs["temperature"] = max(_temperature(data), 0.01)
        outputs = model_obj.generate(**inputs, **generation_kwargs)
        generated = outputs[0][inputs["input_ids"].shape[-1] :]
        text = tokenizer.decode(generated, skip_special_tokens=True)
        return {"kind": "inline", "value": text.strip()}

else:

    async def run_inference_on_flash(data):
        raise _IMPORT_ERROR


_LLM: object = None
_LOADED_MODEL: object = None


def _run_inference(payload: dict) -> dict:
    model = str(payload.get("model") or os.getenv("GPUCALL_RUNPOD_FLASH_MODEL") or "Qwen/Qwen2.5-1.5B-Instruct")
    max_model_len = int(payload.get("max_model_len") or os.getenv("GPUCALL_RUNPOD_FLASH_MAX_MODEL_LEN", "16384"))
    text = _generate_text(payload, model=model, max_model_len=max_model_len)
    return {"kind": "inline", "value": text}


def _generate_text(payload: dict, *, model: str, max_model_len: int) -> str:
    model_obj, tokenizer = _load_transformers(model)
    prompt = _format_prompt_for_model(tokenizer, model, payload)
    inputs = tokenizer(
        prompt,
        return_tensors="pt",
        truncation=True,
        max_length=max(1, max_model_len - _max_new_tokens(payload)),
    )
    device = getattr(model_obj, "device", None)
    if device is not None:
        inputs = {key: value.to(device) for key, value in inputs.items()}
    do_sample = not _is_structured_payload(payload)
    generation_kwargs: dict = {
        "max_new_tokens": _max_new_tokens(payload),
        "do_sample": do_sample,
        "pad_token_id": tokenizer.eos_token_id,
        "eos_token_id": tokenizer.eos_token_id,
    }
    if payload.get("repetition_penalty") is not None:
        generation_kwargs["repetition_penalty"] = float(payload["repetition_penalty"])
    if do_sample:
        generation_kwargs["temperature"] = max(_temperature(payload), 0.01)
    outputs = model_obj.generate(**inputs, **generation_kwargs)
    generated = outputs[0][inputs["input_ids"].shape[-1] :]
    text = tokenizer.decode(generated, skip_special_tokens=True)
    return text.strip()


def _format_prompt_for_model(tokenizer: object, model_id: str, payload: dict) -> str:
    raw_prompt = _prompt_from_payload(payload).strip()
    if "Instruct" not in model_id and not model_id.startswith("Qwen/"):
        return raw_prompt
    messages = _messages_from_payload(payload, raw_prompt)
    template = getattr(tokenizer, "apply_chat_template", None)
    if callable(template):
        try:
            return template(messages, tokenize=False, add_generation_prompt=True)
        except Exception:
            pass
    if model_id.startswith("Qwen/"):
        return (
            "<|im_start|>system\n"
            f"{messages[0]['content']}<|im_end|>\n"
            "<|im_start|>user\n"
            f"{messages[1]['content']}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
    return raw_prompt


def _prompt_from_payload(payload: dict) -> str:
    messages = payload.get("messages") or []
    if messages:
        return "\n\n".join(str(message.get("content", "")) for message in messages if str(message.get("content", "")))
    inline_inputs = payload.get("inline_inputs") or {}
    parts: list = []
    for key in sorted(inline_inputs):
        item = inline_inputs[key] or {}
        value = item.get("value")
        if value:
            parts.append(str(value))
    refs = payload.get("input_refs") or []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        content_type = str(ref.get("content_type") or "")
        if content_type and not content_type.startswith("text/"):
            parts.append(f"[non-text input_ref: {content_type}]")
            continue
        parts.append(_fetch_data_ref_text(ref))
    if parts:
        return "\n\n".join(parts)
    return str(payload.get("prompt") or "")


def _fetch_data_ref_text(ref: dict) -> str:
    data = _fetch_data_ref_bytes(ref)
    expected_sha256 = ref.get("sha256")
    if expected_sha256:
        import hashlib

        actual = hashlib.sha256(data).hexdigest()
        if actual != expected_sha256:
            raise ValueError("input_ref sha256 mismatch")
    return data.decode("utf-8", errors="replace")


def _fetch_data_ref_bytes(ref: dict) -> bytes:
    max_bytes = int(os.getenv("GPUCALL_WORKER_MAX_FETCH_BYTES", "16777216"))
    uri = str(ref.get("uri") or "")
    if not uri:
        raise ValueError("input_ref uri is required")
    if uri.startswith("s3://"):
        if not _ambient_s3_allowed(ref):
            raise ValueError("s3 input_refs require gateway-presigned worker capability")
        return _fetch_s3_ref_bytes(uri, max_bytes=max_bytes)
    if uri.startswith("http://") or uri.startswith("https://"):
        if ref.get("gateway_presigned") is not True:
            raise ValueError("http(s) input_refs must be gateway-presigned")
        with urllib.request.urlopen(uri, timeout=30) as response:  # nosec B310 - signed URLs are policy controlled.
            data = response.read(max_bytes + 1)
    else:
        raise ValueError(f"unsupported input_ref uri scheme: {uri.split(':', 1)[0]}")
    if len(data) > max_bytes:
        raise ValueError("input_ref exceeds worker fetch limit")
    return data


def _fetch_s3_ref_bytes(uri: str, *, max_bytes: int) -> bytes:
    try:
        import boto3
    except ImportError as exc:
        raise RuntimeError("boto3 is required for s3 input_refs") from exc
    bucket_key = uri.removeprefix("s3://")
    bucket, _, key = bucket_key.partition("/")
    if not bucket or not key:
        raise ValueError("s3 input_ref must be s3://bucket/key")
    client = boto3.client("s3", endpoint_url=os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("S3_ENDPOINT_URL"))
    response = client.get_object(Bucket=bucket, Key=key)
    chunks: list[bytes] = []
    total = 0
    for chunk in response["Body"].iter_chunks(chunk_size=1024 * 1024):
        total += len(chunk)
        if total > max_bytes:
            raise ValueError("input_ref exceeds worker fetch limit")
        chunks.append(chunk)
    return b"".join(chunks)


def _ambient_s3_allowed(ref: dict) -> bool:
    if ref.get("allow_worker_s3_credentials") is True:
        return True
    return os.getenv("GPUCALL_WORKER_ALLOW_AMBIENT_S3", "").strip().lower() in {"1", "true", "yes", "on"}


def _system_prompt_for_payload(payload: dict) -> str:
    return str(payload.get("system_prompt") or "")


def _messages_from_payload(payload: dict, raw_prompt: str) -> list[dict[str, str]]:
    raw_messages = payload.get("messages") or []
    if raw_messages:
        return [
            {"role": str(message.get("role", "user")), "content": str(message.get("content", ""))}
            for message in raw_messages
            if str(message.get("content", ""))
        ]
    messages = []
    system_prompt = _system_prompt_for_payload(payload)
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}] + [m for m in messages if m["role"] != "system"]
        if len(messages) == 1 and raw_prompt:
            messages.append({"role": "user", "content": raw_prompt})
    return messages or ([{"role": "user", "content": raw_prompt}] if raw_prompt else [])


def _max_new_tokens(payload: dict) -> int:
    return int(payload.get("max_tokens") or os.getenv("GPUCALL_WORKER_MAX_TOKENS", "256"))


def _temperature(payload: dict) -> float:
    temperature = payload.get("temperature")
    if temperature is None:
        temperature = 0.0
    return float(temperature)


def _is_structured_payload(payload: dict) -> bool:
    response_format = payload.get("response_format") or {}
    return response_format.get("type") in {"json_object", "json_schema"}


def _load_transformers(model_id: str) -> tuple:
    global _LLM, _LOADED_MODEL
    _assert_model_allowed(model_id)
    if _LOADED_MODEL == model_id and _LLM is not None:
        return _LLM
    if _LLM is not None:
        _clear_gpu_memory()
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=True)
    model = AutoModelForCausalLM.from_pretrained(
        model_id,
        torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
        device_map="auto",
        trust_remote_code=True,
    )
    model.eval()
    _LLM = (model, tokenizer)
    _LOADED_MODEL = model_id
    return _LLM


def _clear_gpu_memory() -> None:
    global _LLM, _LOADED_MODEL
    _LLM = None
    _LOADED_MODEL = None
    try:
        import gc
        import torch

        gc.collect()
        torch.cuda.empty_cache()
    except Exception:
        pass


def _bounded_model_len(model_id: str, max_model_len: int) -> int:
    if model_id.startswith("Qwen/"):
        return min(max_model_len, 32768)
    return min(max_model_len, 8192)


def _assert_model_allowed(model_id: str) -> None:
    configured = os.getenv("GPUCALL_WORKER_ALLOWED_MODELS")
    allowed = {item.strip() for item in configured.split(",") if item.strip()} if configured else {
        "Qwen/Qwen2.5-0.5B-Instruct",
        "Qwen/Qwen2.5-1.5B-Instruct",
        "Qwen/Qwen2.5-7B-Instruct",
    }
    if model_id not in allowed:
        raise ValueError(f"model {model_id} is not allowed")


def _get_tokenizer(llm: object) -> object:
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
