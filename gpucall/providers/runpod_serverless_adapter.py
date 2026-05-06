from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import gpucall_provider_result, plan_payload
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter
from gpucall.providers.runpod_common import RUNPOD_API_BASE, json_or_error, requests_session


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
