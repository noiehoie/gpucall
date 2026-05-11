from __future__ import annotations

import os
import hashlib
import io
import json
import sys
import threading
import types
from typing import Any, Iterator
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import modal  # type: ignore
except ImportError as exc:  # pragma: no cover - imported only with Modal installed
    modal = None
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None


def _is_structured_payload(payload: dict[str, Any]) -> bool:
    response_format = payload.get("response_format") or {}
    return response_format.get("type") in {"json_object", "json_schema"}


def _json_object_guided_schema() -> dict[str, Any]:
    return {"type": "object", "additionalProperties": True}


def _system_prompt_for_payload(payload: dict[str, Any]) -> str:
    return str(payload.get("system_prompt") or "")


def _format_prompt_for_model(llm: Any, model_id: str, payload: dict[str, Any]) -> str:
    raw_prompt = prompt_from_payload(payload).strip()
    if not _should_apply_chat_template(model_id, llm):
        return raw_prompt
    messages = _messages_from_payload(payload, raw_prompt)
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


def _messages_from_payload(payload: dict[str, Any], raw_prompt: str) -> list[dict[str, str]]:
    raw_messages = payload.get("messages") or []
    if raw_messages:
        return [
            {"role": str(message.get("role", "user")), "content": str(message.get("content", ""))}
            for message in raw_messages
            if str(message.get("content", ""))
        ]
    messages: list[dict[str, str]] = []
    system_prompt = _system_prompt_for_payload(payload)
    if system_prompt:
        messages = [{"role": "system", "content": system_prompt}, *[m for m in messages if m["role"] != "system"]]
        if len(messages) == 1 and raw_prompt:
            messages.append({"role": "user", "content": raw_prompt})
    return messages or [{"role": "user", "content": raw_prompt}]


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


def prompt_from_payload(payload: dict[str, Any]) -> str:
    messages = payload.get("messages") or []
    if messages:
        return "\n".join(str(message.get("content", "")) for message in messages if str(message.get("content", "")))
    inline = payload.get("inline_inputs") or {}
    parts: list[str] = []
    if "prompt" in inline:
        parts.append(str(inline["prompt"].get("value", "")))
    else:
        for key in sorted(inline):
            value = inline[key]
            if isinstance(value, dict):
                parts.append(str(value.get("value", "")))
    for ref in payload.get("input_refs") or []:
        parts.append(_fetch_data_ref_text(ref))
    return "\n".join(part for part in parts if part)


def vision_prompt_from_payload(payload: dict[str, Any]) -> str:
    inline = payload.get("inline_inputs") or {}
    prompt_item = inline.get("prompt")
    prompt = ""
    if isinstance(prompt_item, dict) and str(prompt_item.get("value", "")):
        prompt = str(prompt_item.get("value", ""))
    else:
        messages = payload.get("messages") or []
        parts = [
            str(message.get("content", ""))
            for message in messages
            if str(message.get("role", "user")) != "system" and str(message.get("content", ""))
        ]
        if parts:
            prompt = "\n".join(parts)
    structured_instruction = _vision_structured_instruction(payload)
    if structured_instruction:
        return "\n\n".join(part for part in (prompt, structured_instruction) if part)
    return prompt


def _vision_structured_instruction(payload: dict[str, Any]) -> str:
    response_format = payload.get("response_format") or {}
    format_type = response_format.get("type")
    if format_type == "json_object":
        return "Return only one valid JSON object. Do not include markdown fences, XML, or explanatory prose."
    if format_type == "json_schema":
        schema = response_format.get("json_schema") or {}
        return (
            "Return only one valid JSON object matching this JSON Schema. "
            "Do not include markdown fences, XML, or explanatory prose.\n"
            + json.dumps(schema, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
    return ""


def _looks_like_document_prompt(prompt: str) -> bool:
    lowered = prompt.lower()
    markers = (
        "ocr",
        "headline",
        "headlines",
        "document",
        "newspaper",
        "text",
        "article",
        "記事",
        "新聞",
        "見出し",
        "ヘッドライン",
        "紙面",
        "文字",
        "読取",
        "読み取",
    )
    return any(marker in lowered for marker in markers)


def _prepare_vision_image(image_body: bytes) -> Any:
    from PIL import Image

    image = Image.open(io.BytesIO(image_body)).convert("RGB")
    min_edge = int(os.getenv("GPUCALL_MODAL_MIN_VISION_EDGE_PX", "4"))
    if min_edge > 1 and (image.width < min_edge or image.height < min_edge):
        width = max(image.width, min_edge)
        height = max(image.height, min_edge)
        image = image.resize((width, height))
    return image


def _fetch_data_ref_text(ref: dict[str, Any]) -> str:
    body = _fetch_data_ref_bytes(ref)
    content_type = str(ref.get("content_type") or "").lower()
    if content_type and not (content_type.startswith("text/") or "json" in content_type):
        return body.hex()
    return body.decode("utf-8")


def _fetch_data_ref_bytes(ref: dict[str, Any]) -> bytes:
    uri = str(ref["uri"])
    max_bytes = min(int(os.getenv("GPUCALL_WORKER_MAX_REF_BYTES", "16777216")), int(ref.get("bytes") or 16777216))
    parsed = urlparse(uri)
    if parsed.scheme == "s3":
        if not _ambient_s3_allowed(ref):
            raise ValueError("s3 data refs require gateway-presigned worker capability")
        body = _fetch_s3_ref_bytes(parsed.netloc, parsed.path.lstrip("/"), max_bytes, ref)
    elif parsed.scheme in {"http", "https"}:
        if ref.get("gateway_presigned") is not True:
            raise ValueError("http(s) input_refs must be gateway-presigned")
        request = Request(uri, headers={"user-agent": "gpucall-modal-worker/2.0"})
        with urlopen(request, timeout=float(os.getenv("GPUCALL_WORKER_REF_TIMEOUT_SECONDS", "30"))) as response:
            body = response.read(max_bytes + 1)
    else:
        raise ValueError(f"unsupported data ref scheme for Modal worker: {parsed.scheme}")
    if len(body) > max_bytes:
        raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    expected = ref.get("sha256")
    if expected and hashlib.sha256(body).hexdigest() != expected:
        raise ValueError("data ref sha256 mismatch")
    return body


def _fetch_s3_ref_bytes(bucket: str, key: str, max_bytes: int, ref: dict[str, Any]) -> bytes:
    import boto3

    kwargs: dict[str, str] = {}
    endpoint = ref.get("endpoint_url") or os.getenv("AWS_ENDPOINT_URL_S3") or os.getenv("R2_ENDPOINT_URL")
    region = ref.get("region") or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if endpoint:
        kwargs["endpoint_url"] = str(endpoint)
    if region:
        kwargs["region_name"] = str(region)
    body = boto3.client("s3", **kwargs).get_object(Bucket=bucket, Key=key)["Body"]
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = body.read(min(1024 * 1024, max_bytes + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > max_bytes:
            raise ValueError(f"data ref exceeds worker limit: {max_bytes} bytes")
    return b"".join(chunks)


def _ambient_s3_allowed(ref: dict[str, Any]) -> bool:
    if ref.get("allow_worker_s3_credentials") is True:
        return True
    return os.getenv("GPUCALL_WORKER_ALLOW_AMBIENT_S3", "").strip().lower() in {"1", "true", "yes", "on"}


def _prefetch_qwen25_vl_3b() -> None:
    os.environ.setdefault("HF_HUB_ENABLE_HF_TRANSFER", "1")
    from huggingface_hub import snapshot_download

    snapshot_download("Qwen/Qwen2.5-VL-3B-Instruct")
    snapshot_download("microsoft/Florence-2-large-ft")


def _env_int(name: str, default: int) -> int:
    try:
        return max(int(os.getenv(name, str(default))), 0)
    except ValueError:
        return default


if modal is not None:
    app = modal.App(os.getenv("GPUCALL_MODAL_WORKER_APP_NAME", "gpucall-worker-json"))
    _VLLM_PACKAGE = os.getenv("GPUCALL_MODAL_VLLM_PACKAGE", "vllm==0.8.5")
    _TRANSFORMERS_PACKAGE = os.getenv("GPUCALL_MODAL_TRANSFORMERS_PACKAGE", "transformers==4.51.3")
    _HUGGINGFACE_HUB_PACKAGE = os.getenv("GPUCALL_MODAL_HUGGINGFACE_HUB_PACKAGE", "huggingface-hub>=0.30.0,<1.0")
    _VLLM_IMAGE = (
        modal.Image.from_registry("nvidia/cuda:12.1.1-devel-ubuntu22.04", add_python="3.11")
        .apt_install("git", "ffmpeg")
        .pip_install(
            "boto3",
            "cryptography",
            "pydantic>=2.7",
            "pillow",
            _VLLM_PACKAGE,
            _TRANSFORMERS_PACKAGE,
            _HUGGINGFACE_HUB_PACKAGE,
            "hf_transfer",
            "pyairports",
        )
        .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
        .run_function(_prefetch_qwen25_vl_3b, timeout=3600)
    )
    _QWEN_1M_IMAGE = (
        modal.Image.from_registry("nvidia/cuda:12.1.1-devel-ubuntu22.04", add_python="3.11")
        .apt_install("git", "ffmpeg")
        .pip_install(
            "boto3",
            "cryptography",
            "pydantic>=2.7",
            "pillow",
            "transformers==4.51.3",
            "accelerate",
            "vllm==0.8.5",
            _HUGGINGFACE_HUB_PACKAGE,
            "hf_transfer",
            "pyairports",
        )
        .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    )
    _VISION_IMAGE = (
        modal.Image.from_registry("nvidia/cuda:12.1.1-devel-ubuntu22.04", add_python="3.11")
        .apt_install("git", "ffmpeg")
        .pip_install(
            "boto3",
            "cryptography",
            "pydantic>=2.7",
            "pillow",
            "torch",
            "torchvision",
            "einops",
            "timm",
            "accelerate",
            "transformers==4.51.3",
            "qwen-vl-utils==0.0.8",
            _HUGGINGFACE_HUB_PACKAGE,
            "hf_transfer",
            "pyairports",
        )
        .env({"HF_HUB_ENABLE_HF_TRANSFER": "1"})
    )

    _ALLOWED_MODELS = frozenset(
        {
            "Qwen/Qwen2.5-0.5B-Instruct",
            "Qwen/Qwen2.5-1.5B-Instruct",
            "Qwen/Qwen2.5-7B-Instruct",
            "Qwen/Qwen2.5-14B-Instruct-1M",
            "Qwen/Qwen2-0.5B-Instruct",
            "Qwen/Qwen2-1.5B-Instruct",
            "facebook/opt-125m",
            "Salesforce/blip-image-captioning-base",
            "Salesforce/blip-vqa-base",
            "meta-llama/Llama-3.1-8B-Instruct",
        }
    )
    _TOP_LEVEL_LLM: Any = None
    _TOP_LEVEL_LOADED_ID: str | None = None
    _TOP_LEVEL_VISION: tuple[Any, Any, str] | None = None
    _STREAMING_MODELS: dict[str, tuple[Any, Any]] = {}

    def _load_top_level_llm(
        model_id: str,
        max_model_len: int,
        *,
        tensor_parallel_size: int = 1,
        long_context: bool = False,
        trust_remote_code: bool = False,
    ) -> Any:
        global _TOP_LEVEL_LLM, _TOP_LEVEL_LOADED_ID
        if model_id not in _ALLOWED_MODELS:
            raise ValueError(f"model {model_id} is not allowed")
        if _TOP_LEVEL_LOADED_ID == model_id and _TOP_LEVEL_LLM is not None:
            return _TOP_LEVEL_LLM
        if _TOP_LEVEL_LLM is not None:
            _TOP_LEVEL_LLM = None
            _TOP_LEVEL_LOADED_ID = None
            try:
                import gc
                import torch

                gc.collect()
                torch.cuda.empty_cache()
            except Exception:
                pass
        from vllm import LLM

        max_model_len = _bounded_model_len(model_id, max_model_len)
        kwargs: dict[str, Any] = {
            "model": model_id,
            "max_model_len": max_model_len,
            "gpu_memory_utilization": float(os.getenv("GPUCALL_MODAL_GPU_MEMORY_UTILIZATION", "0.85" if long_context else "0.90")),
            "trust_remote_code": trust_remote_code,
            "tensor_parallel_size": tensor_parallel_size,
            "disable_log_stats": True,
        }
        if long_context:
            kwargs.update(
                {
                    "enable_chunked_prefill": True,
                    "max_num_batched_tokens": int(os.getenv("GPUCALL_MODAL_MAX_NUM_BATCHED_TOKENS", "131072")),
                    "enforce_eager": True,
                }
            )
        _TOP_LEVEL_LLM = LLM(**kwargs)
        _TOP_LEVEL_LOADED_ID = model_id
        return _TOP_LEVEL_LLM

    def _bounded_model_len(model_id: str, max_model_len: int) -> int:
        if model_id == "Salesforce/blip-image-captioning-base":
            return min(max_model_len, 512)
        if model_id == "facebook/opt-125m":
            return min(max_model_len, 2048)
        if model_id == "Qwen/Qwen2.5-14B-Instruct-1M":
            return min(max_model_len, 1010000)
        if model_id.startswith("Qwen/"):
            return min(max_model_len, 32768)
        return min(max_model_len, 8192)

    def _sampling_params(payload: dict[str, Any]) -> Any:
        from vllm import SamplingParams

        response_format = payload.get("response_format") or {}
        max_tokens = int(payload.get("max_tokens") or os.getenv("GPUCALL_MODAL_MAX_TOKENS", "128"))
        kwargs: dict[str, Any] = {
            "temperature": float(payload["temperature"]) if payload.get("temperature") is not None else 0.0,
            "max_tokens": max_tokens,
        }
        if payload.get("repetition_penalty") is not None:
            kwargs["repetition_penalty"] = float(payload["repetition_penalty"])
        if payload.get("stop_tokens"):
            kwargs["stop"] = list(payload["stop_tokens"])
        guided = _guided_decoding_params(response_format) if payload.get("guided_decoding") else None
        if guided is not None:
            kwargs["guided_decoding"] = guided
        return SamplingParams(**kwargs)

    def _streaming_generate_kwargs(payload: dict[str, Any]) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "max_new_tokens": int(payload.get("max_tokens") or os.getenv("GPUCALL_MODAL_MAX_TOKENS", "128")),
            "do_sample": bool(float(payload["temperature"])) if payload.get("temperature") is not None else False,
        }
        if payload.get("temperature") is not None:
            kwargs["temperature"] = float(payload["temperature"])
        if payload.get("repetition_penalty") is not None:
            kwargs["repetition_penalty"] = float(payload["repetition_penalty"])
        if payload.get("stop_tokens"):
            # TextIteratorStreamer emits token text; stop handling is kept at
            # the generator boundary so callers receive only contract text.
            kwargs["_gpucall_stop_tokens"] = [str(item) for item in payload.get("stop_tokens") or []]
        return kwargs

    def _load_streaming_model(model_id: str, *, trust_remote_code: bool = False) -> tuple[Any, Any]:
        global _STREAMING_MODELS
        if model_id not in _ALLOWED_MODELS:
            raise ValueError(f"model {model_id} is not allowed")
        cached = _STREAMING_MODELS.get(model_id)
        if cached is not None:
            return cached
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model_id, trust_remote_code=trust_remote_code)
        model = AutoModelForCausalLM.from_pretrained(
            model_id,
            torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32,
            device_map="auto",
            trust_remote_code=trust_remote_code,
        )
        _STREAMING_MODELS = {model_id: (tokenizer, model)}
        return tokenizer, model

    def _stream_text(payload: dict[str, Any], model: str | None, max_model_len: int) -> Iterator[str]:
        artifact_result = _execute_artifact_workload(payload)
        if artifact_result is not None:
            yield json.dumps(artifact_result.get("artifact_manifest") or artifact_result, sort_keys=True, separators=(",", ":"))
            return
        if payload.get("task") == "vision":
            yield _generate_vision_text(payload, model)
            return
        requested_model = model or os.getenv("GPUCALL_MODAL_VLLM_MODEL", "facebook/opt-125m")
        tokenizer, model_obj = _load_streaming_model(requested_model, trust_remote_code=bool(payload.get("trust_remote_code")))
        prompt = _format_prompt_for_model(types.SimpleNamespace(tokenizer=tokenizer), requested_model, payload)
        inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=_bounded_model_len(requested_model, max_model_len))
        try:
            device = next(model_obj.parameters()).device
            inputs = {key: value.to(device) for key, value in inputs.items()}
        except Exception:
            pass
        from transformers import TextIteratorStreamer

        streamer = TextIteratorStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True, timeout=1.0)
        kwargs = _streaming_generate_kwargs(payload)
        stop_tokens = kwargs.pop("_gpucall_stop_tokens", [])
        errors: list[BaseException] = []

        def _generate() -> None:
            try:
                model_obj.generate(**inputs, **kwargs, streamer=streamer)
            except BaseException as exc:
                errors.append(exc)
                end = getattr(streamer, "end", None)
                if callable(end):
                    end()

        thread = threading.Thread(target=_generate, daemon=True)
        thread.start()
        buffer = ""
        for chunk in streamer:
            if errors:
                raise errors[0]
            if not chunk:
                continue
            buffer += str(chunk)
            stop_at = min((buffer.find(token) for token in stop_tokens if token in buffer), default=-1)
            if stop_at >= 0:
                tail = buffer[:stop_at]
                if tail:
                    yield tail
                break
            yield str(chunk)
        thread.join(timeout=1.0)
        if errors:
            raise errors[0]

    def _is_structured_response(response_format: dict[str, Any]) -> bool:
        return response_format.get("type") in {"json_object", "json_schema"}

    def _guided_decoding_params(response_format: dict[str, Any]) -> Any | None:
        if not _is_structured_response(response_format):
            return None
        _install_pyairports_stub()
        try:
            from vllm.sampling_params import GuidedDecodingParams
        except Exception:
            return None
        if response_format.get("type") == "json_object":
            return GuidedDecodingParams(json=_json_object_guided_schema())
        if response_format.get("type") == "json_schema":
            return GuidedDecodingParams(json=response_format.get("json_schema") or {})
        return None

    def _install_pyairports_stub() -> None:
        if "pyairports.airports" in sys.modules:
            return
        package = sys.modules.get("pyairports") or types.ModuleType("pyairports")
        airports = types.ModuleType("pyairports.airports")
        airports.AIRPORT_LIST = []
        package.airports = airports
        sys.modules["pyairports"] = package
        sys.modules["pyairports.airports"] = airports

    def _generate_text(
        payload: dict[str, Any],
        model: str | None,
        max_model_len: int,
        *,
        tensor_parallel_size: int = 1,
        long_context: bool = False,
    ) -> str:
        artifact_result = _execute_artifact_workload(payload)
        if artifact_result is not None:
            return json.dumps(artifact_result.get("artifact_manifest") or artifact_result, sort_keys=True, separators=(",", ":"))
        if payload.get("task") == "vision":
            return _generate_vision_text(payload, model)
        requested_model = model or os.getenv("GPUCALL_MODAL_VLLM_MODEL", "facebook/opt-125m")
        llm = _load_top_level_llm(
            requested_model,
            max_model_len,
            tensor_parallel_size=tensor_parallel_size,
            long_context=long_context,
            trust_remote_code=bool(payload.get("trust_remote_code")),
        )
        prompt = _format_prompt_for_model(llm, requested_model, payload)
        outputs = llm.generate([prompt], _sampling_params(payload), use_tqdm=False)
        return outputs[0].outputs[0].text.strip()

    def _load_vision_model(model_id: str, *, trust_remote_code: bool = False) -> tuple[Any, Any, str]:
        global _TOP_LEVEL_VISION
        allowed = {
            "Salesforce/blip-image-captioning-base",
            "Salesforce/blip-vqa-base",
            "Qwen/Qwen2.5-VL-3B-Instruct",
            "Qwen/Qwen2.5-VL-7B-Instruct",
            "microsoft/Florence-2-large-ft",
        }
        if model_id not in allowed:
            raise ValueError(f"vision model {model_id} is not allowed")
        if _TOP_LEVEL_VISION is not None and _TOP_LEVEL_VISION[2] == model_id:
            return _TOP_LEVEL_VISION
        if model_id.startswith("Qwen/Qwen2.5-VL-"):
            from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

            processor = AutoProcessor.from_pretrained(model_id)
            model = Qwen2_5_VLForConditionalGeneration.from_pretrained(model_id, torch_dtype="auto", device_map="auto")
            model.eval()
            _TOP_LEVEL_VISION = (processor, model, model_id)
            return _TOP_LEVEL_VISION
        if model_id == "microsoft/Florence-2-large-ft":
            import torch
            from transformers import AutoModelForCausalLM, AutoProcessor

            device = "cuda:0" if torch.cuda.is_available() else "cpu"
            torch_dtype = torch.float16 if torch.cuda.is_available() else torch.float32
            processor = AutoProcessor.from_pretrained(model_id, trust_remote_code=trust_remote_code)
            model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=torch_dtype, trust_remote_code=trust_remote_code).to(device)
            model.eval()
            _TOP_LEVEL_VISION = (processor, model, model_id)
            return _TOP_LEVEL_VISION
        from transformers import BlipForConditionalGeneration, BlipForQuestionAnswering, BlipProcessor

        processor = BlipProcessor.from_pretrained(model_id)
        if model_id == "Salesforce/blip-vqa-base":
            model = BlipForQuestionAnswering.from_pretrained(model_id)
        else:
            model = BlipForConditionalGeneration.from_pretrained(model_id)
        try:
            import torch

            if torch.cuda.is_available():
                model = model.to("cuda")
        except Exception:
            pass
        _TOP_LEVEL_VISION = (processor, model, model_id)
        return _TOP_LEVEL_VISION

    def _generate_vision_text(payload: dict[str, Any], model: str | None) -> str:
        image_ref = _first_image_ref(payload)
        image_body = _fetch_data_ref_bytes(image_ref)
        image = _prepare_vision_image(image_body)
        model_id = model or os.getenv("GPUCALL_MODAL_VISION_MODEL", "Salesforce/blip-image-captioning-base")
        processor, vision_model, _ = _load_vision_model(model_id, trust_remote_code=bool(payload.get("trust_remote_code")))
        prompt = vision_prompt_from_payload(payload).strip()
        if model_id == "microsoft/Florence-2-large-ft":
            import torch

            task_prompt = "<OCR>" if _looks_like_document_prompt(prompt) else "<MORE_DETAILED_CAPTION>"
            device = next(vision_model.parameters()).device
            torch_dtype = torch.float16 if getattr(device, "type", "") == "cuda" else torch.float32
            inputs = processor(text=task_prompt, images=image, return_tensors="pt").to(device, torch_dtype)
            generated_ids = vision_model.generate(
                input_ids=inputs["input_ids"],
                pixel_values=inputs["pixel_values"],
                max_new_tokens=int(payload.get("max_tokens") or 1024),
                do_sample=False,
                num_beams=3,
            )
            generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
            parsed = processor.post_process_generation(generated_text, task=task_prompt, image_size=(image.width, image.height))
            if isinstance(parsed, dict):
                value = parsed.get(task_prompt)
                if isinstance(value, str):
                    return value.strip()
                if value is not None:
                    return json.dumps(value, ensure_ascii=False, sort_keys=True)
            return generated_text.strip() or "image processed"
        if model_id.startswith("Qwen/Qwen2.5-VL-"):
            prompt = prompt or "Describe this image."
            messages = [
                {
                    "role": "user",
                    "content": [
                        {"type": "image", "image": image},
                        {"type": "text", "text": prompt},
                    ],
                }
            ]
            text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
            inputs = processor(text=[text], images=[image], padding=True, return_tensors="pt")
            try:
                inputs = inputs.to("cuda")
            except Exception:
                pass
            output_ids = vision_model.generate(**inputs, max_new_tokens=int(payload.get("max_tokens") or 256))
            trimmed = [out_ids[len(in_ids) :] for in_ids, out_ids in zip(inputs.input_ids, output_ids)]
            decoded = processor.batch_decode(trimmed, skip_special_tokens=True, clean_up_tokenization_spaces=False)
            return (decoded[0] if decoded else "").strip() or "image processed"
        if model_id == "Salesforce/blip-vqa-base" and not prompt:
            prompt = "What is in the image?"
        if model_id == "Salesforce/blip-vqa-base" and prompt:
            inputs = processor(image, prompt, return_tensors="pt")
        else:
            inputs = processor(image, return_tensors="pt")
        try:
            device = next(vision_model.parameters()).device
            inputs = {key: value.to(device) for key, value in inputs.items()}
        except Exception:
            pass
        output_ids = vision_model.generate(**inputs, max_new_tokens=int(payload.get("max_tokens") or 64))
        text = processor.decode(output_ids[0], skip_special_tokens=True).strip()
        return text or "image processed"

    def _first_image_ref(payload: dict[str, Any]) -> dict[str, Any]:
        for ref in payload.get("input_refs") or []:
            if str(ref.get("content_type") or "").lower().startswith("image/"):
                return ref
        raise ValueError("vision task requires an image data_ref")

    @app.function(
        image=_VLLM_IMAGE,
        gpu="A10G",
        timeout=1800,
        scaledown_window=_env_int("GPUCALL_MODAL_A10G_SCALEDOWN_WINDOW", 60),
        min_containers=_env_int("GPUCALL_MODAL_A10G_MIN_CONTAINERS", 0),
    )
    def run_inference_on_modal(payload: dict[str, Any], workload: str = "infer", **kwargs) -> str:
        payload = {**payload, "task": workload or payload.get("task")}
        return _generate_text(payload, kwargs.get("model"), kwargs.get("max_model_len") or 32768)

    @app.function(
        image=_QWEN_1M_IMAGE,
        gpu="H200:4",
        timeout=3600,
        scaledown_window=_env_int("GPUCALL_MODAL_H200X4_SCALEDOWN_WINDOW", 300),
        min_containers=_env_int("GPUCALL_MODAL_H200X4_MIN_CONTAINERS", 0),
    )
    def run_inference_on_modal_h200x4(payload: dict[str, Any], workload: str = "infer", **kwargs) -> str:
        payload = {**payload, "task": workload or payload.get("task")}
        return _generate_text(
            payload,
            kwargs.get("model"),
            kwargs.get("max_model_len") or 1010000,
            tensor_parallel_size=int(kwargs.get("tensor_parallel_size") or 4),
            long_context=True,
        )

    @app.function(
        image=_VISION_IMAGE,
        gpu="H100",
        timeout=1800,
        scaledown_window=_env_int("GPUCALL_MODAL_VISION_H100_SCALEDOWN_WINDOW", 60),
        min_containers=_env_int("GPUCALL_MODAL_VISION_H100_MIN_CONTAINERS", 0),
    )
    def run_inference_on_modal_vision_h100(payload: dict[str, Any], workload: str = "vision", **kwargs) -> str:
        payload = {**payload, "task": workload or payload.get("task")}
        return _generate_vision_text(payload, kwargs.get("model"))

    @app.function(
        image=_VLLM_IMAGE,
        gpu="A10G",
        timeout=1800,
        scaledown_window=_env_int("GPUCALL_MODAL_A10G_SCALEDOWN_WINDOW", 60),
        min_containers=_env_int("GPUCALL_MODAL_A10G_MIN_CONTAINERS", 0),
    )
    def stream_inference_on_modal(payload: dict[str, Any], workload: str = "infer", **kwargs) -> Iterator[str]:
        payload = {**payload, "task": workload or payload.get("task")}
        yield from _stream_text(payload, kwargs.get("model"), kwargs.get("max_model_len") or 32768)

    class VllmWorkerBase:
        _llm: Any = None
        _loaded_id: str | None = None

        def _load_llm(
            self,
            model_id: str,
            max_model_len: int,
            *,
            tensor_parallel_size: int = 1,
            long_context: bool = False,
            trust_remote_code: bool = False,
        ) -> None:
            if model_id not in _ALLOWED_MODELS:
                raise ValueError(f"model {model_id} is not allowed")
            if self._loaded_id == model_id and self._llm is not None:
                return
            if self._llm is not None:
                self._llm = None
                self._loaded_id = None
                try:
                    import gc
                    import torch

                    gc.collect()
                    torch.cuda.empty_cache()
                except Exception:
                    pass
            from vllm import LLM

            max_model_len = _bounded_model_len(model_id, max_model_len)
            kwargs: dict[str, Any] = {
                "model": model_id,
                "max_model_len": max_model_len,
                "gpu_memory_utilization": float(os.getenv("GPUCALL_MODAL_GPU_MEMORY_UTILIZATION", "0.85" if long_context else "0.90")),
                "trust_remote_code": trust_remote_code,
                "tensor_parallel_size": tensor_parallel_size,
                "disable_log_stats": True,
            }
            if long_context:
                kwargs.update(
                    {
                        "enable_chunked_prefill": True,
                        "max_num_batched_tokens": int(os.getenv("GPUCALL_MODAL_MAX_NUM_BATCHED_TOKENS", "131072")),
                        "enforce_eager": True,
                    }
                )
            self._llm = LLM(**kwargs)
            self._loaded_id = model_id

        def _to_prompt(self, payload: dict[str, Any]) -> str:
            if self._llm is None or self._loaded_id is None:
                return prompt_from_payload(payload)
            return _format_prompt_for_model(self._llm, self._loaded_id, payload)

        def _generate(self, payload: dict[str, Any], model: str | None, max_model_len: int, **kwargs) -> str:
            artifact_result = _execute_artifact_workload(payload)
            if artifact_result is not None:
                return json.dumps(artifact_result.get("artifact_manifest") or artifact_result, sort_keys=True, separators=(",", ":"))
            if payload.get("task") == "vision":
                return _generate_vision_text(payload, model)
            requested_model = model or os.getenv("GPUCALL_MODAL_VLLM_MODEL", "facebook/opt-125m")
            self._load_llm(
                requested_model,
                max_model_len,
                tensor_parallel_size=int(kwargs.get("tensor_parallel_size") or 1),
                long_context=bool(kwargs.get("long_context")),
                trust_remote_code=bool(payload.get("trust_remote_code")),
            )
            outputs = self._llm.generate([self._to_prompt(payload)], _sampling_params(payload), use_tqdm=False)
            return outputs[0].outputs[0].text.strip()

        def _stream(self, payload: dict[str, Any], model: str | None, max_model_len: int) -> Iterator[str]:
            yield from _stream_text(payload, model, max_model_len)

    @app.cls(image=_VLLM_IMAGE, gpu="T4", timeout=1800, scaledown_window=60)
    class VllmWorkerT4(VllmWorkerBase):
        @modal.method()
        def run_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> str:
            payload = {**payload, "task": workload or payload.get("task")}
            worker_kwargs = {key: value for key, value in kwargs.items() if key not in {"model", "max_model_len"}}
            return self._generate(payload, kwargs.get("model"), kwargs.get("max_model_len") or 8192, **worker_kwargs)

        @modal.method()
        def stream_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> Iterator[str]:
            yield from self._stream(payload, kwargs.get("model"), kwargs.get("max_model_len") or 8192)

    @app.cls(image=_VLLM_IMAGE, gpu="A10G", timeout=1800, scaledown_window=60)
    class VllmWorkerA10G(VllmWorkerBase):
        @modal.method()
        def run_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> str:
            payload = {**payload, "task": workload or payload.get("task")}
            worker_kwargs = {key: value for key, value in kwargs.items() if key not in {"model", "max_model_len"}}
            return self._generate(payload, kwargs.get("model"), kwargs.get("max_model_len") or 32768, **worker_kwargs)

        @modal.method()
        def stream_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> Iterator[str]:
            yield from self._stream(payload, kwargs.get("model"), kwargs.get("max_model_len") or 32768)

    vllm_t4_ref = VllmWorkerT4()
    vllm_a10g_ref = VllmWorkerA10G()

else:
    app = None
    vllm_t4_ref = None
    vllm_a10g_ref = None


def _execute_artifact_workload(payload: dict[str, Any]) -> dict[str, Any] | None:
    try:
        from gpucall.worker_contracts.artifacts import execute_artifact_workload
    except ImportError:
        return None
    return execute_artifact_workload(payload)
