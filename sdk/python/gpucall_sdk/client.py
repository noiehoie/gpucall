from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import time
import warnings
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES = 8 * 1024


class GPUCallWarning(Warning):
    pass


class GPUCallJSONParseError(RuntimeError):
    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text


class GPUCallEmptyOutputError(RuntimeError):
    pass


class GPUCallCallerRoutingError(ValueError):
    pass


class GPUCallHTTPError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str | None = None,
        response_body: dict[str, Any] | None = None,
        failure_artifact: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.response_body = response_body or {}
        self.failure_artifact = failure_artifact

    def to_preflight_intake(self, *, task: str | None = None, mode: str | None = None, intent: str | None = None) -> dict[str, Any]:
        from gpucall_recipe_draft.core import DraftInputs, intake_from_error

        return intake_from_error(DraftInputs(error_payload=self.response_body, task=task, mode=mode, intent=intent))


class GPUCallNoRecipeError(GPUCallHTTPError):
    pass


class GPUCallNoEligibleTupleError(GPUCallHTTPError):
    pass


class GPUCallProviderRuntimeError(GPUCallHTTPError):
    pass


class GPUCallColdStartTimeout(TimeoutError):
    def __init__(self, message: str, *, original: Exception | None = None) -> None:
        super().__init__(message)
        self.original = original


class GPUCallClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        transport: httpx.BaseTransport | None = None,
        auto_upload_threshold_bytes: int = DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES,
        redact_http_logs: bool = True,
    ) -> None:
        if redact_http_logs:
            _install_http_log_redaction()
        key = api_key if api_key is not None else os.getenv("GPUCALL_API_KEY")
        headers = {"authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)
        self.auto_upload_threshold_bytes = auto_upload_threshold_bytes
        self.chat = _ChatResource(self)

    def __enter__(self) -> "GPUCallClient":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def close(self) -> None:
        self.client.close()

    def upload_file(self, path: str | Path, *, content_type: str | None = None) -> dict[str, Any]:
        file_path = Path(path)
        body = file_path.read_bytes()
        return self.upload_bytes(body, name=file_path.name, content_type=content_type or mimetypes.guess_type(file_path.name)[0])

    def upload_bytes(self, body: bytes, *, name: str, content_type: str | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(body).hexdigest()
        mime = content_type or "application/octet-stream"
        response = self.client.post(
            "/v2/objects/presign-put",
            json={"name": name, "bytes": len(body), "sha256": digest, "content_type": mime},
        )
        self._raise_for_status(response)
        presign = response.json()
        upload = httpx.put(presign["upload_url"], content=body, headers={"content-type": mime}, timeout=self.client.timeout)
        if upload.status_code >= 400:
            raise RuntimeError(f"object upload failed: {upload.status_code}")
        return presign["data_ref"]

    def infer(
        self,
        *,
        prompt: str | None = None,
        files: list[str | Path] | None = None,
        mode: str = "sync",
        task: str = "infer",
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        messages: list[dict[str, Any]] | None = None,
        auto_upload: bool = True,
        poll: bool = True,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        payload = self._task_payload(
            task=task,
            mode=mode,
            prompt=prompt,
            files=files,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=messages,
            auto_upload=auto_upload,
        )
        try:
            response = self.client.post(f"/v2/tasks/{mode}", json=payload)
        except httpx.TimeoutException as exc:
            raise GPUCallColdStartTimeout("gpucall request timed out; this may be normal cold-start latency and is not a provider circuit-breaker signal", original=exc) from exc
        self._emit_warnings(response)
        self._raise_for_status(response)
        data = response.json()
        if response.status_code == 202 and poll:
            return self.poll_job(data["job_id"], interval=poll_interval)
        return data

    def vision(
        self,
        *,
        image: str | Path,
        prompt: str | None = None,
        mode: str = "sync",
        response_format: dict[str, Any] | None = None,
        poll: bool = True,
    ) -> dict[str, Any]:
        return self.infer(
            prompt=prompt,
            files=[image],
            mode=mode,
            task="vision",
            response_format=response_format,
            poll=poll,
        )

    def stream(
        self,
        *,
        prompt: str | None = None,
        files: list[str | Path] | None = None,
        task: str = "infer",
        response_format: dict[str, Any] | None = None,
    ):
        payload = self._task_payload(
            task=task,
            mode="stream",
            prompt=prompt,
            files=files,
            response_format=response_format,
            max_tokens=None,
            temperature=None,
        )
        with self.client.stream("POST", "/v2/tasks/stream", json=payload) as response:
            self._emit_warnings(response)
            self._raise_for_status(response)
            for chunk in response.iter_text():
                if chunk:
                    yield chunk

    def poll_job(self, job_id: str, *, interval: float = 1.0, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/v2/jobs/{job_id}")
            self._emit_warnings(response)
            self._raise_for_status(response)
            job = response.json()
            if job["state"] in {"COMPLETED", "FAILED", "CANCELLED", "EXPIRED"}:
                return job
            time.sleep(interval)
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    def _task_payload(
        self,
        *,
        task: str,
        mode: str,
        prompt: str | None,
        files: list[str | Path] | None,
        response_format: dict[str, Any] | None,
        max_tokens: int | None,
        temperature: float | None,
        messages: list[dict[str, Any]] | None = None,
        auto_upload: bool = True,
    ) -> dict[str, Any]:
        refs = [self.upload_file(path) for path in files or []]
        inline = {}
        if prompt is not None:
            body = prompt.encode("utf-8")
            if auto_upload and len(body) > self.auto_upload_threshold_bytes:
                refs.append(self.upload_bytes(body, name="prompt.txt", content_type="text/plain"))
            else:
                inline["prompt"] = {"value": prompt, "content_type": "text/plain"}
        payload: dict[str, Any] = {"task": task, "mode": mode, "input_refs": refs, "inline_inputs": inline}
        if messages is not None:
            payload["messages"] = _normalize_messages(messages)
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if mode == "async":
            payload["webhook_url"] = None
        return payload

    def _emit_warnings(self, response: httpx.Response) -> None:
        warning = response.headers.get("x-gpucall-warning")
        if warning:
            warnings.warn(warning, GPUCallWarning, stacklevel=2)

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _raise_typed_http_error(response, exc)


class AsyncGPUCallClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float = 300.0,
        transport: httpx.AsyncBaseTransport | None = None,
        auto_upload_threshold_bytes: int = DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES,
        redact_http_logs: bool = True,
    ) -> None:
        if redact_http_logs:
            _install_http_log_redaction()
        key = api_key if api_key is not None else os.getenv("GPUCALL_API_KEY")
        headers = {"authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, headers=headers, transport=transport)
        self.auto_upload_threshold_bytes = auto_upload_threshold_bytes
        self.chat = _AsyncChatResource(self)

    async def __aenter__(self) -> "AsyncGPUCallClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def infer(
        self,
        *,
        prompt: str | None = None,
        files: list[str | Path] | None = None,
        mode: str = "sync",
        task: str = "infer",
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        messages: list[dict[str, Any]] | None = None,
        auto_upload: bool = True,
        poll: bool = True,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        payload = await self._task_payload(
            task=task,
            mode=mode,
            prompt=prompt,
            files=files,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            messages=messages,
            auto_upload=auto_upload,
        )
        try:
            response = await self.client.post(f"/v2/tasks/{mode}", json=payload)
        except httpx.TimeoutException as exc:
            raise GPUCallColdStartTimeout("gpucall request timed out; this may be normal cold-start latency and is not a provider circuit-breaker signal", original=exc) from exc
        _emit_warnings(response)
        _raise_for_status(response)
        data = response.json()
        if response.status_code == 202 and poll:
            return await self.poll_job(data["job_id"], interval=poll_interval)
        return data

    async def vision(
        self,
        *,
        image: str | Path,
        prompt: str | None = None,
        mode: str = "sync",
        response_format: dict[str, Any] | None = None,
        poll: bool = True,
    ) -> dict[str, Any]:
        return await self.infer(
            prompt=prompt,
            files=[image],
            mode=mode,
            task="vision",
            response_format=response_format,
            poll=poll,
        )

    async def stream(
        self,
        *,
        prompt: str | None = None,
        files: list[str | Path] | None = None,
        task: str = "infer",
        response_format: dict[str, Any] | None = None,
    ):
        payload = await self._task_payload(
            task=task,
            mode="stream",
            prompt=prompt,
            files=files,
            response_format=response_format,
            max_tokens=None,
            temperature=None,
        )
        async with self.client.stream("POST", "/v2/tasks/stream", json=payload) as response:
            _emit_warnings(response)
            _raise_for_status(response)
            async for chunk in response.aiter_text():
                if chunk:
                    yield chunk

    async def upload_file(self, path: str | Path, *, content_type: str | None = None) -> dict[str, Any]:
        file_path = Path(path)
        body = file_path.read_bytes()
        return await self.upload_bytes(body, name=file_path.name, content_type=content_type or mimetypes.guess_type(file_path.name)[0])

    async def upload_bytes(self, body: bytes, *, name: str, content_type: str | None = None) -> dict[str, Any]:
        digest = hashlib.sha256(body).hexdigest()
        mime = content_type or "application/octet-stream"
        response = await self.client.post(
            "/v2/objects/presign-put",
            json={"name": name, "bytes": len(body), "sha256": digest, "content_type": mime},
        )
        _raise_for_status(response)
        presign = response.json()
        upload = await self.client.put(str(presign["upload_url"]), content=body, headers={"content-type": mime})
        if upload.status_code >= 400:
            raise RuntimeError(f"object upload failed: {upload.status_code}")
        return presign["data_ref"]

    async def poll_job(self, job_id: str, *, interval: float = 1.0, timeout: float = 300.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await self.client.get(f"/v2/jobs/{job_id}")
            _emit_warnings(response)
            _raise_for_status(response)
            job = response.json()
            if job["state"] in {"COMPLETED", "FAILED", "CANCELLED", "EXPIRED"}:
                return job
            await _sleep(interval)
        raise TimeoutError(f"job {job_id} did not finish within {timeout}s")

    async def _task_payload(
        self,
        *,
        task: str,
        mode: str,
        prompt: str | None,
        files: list[str | Path] | None,
        response_format: dict[str, Any] | None,
        max_tokens: int | None,
        temperature: float | None,
        messages: list[dict[str, Any]] | None = None,
        auto_upload: bool = True,
    ) -> dict[str, Any]:
        refs = [await self.upload_file(path) for path in files or []]
        inline = {}
        if prompt is not None:
            body = prompt.encode("utf-8")
            if auto_upload and len(body) > self.auto_upload_threshold_bytes:
                refs.append(await self.upload_bytes(body, name="prompt.txt", content_type="text/plain"))
            else:
                inline["prompt"] = {"value": prompt, "content_type": "text/plain"}
        payload: dict[str, Any] = {"task": task, "mode": mode, "input_refs": refs, "inline_inputs": inline}
        if messages is not None:
            payload["messages"] = _normalize_messages(messages)
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        if mode == "async":
            payload["webhook_url"] = None
        return payload


class _ChatResource:
    def __init__(self, root: GPUCallClient) -> None:
        self.completions = _ChatCompletionsResource(root)


class _ChatCompletionsResource:
    def __init__(self, root: GPUCallClient) -> None:
        self._root = root

    def create(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str = "gpucall:auto",
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        mode: str = "sync",
        auto_upload: bool = True,
        parse_json: bool = False,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        message_payload = _normalize_messages(messages)
        result = self._root.infer(
            prompt=None,
            messages=message_payload,
            mode=mode,
            response_format=response_format,
            max_tokens=max_tokens,
            auto_upload=auto_upload,
            temperature=temperature,
            poll=True,
            poll_interval=poll_interval,
        )
        value = _extract_result_text(result)
        _raise_if_empty_output(value)
        output_validated = _extract_output_validated(result)
        response = _openai_like_response(model, value, output_validated=output_validated)
        if parse_json:
            if output_validated is False:
                raise GPUCallJSONParseError("gpucall returned unvalidated JSON output", raw_text=value)
            try:
                response["parsed"] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise GPUCallJSONParseError("failed to parse gpucall JSON output", raw_text=value) from exc
        return response


class _AsyncChatResource:
    def __init__(self, root: AsyncGPUCallClient) -> None:
        self.completions = _AsyncChatCompletionsResource(root)


class _AsyncChatCompletionsResource:
    def __init__(self, root: AsyncGPUCallClient) -> None:
        self._root = root

    async def create(
        self,
        *,
        messages: list[dict[str, Any]],
        model: str = "gpucall:auto",
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        mode: str = "sync",
        auto_upload: bool = True,
        parse_json: bool = False,
        poll_interval: float = 1.0,
    ) -> dict[str, Any]:
        message_payload = _normalize_messages(messages)
        result = await self._root.infer(
            prompt=None,
            messages=message_payload,
            mode=mode,
            response_format=response_format,
            max_tokens=max_tokens,
            auto_upload=auto_upload,
            temperature=temperature,
            poll=True,
            poll_interval=poll_interval,
        )
        value = _extract_result_text(result)
        _raise_if_empty_output(value)
        output_validated = _extract_output_validated(result)
        response = _openai_like_response(model, value, output_validated=output_validated)
        if parse_json:
            if output_validated is False:
                raise GPUCallJSONParseError("gpucall returned unvalidated JSON output", raw_text=value)
            try:
                response["parsed"] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise GPUCallJSONParseError("failed to parse gpucall JSON output", raw_text=value) from exc
        return response


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for message in messages:
        role = str(message.get("role", "user"))
        content = message.get("content", "")
        if not isinstance(content, str):
            raise GPUCallCallerRoutingError(
                "structured or multimodal message content is not supported by this SDK method; "
                "use explicit gpucall DataRef APIs instead"
            )
        text = content
        if text:
            normalized.append({"role": role, "content": text})
    return normalized


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    return "\n".join(message["content"] for message in _normalize_messages(messages))


def _extract_result_text(result: dict[str, Any]) -> str:
    payload = result.get("result") or {}
    value = payload.get("value")
    return value if isinstance(value, str) else ""


def _raise_if_empty_output(value: str) -> None:
    if not value.strip():
        raise GPUCallEmptyOutputError("gpucall returned an empty output")


def _extract_output_validated(result: dict[str, Any]) -> bool | None:
    payload = result.get("result") or {}
    value = payload.get("output_validated")
    return value if isinstance(value, bool) else None


def _openai_like_response(model: str, content: str, *, output_validated: bool | None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "id": f"chatcmpl-{hashlib.sha256(f'{time.time()}:{content}'.encode()).hexdigest()[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
    }
    if output_validated is not None:
        response["output_validated"] = output_validated
    return response


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(seconds)


def _emit_warnings(response: httpx.Response) -> None:
    warning = response.headers.get("x-gpucall-warning")
    if warning:
        warnings.warn(warning, GPUCallWarning, stacklevel=2)


def _raise_for_status(response: httpx.Response) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _raise_typed_http_error(response, exc)


def _raise_typed_http_error(response: httpx.Response, exc: httpx.HTTPStatusError) -> None:
    detail = None
    code = None
    raw_text = None
    payload: dict[str, Any] | None = None
    failure_artifact: dict[str, Any] | None = None
    try:
        payload = response.json()
        detail = payload.get("detail")
        code = payload.get("code")
        artifact = payload.get("failure_artifact") or (payload.get("error") or {}).get("gpucall_failure_artifact")
        failure_artifact = artifact if isinstance(artifact, dict) else None
        result = payload.get("result") or {}
        raw_text = result.get("value") if isinstance(result, dict) else None
    except Exception:
        pass
    if code == "EMPTY_OUTPUT":
        raise GPUCallEmptyOutputError(detail or "gpucall returned an empty output") from exc
    if code == "MALFORMED_OUTPUT":
        raise GPUCallJSONParseError(detail or "gpucall returned malformed JSON output", raw_text=raw_text) from exc
    error_class: type[GPUCallHTTPError] = GPUCallHTTPError
    detail_text = str(detail or "")
    if code == "NO_AUTO_SELECTABLE_RECIPE":
        error_class = GPUCallNoRecipeError
    elif code == "NO_ELIGIBLE_TUPLE" or "NO_ELIGIBLE_TUPLE" in detail_text or "no eligible provider" in detail_text:
        error_class = GPUCallNoEligibleTupleError
    elif response.status_code >= 500:
        error_class = GPUCallProviderRuntimeError
    raise error_class(
        detail or str(exc),
        status_code=response.status_code,
        code=code,
        response_body=payload,
        failure_artifact=failure_artifact,
    ) from exc


class _HTTPLogRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = _redact_url_text(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_redact_log_arg(arg) for arg in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: _redact_log_arg(value) for key, value in record.args.items()}
        return True


def _redact_log_arg(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_url_text(value)
    text = str(value)
    if "://" in text:
        redacted = _redact_url_text(text)
        if redacted != text:
            return redacted
    return value


def _redact_url_text(value: str) -> str:
    if "://" not in value:
        return value
    try:
        parsed = urlsplit(value)
    except ValueError:
        return value
    if parsed.scheme not in {"http", "https"}:
        return value
    if parsed.query:
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "<redacted>", parsed.fragment))
    return value


_HTTP_LOG_FILTER = _HTTPLogRedactionFilter()
_HTTP_LOG_FILTER_INSTALLED = False


def _install_http_log_redaction() -> None:
    global _HTTP_LOG_FILTER_INSTALLED
    if _HTTP_LOG_FILTER_INSTALLED:
        return
    for name in ("httpx", "httpcore"):
        logger = logging.getLogger(name)
        logger.addFilter(_HTTP_LOG_FILTER)
    _HTTP_LOG_FILTER_INSTALLED = True
