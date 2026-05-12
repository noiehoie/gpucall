from __future__ import annotations

import json

import httpx

import pytest

from gpucall_sdk import (
    AsyncGPUCallClient,
    GPUCallCallerRoutingError,
    GPUCallCircuitBreaker,
    GPUCallCircuitOpenError,
    GPUCallCircuitScope,
    GPUCallClient,
    GPUCallColdStartTimeout,
    GPUCallEmptyOutputError,
    GPUCallHTTPError,
    GPUCallJSONParseError,
    GPUCallNoEligibleTupleError,
    GPUCallProviderRuntimeError,
)
from gpucall_sdk.client import DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS, DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES


def test_chat_completions_sends_no_recipe_or_provider() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(
            200,
            json={"result": {"kind": "inline", "value": "{\"answer\":2}", "output_validated": True}},
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    response = client.chat.completions.create(
        messages=[{"role": "user", "content": "1+1?"}],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=64,
        parse_json=True,
    )

    assert sent_payload["task"] == "infer"
    assert sent_payload["mode"] == "sync"
    assert sent_payload["temperature"] == 0
    assert sent_payload["max_tokens"] == 64
    assert "recipe" not in sent_payload
    assert "requested_tuple" not in sent_payload
    assert sent_payload["messages"] == [{"role": "user", "content": "1+1?"}]
    assert sent_payload["inline_inputs"] == {}
    assert response["parsed"] == {"answer": 2}


def test_chat_completions_sends_intent_and_openai_hints_as_metadata() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "return json"}],
        task_family="extract_json",
        top_p=0.9,
        seed=7,
        tools=[{"type": "function", "function": {"name": "noop"}}],
        custom_openai_field=True,
    )

    assert sent_payload["task"] == "infer"
    assert sent_payload["intent"] == "extract_json"
    assert "recipe" not in sent_payload
    assert "requested_tuple" not in sent_payload
    assert sent_payload["metadata"]["task_family"] == "extract_json"
    assert sent_payload["metadata"]["openai.model"] == "gpt-4o-mini"
    assert sent_payload["metadata"]["openai.top_p"] == "0.9"
    assert sent_payload["metadata"]["openai.seed"] == "7"
    assert "openai.tools" in sent_payload["metadata"]
    assert sent_payload["metadata"]["openai.extra_keys"] == "custom_openai_field"


def test_vision_sends_vision_task_with_intent() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        if request.url.path == "/v2/objects/presign-put":
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://example.com/upload",
                    "method": "PUT",
                    "data_ref": {"uri": "s3://bucket/image.png", "sha256": "a" * 64, "bytes": 3, "content_type": "image/png"},
                },
            )
        sent_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    def put(url, **kwargs):
        return httpx.Response(200)

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    import tempfile
    from pathlib import Path
    from unittest.mock import patch

    with tempfile.TemporaryDirectory() as directory, patch("httpx.put", put):
        image = Path(directory) / "image.png"
        image.write_bytes(b"png")
        client.vision(image=image, prompt="read", task_family="understand_document_image")

    assert sent_payload["task"] == "vision"
    assert sent_payload["intent"] == "understand_document_image"
    assert "recipe" not in sent_payload
    assert "requested_tuple" not in sent_payload


def test_sdk_rejects_intent_name_as_task() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(GPUCallCallerRoutingError, match="pass workload purpose with intent"):
        client.infer(task="extract_json", messages=[{"role": "user", "content": "json"}])


def test_chat_completions_rejects_structured_message_content_without_flattening() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    for content in (
        [
            {"type": "text", "text": "look"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
        {"type": "text", "text": "look"},
    ):
        with pytest.raises(GPUCallCallerRoutingError, match="structured or multimodal"):
            client.chat.completions.create(messages=[{"role": "user", "content": content}])


def test_chat_completions_rejects_legacy_structured_message_content() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(GPUCallCallerRoutingError, match="structured or multimodal"):
        client.chat.completions.create(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "look"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                    ],
                }
            ]
        )


def test_chat_completions_preserves_large_messages_without_flat_upload(monkeypatch) -> None:
    uploaded = False
    task_payload = {}

    def put(url, **kwargs):
        nonlocal uploaded
        uploaded = True
        assert url == "https://example.com/upload"
        return httpx.Response(200)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_payload
        if request.url.path == "/v2/objects/presign-put":
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://example.com/upload",
                    "method": "PUT",
                    "data_ref": {"uri": "s3://bucket/prompt.txt", "sha256": "a" * 64, "bytes": 40},
                },
            )
        if request.url.path == "/v2/objects/presign-get":
            return httpx.Response(
                200,
                json={
                    "download_url": "https://example.com/prompt.txt",
                    "method": "GET",
                    "data_ref": {"uri": "https://example.com/prompt.txt", "sha256": "a" * 64, "bytes": 40},
                },
            )
        task_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    monkeypatch.setattr("httpx.put", put)
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler), auto_upload_threshold_bytes=16)

    client.chat.completions.create(messages=[{"role": "user", "content": "x" * 40}])

    assert not uploaded
    assert task_payload["messages"] == [{"role": "user", "content": "x" * 40}]
    assert task_payload["input_refs"] == []
    assert task_payload["inline_inputs"] == {}


def test_default_auto_upload_threshold_matches_gateway_inline_limit() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    assert DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES == 8 * 1024
    assert client.auto_upload_threshold_bytes == 8 * 1024


def test_parse_json_error_keeps_raw_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"kind": "inline", "value": "not-json", "output_validated": False}},
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    try:
        client.chat.completions.create(messages=[{"role": "user", "content": "json"}], parse_json=True)
    except GPUCallJSONParseError as exc:
        assert exc.raw_text == "not-json"
    else:
        raise AssertionError("expected GPUCallJSONParseError")


def test_empty_output_is_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"kind": "inline", "value": ""}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallEmptyOutputError):
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}])


def test_422_empty_output_maps_to_typed_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "empty tuple output", "code": "EMPTY_OUTPUT"})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallEmptyOutputError):
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}])


def test_422_malformed_output_maps_to_typed_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": "malformed structured output",
                "code": "MALFORMED_OUTPUT",
                "result": {"kind": "inline", "value": "{bad", "output_validated": False},
            },
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallJSONParseError) as exc_info:
        client.chat.completions.create(messages=[{"role": "user", "content": "json"}], parse_json=True)
    assert exc_info.value.raw_text == "{bad"


def test_http_error_preserves_failure_artifact() -> None:
    artifact = {
        "schema_version": 1,
        "failure_id": "pf-test",
        "failure_kind": "tuple_runtime",
        "caller_action": "retry_later",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            502,
            json={
                "detail": "tuple execution failed (PROVIDER_PROVISION_FAILED)",
                "code": "PROVIDER_PROVISION_FAILED",
                "failure_artifact": artifact,
            },
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallHTTPError) as exc_info:
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}])

    assert exc_info.value.status_code == 502
    assert exc_info.value.code == "PROVIDER_PROVISION_FAILED"
    assert exc_info.value.failure_artifact == artifact
    assert exc_info.value.response_body["failure_artifact"] == artifact


def test_redacts_presigned_httpx_url_log_args() -> None:
    import logging

    from gpucall_sdk.client import _install_http_log_redaction, _redact_log_arg

    redacted = _redact_log_arg(
        httpx.URL("https://bucket.example/object.txt?X-Amz-Credential=secret&X-Amz-Signature=signature")
    )
    record = logging.LogRecord(
        "httpx",
        logging.INFO,
        __file__,
        1,
        "HTTP Request: %s",
        (httpx.URL("https://bucket.example/object.txt?X-Amz-Credential=secret&X-Amz-Signature=signature"),),
        None,
    )
    _install_http_log_redaction()
    for log_filter in logging.getLogger("httpx").filters:
        log_filter.filter(record)

    assert redacted == "https://bucket.example/object.txt?<redacted>"
    assert "secret" not in record.getMessage()
    assert "signature" not in record.getMessage()


def test_infer_has_no_caller_routing_selector() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(TypeError):
        client.infer(recipe="text-infer-standard")
    with pytest.raises(TypeError):
        client.infer(provider="modal-a10g")


def test_circuit_breaker_scope_prevents_cross_intent_contamination() -> None:
    breaker = GPUCallCircuitBreaker(failure_threshold=2)
    vision = GPUCallCircuitScope(task="vision", intent="understand_document_image", mode="sync", transport="v2")
    extract = GPUCallCircuitScope(task="infer", intent="extract_json", mode="sync", transport="v2")
    err = GPUCallProviderRuntimeError("provider exhausted", status_code=503, code="PROVIDER_RESOURCE_EXHAUSTED")

    breaker.record_exception(vision, err)
    breaker.record_exception(vision, err)

    with pytest.raises(GPUCallCircuitOpenError):
        breaker.before_request(vision)
    breaker.before_request(extract)


def test_circuit_breaker_ignores_governance_and_timeout_failures() -> None:
    breaker = GPUCallCircuitBreaker(failure_threshold=1)
    scope = GPUCallCircuitScope(task="infer", intent="rank_text_items", mode="async", transport="v2")

    breaker.record_exception(scope, GPUCallNoEligibleTupleError("no tuple", status_code=503, code="NO_ELIGIBLE_TUPLE"))
    breaker.record_exception(scope, GPUCallColdStartTimeout("cold start"))

    breaker.before_request(scope)
    assert breaker.failure_count(scope) == 0


def test_circuit_breaker_rejects_global_scope_and_async_poll_default_is_600s() -> None:
    breaker = GPUCallCircuitBreaker()
    with pytest.raises(ValueError):
        breaker.before_request("global")
    assert DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS >= 600


async def test_async_upload_uses_clean_presigned_put_client(monkeypatch) -> None:
    seen_upload_auth = "not-called"

    class UploadClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def put(self, url, *, content, headers):
            nonlocal seen_upload_auth
            seen_upload_auth = headers.get("authorization")
            assert url == "https://bucket.example/upload"
            return httpx.Response(200)

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/objects/presign-put":
            assert request.headers.get("authorization") == "Bearer api-key"
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://bucket.example/upload",
                    "method": "PUT",
                    "data_ref": {"uri": "s3://bucket/key", "sha256": "a" * 64, "bytes": 4},
                },
            )
        return httpx.Response(404)

    client = AsyncGPUCallClient("http://gpucall.test", api_key="api-key", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", UploadClient)
    try:
        await client.upload_bytes(b"data", name="data.txt", content_type="text/plain")
    finally:
        await client.close()

    assert seen_upload_auth is None
