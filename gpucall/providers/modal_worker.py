from __future__ import annotations

import os
import hashlib
import io
import json
import sys
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
    if isinstance(prompt_item, dict) and str(prompt_item.get("value", "")):
        return str(prompt_item.get("value", ""))
    messages = payload.get("messages") or []
    parts = [
        str(message.get("content", ""))
        for message in messages
        if str(message.get("role", "user")) != "system" and str(message.get("content", ""))
    ]
    if parts:
        return "\n".join(parts)
    return ""


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


if modal is not None:
    app = modal.App(os.getenv("GPUCALL_MODAL_WORKER_APP_NAME", "gpucall-worker-json"))
    _VLLM_IMAGE = (
        modal.Image.from_registry("nvidia/cuda:12.1.1-devel-ubuntu22.04", add_python="3.11")
        .apt_install("git", "ffmpeg")
        .pip_install(
            "boto3",
            "cryptography",
            "pillow",
            "vllm==0.6.3",
            "transformers==4.45.2",
            "huggingface-hub[hf_transfer]",
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
            "Qwen/Qwen2-0.5B-Instruct",
            "Qwen/Qwen2-1.5B-Instruct",
            "facebook/opt-125m",
            "Salesforce/blip-image-captioning-base",
            "meta-llama/Llama-3.1-8B-Instruct",
        }
    )
    _TOP_LEVEL_LLM: Any = None
    _TOP_LEVEL_LOADED_ID: str | None = None
    _TOP_LEVEL_VISION: tuple[Any, Any, str] | None = None

    def _load_top_level_llm(model_id: str, max_model_len: int) -> Any:
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
        _TOP_LEVEL_LLM = LLM(
            model=model_id,
            max_model_len=max_model_len,
            gpu_memory_utilization=0.90,
            trust_remote_code=True,
            tensor_parallel_size=1,
            disable_log_stats=True,
        )
        _TOP_LEVEL_LOADED_ID = model_id
        return _TOP_LEVEL_LLM

    def _bounded_model_len(model_id: str, max_model_len: int) -> int:
        if model_id == "Salesforce/blip-image-captioning-base":
            return min(max_model_len, 512)
        if model_id == "facebook/opt-125m":
            return min(max_model_len, 2048)
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
            try:
                import inspect

                params = inspect.signature(GuidedDecodingParams).parameters
                if "json_object" in params:
                    return GuidedDecodingParams(json_object=True)
            except Exception:
                pass
            return GuidedDecodingParams(json={})
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

    def _generate_text(payload: dict[str, Any], model: str | None, max_model_len: int) -> str:
        artifact_result = _execute_artifact_workload(payload)
        if artifact_result is not None:
            return json.dumps(artifact_result.get("artifact_manifest") or artifact_result, sort_keys=True, separators=(",", ":"))
        if payload.get("task") == "vision":
            return _generate_vision_text(payload, model)
        requested_model = model or os.getenv("GPUCALL_MODAL_VLLM_MODEL", "facebook/opt-125m")
        llm = _load_top_level_llm(requested_model, max_model_len)
        prompt = _format_prompt_for_model(llm, requested_model, payload)
        outputs = llm.generate([prompt], _sampling_params(payload), use_tqdm=False)
        return outputs[0].outputs[0].text.strip()

    def _load_vision_model(model_id: str) -> tuple[Any, Any, str]:
        global _TOP_LEVEL_VISION
        allowed = {"Salesforce/blip-image-captioning-base", "Salesforce/blip-vqa-base"}
        if model_id not in allowed:
            raise ValueError(f"vision model {model_id} is not allowed")
        if _TOP_LEVEL_VISION is not None and _TOP_LEVEL_VISION[2] == model_id:
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
        from PIL import Image

        image = Image.open(io.BytesIO(image_body)).convert("RGB")
        model_id = model or os.getenv("GPUCALL_MODAL_VISION_MODEL", "Salesforce/blip-image-captioning-base")
        processor, vision_model, _ = _load_vision_model(model_id)
        prompt = vision_prompt_from_payload(payload).strip()
        if model_id == "Salesforce/blip-vqa-base" and not prompt:
            prompt = "What is in the image?"
        if prompt:
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

    @app.function(image=_VLLM_IMAGE, gpu="A10G", timeout=1800, scaledown_window=300)
    def run_inference_on_modal(payload: dict[str, Any], workload: str = "infer", **kwargs) -> str:
        payload = {**payload, "task": workload or payload.get("task")}
        return _generate_text(payload, kwargs.get("model"), kwargs.get("max_model_len") or 32768)

    @app.function(image=_VLLM_IMAGE, gpu="A10G", timeout=1800, scaledown_window=300)
    def stream_inference_on_modal(payload: dict[str, Any], workload: str = "infer", **kwargs) -> Iterator[str]:
        raise RuntimeError("Modal true streaming is not implemented in gpucall v2.0")

    class VllmWorkerBase:
        _llm: Any = None
        _loaded_id: str | None = None

        def _load_llm(self, model_id: str, max_model_len: int) -> None:
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
            self._llm = LLM(
                model=model_id,
                max_model_len=max_model_len,
                gpu_memory_utilization=0.90,
                trust_remote_code=True,
                tensor_parallel_size=1,
                disable_log_stats=True,
            )
            self._loaded_id = model_id

        def _to_prompt(self, payload: dict[str, Any]) -> str:
            if self._llm is None or self._loaded_id is None:
                return prompt_from_payload(payload)
            return _format_prompt_for_model(self._llm, self._loaded_id, payload)

        def _generate(self, payload: dict[str, Any], model: str | None, max_model_len: int) -> str:
            artifact_result = _execute_artifact_workload(payload)
            if artifact_result is not None:
                return json.dumps(artifact_result.get("artifact_manifest") or artifact_result, sort_keys=True, separators=(",", ":"))
            if payload.get("task") == "vision":
                return _generate_vision_text(payload, model)
            requested_model = model or os.getenv("GPUCALL_MODAL_VLLM_MODEL", "facebook/opt-125m")
            self._load_llm(requested_model, max_model_len)
            outputs = self._llm.generate([self._to_prompt(payload)], _sampling_params(payload), use_tqdm=False)
            return outputs[0].outputs[0].text.strip()

        def _stream(self, payload: dict[str, Any], model: str | None, max_model_len: int) -> Iterator[str]:
            raise RuntimeError("Modal true streaming is not implemented in gpucall v2.0")

    @app.cls(image=_VLLM_IMAGE, gpu="T4", timeout=1800, scaledown_window=300)
    class VllmWorkerT4(VllmWorkerBase):
        @modal.method()
        def run_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> str:
            payload = {**payload, "task": workload or payload.get("task")}
            return self._generate(payload, kwargs.get("model"), kwargs.get("max_model_len") or 8192)

        @modal.method()
        def stream_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> Iterator[str]:
            yield from self._stream(payload, kwargs.get("model"), kwargs.get("max_model_len") or 8192)

    @app.cls(image=_VLLM_IMAGE, gpu="A10G", timeout=1800, scaledown_window=300)
    class VllmWorkerA10G(VllmWorkerBase):
        @modal.method()
        def run_inference_on_modal(self, payload: dict[str, Any], workload: str, **kwargs) -> str:
            payload = {**payload, "task": workload or payload.get("task")}
            return self._generate(payload, kwargs.get("model"), kwargs.get("max_model_len") or 32768)

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
        from gpucall.providers.worker_artifacts import execute_artifact_workload
    except ImportError:
        return None
    return execute_artifact_workload(payload)
