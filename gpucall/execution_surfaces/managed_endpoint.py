from __future__ import annotations


from typing import Any

from gpucall.domain import ProviderError

RUNPOD_API_BASE = "https://api.runpod.ai/v2"


def requests_session():
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:
        raise ProviderError("requests/urllib3 are required for RunPod", retryable=False, status_code=501) from exc
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=0)))
    return session


def json_or_error(response: Any, message: str) -> dict[str, Any]:
    if response.status_code in {200, 201, 202}:
        data = response.json()
        return data if isinstance(data, dict) else {"output": data}
    retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    raise ProviderError(
        f"{message}: {response.status_code}",
        retryable=retryable,
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


import asyncio
import os
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.execution.base import ProviderAdapter, RemoteHandle
from gpucall.execution.payloads import openai_chat_completion_result
from gpucall.execution.registry import ProviderAdapterDescriptor, register_adapter


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
            account_ref="runpod",
            execution_surface="managed_endpoint",
            resource_kind="endpoint_request",
            cleanup_required=False,
            reaper_eligible=False,
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
        health = self._health_sync()
        if runpod_vllm_health_rejection_reason(health):
            raise ProviderError(
                "RunPod worker-vLLM endpoint is not ready: " + runpod_vllm_health_rejection_reason(health),
                retryable=True,
                status_code=503,
                code="PROVIDER_CAPACITY_UNAVAILABLE",
            )
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

    def _health_sync(self) -> dict[str, Any]:
        response = requests_session().get(
            f"{self.base_url}/{self.endpoint_id}/health",
            headers=self._headers(),
            timeout=30,
        )
        return json_or_error(response, "RunPod worker-vLLM health check failed")


def runpod_vllm_health_rejection_reason(health: dict[str, Any]) -> str | None:
    workers = health.get("workers") if isinstance(health, dict) else None
    if not isinstance(workers, dict):
        return "health response did not include workers"
    ready = int(workers.get("ready") or 0)
    running = int(workers.get("running") or 0)
    initializing = int(workers.get("initializing") or 0)
    throttled = int(workers.get("throttled") or 0)
    unhealthy = int(workers.get("unhealthy") or 0)
    if unhealthy > 0:
        return "workers.unhealthy is non-zero"
    if ready + running > 0:
        return None
    if initializing > 0:
        return "workers are still initializing"
    if throttled > 0:
        return "workers are throttled and no ready worker is available"
    return "no ready worker is available"


def runpod_vllm_config_findings(provider: Any) -> list[str]:
    findings: list[str] = []
    if not provider.target:
        findings.append(f"provider {provider.name!r} must declare RunPod endpoint id in target")
    if not provider.model:
        findings.append(f"provider {provider.name!r} must declare deployed worker-vLLM model")
    if not provider.image:
        findings.append(f"provider {provider.name!r} must declare official worker-vLLM image")
    elif not str(provider.image).startswith("runpod/worker-v1-vllm:"):
        findings.append(f"provider {provider.name!r} image must be the official runpod/worker-v1-vllm image")
    if "data_refs" in set(provider.input_contracts or []):
        findings.append(f"provider {provider.name!r} official worker-vLLM path must not declare DataRef input support")

    worker_env = (provider.provider_params or {}).get("worker_env")
    if not isinstance(worker_env, dict):
        findings.append(f"provider {provider.name!r} must declare provider_params.worker_env for official worker-vLLM deployment")
        return findings

    model_name = str(worker_env.get("MODEL_NAME") or "")
    served_name = str(worker_env.get("OPENAI_SERVED_MODEL_NAME_OVERRIDE") or "")
    declared_model = str(provider.model or "")
    if declared_model and declared_model not in {model_name, served_name}:
        findings.append(
            f"provider {provider.name!r} model must match MODEL_NAME or OPENAI_SERVED_MODEL_NAME_OVERRIDE in worker_env"
        )
    try:
        max_model_len = int(worker_env.get("MAX_MODEL_LEN"))
    except (TypeError, ValueError):
        findings.append(f"provider {provider.name!r} worker_env.MAX_MODEL_LEN must be an integer")
    else:
        if max_model_len < int(provider.max_model_len):
            findings.append(f"provider {provider.name!r} worker_env.MAX_MODEL_LEN is below provider max_model_len")
    if "GPU_MEMORY_UTILIZATION" not in worker_env:
        findings.append(f"provider {provider.name!r} worker_env.GPU_MEMORY_UTILIZATION must be declared")
    if "MAX_CONCURRENCY" not in worker_env:
        findings.append(f"provider {provider.name!r} worker_env.MAX_CONCURRENCY must be declared")
    return findings


@register_adapter(
    "runpod-vllm-serverless",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        config_validator=runpod_vllm_config_findings,
        official_sources=(
            "https://docs.runpod.io/serverless/vllm/openai-compatibility",
            "https://docs.runpod.io/serverless/endpoints/send-requests",
            "https://docs.runpod.io/serverless/vllm/environment-variables",
            "https://github.com/runpod-workers/worker-vllm",
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

import asyncio
import os
import time
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.execution.base import ProviderAdapter, RemoteHandle
from gpucall.execution.payloads import gpucall_provider_result, plan_payload
from gpucall.execution.registry import ProviderAdapterDescriptor, register_adapter


class RunpodServerlessAdapter(ProviderAdapter):
    """RunPod queue-based Serverless adapter using the official REST contract."""

    def __init__(
        self,
        name: str = "runpod",
        *,
        api_key: str | None = None,
        endpoint_id: str | None = None,
        model: str | None = None,
        max_model_len: int | None = None,
        base_url: str | None = None,
        poll_interval_seconds: float = 2.0,
    ) -> None:
        self.name = name
        self.api_key = api_key or os.getenv("GPUCALL_RUNPOD_API_KEY", "")
        self.endpoint_id = endpoint_id or os.getenv("GPUCALL_RUNPOD_ENDPOINT_ID", "")
        self.model = model
        self.max_model_len = max_model_len
        self.base_url = (base_url or RUNPOD_API_BASE).rstrip("/")
        self.poll_interval_seconds = poll_interval_seconds

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        if not self.api_key or not self.endpoint_id:
            raise ProviderError("RunPod api_key or endpoint_id is not configured", retryable=False, status_code=401)
        if plan.mode.value == "sync":
            run_request = await asyncio.to_thread(self._runsync_sync, plan)
        else:
            run_request = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(
            provider=self.name,
            remote_id=run_request["job_id"],
            expires_at=plan.expires_at(),
            account_ref="runpod",
            execution_surface="managed_endpoint",
            resource_kind="serverless_job",
            cleanup_required=True,
            reaper_eligible=False,
            meta=run_request,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        return await asyncio.to_thread(self._wait_sync, handle, plan)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        if self.api_key and self.endpoint_id and handle.remote_id:
            await asyncio.to_thread(self._cancel_sync, handle.remote_id)

    def _start_sync(self, plan: CompiledPlan) -> dict[str, str]:
        response = requests_session().post(
            f"{self.base_url}/{self.endpoint_id}/run",
            headers=self._headers(),
            json={
                "input": self._payload(plan),
                "policy": {
                    "executionTimeout": max(int(plan.timeout_seconds * 1000), 5000),
                    "ttl": max(int(plan.lease_ttl_seconds * 1000), 10000),
                },
            },
            timeout=10,
        )
        data = json_or_error(response, "RunPod start failed")
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise ProviderError("RunPod start response did not include job id", retryable=True, status_code=502)
        return {"job_id": str(job_id)}

    def _runsync_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        response = requests_session().post(
            f"{self.base_url}/{self.endpoint_id}/runsync",
            headers=self._headers(),
            json={
                "input": self._payload(plan),
                "policy": {
                    "executionTimeout": max(int(plan.timeout_seconds * 1000), 5000),
                    "ttl": max(int(plan.lease_ttl_seconds * 1000), 10000),
                },
            },
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        data = json_or_error(response, "RunPod runsync failed")
        status = data.get("status")
        if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            code = "PROVIDER_TIMEOUT" if status == "TIMED_OUT" else "PROVIDER_JOB_FAILED"
            raise ProviderError(f"RunPod status: {status}", retryable=status == "TIMED_OUT", status_code=502, code=code)
        if "output" in data and status in {None, "COMPLETED"}:
            return {"job_id": str(data.get("id") or data.get("job_id") or f"runsync-{plan.plan_id}"), "completed_output": data["output"]}
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise ProviderError("RunPod runsync response did not include output or job id", retryable=True, status_code=502)
        return {"job_id": str(job_id)}

    def _wait_sync(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        if "completed_output" in handle.meta:
            return gpucall_provider_result(handle.meta["completed_output"])
        deadline = time.monotonic() + plan.timeout_seconds
        while time.monotonic() < deadline:
            response = requests_session().get(
                f"{self.base_url}/{self.endpoint_id}/status/{handle.remote_id}",
                headers=self._headers(),
                timeout=10,
            )
            data = json_or_error(response, "RunPod status failed")
            status = data.get("status")
            if status == "COMPLETED":
                return gpucall_provider_result(data.get("output"))
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                raise ProviderError(f"RunPod status: {status}", retryable=status == "TIMED_OUT", status_code=502)
            time.sleep(self.poll_interval_seconds)
        raise ProviderError("RunPod polling timed out", retryable=True, status_code=504)

    def _cancel_sync(self, job_id: str) -> None:
        response = requests_session().post(
            f"{self.base_url}/{self.endpoint_id}/cancel/{job_id}",
            headers=self._headers(),
            timeout=10,
        )
        if response.status_code in {200, 202, 204, 404}:
            return
        json_or_error(response, "RunPod cancel failed")

    def _payload(self, plan: CompiledPlan) -> dict[str, Any]:
        payload = plan_payload(plan)
        if self.model:
            payload["model"] = self.model
        if self.max_model_len:
            payload["max_model_len"] = self.max_model_len
        return payload

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}", "content-type": "application/json", "accept": "application/json"}


@register_adapter(
    "runpod-serverless",
    aliases=("runpod",),
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="runpod-serverless",
        output_contract="gpucall-provider-result",
        production_eligible=False,
        production_rejection_reason=(
            "RunPod generic Serverless queue operations are official, but the gpucall-provider-result worker "
            "contract is custom and must not be treated as the official worker-vLLM production route"
        ),
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        official_sources=(
            "https://docs.runpod.io/serverless/endpoints/send-requests",
            "https://docs.runpod.io/serverless/references/operations",
        ),
    ),
)
def build_runpod_serverless_adapter(spec, credentials):
    runpod = credentials.get("runpod", {})
    return RunpodServerlessAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
    )
