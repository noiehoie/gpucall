from __future__ import annotations

import json
import time
from typing import Any
from uuid import uuid4


def openai_chat_response(
    model: str,
    content: str | None,
    usage: dict[str, int],
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    function_call: dict[str, Any] | None = None,
    finish_reason: str | None = None,
    gpucall: dict[str, Any] | None = None,
    output_validated: bool | None = None,
) -> dict[str, Any]:
    now = int(time.time())
    message: dict[str, Any] = {"role": "assistant", "content": content if content is not None else (None if tool_calls or function_call else "")}
    if tool_calls:
        message["tool_calls"] = tool_calls
    if function_call:
        message["function_call"] = function_call
    resolved_finish_reason = finish_reason or ("tool_calls" if tool_calls else ("function_call" if function_call else "stop"))

    payload: dict[str, Any] = {
        "id": f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": now,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": resolved_finish_reason,
            }
        ],
        "usage": usage,
    }
    if gpucall is not None:
        payload["gpucall"] = gpucall
    if output_validated is not None:
        payload["output_validated"] = output_validated
    return payload


def openai_stream_chunks(model: str, event: str, already_started: bool, *, stream_id: str):
    for line in event.splitlines():
        if not line.startswith("data:"):
            continue
        raw_data = line.removeprefix("data:").strip()
        if not raw_data or raw_data == "[DONE]":
            continue

        try:
            chunk_data = json.loads(raw_data)
            if isinstance(chunk_data, dict) and "choices" in chunk_data:
                chunk_data["id"] = stream_id
                chunk_data.setdefault("object", "chat.completion.chunk")
                chunk_data.setdefault("created", int(time.time()))
                chunk_data["model"] = model
                yield "data: " + json.dumps(chunk_data, separators=(",", ":")) + "\n\n", _openai_chunk_is_terminal(chunk_data)
                continue
            if isinstance(chunk_data, dict):
                continue
        except json.JSONDecodeError:
            pass

        if not already_started:
            yield "data: " + json.dumps(openai_stream_chunk(model, "", stream_id=stream_id, role="assistant"), separators=(",", ":")) + "\n\n", False
            already_started = True
        yield "data: " + json.dumps(openai_stream_chunk(model, raw_data, stream_id=stream_id), separators=(",", ":")) + "\n\n", False


def _openai_chunk_is_terminal(chunk: dict[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    return any(isinstance(choice, dict) and choice.get("finish_reason") is not None for choice in choices)


def openai_stream_chunk(
    model: str,
    content: str,
    *,
    stream_id: str | None = None,
    role: str | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    delta: dict[str, str] = {}
    if role is not None:
        delta["role"] = role
    if content:
        delta["content"] = content
    return {
        "id": stream_id or f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
