from __future__ import annotations

import pytest
from pydantic import BaseModel

from gpucall.app import _openai_stream_chunk
from gpucall.app_helpers import openai_chat_response


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

    chunk = _openai_stream_chunk("gpucall:auto", "hello", stream_id="chatcmpl-test", role="assistant")

    parsed = ChatCompletionChunk.model_validate(chunk)
    assert parsed.object == "chat.completion.chunk"
    assert parsed.choices[0].delta.content == "hello"


def test_openai_official_structured_output_helper_available_for_oracle() -> None:
    pytest.importorskip("openai")
    from openai import pydantic_function_tool

    class Lookup(BaseModel):
        query: str

    tool = pydantic_function_tool(Lookup)
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "Lookup"
