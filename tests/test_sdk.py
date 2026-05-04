from __future__ import annotations

import httpx
import pytest
import json

from gpucall_sdk import (
    AsyncGPUCallClient,
    GPUCallCallerRoutingError,
    GPUCallClient,
    GPUCallEmptyOutputError,
    GPUCallJSONParseError,
)
from gpucall_sdk.client import DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES


def test_python_sdk_uploads_file_and_sends_data_ref(tmp_path, monkeypatch) -> None:
    uploaded = False
    sent_payload = {}

    def put(url, **kwargs):
        nonlocal uploaded
        uploaded = True
        assert url == "https://example.com/upload"
        return httpx.Response(200)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        if request.url.path == "/v2/objects/presign-put":
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://example.com/upload",
                    "method": "PUT",
                    "data_ref": {"uri": "s3://bucket/key", "sha256": "a" * 64, "bytes": 5},
                },
            )
        if request.url.path == "/v2/objects/presign-get":
            return httpx.Response(
                200,
                json={
                    "download_url": "https://example.com/download",
                    "method": "GET",
                    "data_ref": {"uri": "https://example.com/download", "sha256": "a" * 64, "bytes": 5},
                },
            )
        if request.url.path == "/v2/tasks/sync":
            sent_payload = request.read()
            return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})
        return httpx.Response(404)

    path = tmp_path / "input.txt"
    path.write_text("hello", encoding="utf-8")
    monkeypatch.setattr("httpx.put", put)
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    result = client.infer(files=[path])

    assert uploaded
    assert result["result"]["value"] == "ok"
    assert b"s3://bucket/key" in sent_payload
    payload = json.loads(sent_payload)
    assert "requested_provider" not in payload
    assert "recipe" not in payload


def test_python_sdk_uses_env_api_key(monkeypatch) -> None:
    seen_auth = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_auth
        seen_auth = request.headers.get("authorization")
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    monkeypatch.setenv("GPUCALL_API_KEY", "client-secret")
    with GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler)) as client:
        client.infer()

    assert seen_auth == "Bearer client-secret"


def test_python_sdk_sends_response_format() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "{}"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    client.infer(prompt="return json", response_format={"type": "json_object"}, max_tokens=64, temperature=0.0)

    assert sent_payload["response_format"] == {"type": "json_object"}
    assert sent_payload["max_tokens"] == 64
    assert sent_payload["temperature"] == 0.0


def test_python_sdk_chat_completions_create_returns_openai_like_shape() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(
            200,
            json={"result": {"kind": "inline", "value": "{\"answer\":2}", "output_validated": True}},
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    result = client.chat.completions.create(
        messages=[{"role": "user", "content": "1+1?"}],
        response_format={"type": "json_object"},
        parse_json=True,
    )

    assert sent_payload["task"] == "infer"
    assert "recipe" not in sent_payload
    assert "requested_provider" not in sent_payload
    assert sent_payload["response_format"] == {"type": "json_object"}
    assert result["choices"][0]["message"]["content"] == "{\"answer\":2}"
    assert result["output_validated"] is True
    assert result["parsed"] == {"answer": 2}
    assert sent_payload["messages"] == [{"role": "user", "content": "1+1?"}]
    assert sent_payload["inline_inputs"] == {}


def test_python_sdk_rejects_structured_message_content_without_flattening() -> None:
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


def test_python_sdk_rejects_legacy_structured_message_content() -> None:
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


def test_python_sdk_chat_preserves_large_messages_without_flat_upload(monkeypatch) -> None:
    uploaded = False
    task_payload = {}

    def put(url, **kwargs):
        nonlocal uploaded
        uploaded = True
        assert kwargs["content"] == b"x" * 34
        return httpx.Response(200)

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal task_payload
        if request.url.path == "/v2/objects/presign-put":
            return httpx.Response(
                200,
                json={
                    "upload_url": "https://example.com/upload",
                    "method": "PUT",
                    "data_ref": {"uri": "s3://bucket/prompt.txt", "sha256": "b" * 64, "bytes": 40},
                },
            )
        if request.url.path == "/v2/objects/presign-get":
            return httpx.Response(
                200,
                json={
                    "download_url": "https://example.com/prompt.txt",
                    "method": "GET",
                    "data_ref": {"uri": "https://example.com/prompt.txt", "sha256": "b" * 64, "bytes": 40},
                },
            )
        task_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    monkeypatch.setattr("httpx.put", put)
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler), auto_upload_threshold_bytes=16)

    client.chat.completions.create(messages=[{"role": "user", "content": "x" * 34}])

    assert not uploaded
    assert task_payload["messages"] == [{"role": "user", "content": "x" * 34}]
    assert task_payload["input_refs"] == []
    assert task_payload["inline_inputs"] == {}


def test_python_sdk_default_auto_upload_threshold_matches_gateway_inline_limit() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    assert DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES == 8 * 1024
    assert client.auto_upload_threshold_bytes == 8 * 1024


def test_python_sdk_chat_parse_json_raises_with_raw_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"result": {"kind": "inline", "value": "not-json", "output_validated": False}},
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallJSONParseError) as exc_info:
        client.chat.completions.create(messages=[{"role": "user", "content": "json"}], parse_json=True)

    assert exc_info.value.raw_text == "not-json"


def test_python_sdk_chat_empty_output_is_not_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"kind": "inline", "value": ""}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallEmptyOutputError):
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}])


def test_python_sdk_422_empty_output_maps_to_typed_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"detail": "empty provider output", "code": "EMPTY_OUTPUT"})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallEmptyOutputError):
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}])


def test_python_sdk_422_malformed_output_maps_to_typed_exception() -> None:
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


def test_python_sdk_redacts_presigned_urls_from_http_logs() -> None:
    import logging

    from gpucall_sdk.client import _install_http_log_redaction, _redact_log_arg, _redact_url_text

    redacted = _redact_url_text(
        "https://bucket.example/object.txt?X-Amz-Credential=secret&X-Amz-Signature=signature"
    )
    arg_redacted = _redact_log_arg(httpx.URL("https://bucket.example/object.txt?X-Amz-Credential=secret&X-Amz-Signature=signature"))
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

    assert "secret" not in redacted
    assert "signature" not in redacted
    assert redacted == "https://bucket.example/object.txt?<redacted>"
    assert arg_redacted == "https://bucket.example/object.txt?<redacted>"
    assert "secret" not in record.getMessage()
    assert "signature" not in record.getMessage()


def test_python_sdk_streams_chunks() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/tasks/stream"
        return httpx.Response(200, text=": heartbeat\n\ndata: hello\n\n")

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    assert list(client.stream(prompt="hi")) == [": heartbeat\n\ndata: hello\n\n"]


def test_python_sdk_emits_warning_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-gpucall-warning": "fallback used"}, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.warns(Warning, match="fallback used"):
        client.infer()


def test_python_sdk_rejects_caller_routing() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(GPUCallCallerRoutingError):
        client.infer(recipe="text-infer-standard")
    with pytest.raises(GPUCallCallerRoutingError):
        client.infer(provider="local-echo")


async def test_async_python_sdk_polls_accepted_job() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path == "/v2/tasks/async":
            return httpx.Response(202, json={"job_id": "job-1", "state": "PENDING", "status_url": "/v2/jobs/job-1"})
        if request.url.path == "/v2/jobs/job-1":
            calls += 1
            return httpx.Response(200, json={"job_id": "job-1", "state": "COMPLETED", "result_ref": None})
        return httpx.Response(404)

    async with AsyncGPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler)) as client:
        result = await client.infer(mode="async", poll_interval=0)

    assert calls == 1
    assert result["state"] == "COMPLETED"


async def test_async_python_sdk_streams_chunks() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/tasks/stream"
        return httpx.Response(200, text=": heartbeat\n\ndata: hello\n\n")

    async with AsyncGPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler)) as client:
        chunks = [chunk async for chunk in client.stream(prompt="hi")]

    assert chunks == [": heartbeat\n\ndata: hello\n\n"]
