from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import gpucall_provider_result, openai_chat_completion_result, plan_payload
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter

RUNPOD_API_BASE = "https://api.runpod.ai/v2"


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
            return RemoteHandle(
                provider=self.name,
                remote_id=run_request["job_id"],
                expires_at=plan.expires_at(),
                meta=run_request,
            )
        run_request = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(
            provider=self.name,
            remote_id=run_request["job_id"],
            expires_at=plan.expires_at(),
            meta=run_request,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        return await asyncio.to_thread(self._wait_sync, handle, plan)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        if self.api_key and self.endpoint_id and handle.remote_id:
            await asyncio.to_thread(self._cancel_sync, handle.remote_id)

    def _start_sync(self, plan: CompiledPlan) -> Any:
        session = self._session()
        payload = self._payload(plan)
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/run",
            headers=self._headers(),
            json={
                "input": payload,
                "policy": {
                    "executionTimeout": max(int(plan.timeout_seconds * 1000), 5000),
                    "ttl": max(int(plan.lease_ttl_seconds * 1000), 10000),
                },
            },
            timeout=10,
        )
        data = self._json_or_error(response, "RunPod start failed")
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise ProviderError("RunPod start response did not include job id", retryable=True, status_code=502)
        return {"job_id": str(job_id)}

    def _wait_sync(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        if "completed_output" in handle.meta:
            return gpucall_provider_result(handle.meta["completed_output"])
        session = self._session()
        deadline = time.monotonic() + plan.timeout_seconds
        while time.monotonic() < deadline:
            response = session.get(
                f"{self.base_url}/{self.endpoint_id}/status/{handle.remote_id}",
                headers=self._headers(),
                timeout=10,
            )
            data = self._json_or_error(response, "RunPod status failed")
            status = data.get("status")
            if status == "COMPLETED":
                return gpucall_provider_result(data.get("output"))
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                raise ProviderError(f"RunPod status: {status}", retryable=status == "TIMED_OUT", status_code=502)
            time.sleep(self.poll_interval_seconds)
        raise ProviderError("RunPod polling timed out", retryable=True, status_code=504)

    def _cancel_sync(self, job_id: str) -> None:
        session = self._session()
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/cancel/{job_id}",
            headers=self._headers(),
            timeout=10,
        )
        if response.status_code in {200, 202, 204, 404}:
            return
        self._json_or_error(response, "RunPod cancel failed")

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }

    def _session(self):
        try:
            import requests
            from requests.adapters import HTTPAdapter
            from urllib3.util.retry import Retry
        except ImportError as exc:
            raise ProviderError("requests/urllib3 are required for RunPod", retryable=False, status_code=501) from exc
        session = requests.Session()
        retry = Retry(total=0)
        session.mount("https://", HTTPAdapter(max_retries=retry))
        return session

    def _payload(self, plan: CompiledPlan) -> dict[str, Any]:
        payload = plan_payload(plan)
        if self.model:
            payload["model"] = self.model
        if self.max_model_len:
            payload["max_model_len"] = self.max_model_len
        return payload

    def _json_or_error(self, response: Any, message: str) -> dict[str, Any]:
        if response.status_code in {200, 201, 202}:
            data = response.json()
            return data if isinstance(data, dict) else {"output": data}
        retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
        raise ProviderError(
            f"{message}: {response.status_code}",
            retryable=retryable,
            status_code=502 if response.status_code >= 500 else response.status_code,
        )

    def _runsync_sync(self, plan: CompiledPlan) -> dict[str, Any]:
        session = self._session()
        payload = self._payload(plan)
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/runsync",
            headers=self._headers(),
            json={
                "input": payload,
                "policy": {
                    "executionTimeout": max(int(plan.timeout_seconds * 1000), 5000),
                    "ttl": max(int(plan.lease_ttl_seconds * 1000), 10000),
                },
            },
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        data = self._json_or_error(response, "RunPod runsync failed")
        status = data.get("status")
        if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            code = "PROVIDER_TIMEOUT" if status == "TIMED_OUT" else "PROVIDER_JOB_FAILED"
            raise ProviderError(f"RunPod status: {status}", retryable=status == "TIMED_OUT", status_code=502, code=code)
        if status == "COMPLETED" and "output" in data:
            return {
                "job_id": str(data.get("id") or data.get("job_id") or f"runsync-{plan.plan_id}"),
                "completed_output": data["output"],
            }
        if "output" in data and status is None:
            return {
                "job_id": str(data.get("id") or data.get("job_id") or f"runsync-{plan.plan_id}"),
                "completed_output": data["output"],
            }
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise ProviderError("RunPod runsync response did not include output or job id", retryable=True, status_code=502)
        return {"job_id": str(job_id)}


class RunpodFlashAdapter(ProviderAdapter):
    """RunPod worker-vLLM adapter for deployed, synchronous real-model workers.

    The production path is RunPod's official worker-vLLM OpenAI-compatible
    endpoint. The historical Flash SDK path remains behind an explicit
    experimental environment flag.
    """

    def __init__(
        self,
        name: str = "runpod-flash",
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
        if plan.mode.value == "stream":
            raise ProviderError("RunPod worker-vLLM streaming is not supported in v2.0", retryable=False, status_code=400)
        resource_name = f"gpucall-flash-worker-{plan.plan_id}"
        if self._uses_official_vllm():
            if not self.endpoint_id:
                raise ProviderError("RunPod worker-vLLM endpoint_id is not configured", retryable=False, status_code=400)
            if not self.model:
                raise ProviderError("RunPod worker-vLLM model is not configured", retryable=False, status_code=400)
            return RemoteHandle(
                provider=self.name,
                remote_id=f"openai-{plan.plan_id}",
                expires_at=plan.expires_at(),
                meta={"resource_name": resource_name, "official_vllm": True},
            )
        if not _allow_runpod_flash_experimental_worker():
            raise ProviderError(
                "RunPod production mode requires a deployed official worker-vLLM endpoint",
                retryable=False,
                status_code=400,
            )
        if self.endpoint_id:
            job_id = await asyncio.to_thread(self._start_endpoint_job_sync, plan, resource_name)
            return RemoteHandle(
                provider=self.name,
                remote_id=job_id,
                expires_at=plan.expires_at(),
                meta={"resource_name": resource_name, "job_id": job_id},
            )
        return RemoteHandle(
            provider=self.name,
            remote_id=resource_name,
            expires_at=plan.expires_at(),
            meta={"resource_name": resource_name},
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        try:
            if handle.meta.get("official_vllm"):
                value = await asyncio.to_thread(self._call_official_vllm_sync, plan)
            elif self.endpoint_id and handle.meta.get("job_id"):
                value = await asyncio.to_thread(self._poll_endpoint_job_sync, str(handle.meta["job_id"]), plan)
            else:
                value = await asyncio.wait_for(self._run_flash(plan), timeout=plan.timeout_seconds)
            if handle.meta.get("official_vllm"):
                return value
            return gpucall_provider_result(value)
        except asyncio.TimeoutError as exc:
            raise ProviderError("RunPod worker-vLLM timed out", retryable=True, status_code=504) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        job_id = handle.meta.get("job_id")
        if job_id and self.endpoint_id:
            await asyncio.to_thread(self._cancel_endpoint_job_sync, str(job_id))
        resource_id = handle.meta.get("resource_id")
        resource_name = handle.meta.get("resource_name")
        if not resource_id:
            return
        await asyncio.to_thread(_runpod_flash_cleanup_resource_sync, str(resource_id), str(resource_name or ""))

    async def _run_flash(self, plan: CompiledPlan) -> Any:
        os.environ.setdefault("FLASH_SENTINEL_TIMEOUT", str(max(int(plan.timeout_seconds), 300)))
        os.environ.setdefault("FLASH_IS_LIVE_PROVISIONING", "true")
        if self.api_key:
            os.environ.setdefault("RUNPOD_API_KEY", self.api_key)
        payload = plan_payload(plan)
        payload["resource_name"] = f"gpucall-flash-worker-{plan.plan_id}"
        if self.model:
            payload["model"] = self.model
        if self.max_model_len:
            payload["max_model_len"] = self.max_model_len
        if self.endpoint_id:
            return await asyncio.to_thread(self._runsync_endpoint_sync, payload, plan)
        try:
            from runpod_flash import Endpoint  # type: ignore
            from runpod_flash.endpoint import EndpointJob  # type: ignore
            from gpucall.providers.runpod_flash_worker import run_inference_on_flash
        except ImportError as exc:
            raise ProviderError("runpod-flash is not installed", retryable=False, status_code=501) from exc
        value = run_inference_on_flash(payload)
        if inspect.isawaitable(value):
            value = await value
        if isinstance(value, dict) and value.get("id") and value.get("status") not in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
            endpoint = Endpoint(id=self.endpoint_id) if self.endpoint_id else Endpoint(name="gpucall-flash-worker")
            job = EndpointJob(value, endpoint)
            await job.wait(timeout=max(float(plan.timeout_seconds), 300.0))
            if job.error:
                raise ProviderError("RunPod Flash job failed", retryable=True, status_code=502, code="PROVIDER_JOB_FAILED")
            return job.output
        if hasattr(value, "wait") and hasattr(value, "output"):
            await value.wait(timeout=max(float(plan.timeout_seconds), 300.0))
            if getattr(value, "error", None):
                raise ProviderError("RunPod Flash job failed", retryable=True, status_code=502, code="PROVIDER_JOB_FAILED")
            return value.output
        if isinstance(value, dict) and value.get("status") == "COMPLETED" and "output" in value:
            return value["output"]
        return value

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        raise ProviderError("RunPod worker-vLLM streaming is not supported in v2.0", retryable=False, status_code=400)

    def _start_endpoint_job_sync(self, plan: CompiledPlan, resource_name: str) -> str:
        payload = plan_payload(plan)
        payload["resource_name"] = resource_name
        if self.model:
            payload["model"] = self.model
        if self.max_model_len:
            payload["max_model_len"] = self.max_model_len
        session = _requests_session()
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/run",
            headers=self._headers(),
            json={"input": payload},
            timeout=10,
        )
        data = _json_or_error(response, "RunPod Flash run failed")
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise ProviderError("RunPod Flash run response did not include job id", retryable=True, status_code=502)
        return str(job_id)

    def _runsync_endpoint_sync(self, payload: dict[str, Any], plan: CompiledPlan) -> Any:
        session = _requests_session()
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/runsync",
            headers=self._headers(),
            json={"input": payload},
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        data = _json_or_error(response, "RunPod Flash runsync failed")
        return self._extract_runsync_output(data, plan)

    def _extract_runsync_output(self, data: dict[str, Any], plan: CompiledPlan) -> Any:
        status = data.get("status")
        if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
            code = "PROVIDER_TIMEOUT" if status == "TIMED_OUT" else "PROVIDER_JOB_FAILED"
            raise ProviderError(f"RunPod Flash job failed: {status}", retryable=status == "TIMED_OUT", status_code=502, code=code)
        if status == "COMPLETED" and "output" in data:
            return data["output"]
        if "output" in data and status is None:
            return data["output"]
        job_id = data.get("id") or data.get("job_id")
        if job_id:
            return self._poll_endpoint_job_sync(str(job_id), plan)
        if "error" in data:
            raise ProviderError("RunPod Flash job failed", retryable=True, status_code=502, code="PROVIDER_JOB_FAILED")
        return data

    def _poll_endpoint_job_sync(self, job_id: str, plan: CompiledPlan) -> Any:
        session = _requests_session()
        deadline = time.monotonic() + plan.timeout_seconds
        while time.monotonic() < deadline:
            response = session.get(
                f"{self.base_url}/{self.endpoint_id}/status/{job_id}",
                headers=self._headers(),
                timeout=10,
            )
            data = _json_or_error(response, "RunPod Flash status failed")
            status = data.get("status")
            if status == "COMPLETED" and "output" in data:
                return data["output"]
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                code = "PROVIDER_TIMEOUT" if status == "TIMED_OUT" else "PROVIDER_JOB_FAILED"
                raise ProviderError(f"RunPod Flash job failed: {status}", retryable=status == "TIMED_OUT", status_code=502, code=code)
            time.sleep(2.0)
        raise ProviderError("RunPod Flash polling timed out", retryable=True, status_code=504)

    def _cancel_endpoint_job_sync(self, job_id: str) -> None:
        session = _requests_session()
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/cancel/{job_id}",
            headers=self._headers(),
            timeout=10,
        )
        if response.status_code in {200, 202, 204, 404}:
            return
        _json_or_error(response, "RunPod Flash cancel failed")

    def _uses_official_vllm(self) -> bool:
        return self.endpoint_contract == "openai-chat-completions"

    def _call_official_vllm_sync(self, plan: CompiledPlan) -> ProviderResult:
        if plan.input_refs:
            raise ProviderError(
                "RunPod worker-vLLM does not fetch DataRef inputs; falling back",
                retryable=True,
                status_code=502,
            )
        if not plan.messages:
            raise ProviderError(
                "RunPod worker-vLLM openai-chat-completions contract requires compiled messages",
                retryable=True,
                status_code=502,
            )
        if not self.model:
            raise ProviderError("RunPod worker-vLLM model is not configured", retryable=False, status_code=400)
        session = _requests_session()
        response = session.post(
            f"{self.base_url}/{self.endpoint_id}/openai/v1/chat/completions",
            headers=self._headers(),
            json=self._official_vllm_openai_payload(plan),
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        data = _json_or_error(response, "RunPod worker-vLLM chat completion failed")
        return self._official_vllm_result(data)

    def _official_vllm_openai_payload(self, plan: CompiledPlan) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._official_vllm_messages(plan),
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
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": plan.response_format.json_schema,
                }
        return payload

    def _official_vllm_messages(self, plan: CompiledPlan) -> list[dict[str, str]]:
        if not plan.messages:
            raise ProviderError("RunPod worker-vLLM requires compiled chat messages", retryable=False, status_code=400)
        return [
            {
                "role": getattr(message, "role", None) or message.get("role", "user"),
                "content": getattr(message, "content", None) or message.get("content", ""),
            }
            for message in plan.messages
        ]

    def _official_vllm_result(self, data: dict[str, Any]) -> ProviderResult:
        return openai_chat_completion_result(data)

    def _headers(self) -> dict[str, str]:
        return {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "accept": "application/json",
        }


def _requests_session():
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:
        raise ProviderError("requests/urllib3 are required for RunPod", retryable=False, status_code=501) from exc
    session = requests.Session()
    retry = Retry(total=0)
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


def _json_or_error(response: Any, message: str) -> dict[str, Any]:
    if response.status_code in {200, 201, 202}:
        data = response.json()
        return data if isinstance(data, dict) else {"output": data}
    retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    raise ProviderError(
        f"{message}: {response.status_code}",
        retryable=retryable,
        status_code=502 if response.status_code >= 500 else response.status_code,
    )


async def _async_cleanup_runpod_flash_resource(resource_id: str, resource_name: str | None) -> None:
    try:
        from runpod_flash.core.resources.resource_manager import ResourceManager  # type: ignore
    except ImportError:
        return
    manager = ResourceManager()
    for force in (False, True):
        try:
            result = await manager.undeploy_resource(resource_id, resource_name, force_remove=force)
            if result is None or (isinstance(result, dict) and result.get("success")):
                return
        except TypeError:
            if not force:
                await manager.undeploy_resource(resource_id, resource_name)
                return
        except Exception:
            if force:
                return


def _runpod_flash_cleanup_resource_sync(resource_id: str, resource_name: str | None = None) -> None:
    try:
        asyncio.run(_async_cleanup_runpod_flash_resource(resource_id, resource_name))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(_async_cleanup_runpod_flash_resource(resource_id, resource_name))
        finally:
            loop.close()


def _allow_runpod_flash_experimental_worker() -> bool:
    return os.getenv("GPUCALL_RUNPOD_FLASH_EXPERIMENTAL_WORKER", "").strip().lower() in {"1", "true", "yes", "on"}


class RunpodVllmServerlessAdapter(RunpodFlashAdapter):
    """RunPod official Serverless worker-vLLM endpoint."""


class RunpodVllmFlashBootAdapter(RunpodFlashAdapter):
    """RunPod Flash SDK function endpoint for live-provisioned FlashBoot jobs."""

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        if not self.api_key:
            raise ProviderError("RunPod api_key is not configured", retryable=False, status_code=401)
        if plan.mode.value == "stream":
            raise ProviderError("RunPod FlashBoot streaming is not supported in v2.0", retryable=False, status_code=400)
        if not self.model:
            raise ProviderError("RunPod FlashBoot model is not configured", retryable=False, status_code=400)
        resource_name = f"gpucall-flash-worker-{plan.plan_id}"
        return RemoteHandle(
            provider=self.name,
            remote_id=resource_name,
            expires_at=plan.expires_at(),
            meta={"resource_name": resource_name, "flash_function": True},
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        try:
            value = await asyncio.wait_for(self._run_flash(plan), timeout=max(float(plan.timeout_seconds), 300.0))
        except asyncio.TimeoutError as exc:
            raise ProviderError("RunPod FlashBoot timed out", retryable=True, status_code=504) from exc
        return gpucall_provider_result(value)


def _integer_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    usage: dict[str, int] = {}
    for key, raw in value.items():
        if isinstance(raw, bool):
            continue
        if isinstance(raw, int):
            usage[str(key)] = raw
    return usage


@register_adapter(
    "runpod-serverless",
    aliases=("runpod",),
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="runpod-serverless",
        output_contract="gpucall-provider-result",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
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


@register_adapter(
    "runpod-vllm-serverless",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
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


@register_adapter(
    "runpod-vllm-flashboot",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="gpucall-provider-result",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
    ),
)
def build_runpod_vllm_flashboot_adapter(spec, credentials):
    runpod = credentials.get("runpod", {})
    return RunpodVllmFlashBootAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
        image=spec.image,
        endpoint_contract=spec.endpoint_contract,
    )


@register_adapter(
    "runpod-flash",
    aliases=("flash",),
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
    ),
)
def build_runpod_flash_adapter(spec, credentials):
    runpod = credentials.get("runpod", {})
    return RunpodFlashAdapter(
        name=spec.name,
        api_key=runpod.get("api_key"),
        endpoint_id=spec.target,
        base_url=str(spec.endpoint) if spec.endpoint else None,
        model=spec.model,
        max_model_len=spec.max_model_len,
        image=spec.image,
        endpoint_contract=spec.endpoint_contract,
    )
