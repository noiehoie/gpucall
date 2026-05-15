from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
from typing import Any
from urllib.parse import urlparse

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
    dev_insecure = os.environ.get("GPUCALL_LOCAL_DATAREF_DEV_INSECURE", "").strip().lower() in {"1", "true", "yes", "on"}

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(DEFAULT_WORKER_PATH)
    async def chat(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        if not configured_worker_api_key and not dev_insecure:
            raise HTTPException(status_code=503, detail="local DataRef worker API key is not configured")
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
        status_code = exc.response.status_code
        if status_code == 429:
            raise TupleError("local OpenAI-compatible server rate limited", retryable=True, status_code=429) from exc
        raise TupleError(f"local OpenAI-compatible server failed: {status_code}", retryable=status_code >= 500, status_code=502) from exc
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
            _validate_dataref_fetch_uri(uri, ref)
            declared_bytes = ref.get("bytes")
            if isinstance(declared_bytes, int) and declared_bytes > max_dataref_bytes:
                raise TupleError("DataRef exceeds local worker byte limit", retryable=False, status_code=413)
            try:
                async with client.stream("GET", uri) as response:
                    response.raise_for_status()
                    if response.status_code != 200:
                        raise httpx.HTTPStatusError(
                            f"unexpected DataRef fetch status: {response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > max_dataref_bytes:
                            raise TupleError("DataRef exceeds local worker byte limit", retryable=False, status_code=413)
                        chunks.append(chunk)
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code == 429:
                    raise TupleError("DataRef fetch rate limited", retryable=True, status_code=429) from exc
                raise TupleError("DataRef fetch failed", retryable=status_code >= 500, status_code=502) from exc
            except httpx.TimeoutException as exc:
                raise TupleError("DataRef fetch timed out", retryable=True, status_code=504) from exc
            except httpx.RequestError as exc:
                raise TupleError("DataRef fetch request failed", retryable=True, status_code=502) from exc
            content = b"".join(chunks)
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


def _validate_dataref_fetch_uri(uri: str, ref: dict[str, Any]) -> None:
    parsed = urlparse(uri)
    if parsed.scheme not in {"http", "https"}:
        raise TupleError("local DataRef worker requires HTTP(S) DataRef URI", retryable=False, status_code=400)
    if ref.get("gateway_presigned") is not True:
        raise TupleError("local DataRef worker requires gateway-presigned DataRefs", retryable=False, status_code=400)
    if parsed.username or parsed.password:
        raise TupleError("DataRef URI must not contain userinfo", retryable=False, status_code=400)
    if not parsed.hostname:
        raise TupleError("DataRef URI must include a host", retryable=False, status_code=400)
    allowed_hosts = {
        item.strip().lower()
        for item in os.environ.get("GPUCALL_LOCAL_DATAREF_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    }
    host = parsed.hostname.lower()
    if not allowed_hosts:
        raise TupleError("local DataRef worker requires GPUCALL_LOCAL_DATAREF_ALLOWED_HOSTS", retryable=False, status_code=400)
    if host not in allowed_hosts:
        raise TupleError("DataRef host is not in GPUCALL_LOCAL_DATAREF_ALLOWED_HOSTS", retryable=False, status_code=400)
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None and (
        literal_ip.is_private
        or literal_ip.is_loopback
        or literal_ip.is_link_local
        or literal_ip.is_multicast
        or literal_ip.is_reserved
        or literal_ip.is_unspecified
    ):
        raise TupleError("DataRef host resolves to a non-public address", retryable=False, status_code=400)
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise TupleError("DataRef host could not be resolved", retryable=True, status_code=502) from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise TupleError("DataRef host resolves to a non-public address", retryable=False, status_code=400)


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
