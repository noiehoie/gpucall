from __future__ import annotations

import pytest
from pydantic import BaseModel

from gpucall.openai_facade import openai_chat_response, openai_stream_chunk, openai_stream_chunks


def test_openai_chat_response_matches_official_sdk_type_oracle() -> None:
    pytest.importorskip("openai")
    from openai.types.chat import ChatCompletion

    payload = openai_chat_response(
        "gpucall:auto",
        "hello",
        {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
        gpucall={"plan_id": "plan-1"},
    )

    parsed = ChatCompletion.model_validate(payload)
    assert parsed.object == "chat.completion"
    assert parsed.choices[0].message.content == "hello"


def test_openai_stream_chunk_matches_official_sdk_type_oracle() -> None:
    pytest.importorskip("openai")
    from openai.types.chat import ChatCompletionChunk

    chunk = openai_stream_chunk("gpucall:auto", "hello", stream_id="chatcmpl-test", role="assistant")

    parsed = ChatCompletionChunk.model_validate(chunk)
    assert parsed.object == "chat.completion.chunk"
    assert parsed.choices[0].delta.content == "hello"


def test_openai_stream_chunks_emit_role_once_per_event_batch() -> None:
    event = "data: hello\n\ndata: world\n\n"

    chunks = list(openai_stream_chunks("gpucall:auto", event, already_started=False, stream_id="chatcmpl-test"))

    role_chunks = [chunk for chunk, _terminal in chunks if '"role":"assistant"' in chunk]
    assert len(role_chunks) == 1


def test_openai_stream_chunks_skip_non_choice_json_events() -> None:
    chunks = list(openai_stream_chunks("gpucall:auto", 'data: {"usage":{"total_tokens":1}}\n\n', already_started=False, stream_id="chatcmpl-test"))

    assert chunks == []


def test_openai_official_structured_output_helper_available_for_oracle() -> None:
    pytest.importorskip("openai")
    from openai import pydantic_function_tool

    class Lookup(BaseModel):
        query: str

    tool = pydantic_function_tool(Lookup)
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "Lookup"
