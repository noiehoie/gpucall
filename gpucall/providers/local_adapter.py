from __future__ import annotations

from uuid import uuid4

import httpx

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import ollama_generate_result


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
        return RemoteHandle(provider=self.name, remote_id=uuid4().hex, expires_at=plan.expires_at())

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
