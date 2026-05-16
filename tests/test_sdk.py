from __future__ import annotations

import httpx
import pytest
import json
import inspect
import subprocess
import sys

from gpucall_sdk import (
    AsyncGPUCallClient,
    GPUCallCallerRoutingError,
    GPUCallCircuitBreaker,
    GPUCallCircuitOpenError,
    GPUCallCircuitScope,
    GPUCallColdStartTimeout,
    GPUCallClient,
    GPUCallEmptyOutputError,
    GPUCallJSONParseError,
    GPUCallNoEligibleTupleError,
    GPUCallNoRecipeError,
    GPUCallProviderRuntimeError,
)
from gpucall_sdk.client import DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS, DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES, DEFAULT_REQUEST_TIMEOUT_SECONDS


def test_python_sdk_import_smoke_from_source_tree() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from gpucall_sdk import GPUCallClient, AsyncGPUCallClient; "
                "from gpucall_sdk.client import DEFAULT_REQUEST_TIMEOUT_SECONDS; "
                "print(GPUCallClient.__name__); "
                "print(AsyncGPUCallClient.__name__); "
                "print(DEFAULT_REQUEST_TIMEOUT_SECONDS)"
            ),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert "GPUCallClient" in result.stdout
    assert "AsyncGPUCallClient" in result.stdout
    assert "600.0" in result.stdout


def test_python_sdk_uploads_file_and_sends_data_ref(tmp_path, monkeypatch) -> None:
    uploaded = False
    sent_payload = {}

    def put(url, **kwargs):
        nonlocal uploaded
        uploaded = True
        assert url == "https://example.com/upload"
        assert kwargs.get("follow_redirects") is True
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
    assert "requested_tuple" not in payload
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


def test_python_sdk_sends_intent_without_recipe_or_tuple() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    client.infer(prompt="translate", task_family="translate_text")

    assert sent_payload["intent"] == "translate_text"
    assert "recipe" not in sent_payload
    assert "requested_tuple" not in sent_payload


def test_python_sdk_sends_idempotency_key() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    client.infer(prompt="hello", idempotency_key="canary-key")

    assert sent_payload["idempotency_key"] == "canary-key"
    assert sent_payload["task"] == "infer"


def test_python_sdk_sends_async_webhook_url() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(202, json={"job_id": "job-1", "state": "PENDING"})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    result = client.infer(mode="async", poll=False, webhook_url="https://example.com/hook")

    assert result["job_id"] == "job-1"
    assert sent_payload["webhook_url"] == "https://example.com/hook"


def test_python_sdk_default_and_per_request_timeouts_are_cold_start_safe() -> None:
    seen_timeout = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_timeout
        seen_timeout = request.extensions["timeout"]
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    assert client.client.timeout.read >= 600
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS >= 600

    client.infer(prompt="hello", request_timeout=900)

    assert seen_timeout["read"] == 900


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
    assert "requested_tuple" not in sent_payload
    assert sent_payload["response_format"] == {"type": "json_object"}
    assert result["choices"][0]["message"]["content"] == "{\"answer\":2}"
    assert result["output_validated"] is True
    assert result["parsed"] == {"answer": 2}
    assert sent_payload["messages"] == [{"role": "user", "content": "1+1?"}]
    assert sent_payload["inline_inputs"] == {}


def test_python_sdk_chat_sends_n_and_preserves_openai_choices() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "result": {
                    "kind": "inline",
                    "value": "one",
                    "openai_choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "one"}, "finish_reason": "stop"},
                        {"index": 1, "message": {"role": "assistant", "content": "two"}, "finish_reason": "stop"},
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                }
            },
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    result = client.chat.completions.create(messages=[{"role": "user", "content": "pick"}], n=2)

    assert sent_payload["n"] == 2
    assert len(result["choices"]) == 2
    assert result["choices"][1]["message"]["content"] == "two"
    assert result["usage"]["total_tokens"] == 3


def test_python_sdk_chat_rejects_invalid_openai_choices() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "one", "openai_choices": ["bad"]}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallProviderRuntimeError, match="invalid openai_choices"):
        client.chat.completions.create(messages=[{"role": "user", "content": "pick"}])


def test_python_sdk_chat_preserves_refusal_response_field() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "cannot comply", "refusal": "cannot comply"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    result = client.chat.completions.create(messages=[{"role": "user", "content": "bad request"}])

    assert result["choices"][0]["message"]["content"] is None
    assert result["choices"][0]["message"]["refusal"] == "cannot comply"


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


def test_python_sdk_chat_create_rejects_stream_mode() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(GPUCallCallerRoutingError, match="streamed chunks"):
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}], mode="stream")


def test_python_sdk_chat_accepts_assistant_refusal_history() -> None:
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    client.chat.completions.create(messages=[{"role": "assistant", "refusal": "cannot comply"}])

    assert sent_payload["messages"] == [{"role": "assistant", "refusal": "cannot comply"}]


def test_python_sdk_chat_failed_async_job_maps_to_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v2/tasks/async":
            return httpx.Response(202, json={"job_id": "job-1", "state": "PENDING"})
        if request.url.path == "/v2/jobs/job-1":
            return httpx.Response(
                200,
                json={"job_id": "job-1", "state": "FAILED", "error": "capacity unavailable", "provider_error_code": "PROVIDER_CAPACITY_UNAVAILABLE"},
            )
        return httpx.Response(404)

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallProviderRuntimeError) as exc_info:
        client.chat.completions.create(messages=[{"role": "user", "content": "hello"}], mode="async", poll_interval=0)
    assert exc_info.value.code == "PROVIDER_CAPACITY_UNAVAILABLE"


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
        return httpx.Response(422, json={"detail": "empty tuple output", "code": "EMPTY_OUTPUT"})

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


def test_python_sdk_governance_errors_map_to_recipe_helpers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "detail": "no recipe",
                "code": "NO_AUTO_SELECTABLE_RECIPE",
                "failure_artifact": {"safe_request_summary": {"task": "infer", "mode": "sync"}},
            },
        )

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallNoRecipeError) as exc_info:
        client.infer()

    intake = exc_info.value.to_preflight_intake(task="infer", intent="translate_text")
    assert intake["phase"] == "deterministic-intake"
    assert intake["sanitized_request"]["intent"] == "translate_text"


def test_python_sdk_no_eligible_tuple_is_not_generic_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "no eligible tuple after policy, recipe, and circuit constraints", "code": "NO_ELIGIBLE_TUPLE"})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallNoEligibleTupleError):
        client.infer()


def test_python_sdk_timeout_maps_to_cold_start_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("cold start")

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallColdStartTimeout):
        client.infer()


def test_python_sdk_poll_timeout_maps_to_cold_start_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"job_id": "job-1", "state": "RUNNING"})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(GPUCallColdStartTimeout):
        client.poll_job("job-1", interval=0, timeout=0.001)


def test_python_sdk_circuit_breaker_is_scoped_by_task_intent_mode_transport() -> None:
    breaker = GPUCallCircuitBreaker(failure_threshold=2, recovery_timeout_seconds=60)
    vision_scope = GPUCallCircuitScope(task="vision", intent="understand_document_image", mode="sync", transport="v2")
    extract_scope = GPUCallCircuitScope(task="infer", intent="extract_json", mode="sync", transport="v2")
    err = GPUCallProviderRuntimeError("provider exhausted", status_code=503, code="PROVIDER_RESOURCE_EXHAUSTED")

    breaker.record_exception(vision_scope, err)
    breaker.record_exception(vision_scope, err)

    with pytest.raises(GPUCallCircuitOpenError):
        breaker.before_request(vision_scope)
    breaker.before_request(extract_scope)
    assert breaker.failure_count(extract_scope) == 0


def test_python_sdk_circuit_breaker_ignores_governance_and_timeout_errors() -> None:
    breaker = GPUCallCircuitBreaker(failure_threshold=1)
    scope = GPUCallCircuitScope(task="infer", intent="rank_text_items", mode="async", transport="v2")

    breaker.record_exception(scope, GPUCallNoEligibleTupleError("no tuple", status_code=503, code="NO_ELIGIBLE_TUPLE"))
    breaker.record_exception(scope, GPUCallColdStartTimeout("poll timeout"))

    breaker.before_request(scope)
    assert breaker.failure_count(scope) == 0


def test_python_sdk_circuit_breaker_rejects_global_scope() -> None:
    breaker = GPUCallCircuitBreaker()

    with pytest.raises(ValueError, match="task:intent:mode:transport"):
        breaker.before_request("global")
    with pytest.raises(ValueError, match="requires task"):
        GPUCallCircuitScope(task="vision", intent="", mode="sync", transport="v2").key()


def test_python_sdk_async_poll_default_is_cold_start_safe() -> None:
    assert DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS >= 600
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))
    assert inspect.signature(client.poll_job).parameters["timeout"].default >= 600


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
    sent_payload = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        assert request.url.path == "/v2/tasks/stream"
        sent_payload = json.loads(request.read())
        return httpx.Response(200, text=": heartbeat\n\ndata: hello\n\n")

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    assert list(client.stream(prompt="hi", max_tokens=12, temperature=0.2)) == [": heartbeat\n\ndata: hello\n\n"]
    assert sent_payload["max_tokens"] == 12
    assert sent_payload["temperature"] == 0.2


def test_python_sdk_upload_rejects_malformed_presign_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.raises(RuntimeError, match="missing upload_url"):
        client.upload_bytes(b"hello", name="input.txt")


def test_python_sdk_emits_warning_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-gpucall-warning": "fallback used"}, json={"result": {"kind": "inline", "value": "ok"}})

    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler))

    with pytest.warns(Warning, match="fallback used"):
        client.infer()


def test_python_sdk_has_no_caller_routing_selector() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(TypeError):
        client.infer(recipe="text-infer-standard")
    with pytest.raises(TypeError):
        client.infer(provider="local-echo")


def test_python_sdk_infer_rejects_stream_mode() -> None:
    client = GPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500)))

    with pytest.raises(GPUCallCallerRoutingError, match="use stream"):
        client.infer(mode="stream")


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


async def test_async_python_sdk_chat_sends_n_and_preserves_openai_choices() -> None:
    sent_payload = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal sent_payload
        sent_payload = json.loads(request.read())
        return httpx.Response(
            200,
            json={
                "result": {
                    "kind": "inline",
                    "value": "one",
                    "openai_choices": [
                        {"index": 0, "message": {"role": "assistant", "content": "one"}, "finish_reason": "stop"},
                        {"index": 1, "message": {"role": "assistant", "content": "two"}, "finish_reason": "stop"},
                    ],
                }
            },
        )

    async with AsyncGPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler)) as client:
        result = await client.chat.completions.create(messages=[{"role": "user", "content": "pick"}], n=2)

    assert sent_payload["n"] == 2
    assert len(result["choices"]) == 2
    assert result["choices"][1]["message"]["content"] == "two"


async def test_async_python_sdk_infer_rejects_stream_mode() -> None:
    async with AsyncGPUCallClient("http://gpucall.test", transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        with pytest.raises(GPUCallCallerRoutingError, match="use stream"):
            await client.infer(mode="stream")


async def test_async_python_sdk_uploads_presigned_url_without_api_auth(monkeypatch) -> None:
    seen_upload_auth = "not-called"

    class UploadClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def put(self, url, *, content, headers, **kwargs):
            nonlocal seen_upload_auth
            seen_upload_auth = headers.get("authorization")
            assert url == "https://bucket.example/upload"
            assert kwargs.get("follow_redirects") is True
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
        return httpx.Response(200, json={"result": {"kind": "inline", "value": "ok"}})

    client = AsyncGPUCallClient("http://gpucall.test", api_key="api-key", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(httpx, "AsyncClient", UploadClient)
    try:
        await client.upload_bytes(b"data", name="data.txt", content_type="text/plain")
    finally:
        await client.close()

    assert seen_upload_auth is None


async def test_async_python_sdk_streams_chunks() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/tasks/stream"
        return httpx.Response(200, text=": heartbeat\n\ndata: hello\n\n")

    async with AsyncGPUCallClient("http://gpucall.test", transport=httpx.MockTransport(handler)) as client:
        chunks = [chunk async for chunk in client.stream(prompt="hi")]

    assert chunks == [": heartbeat\n\ndata: hello\n\n"]
