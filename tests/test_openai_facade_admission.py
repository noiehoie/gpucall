from __future__ import annotations

import pytest

from gpucall.domain import ExecutionMode
from gpucall.openai_facade import OpenAIProtocolError, admit_openai_chat_completion


def _payload(**overrides):
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hello"}],
    }
    payload.update(overrides)
    return payload


def test_openai_admission_uses_generated_schema_and_builds_task_request() -> None:
    admission = admit_openai_chat_completion(
        _payload(
            metadata={"intent": "extract_json"},
            response_format={"type": "json_object"},
            temperature=0,
        ),
        inline_bytes_limit=10_000,
    )

    assert admission.requested_model == "gpt-4o-mini"
    assert admission.task_request.task == "infer"
    assert admission.task_request.mode is ExecutionMode.SYNC
    assert admission.task_request.intent == "extract_json"
    assert admission.task_request.response_format is not None
    assert admission.task_request.messages[0].content == "hello"


def test_openai_admission_rejects_unknown_field() -> None:
    with pytest.raises(OpenAIProtocolError, match=r"unknown.not_openai"):
        admit_openai_chat_completion(_payload(not_openai=True), inline_bytes_limit=10_000)


def test_openai_admission_rejects_official_unsupported_field_by_name() -> None:
    with pytest.raises(OpenAIProtocolError, match=r"modalities"):
        admit_openai_chat_completion(_payload(modalities=["text"]), inline_bytes_limit=10_000)


def test_openai_admission_accepts_stream_structured_output_for_openai_worker_contract() -> None:
    admission = admit_openai_chat_completion(
        _payload(stream=True, response_format={"type": "json_object"}),
        inline_bytes_limit=10_000,
    )

    assert admission.stream is True
    assert admission.task_request.response_format is not None


def test_openai_admission_rejects_image_content_parts() -> None:
    with pytest.raises(OpenAIProtocolError, match=r"DataRef"):
        admit_openai_chat_completion(
            _payload(messages=[{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.test/x.png"}}]}]),
            inline_bytes_limit=10_000,
        )


def test_openai_admission_allows_gpucall_extension_fields_outside_protocol() -> None:
    admission = admit_openai_chat_completion(_payload(intent="summarize_text"), inline_bytes_limit=10_000)

    assert admission.task_request.intent == "summarize_text"
    assert admission.gpucall_extensions == {"intent": "summarize_text"}


def test_openai_admission_preserves_text_part_boundaries_without_added_newlines() -> None:
    admission = admit_openai_chat_completion(
        _payload(messages=[{"role": "user", "content": [{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}]}]),
        inline_bytes_limit=10_000,
    )

    assert admission.task_request.messages[0].content == "hello"


def test_openai_admission_preserves_n_and_stream_usage() -> None:
    admission = admit_openai_chat_completion(
        _payload(n=2, stream=True, stream_options={"include_usage": True}),
        inline_bytes_limit=10_000,
    )

    assert admission.task_request.n == 2
    assert admission.task_request.stream_options == {"include_usage": True}
