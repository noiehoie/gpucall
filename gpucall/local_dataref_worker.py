from __future__ import annotations

import hashlib
import os
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException

from gpucall.domain import TupleError, TupleResult
from gpucall.execution.payloads import openai_chat_completion_result

DEFAULT_WORKER_PATH = "/gpucall/local-dataref-openai/v1/chat"
DEFAULT_MAX_DATAREF_BYTES = 16 * 1024 * 1024
_TEXT_CONTENT_TYPES = {
    "application/json",
    "application/ld+json",
    "application/x-ndjson",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
}


def create_app(
    *,
    openai_base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    worker_api_key: str | None = None,
    max_dataref_bytes: int | None = None,
) -> FastAPI:
    app = FastAPI(title="gpucall local DataRef OpenAI worker")
    configured_openai_base_url = (openai_base_url or os.environ.get("GPUCALL_LOCAL_OPENAI_BASE_URL") or "http://127.0.0.1:8000/v1").rstrip("/")
    configured_model = model or os.environ.get("GPUCALL_LOCAL_OPENAI_MODEL") or "local-model"
    configured_api_key = api_key if api_key is not None else os.environ.get("GPUCALL_LOCAL_OPENAI_API_KEY", "local")
    configured_worker_api_key = worker_api_key if worker_api_key is not None else os.environ.get("GPUCALL_LOCAL_DATAREF_WORKER_API_KEY", "")
    configured_max_dataref_bytes = max_dataref_bytes or int(os.environ.get("GPUCALL_LOCAL_DATAREF_MAX_BYTES", DEFAULT_MAX_DATAREF_BYTES))

    @app.post(DEFAULT_WORKER_PATH)
    async def chat(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if configured_worker_api_key and authorization != f"Bearer {configured_worker_api_key}":
            raise HTTPException(status_code=401, detail="unauthorized")
        try:
            result = await run_dataref_openai_request(
                payload,
                openai_base_url=configured_openai_base_url,
                model=configured_model,
                api_key=configured_api_key,
                max_dataref_bytes=configured_max_dataref_bytes,
            )
        except TupleError as exc:
            raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
        return result.model_dump(mode="json")

    return app


async def run_dataref_openai_request(
    payload: dict[str, Any],
    *,
    openai_base_url: str,
    model: str,
    api_key: str = "local",
    max_dataref_bytes: int = DEFAULT_MAX_DATAREF_BYTES,
    dataref_transport: httpx.AsyncBaseTransport | None = None,
    openai_transport: httpx.AsyncBaseTransport | None = None,
) -> TupleResult:
    dataref_texts = await _fetch_dataref_texts(
        payload.get("input_refs") or [],
        max_dataref_bytes=max_dataref_bytes,
        transport=dataref_transport,
    )
    chat_payload = _chat_payload(payload, model=model, dataref_texts=dataref_texts)
    try:
        async with httpx.AsyncClient(timeout=_timeout(payload), transport=openai_transport) as client:
            response = await client.post(
                f"{openai_base_url.rstrip('/')}/chat/completions",
                headers={"authorization": f"Bearer {api_key}"},
                json=chat_payload,
            )
        response.raise_for_status()
        return openai_chat_completion_result(response.json())
    except TupleError:
        raise
    except httpx.ConnectError as exc:
        raise TupleError("local OpenAI-compatible server is unavailable", retryable=True, status_code=503) from exc
    except httpx.HTTPStatusError as exc:
        retryable = exc.response.status_code >= 500
        raise TupleError(f"local OpenAI-compatible server failed: {exc.response.status_code}", retryable=retryable, status_code=502) from exc
    except httpx.TimeoutException as exc:
        raise TupleError("local OpenAI-compatible server timed out", retryable=True, status_code=504) from exc


async def _fetch_dataref_texts(
    refs: list[dict[str, Any]],
    *,
    max_dataref_bytes: int,
    transport: httpx.AsyncBaseTransport | None,
) -> list[str]:
    texts: list[str] = []
    async with httpx.AsyncClient(timeout=60, transport=transport, follow_redirects=False) as client:
        for ref in refs:
            if not isinstance(ref, dict):
                raise TupleError("DataRef must be an object", retryable=False, status_code=400)
            uri = str(ref.get("uri") or "")
            if not uri.startswith(("http://", "https://")):
                raise TupleError("local DataRef worker requires HTTP(S) DataRef URI", retryable=False, status_code=400)
            declared_bytes = ref.get("bytes")
            if isinstance(declared_bytes, int) and declared_bytes > max_dataref_bytes:
                raise TupleError("DataRef exceeds local worker byte limit", retryable=False, status_code=413)
            try:
                response = await client.get(uri)
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise TupleError("DataRef fetch failed", retryable=exc.response.status_code >= 500, status_code=502) from exc
            except httpx.TimeoutException as exc:
                raise TupleError("DataRef fetch timed out", retryable=True, status_code=504) from exc
            content = response.content
            if len(content) > max_dataref_bytes:
                raise TupleError("DataRef exceeds local worker byte limit", retryable=False, status_code=413)
            if isinstance(declared_bytes, int) and len(content) != declared_bytes:
                raise TupleError("DataRef byte length mismatch", retryable=False, status_code=422)
            expected_sha = ref.get("sha256")
            if isinstance(expected_sha, str) and expected_sha and hashlib.sha256(content).hexdigest() != expected_sha:
                raise TupleError("DataRef sha256 mismatch", retryable=False, status_code=422)
            content_type = _base_content_type(str(ref.get("content_type") or response.headers.get("content-type") or ""))
            if not _is_text_content_type(content_type):
                raise TupleError("DataRef content type is not supported by local text worker", retryable=False, status_code=415)
            try:
                texts.append(content.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise TupleError("DataRef is not valid UTF-8 text", retryable=False, status_code=415) from exc
    return texts


def _chat_payload(payload: dict[str, Any], *, model: str, dataref_texts: list[str]) -> dict[str, Any]:
    chat_payload: dict[str, Any] = {
        "model": model,
        "messages": _messages(payload, dataref_texts),
        "stream": False,
    }
    for key in ("max_tokens", "temperature", "response_format"):
        value = payload.get(key)
        if value is not None:
            chat_payload[key] = value
    return chat_payload


def _messages(payload: dict[str, Any], dataref_texts: list[str]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    raw_messages = payload.get("messages") or []
    has_system = any(isinstance(message, dict) and message.get("role") == "system" for message in raw_messages)
    system_prompt = payload.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt and not has_system:
        messages.append({"role": "system", "content": system_prompt})
    for message in raw_messages:
        if isinstance(message, dict) and isinstance(message.get("role"), str) and isinstance(message.get("content"), str):
            messages.append({"role": message["role"], "content": message["content"]})
    inline_inputs = payload.get("inline_inputs") or {}
    inline_texts = [
        value.get("value")
        for value in inline_inputs.values()
        if isinstance(value, dict) and isinstance(value.get("value"), str)
    ]
    user_parts = inline_texts + dataref_texts
    if not messages or user_parts:
        messages.append({"role": "user", "content": "\n\n".join(user_parts)})
    return messages


def _base_content_type(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


def _is_text_content_type(content_type: str) -> bool:
    return content_type.startswith("text/") or content_type in _TEXT_CONTENT_TYPES


def _timeout(payload: dict[str, Any]) -> float:
    value = payload.get("timeout_seconds")
    if isinstance(value, int | float) and value > 0:
        return float(value)
    return 600.0


app = create_app()
