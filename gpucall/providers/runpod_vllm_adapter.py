from __future__ import annotations

import asyncio
import os
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import openai_chat_completion_result
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter
from gpucall.providers.runpod_common import RUNPOD_API_BASE, json_or_error, requests_session


class RunpodVllmServerlessAdapter(ProviderAdapter):
    """RunPod official Serverless worker-vLLM OpenAI-compatible adapter."""

    def __init__(
        self,
        name: str = "runpod-vllm-serverless",
        *,
        api_key: str | None = None,
        endpoint_id: str | None = None,
        model: str | None = None,
        max_model_len: int | None = None,
        image: str | None = None,
        base_url: str | None = None,
        endpoint_contract: str | None = None,
    ) -> None:
        self.name = name
        self.api_key = api_key or os.getenv("GPUCALL_RUNPOD_API_KEY", "")
        self.endpoint_id = endpoint_id or os.getenv("GPUCALL_RUNPOD_FLASH_ENDPOINT_ID", "")
        self.model = model
        self.max_model_len = max_model_len
        self.image = image
        self.base_url = (base_url or RUNPOD_API_BASE).rstrip("/")
        self.endpoint_contract = endpoint_contract

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        if not self.api_key:
            raise ProviderError("RunPod api_key is not configured", retryable=False, status_code=401)
        if not self.endpoint_id:
            raise ProviderError("RunPod worker-vLLM endpoint_id is not configured", retryable=False, status_code=400)
        if not self.model:
            raise ProviderError("RunPod worker-vLLM model is not configured", retryable=False, status_code=400)
        if self.endpoint_contract != "openai-chat-completions":
            raise ProviderError("RunPod worker-vLLM requires endpoint_contract=openai-chat-completions", retryable=False, status_code=400)
        if plan.mode.value == "stream":
            raise ProviderError("RunPod worker-vLLM streaming is not supported in v2.0", retryable=False, status_code=400)
        return RemoteHandle(
            provider=self.name,
            remote_id=f"openai-{plan.plan_id}",
            expires_at=plan.expires_at(),
            meta={"official_vllm": True},
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        try:
            return await asyncio.to_thread(self._call_sync, plan)
        except asyncio.TimeoutError as exc:
            raise ProviderError("RunPod worker-vLLM timed out", retryable=True, status_code=504) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        return None

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        raise ProviderError("RunPod worker-vLLM streaming is not supported in v2.0", retryable=False, status_code=400)

    def _call_sync(self, plan: CompiledPlan) -> ProviderResult:
        if plan.input_refs:
            raise ProviderError("RunPod worker-vLLM does not fetch DataRef inputs; falling back", retryable=True, status_code=502)
        if not plan.messages:
            raise ProviderError("RunPod worker-vLLM openai-chat-completions contract requires compiled messages", retryable=True, status_code=502)
        response = requests_session().post(
            f"{self.base_url}/{self.endpoint_id}/openai/v1/chat/completions",
            headers=self._headers(),
            json=self._payload(plan),
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        return openai_chat_completion_result(json_or_error(response, "RunPod worker-vLLM chat completion failed"))

    def _payload(self, plan: CompiledPlan) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._messages(plan),
            "stream": False,
        }
        if plan.temperature is not None:
            payload["temperature"] = plan.temperature
        if plan.max_tokens is not None:
            payload["max_tokens"] = plan.max_tokens
        if plan.response_format is not None:
            if plan.response_format.type.value == "json_object":
                payload["response_format"] = {"type": "json_object"}
            elif plan.response_format.type.value == "json_schema":
                payload["response_format"] = {"type": "json_schema", "json_schema": plan.response_format.json_schema}
        return payload

    def _messages(self, plan: CompiledPlan) -> list[dict[str, str]]:
        if not plan.messages:
            raise ProviderError("RunPod worker-vLLM requires compiled chat messages", retryable=False, status_code=400)
        return [{"role": message.role, "content": message.content} for message in plan.messages]

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}", "content-type": "application/json", "accept": "application/json"}


@register_adapter(
    "runpod-vllm-serverless",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        official_sources=(
            "https://docs.runpod.io/serverless/vllm/openai-compatibility",
            "https://docs.runpod.io/serverless/endpoints/send-requests",
        ),
    ),
)
def build_runpod_vllm_serverless_adapter(spec, credentials):
    runpod = credentials.get("runpod", {})
    return RunpodVllmServerlessAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
        image=spec.image,
        endpoint_contract=spec.endpoint_contract,
    )
