from __future__ import annotations

import json
import base64

import pytest

from gpucall.worker_contracts.modal import (
    _env_int,
    _format_prompt_for_model,
    _json_object_guided_schema,
    _looks_like_document_prompt,
    _prepare_vision_image,
    vision_prompt_from_payload,
)


class FakeTokenizer:
    chat_template = "present"

    def apply_chat_template(self, messages, *, tokenize: bool, add_generation_prompt: bool):
        assert tokenize is False
        assert add_generation_prompt is True
        return json.dumps(messages, ensure_ascii=False)


class FakeLLM:
    def get_tokenizer(self):
        return FakeTokenizer()


class NoTemplateTokenizer:
    chat_template = None


class NoTemplateLLM:
    def get_tokenizer(self):
        return NoTemplateTokenizer()


def test_qwen_worker_applies_chat_template_to_instruction_model() -> None:
    payload = {
        "system_prompt": "Answer directly.",
        "inline_inputs": {"prompt": {"value": "1+1?", "content_type": "text/plain"}},
    }

    prompt = _format_prompt_for_model(FakeLLM(), "Qwen/Qwen2.5-1.5B-Instruct", payload)

    messages = json.loads(prompt)
    assert messages[0]["role"] == "system"
    assert messages[1] == {"role": "user", "content": "1+1?"}


def test_structured_worker_prompt_demands_json_only() -> None:
    payload = {
        "system_prompt": "Return only valid JSON. Do not include markdown fences or prose.",
        "inline_inputs": {"prompt": {"value": "return answer", "content_type": "text/plain"}},
        "response_format": {"type": "json_object"},
    }

    prompt = _format_prompt_for_model(FakeLLM(), "Qwen/Qwen2.5-1.5B-Instruct", payload)

    messages = json.loads(prompt)
    assert "Return only valid JSON" in messages[0]["content"]


def test_json_object_guided_decoding_uses_object_schema() -> None:
    assert _json_object_guided_schema() == {"type": "object", "additionalProperties": True}


def test_qwen_fallback_template_preserves_all_messages() -> None:
    payload = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "user", "content": "third"},
        ]
    }

    prompt = _format_prompt_for_model(NoTemplateLLM(), "Qwen/Qwen2.5-1.5B-Instruct", payload)

    assert "system" in prompt
    assert "first" in prompt
    assert "second" in prompt
    assert "third" in prompt


def test_vision_prompt_excludes_gateway_system_prompt() -> None:
    payload = {
        "system_prompt": "Answer the user's vision request directly from the supplied image and prompt.",
        "inline_inputs": {"prompt": {"value": "この画像に写っている新聞紙名を答えよ", "content_type": "text/plain"}},
        "messages": [{"role": "system", "content": "Answer the user's vision request directly from the supplied image and prompt."}],
    }

    prompt = vision_prompt_from_payload(payload)

    assert prompt == "この画像に写っている新聞紙名を答えよ"
    assert "vision request directly" not in prompt


def test_florence_document_prompt_detects_japanese_headline_request() -> None:
    assert _looks_like_document_prompt("この新聞紙面の主要ヘッドライン上位3件を日本語で箇条書きにせよ。")


def test_modal_worker_normalizes_tiny_vision_images() -> None:
    pytest.importorskip("PIL.Image")
    image_body = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )

    image = _prepare_vision_image(image_body)

    assert image.mode == "RGB"
    assert image.size == (4, 4)


def test_modal_autoscaler_env_int_is_non_negative(monkeypatch) -> None:
    monkeypatch.setenv("GPUCALL_MODAL_H200X4_MIN_CONTAINERS", "1")
    assert _env_int("GPUCALL_MODAL_H200X4_MIN_CONTAINERS", 0) == 1
    monkeypatch.setenv("GPUCALL_MODAL_H200X4_MIN_CONTAINERS", "-1")
    assert _env_int("GPUCALL_MODAL_H200X4_MIN_CONTAINERS", 0) == 0


def test_modal_worker_image_dependencies_are_pinned() -> None:
    source = __import__("pathlib").Path("gpucall/worker_contracts/modal.py").read_text(encoding="utf-8")

    assert 'os.getenv("GPUCALL_MODAL_VLLM_PACKAGE", "vllm==0.8.5")' in source
    assert 'os.getenv("GPUCALL_MODAL_TRANSFORMERS_PACKAGE", "transformers==4.51.3")' in source
    assert 'os.getenv("GPUCALL_MODAL_HUGGINGFACE_HUB_PACKAGE", "huggingface-hub>=0.30.0,<1.0")' in source
    assert '"huggingface-hub[hf_transfer]"' not in source
