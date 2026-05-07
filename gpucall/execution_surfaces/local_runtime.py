from __future__ import annotations


import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.execution.base import ProviderAdapter, RemoteHandle
from gpucall.execution.registry import ProviderAdapterDescriptor, register_adapter


class EchoProvider(ProviderAdapter):
    """Local deterministic provider for development and contract tests."""

    def __init__(self, name: str = "local-echo", latency_seconds: float = 0.01) -> None:
        self.name = name
        self.latency_seconds = latency_seconds
        self.cancelled: set[str] = set()

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(
            provider=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_invocation",
            cleanup_required=False,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        if datetime.now(timezone.utc) >= handle.expires_at:
            raise ProviderError("lease expired before completion", retryable=True, status_code=504)
        await asyncio.sleep(self.latency_seconds)
        if handle.remote_id in self.cancelled:
            raise ProviderError("remote execution cancelled", retryable=False, status_code=499)
        return ProviderResult(kind="inline", value=f"ok:{plan.task}:{handle.provider}")

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        self.cancelled.add(handle.remote_id)

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        yield ": heartbeat\n\n"
        await asyncio.sleep(self.latency_seconds)
        yield f"data: ok:{plan.task}:{handle.provider}\n\n"


@register_adapter(
    "echo",
    descriptor=ProviderAdapterDescriptor(
        requires_contracts=False,
        stream_contract=None,
        production_eligible=False,
        production_rejection_reason="smoke/fake provider is not eligible for production auto-routing",
        local_execution=True,
        requires_model_for_auto=False,
    ),
)
def build_echo_adapter(spec, _credentials):
    return EchoProvider(name=spec.name)


from uuid import uuid4

import httpx

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.execution.base import ProviderAdapter, RemoteHandle
from gpucall.execution.payloads import ollama_generate_result
from gpucall.execution.registry import ProviderAdapterDescriptor, register_adapter


class LocalOllamaAdapter(ProviderAdapter):
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
            provider=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_process",
            cleanup_required=True,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        prompt = self._prompt_from_plan(plan)
        try:
            async with httpx.AsyncClient(timeout=plan.timeout_seconds) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json={"model": self.model, "prompt": prompt, "stream": False},
                )
            if response.status_code == 404:
                raise ProviderError("local Ollama model or endpoint not found", retryable=False, status_code=502)
            response.raise_for_status()
            return ollama_generate_result(response.json())
        except ProviderError:
            raise
        except httpx.ConnectError as exc:
            raise ProviderError("local Ollama is unavailable", retryable=True, status_code=503) from exc
        except httpx.HTTPStatusError as exc:
            retryable = exc.response.status_code >= 500
            raise ProviderError(f"local Ollama failed: {exc.response.status_code}", retryable=retryable, status_code=502) from exc
        except httpx.TimeoutException as exc:
            raise ProviderError("local Ollama timed out", retryable=True, status_code=504) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        return None

    def _prompt_from_plan(self, plan: CompiledPlan) -> str:
        if plan.input_refs:
            raise ProviderError("local Ollama does not support data_refs", retryable=False, status_code=400)
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
    descriptor=ProviderAdapterDescriptor(
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
