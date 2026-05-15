from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import time
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from gpucall_sdk.openai_contract import (
    OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS,
    OPENAI_CHAT_COMPLETIONS_FIELDS,
)

DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES = 8 * 1024
DEFAULT_REQUEST_TIMEOUT_SECONDS = 600.0
DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS = 600.0
SUPPORTED_TASKS = {"infer", "vision", "transcribe", "convert", "train", "fine-tune", "split-infer"}
TERMINAL_JOB_STATES = {
    "SUCCEEDED",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "EXPIRED",
    "COMPLETED_AFTER_CALLER_TIMEOUT",
}


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


class GPUCallCircuitOpenError(RuntimeError):
    pass


@dataclass(frozen=True)
class GPUCallCircuitScope:
    task: str
    intent: str
    mode: str
    transport: str

    def key(self) -> str:
        parts = {
            "task": self.task,
            "intent": self.intent,
            "mode": self.mode,
            "transport": self.transport,
        }
        missing = [name for name, value in parts.items() if not str(value or "").strip()]
        if missing:
            raise ValueError("circuit breaker scope requires task, intent, mode, and transport")
        return ":".join(str(parts[name]).strip() for name in ("task", "intent", "mode", "transport"))


class GPUCallCircuitBreaker:
    """Small caller-side circuit breaker keyed by task/intent/mode/transport.

    This helper is optional. It deliberately rejects global circuits because a
    vision provider outage must not stop unrelated infer/extract_json calls.
    """

    def __init__(self, *, failure_threshold: int = 5, recovery_timeout_seconds: float = 60.0) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        if recovery_timeout_seconds < 0:
            raise ValueError("recovery_timeout_seconds must be >= 0")
        self.failure_threshold = failure_threshold
        self.recovery_timeout_seconds = recovery_timeout_seconds
        self._states: dict[str, dict[str, float | int]] = {}

    @staticmethod
    def scope_key(*, task: str, intent: str, mode: str, transport: str) -> str:
        return GPUCallCircuitScope(task=task, intent=intent, mode=mode, transport=transport).key()

    def before_request(self, scope: GPUCallCircuitScope | str) -> None:
        key = self._coerce_key(scope)
        state = self._states.get(key)
        if not state:
            return
        opened_at = float(state.get("opened_at") or 0.0)
        if not opened_at:
            return
        if time.monotonic() - opened_at >= self.recovery_timeout_seconds:
            state["opened_at"] = 0.0
            return
        raise GPUCallCircuitOpenError(f"gpucall circuit open for scope {key}")

    def record_success(self, scope: GPUCallCircuitScope | str) -> None:
        self._states.pop(self._coerce_key(scope), None)

    def record_exception(self, scope: GPUCallCircuitScope | str, exc: BaseException) -> None:
        if not self.is_provider_failure(exc):
            return
        key = self._coerce_key(scope)
        state = self._states.setdefault(key, {"failures": 0, "opened_at": 0.0})
        failures = int(state.get("failures") or 0) + 1
        state["failures"] = failures
        if failures >= self.failure_threshold:
            state["opened_at"] = time.monotonic()

    def failure_count(self, scope: GPUCallCircuitScope | str) -> int:
        return int(self._states.get(self._coerce_key(scope), {}).get("failures") or 0)

    @staticmethod
    def is_provider_failure(exc: BaseException) -> bool:
        if isinstance(exc, GPUCallProviderRuntimeError):
            return exc.status_code >= 500
        return False

    @staticmethod
    def _coerce_key(scope: GPUCallCircuitScope | str) -> str:
        if isinstance(scope, GPUCallCircuitScope):
            return scope.key()
        key = str(scope).strip()
        if key.count(":") < 3:
            raise ValueError("circuit breaker scope key must include task:intent:mode:transport")
        return key


class GPUCallClient:
    def __init__(
        self,
        base_url: str,
        *,
        api_key: str | None = None,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
        auto_upload_threshold_bytes: int = DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES,
        redact_http_logs: bool = True,
    ) -> None:
        if redact_http_logs:
            _install_http_log_redaction()
        key = api_key if api_key is not None else os.getenv("GPUCALL_API_KEY")
        headers = {"authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout,
            headers=headers,
            transport=transport,
        )
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
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, Any] | None = None,
        stream_options: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auto_upload: bool = True,
        poll: bool = True,
        poll_interval: float = 1.0,
        poll_timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = self._task_payload(
            task=task,
            mode=mode,
            prompt=prompt,
            files=files,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            seed=seed,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            tools=tools,
            tool_choice=tool_choice,
            functions=functions,
            function_call=function_call,
            stream_options=stream_options,
            messages=messages,
            intent=intent,
            task_family=task_family,
            metadata=metadata,
            idempotency_key=idempotency_key,
            auto_upload=auto_upload,
        )
        try:
            request_kwargs: dict[str, Any] = {}
            if request_timeout is not None:
                request_kwargs["timeout"] = request_timeout
            response = self.client.post(f"/v2/tasks/{mode}", json=payload, **request_kwargs)
        except httpx.TimeoutException as exc:
            raise GPUCallColdStartTimeout("gpucall request timed out; this may be normal cold-start latency and is not a provider circuit-breaker signal", original=exc) from exc
        self._emit_warnings(response)
        self._raise_for_status(response)
        data = response.json()
        if response.status_code == 202 and poll:
            return self.poll_job(data["job_id"], interval=poll_interval, timeout=poll_timeout)
        return data

    def vision(
        self,
        *,
        image: str | Path,
        prompt: str | None = None,
        mode: str = "sync",
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        response_format: dict[str, Any] | None = None,
        poll: bool = True,
        poll_timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        return self.infer(
            prompt=prompt,
            files=[image],
            mode=mode,
            task="vision",
            intent=intent,
            task_family=task_family,
            metadata=metadata,
            idempotency_key=idempotency_key,
            response_format=response_format,
            poll=poll,
            poll_timeout=poll_timeout,
            request_timeout=request_timeout,
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

    def poll_job(self, job_id: str, *, interval: float = 1.0, timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.client.get(f"/v2/jobs/{job_id}")
            self._emit_warnings(response)
            self._raise_for_status(response)
            job = response.json()
            if job["state"] in TERMINAL_JOB_STATES:
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
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, Any] | None = None,
        stream_options: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auto_upload: bool = True,
    ) -> dict[str, Any]:
        _validate_task(task)
        refs = [self.upload_file(path) for path in files or []]
        inline = {}
        if prompt is not None:
            body = prompt.encode("utf-8")
            if auto_upload and len(body) > self.auto_upload_threshold_bytes:
                refs.append(self.upload_bytes(body, name="prompt.txt", content_type="text/plain"))
            else:
                inline["prompt"] = {"value": prompt, "content_type": "text/plain"}
        payload: dict[str, Any] = {"task": task, "mode": mode, "input_refs": refs, "inline_inputs": inline}
        selected_intent = intent or task_family
        if selected_intent is not None:
            payload["intent"] = selected_intent
        if metadata:
            payload["metadata"] = {str(key): _metadata_value(value) for key, value in metadata.items() if value is not None}
        if idempotency_key is not None:
            payload["idempotency_key"] = str(idempotency_key)
        if messages is not None:
            payload["messages"] = _normalize_messages(messages)
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        optional = {
            "top_p": top_p,
            "stop": stop,
            "seed": seed,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "tools": tools,
            "tool_choice": tool_choice,
            "functions": functions,
            "function_call": function_call,
            "stream_options": stream_options,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
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
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        auto_upload_threshold_bytes: int = DEFAULT_AUTO_UPLOAD_THRESHOLD_BYTES,
        redact_http_logs: bool = True,
    ) -> None:
        if redact_http_logs:
            _install_http_log_redaction()
        key = api_key if api_key is not None else os.getenv("GPUCALL_API_KEY")
        headers = {"authorization": f"Bearer {key}"} if key else {}
        self.client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS if timeout is None else timeout,
            headers=headers,
            transport=transport,
        )
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
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, Any] | None = None,
        stream_options: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auto_upload: bool = True,
        poll: bool = True,
        poll_interval: float = 1.0,
        poll_timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        payload = await self._task_payload(
            task=task,
            mode=mode,
            prompt=prompt,
            files=files,
            response_format=response_format,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            seed=seed,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            tools=tools,
            tool_choice=tool_choice,
            functions=functions,
            function_call=function_call,
            stream_options=stream_options,
            messages=messages,
            intent=intent,
            task_family=task_family,
            metadata=metadata,
            idempotency_key=idempotency_key,
            auto_upload=auto_upload,
        )
        try:
            request_kwargs: dict[str, Any] = {}
            if request_timeout is not None:
                request_kwargs["timeout"] = request_timeout
            response = await self.client.post(f"/v2/tasks/{mode}", json=payload, **request_kwargs)
        except httpx.TimeoutException as exc:
            raise GPUCallColdStartTimeout("gpucall request timed out; this may be normal cold-start latency and is not a provider circuit-breaker signal", original=exc) from exc
        _emit_warnings(response)
        _raise_for_status(response)
        data = response.json()
        if response.status_code == 202 and poll:
            return await self.poll_job(data["job_id"], interval=poll_interval, timeout=poll_timeout)
        return data

    async def vision(
        self,
        *,
        image: str | Path,
        prompt: str | None = None,
        mode: str = "sync",
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        response_format: dict[str, Any] | None = None,
        poll: bool = True,
        poll_timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
    ) -> dict[str, Any]:
        return await self.infer(
            prompt=prompt,
            files=[image],
            mode=mode,
            task="vision",
            intent=intent,
            task_family=task_family,
            metadata=metadata,
            idempotency_key=idempotency_key,
            response_format=response_format,
            poll=poll,
            poll_timeout=poll_timeout,
            request_timeout=request_timeout,
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
        async with httpx.AsyncClient(timeout=self.client.timeout) as upload_client:
            upload = await upload_client.put(str(presign["upload_url"]), content=body, headers={"content-type": mime})
        if upload.status_code >= 400:
            raise RuntimeError(f"object upload failed: {upload.status_code}")
        return presign["data_ref"]

    async def poll_job(self, job_id: str, *, interval: float = 1.0, timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = await self.client.get(f"/v2/jobs/{job_id}")
            _emit_warnings(response)
            _raise_for_status(response)
            job = response.json()
            if job["state"] in TERMINAL_JOB_STATES:
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
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        seed: int | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, Any] | None = None,
        stream_options: dict[str, Any] | None = None,
        messages: list[dict[str, Any]] | None = None,
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        auto_upload: bool = True,
    ) -> dict[str, Any]:
        _validate_task(task)
        refs = [await self.upload_file(path) for path in files or []]
        inline = {}
        if prompt is not None:
            body = prompt.encode("utf-8")
            if auto_upload and len(body) > self.auto_upload_threshold_bytes:
                refs.append(await self.upload_bytes(body, name="prompt.txt", content_type="text/plain"))
            else:
                inline["prompt"] = {"value": prompt, "content_type": "text/plain"}
        payload: dict[str, Any] = {"task": task, "mode": mode, "input_refs": refs, "inline_inputs": inline}
        selected_intent = intent or task_family
        if selected_intent is not None:
            payload["intent"] = selected_intent
        if metadata:
            payload["metadata"] = {str(key): _metadata_value(value) for key, value in metadata.items() if value is not None}
        if idempotency_key is not None:
            payload["idempotency_key"] = str(idempotency_key)
        if messages is not None:
            payload["messages"] = _normalize_messages(messages)
        if response_format is not None:
            payload["response_format"] = response_format
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if temperature is not None:
            payload["temperature"] = temperature
        optional = {
            "top_p": top_p,
            "stop": stop,
            "seed": seed,
            "presence_penalty": presence_penalty,
            "frequency_penalty": frequency_penalty,
            "tools": tools,
            "tool_choice": tool_choice,
            "functions": functions,
            "function_call": function_call,
            "stream_options": stream_options,
        }
        for key, value in optional.items():
            if value is not None:
                payload[key] = value
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
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        seed: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, Any] | None = None,
        stream_options: dict[str, Any] | None = None,
        user: str | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        n: int | None = None,
        mode: str = "sync",
        auto_upload: bool = True,
        parse_json: bool = False,
        poll_interval: float = 1.0,
        poll_timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        _reject_extra_openai_fields(extra)
        message_payload = _normalize_messages(messages)
        request_metadata = _openai_metadata(
            metadata=metadata,
            model=model,
            intent=intent,
            task_family=task_family,
            top_p=top_p,
            stop=stop,
            seed=seed,
            tools=tools,
            tool_choice=tool_choice,
            functions=functions,
            function_call=function_call,
            user=user,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            n=n,
            extra=extra,
        )
        result = self._root.infer(
            prompt=None,
            messages=message_payload,
            mode=mode,
            response_format=response_format,
            max_tokens=max_tokens,
            auto_upload=auto_upload,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            seed=seed,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            tools=tools,
            tool_choice=tool_choice,
            functions=functions,
            function_call=function_call,
            stream_options=stream_options,
            intent=intent,
            task_family=task_family,
            metadata=request_metadata,
            idempotency_key=idempotency_key,
            poll=True,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            request_timeout=request_timeout,
        )
        value = _extract_result_text(result)
        tool_calls = _extract_tool_calls(result)
        function_call = _extract_function_call(result)
        _raise_if_empty_output(value, tool_calls=tool_calls, function_call=function_call)
        output_validated = _extract_output_validated(result)
        response = _openai_like_response(
            model,
            value,
            usage=_extract_usage(result),
            output_validated=output_validated,
            tool_calls=tool_calls,
            function_call=function_call,
            finish_reason=_extract_finish_reason(result),
        )
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
        intent: str | None = None,
        task_family: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        top_p: float | None = None,
        stop: str | list[str] | None = None,
        seed: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        functions: list[dict[str, Any]] | None = None,
        function_call: str | dict[str, Any] | None = None,
        stream_options: dict[str, Any] | None = None,
        user: str | None = None,
        presence_penalty: float | None = None,
        frequency_penalty: float | None = None,
        n: int | None = None,
        mode: str = "sync",
        auto_upload: bool = True,
        parse_json: bool = False,
        poll_interval: float = 1.0,
        poll_timeout: float = DEFAULT_ASYNC_POLL_TIMEOUT_SECONDS,
        request_timeout: float | None = None,
        **extra: Any,
    ) -> dict[str, Any]:
        _reject_extra_openai_fields(extra)
        message_payload = _normalize_messages(messages)
        request_metadata = _openai_metadata(
            metadata=metadata,
            model=model,
            intent=intent,
            task_family=task_family,
            top_p=top_p,
            stop=stop,
            seed=seed,
            tools=tools,
            tool_choice=tool_choice,
            functions=functions,
            function_call=function_call,
            user=user,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            n=n,
            extra=extra,
        )
        result = await self._root.infer(
            prompt=None,
            messages=message_payload,
            mode=mode,
            response_format=response_format,
            max_tokens=max_tokens,
            auto_upload=auto_upload,
            temperature=temperature,
            top_p=top_p,
            stop=stop,
            seed=seed,
            presence_penalty=presence_penalty,
            frequency_penalty=frequency_penalty,
            tools=tools,
            tool_choice=tool_choice,
            functions=functions,
            function_call=function_call,
            stream_options=stream_options,
            intent=intent,
            task_family=task_family,
            metadata=request_metadata,
            idempotency_key=idempotency_key,
            poll=True,
            poll_interval=poll_interval,
            poll_timeout=poll_timeout,
            request_timeout=request_timeout,
        )
        value = _extract_result_text(result)
        tool_calls = _extract_tool_calls(result)
        function_call = _extract_function_call(result)
        _raise_if_empty_output(value, tool_calls=tool_calls, function_call=function_call)
        output_validated = _extract_output_validated(result)
        response = _openai_like_response(
            model,
            value,
            usage=_extract_usage(result),
            output_validated=output_validated,
            tool_calls=tool_calls,
            function_call=function_call,
            finish_reason=_extract_finish_reason(result),
        )
        if parse_json:
            if output_validated is False:
                raise GPUCallJSONParseError("gpucall returned unvalidated JSON output", raw_text=value)
            try:
                response["parsed"] = json.loads(value)
            except json.JSONDecodeError as exc:
                raise GPUCallJSONParseError("failed to parse gpucall JSON output", raw_text=value) from exc
        return response


def _normalize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    allowed_keys = {"role", "content", "name", "tool_calls", "tool_call_id", "function_call"}
    for message in messages:
        extra = sorted(set(message) - allowed_keys)
        if extra:
            raise GPUCallCallerRoutingError(f"unsupported OpenAI message fields: {', '.join(extra)}")
        role = str(message.get("role", "user"))
        has_content = "content" in message
        content = _normalize_message_content(message.get("content") if has_content else "")
        _validate_openai_message(role=role, content=content, message=message, has_content=has_content)
        item: dict[str, Any] = {"role": role}
        if has_content or not any(message.get(key) is not None for key in ("tool_calls", "tool_call_id", "function_call")):
            item["content"] = content
        for key in ("name", "tool_calls", "tool_call_id", "function_call"):
            if message.get(key) is not None:
                item[key] = message[key]
        normalized.append(item)
    return normalized


def _validate_openai_message(*, role: str, content: str | None, message: dict[str, Any], has_content: bool) -> None:
    if role == "tool":
        if not has_content or content is None or not isinstance(message.get("tool_call_id"), str):
            raise GPUCallCallerRoutingError("tool messages require content and tool_call_id")
        return
    if role == "function":
        if not has_content or content is None or not isinstance(message.get("name"), str):
            raise GPUCallCallerRoutingError("function messages require content and name")
        return
    if role == "assistant" and (message.get("tool_calls") is not None or message.get("function_call") is not None):
        return
    if not has_content or content is None:
        raise GPUCallCallerRoutingError("message content is required unless assistant tool_calls/function_call is present")


def _normalize_message_content(content: Any) -> str | None:
    if content is None:
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        unsupported: list[str] = []
        for item in content:
            if not isinstance(item, dict):
                unsupported.append(type(item).__name__)
                continue
            kind = str(item.get("type") or "")
            if kind in {"text", "input_text"} and isinstance(item.get("text"), str):
                parts.append(item["text"])
                continue
            unsupported.append(kind or "<missing>")
        if parts and not unsupported:
            return "\n".join(parts)
    raise GPUCallCallerRoutingError(
        "structured or multimodal message content is not supported by this SDK method; "
        "use explicit gpucall DataRef APIs instead"
    )


def _validate_task(task: str) -> None:
    if task in SUPPORTED_TASKS:
        return
    raise GPUCallCallerRoutingError(
        f"unsupported gpucall task {task!r}; keep task as one of {sorted(SUPPORTED_TASKS)} "
        "and pass workload purpose with intent=... or task_family=..."
    )


def _openai_metadata(
    *,
    metadata: dict[str, Any] | None,
    model: str,
    intent: str | None,
    task_family: str | None,
    top_p: float | None,
    stop: str | list[str] | None,
    seed: int | None,
    tools: list[dict[str, Any]] | None,
    tool_choice: str | dict[str, Any] | None,
    functions: list[dict[str, Any]] | None,
    function_call: str | dict[str, Any] | None,
    user: str | None,
    presence_penalty: float | None,
    frequency_penalty: float | None,
    n: int | None,
    extra: dict[str, Any],
) -> dict[str, str]:
    result = {str(key): _metadata_value(value) for key, value in (metadata or {}).items() if value is not None}
    result["openai.model"] = model
    if intent:
        result.setdefault("intent", intent)
    if task_family:
        result.setdefault("task_family", task_family)
    optional = {
        "openai.top_p": top_p,
        "openai.stop": stop,
        "openai.seed": seed,
        "openai.tools": tools,
        "openai.tool_choice": tool_choice,
        "openai.functions": functions,
        "openai.function_call": function_call,
        "openai.user": user,
        "openai.presence_penalty": presence_penalty,
        "openai.frequency_penalty": frequency_penalty,
        "openai.n": n,
    }
    for key, value in optional.items():
        if value is not None:
            result[key] = _metadata_value(value)
    if extra:
        result["openai.extra_keys"] = ",".join(sorted(extra))
    return result


def _reject_extra_openai_fields(extra: dict[str, Any]) -> None:
    unsupported: list[str] = []
    for key in sorted(str(key) for key in extra):
        if key in OPENAI_CHAT_COMPLETIONS_FAIL_CLOSED_FIELDS:
            unsupported.append(key)
        elif key in OPENAI_CHAT_COMPLETIONS_FIELDS:
            unsupported.append(key)
        else:
            unsupported.append(f"unknown.{key}")
    if unsupported:
        fields = ", ".join(unsupported)
        raise GPUCallCallerRoutingError(f"unsupported OpenAI chat.completions fields: {fields}")


def _metadata_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _messages_to_prompt(messages: list[dict[str, Any]]) -> str:
    return "\n".join(message["content"] for message in _normalize_messages(messages))


def _extract_result_text(result: dict[str, Any]) -> str:
    payload = result.get("result") or {}
    value = payload.get("value")
    return value if isinstance(value, str) else ""


def _raise_if_empty_output(
    value: str,
    *,
    tool_calls: list[dict[str, Any]] | None = None,
    function_call: dict[str, Any] | None = None,
) -> None:
    if not value.strip() and not tool_calls and not function_call:
        raise GPUCallEmptyOutputError("gpucall returned an empty output")


def _extract_output_validated(result: dict[str, Any]) -> bool | None:
    payload = result.get("result") or {}
    value = payload.get("output_validated")
    return value if isinstance(value, bool) else None


def _extract_usage(result: dict[str, Any]) -> dict[str, int]:
    payload = result.get("result") or {}
    usage = payload.get("usage")
    return usage if isinstance(usage, dict) else {}


def _extract_tool_calls(result: dict[str, Any]) -> list[dict[str, Any]] | None:
    payload = result.get("result") or {}
    tool_calls = payload.get("tool_calls")
    return tool_calls if isinstance(tool_calls, list) else None


def _extract_function_call(result: dict[str, Any]) -> dict[str, Any] | None:
    payload = result.get("result") or {}
    function_call = payload.get("function_call")
    return function_call if isinstance(function_call, dict) else None


def _extract_finish_reason(result: dict[str, Any]) -> str | None:
    payload = result.get("result") or {}
    finish_reason = payload.get("finish_reason")
    return finish_reason if isinstance(finish_reason, str) else None


def _openai_like_response(
    model: str,
    content: str,
    *,
    usage: dict[str, int],
    output_validated: bool | None,
    tool_calls: list[dict[str, Any]] | None = None,
    function_call: dict[str, Any] | None = None,
    finish_reason: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content if content else (None if tool_calls or function_call else ""),
    }
    if tool_calls:
        message["tool_calls"] = tool_calls
    if function_call:
        message["function_call"] = function_call
    resolved_finish_reason = finish_reason or ("tool_calls" if tool_calls else ("function_call" if function_call else "stop"))
    response: dict[str, Any] = {
        "id": f"chatcmpl-{hashlib.sha256(f'{time.time()}:{content}'.encode()).hexdigest()[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
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
    elif code == "NO_ELIGIBLE_TUPLE" or "NO_ELIGIBLE_TUPLE" in detail_text:
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
