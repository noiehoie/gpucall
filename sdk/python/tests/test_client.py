from __future__ import annotations

import json

import httpx

import pytest

from gpucall_sdk import GPUCallCallerRoutingError, GPUCallClient, GPUCallEmptyOutputError, GPUCallHTTPError, GPUCallJSONParseError
from gpucall_sdk.client import DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES


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


def test_infer_rejects_recipe_and_has_no_tuple_selector() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(GPUCallCallerRoutingError):
        client.infer(recipe="text-infer-standard")
    with pytest.raises(TypeError):
        client.infer(provider="modal-a10g")
