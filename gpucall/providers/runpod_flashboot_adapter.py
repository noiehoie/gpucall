from __future__ import annotations

import asyncio
import inspect
import os
import time
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import gpucall_provider_result, plan_payload
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter
from gpucall.providers.runpod_common import RUNPOD_API_BASE, json_or_error, requests_session


class RunpodVllmFlashBootAdapter(ProviderAdapter):
    """RunPod Flash SDK function endpoint for live-provisioned FlashBoot jobs."""

    def __init__(
        self,
        name: str = "runpod-vllm-flashboot",
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
            raise ProviderError("RunPod FlashBoot streaming is not supported in v2.0", retryable=False, status_code=400)
        if not self.model:
            raise ProviderError("RunPod FlashBoot model is not configured", retryable=False, status_code=400)
        resource_name = f"gpucall-flash-worker-{plan.plan_id}"
        return RemoteHandle(
            provider=self.name,
            remote_id=resource_name,
            expires_at=plan.expires_at(),
            account_ref="runpod",
            execution_surface="function_runtime",
            resource_kind="function_runtime",
            cleanup_required=True,
            reaper_eligible=True,
            meta={"resource_name": resource_name, "flash_function": True},
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        try:
            value = await asyncio.wait_for(self._run_flash(plan), timeout=max(float(plan.timeout_seconds), 300.0))
        except asyncio.TimeoutError as exc:
            raise ProviderError("RunPod FlashBoot timed out", retryable=True, status_code=504) from exc
        return gpucall_provider_result(value)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        resource_id = handle.meta.get("resource_id")
        resource_name = handle.meta.get("resource_name")
        if resource_id:
            await asyncio.to_thread(runpod_flash_cleanup_resource_sync, str(resource_id), str(resource_name or ""))

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

    def _runsync_endpoint_sync(self, payload: dict[str, Any], plan: CompiledPlan) -> Any:
        response = requests_session().post(
            f"{self.base_url}/{self.endpoint_id}/runsync",
            headers=self._headers(),
            json={"input": payload},
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        return self._extract_runsync_output(json_or_error(response, "RunPod Flash runsync failed"), plan)

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
        deadline = time.monotonic() + plan.timeout_seconds
        while time.monotonic() < deadline:
            response = requests_session().get(f"{self.base_url}/{self.endpoint_id}/status/{job_id}", headers=self._headers(), timeout=10)
            data = json_or_error(response, "RunPod Flash status failed")
            status = data.get("status")
            if status == "COMPLETED" and "output" in data:
                return data["output"]
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                code = "PROVIDER_TIMEOUT" if status == "TIMED_OUT" else "PROVIDER_JOB_FAILED"
                raise ProviderError(f"RunPod Flash job failed: {status}", retryable=status == "TIMED_OUT", status_code=502, code=code)
            time.sleep(2.0)
        raise ProviderError("RunPod Flash polling timed out", retryable=True, status_code=504)

    def _headers(self) -> dict[str, str]:
        return {"authorization": f"Bearer {self.api_key}", "content-type": "application/json", "accept": "application/json"}


async def async_cleanup_runpod_flash_resource(resource_id: str, resource_name: str | None) -> None:
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


def runpod_flash_cleanup_resource_sync(resource_id: str, resource_name: str | None = None) -> None:
    try:
        asyncio.run(async_cleanup_runpod_flash_resource(resource_id, resource_name))
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(async_cleanup_runpod_flash_resource(resource_id, resource_name))
        finally:
            loop.close()


@register_adapter(
    "runpod-vllm-flashboot",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="runpod-flash-sdk",
        output_contract="gpucall-provider-result",
        production_eligible=False,
        production_rejection_reason="RunPod FlashBoot uses the runpod-flash SDK path and is not the official worker-vLLM OpenAI endpoint",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        official_sources=("https://docs.runpod.io/serverless/endpoints/send-requests",),
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
