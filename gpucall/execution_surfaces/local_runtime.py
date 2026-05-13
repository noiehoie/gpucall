from __future__ import annotations


import asyncio
import os
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

import httpx

from gpucall.domain import CompiledPlan, ProviderErrorCode, TupleError, TupleResult
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.payloads import gpucall_tuple_result
from gpucall.execution.payloads import openai_chat_completion_result
from gpucall.execution.payloads import ollama_generate_result
from gpucall.execution.payloads import plan_payload
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class EchoTuple(TupleAdapter):
    """Local deterministic tuple for development and contract tests."""

    def __init__(self, name: str = "local-echo", latency_seconds: float = 0.01) -> None:
        self.name = name
        self.latency_seconds = latency_seconds
        self.cancelled: set[str] = set()

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(
            tuple=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_invocation",
            cleanup_required=False,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        if datetime.now(timezone.utc) >= handle.expires_at:
            raise TupleError(
                "lease expired before completion",
                retryable=True,
                status_code=504,
                code=ProviderErrorCode.PROVIDER_LEASE_EXPIRED,
            )
        await asyncio.sleep(self.latency_seconds)
        if handle.remote_id in self.cancelled:
            raise TupleError("remote execution cancelled", retryable=False, status_code=499)
        return TupleResult(kind="inline", value=f"ok:{plan.task}:{handle.tuple}")

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        self.cancelled.add(handle.remote_id)

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        yield ": heartbeat\n\n"
        await asyncio.sleep(self.latency_seconds)
        yield f"data: ok:{plan.task}:{handle.tuple}\n\n"


@register_adapter(
    "echo",
    descriptor=TupleAdapterDescriptor(
        requires_contracts=False,
        stream_contract=None,
        production_eligible=False,
        production_rejection_reason="smoke/fake tuple is not eligible for production auto-routing",
        local_execution=True,
        requires_model_for_auto=False,
    ),
)
def build_echo_adapter(spec, _credentials):
    return EchoTuple(name=spec.name)


class LocalOllamaAdapter(TupleAdapter):
    def __init__(
        self,
        name: str = "local-ollama",
        *,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "llama3",
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(
            tuple=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_process",
            cleanup_required=True,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        prompt = self._prompt_from_plan(plan)
        try:
            payload: dict[str, Any] = {"model": self.model, "prompt": prompt, "stream": False}
            if plan.max_tokens is not None:
                payload["options"] = {"num_predict": int(plan.max_tokens)}
            if plan.temperature is not None:
                payload.setdefault("options", {})["temperature"] = float(plan.temperature)
            if plan.response_format is not None and plan.response_format.type.value == "json_object":
                payload["format"] = "json"
            async with httpx.AsyncClient(timeout=plan.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                )
            if response.status_code == 404:
                raise TupleError("local Ollama model or endpoint not found", retryable=False, status_code=502)
            response.raise_for_status()
            return ollama_generate_result(response.json())
        except TupleError:
            raise
        except httpx.ConnectError as exc:
            raise TupleError(
                "local Ollama is unavailable",
                retryable=True,
                status_code=503,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500
            raise TupleError(
                f"local Ollama failed: {exc.response.status_code}",
                retryable=retryable,
                status_code=502,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE if retryable else None,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TupleError(
                "local Ollama timed out",
                retryable=True,
                status_code=504,
                code=ProviderErrorCode.PROVIDER_TIMEOUT,
            ) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        return None

    def _prompt_from_plan(self, plan: CompiledPlan) -> str:
        if plan.input_refs:
            raise TupleError("local Ollama does not support data_refs", retryable=False, status_code=400)
        if "prompt" in plan.inline_inputs:
            return plan.inline_inputs["prompt"].value
        if plan.messages:
            return "\n".join(message.content for message in plan.messages if message.content)
        if plan.inline_inputs:
            return "\n".join(value.value for value in plan.inline_inputs.values())
        return ""


@register_adapter(
    "local-ollama",
    aliases=("local", "ollama"),
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="ollama-generate",
        output_contract="ollama-generate",
        local_execution=True,
        official_sources=(
            "https://github.com/ollama/ollama/blob/main/docs/api.md#generate-a-completion",
        ),
    ),
)
def build_local_ollama_adapter(spec, _credentials):
    if not spec.endpoint or not spec.model:
        raise ValueError("local-ollama tuple requires explicit endpoint and model")
    return LocalOllamaAdapter(
        name=spec.name,
        base_url=str(spec.endpoint),
        model=spec.model,
    )


class LocalOpenAICompatibleAdapter(TupleAdapter):
    def __init__(
        self,
        name: str = "local-openai-compatible",
        *,
        base_url: str,
        model: str,
        api_key: str = "local",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.api_key = api_key
        self.transport = transport

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(
            tuple=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_openai_compatible_request",
            cleanup_required=False,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        if plan.input_refs:
            raise TupleError("local OpenAI-compatible adapter does not dereference DataRefs", retryable=False, status_code=400)
        try:
            async with httpx.AsyncClient(timeout=plan.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"authorization": f"Bearer {self.api_key}"},
                    json=self._chat_payload(plan, stream=False),
                )
            if response.status_code == 404:
                raise TupleError("local OpenAI-compatible endpoint not found", retryable=False, status_code=502)
            response.raise_for_status()
            return openai_chat_completion_result(response.json())
        except TupleError:
            raise
        except httpx.ConnectError as exc:
            raise TupleError(
                "local OpenAI-compatible server is unavailable",
                retryable=True,
                status_code=503,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500
            raise TupleError(
                f"local OpenAI-compatible server failed: {exc.response.status_code}",
                retryable=retryable,
                status_code=502,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE if retryable else None,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TupleError(
                "local OpenAI-compatible server timed out",
                retryable=True,
                status_code=504,
                code=ProviderErrorCode.PROVIDER_TIMEOUT,
            ) from exc

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        if plan.input_refs:
            raise TupleError("local OpenAI-compatible adapter does not dereference DataRefs", retryable=False, status_code=400)
        try:
            async with httpx.AsyncClient(timeout=plan.timeout_seconds, transport=self.transport) as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    headers={"authorization": f"Bearer {self.api_key}"},
                    json=self._chat_payload(plan, stream=True),
                ) as response:
                    if response.status_code == 404:
                        raise TupleError("local OpenAI-compatible endpoint not found", retryable=False, status_code=502)
                    response.raise_for_status()
                    async for chunk in response.aiter_text():
                        if chunk:
                            yield chunk
        except TupleError:
            raise
        except httpx.ConnectError as exc:
            raise TupleError(
                "local OpenAI-compatible server is unavailable",
                retryable=True,
                status_code=503,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500
            raise TupleError(
                f"local OpenAI-compatible server failed: {exc.response.status_code}",
                retryable=retryable,
                status_code=502,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE if retryable else None,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TupleError(
                "local OpenAI-compatible server timed out",
                retryable=True,
                status_code=504,
                code=ProviderErrorCode.PROVIDER_TIMEOUT,
            ) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        return None

    def _chat_payload(self, plan: CompiledPlan, *, stream: bool) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages_from_plan(plan),
            "stream": stream,
        }
        if plan.max_tokens is not None:
            payload["max_tokens"] = plan.max_tokens
        if plan.temperature is not None:
            payload["temperature"] = plan.temperature
        if plan.response_format is not None:
            payload["response_format"] = plan.response_format.model_dump(mode="json")
        return payload

    def _messages_from_plan(self, plan: CompiledPlan) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        has_system_message = any(message.role == "system" for message in plan.messages)
        if plan.system_prompt and not has_system_message:
            messages.append({"role": "system", "content": plan.system_prompt})
        if plan.messages:
            messages.extend({"role": message.role, "content": message.content} for message in plan.messages)
        elif "prompt" in plan.inline_inputs:
            messages.append({"role": "user", "content": plan.inline_inputs["prompt"].value})
        elif plan.inline_inputs:
            messages.append({"role": "user", "content": "\n".join(value.value for value in plan.inline_inputs.values())})
        else:
            messages.append({"role": "user", "content": ""})
        return messages


@register_adapter(
    "local-openai-compatible",
    aliases=("local-openai", "openai-compatible-local"),
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        stream_contract="sse",
        local_execution=True,
        official_sources=(
            "https://platform.openai.com/docs/api-reference/chat/create",
        ),
    ),
)
def build_local_openai_compatible_adapter(spec, _credentials):
    if not spec.endpoint or not spec.model:
        raise ValueError("local-openai-compatible tuple requires explicit endpoint and model")
    api_key = str(spec.provider_params.get("api_key") or "local")
    return LocalOpenAICompatibleAdapter(
        name=spec.name,
        base_url=str(spec.endpoint),
        model=spec.model,
        api_key=api_key,
    )


class LocalDataRefOpenAIWorkerAdapter(TupleAdapter):
    """Adapter for a separate local worker that fetches DataRefs inside its boundary."""

    def __init__(
        self,
        name: str = "local-dataref-openai-worker",
        *,
        worker_url: str,
        api_key: str = "",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.name = name
        self.worker_url = worker_url.rstrip("/")
        self.api_key = api_key
        self.transport = transport

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(
            tuple=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_dataref_openai_worker_request",
            cleanup_required=False,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        try:
            async with httpx.AsyncClient(timeout=plan.timeout_seconds, transport=self.transport) as client:
                response = await client.post(
                    f"{self.worker_url}/gpucall/local-dataref-openai/v1/chat",
                    headers=self._headers(),
                    json=plan_payload(plan),
                )
            if response.status_code == 404:
                raise TupleError("local DataRef OpenAI worker endpoint not found", retryable=False, status_code=502)
            response.raise_for_status()
            return gpucall_tuple_result(response.json())
        except TupleError:
            raise
        except httpx.ConnectError as exc:
            raise TupleError(
                "local DataRef OpenAI worker is unavailable",
                retryable=True,
                status_code=503,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE,
            ) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500
            raise TupleError(
                f"local DataRef OpenAI worker failed: {exc.response.status_code}",
                retryable=retryable,
                status_code=502,
                code=ProviderErrorCode.PROVIDER_UPSTREAM_UNAVAILABLE if retryable else None,
            ) from exc
        except httpx.TimeoutException as exc:
            raise TupleError(
                "local DataRef OpenAI worker timed out",
                retryable=True,
                status_code=504,
                code=ProviderErrorCode.PROVIDER_TIMEOUT,
            ) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        return None

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            return {}
        return {"authorization": f"Bearer {self.api_key}"}


@register_adapter(
    "local-dataref-openai-worker",
    aliases=("local-dataref-openai", "dataref-openai-local"),
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="local-dataref-openai-worker",
        output_contract="gpucall-tuple-result",
        stream_contract="none",
        local_execution=True,
        required_auto_fields={
            "endpoint": "local DataRef OpenAI worker endpoint is not configured",
        },
        official_sources=(
            "https://platform.openai.com/docs/api-reference/chat/create",
        ),
    ),
)
def build_local_dataref_openai_worker_adapter(spec, _credentials):
    if not spec.endpoint:
        raise ValueError("local-dataref-openai-worker tuple requires explicit worker endpoint")
    api_key_env = str(spec.provider_params.get("worker_api_key_env") or "GPUCALL_LOCAL_DATAREF_WORKER_API_KEY")
    return LocalDataRefOpenAIWorkerAdapter(
        name=spec.name,
        worker_url=str(spec.endpoint),
        api_key=os.environ.get(api_key_env, ""),
    )
