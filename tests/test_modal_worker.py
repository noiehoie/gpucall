from __future__ import annotations

import json

from gpucall.providers.modal_worker import (
    _format_prompt_for_model,
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
