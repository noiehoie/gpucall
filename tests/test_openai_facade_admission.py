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


def test_openai_admission_report_records_governance_mapping_and_model_policy() -> None:
    admission = admit_openai_chat_completion(
        _payload(
            model="gpt-4o-mini",
            user="caller-1",
            stream=True,
            stream_options={"include_usage": True},
            metadata={"intent": "extract_json", "trace": "abc"},
            max_completion_tokens=16,
            temperature=0,
        ),
        inline_bytes_limit=10_000,
    )

    report = admission.report.to_dict()
    assert report == {
        "protocol": "openai.chat.completions",
        "governance_contract": "TaskRequest",
        "admitted": [
            "max_completion_tokens",
            "messages",
            "metadata",
            "stream",
            "stream_options",
            "temperature",
        ],
        "transformed": {
            "max_completion_tokens": "TaskRequest.max_tokens",
            "messages": "TaskRequest.messages",
            "metadata": "TaskRequest.metadata",
            "stream": "TaskRequest.mode",
            "stream_options": "TaskRequest.stream_options",
            "temperature": "TaskRequest.temperature",
        },
        "rejected": [],
        "ignored": [],
        "metadata_only": ["model", "user"],
        "gpucall_extensions": [],
        "model_policy": "gpucall_auto_or_metadata_only",
        "model_value": "gpt-4o-mini",
    }
    assert admission.task_request.metadata["openai.model"] == "gpt-4o-mini"
    assert not hasattr(admission.task_request, "model")


def test_openai_admission_report_records_extension_transform() -> None:
    admission = admit_openai_chat_completion(
        _payload(intent="summarize_text"),
        inline_bytes_limit=10_000,
    )

    report = admission.report.to_dict()
    assert report["gpucall_extensions"] == ["intent"]
    assert report["transformed"]["gpucall.intent"] == "TaskRequest.intent"
    assert admission.task_request.intent == "summarize_text"


def test_openai_admission_error_records_rejected_fields() -> None:
    with pytest.raises(OpenAIProtocolError) as exc_info:
        admit_openai_chat_completion(
            _payload(stream=True, stream_options={"include_obfuscation": True}),
            inline_bytes_limit=10_000,
        )

    assert exc_info.value.admission_report is not None
    report = exc_info.value.admission_report.to_dict()
    assert report["rejected"] == ["stream_options.include_obfuscation"]
    assert report["transformed"]["stream"] == "TaskRequest.mode"


def test_openai_admission_rejects_feature_gated_stream_option_even_when_false() -> None:
    with pytest.raises(OpenAIProtocolError) as exc_info:
        admit_openai_chat_completion(
            _payload(stream=True, stream_options={"include_obfuscation": False}),
            inline_bytes_limit=10_000,
        )

    assert exc_info.value.code == "unsupported_openai_field"
    assert exc_info.value.admission_report is not None
    assert exc_info.value.admission_report.to_dict()["rejected"] == ["stream_options.include_obfuscation"]


def test_openai_admission_rejects_reserved_metadata_namespace() -> None:
    with pytest.raises(OpenAIProtocolError) as exc_info:
        admit_openai_chat_completion(
            _payload(metadata={"openai.model": "caller-forged"}),
            inline_bytes_limit=10_000,
        )

    assert exc_info.value.code == "unsupported_openai_field"
    assert exc_info.value.admission_report is not None
    assert exc_info.value.admission_report.to_dict()["rejected"] == ["metadata.openai.model"]


def test_openai_admission_rejects_zero_token_limit_via_governance_contract() -> None:
    with pytest.raises(OpenAIProtocolError) as exc_info:
        admit_openai_chat_completion(_payload(max_tokens=0), inline_bytes_limit=10_000)

    assert exc_info.value.code == "unsupported_openai_field"
    assert exc_info.value.admission_report is not None
    report = exc_info.value.admission_report.to_dict()
    assert report["rejected"] == ["max_tokens"]
    assert report["transformed"]["max_tokens"] == "TaskRequest.max_tokens"
