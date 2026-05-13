from __future__ import annotations


from datetime import datetime, timezone
import os
import time
from typing import Any
from urllib.parse import urlparse

from gpucall.domain import TupleError
from gpucall.live_catalog import live_error, live_info, price_per_second_from_mapping
from gpucall.targeting import is_configured_target

RUNPOD_API_BASE = "https://api.runpod.ai/v2"


def requests_session():
    try:
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
    except ImportError as exc:
        raise TupleError("requests/urllib3 are required for RunPod", retryable=False, status_code=501) from exc
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=Retry(total=0)))
    return session


def json_or_error(response: Any, message: str) -> dict[str, Any]:
    if response.status_code in {200, 201, 202}:
        data = response.json()
        return data if isinstance(data, dict) else {"output": data}
    retryable = response.status_code in {408, 409, 425, 429, 500, 502, 503, 504}
    code = _provider_http_error_code(response.status_code, response.text)
    raise TupleError(
        f"{message}: {response.status_code}",
        retryable=retryable,
        status_code=502 if response.status_code >= 500 else response.status_code,
        code=code,
    )


def _provider_http_error_code(status_code: int, body: str | None = None) -> str | None:
    lowered = (body or "").lower()
    if status_code == 429:
        if "quota" in lowered or "spend" in lowered or "limit" in lowered:
            return "PROVIDER_QUOTA_EXCEEDED"
        return "PROVIDER_RATE_LIMITED"
    if status_code in {408, 504}:
        return "PROVIDER_TIMEOUT"
    if status_code in {409, 425}:
        return "PROVIDER_CONCURRENCY_LIMIT"
    if status_code in {502, 503}:
        if "maintenance" in lowered:
            return "PROVIDER_MAINTENANCE"
        return "PROVIDER_UPSTREAM_UNAVAILABLE"
    if status_code >= 500:
        return "PROVIDER_UPSTREAM_UNAVAILABLE"
    return None


def runpod_endpoint_catalog_findings(tuples: list[Any], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    api_key = credentials.get("runpod", {}).get("api_key")
    findings: list[dict[str, Any]] = []
    for tuple in tuples:
        if not api_key:
            findings.append(live_error(tuple, dimension="credential", reason="missing RunPod API key; cannot verify endpoint health"))
            continue
        if not is_configured_target(tuple.target):
            findings.append(live_error(tuple, dimension="endpoint", field="target", reason="RunPod endpoint target is not configured"))
            continue
        base_url = str(tuple.endpoint or RUNPOD_API_BASE).rstrip("/")
        try:
            response = requests_session().get(
                f"{base_url}/{tuple.target}/health",
                headers={"authorization": f"Bearer {api_key}", "accept": "application/json"},
                timeout=10,
            )
            if response.status_code not in {200, 201, 202}:
                findings.append(
                    live_error(
                        tuple,
                        dimension="endpoint",
                        field="target",
                        reason=f"RunPod endpoint health check did not return success: {response.status_code}",
                    )
                )
            else:
                health = response.json() if hasattr(response, "json") else {}
                stock_state = "unavailable" if runpod_vllm_health_rejection_reason(health) else "available"
                findings.append(
                    live_info(
                        tuple,
                        dimension="stock",
                        source=f"{base_url}/{tuple.target}/health",
                        live_stock_state=stock_state,
                        raw={"workers": health.get("workers")} if isinstance(health, dict) else {},
                    )
                )
        except Exception as exc:
            findings.append(
                live_error(tuple, dimension="endpoint", field="target", reason=f"RunPod endpoint health lookup failed: {exc}")
            )
            continue
        price = _runpod_endpoint_live_price(tuple, api_key, base_url)
        if price is not None:
            findings.append(
                live_info(
                    tuple,
                    dimension="price",
                    source=price["source"],
                    live_price_per_second=price["price_per_second"],
                    raw=price["raw"],
                )
            )
    return findings


def _runpod_endpoint_live_price(tuple: Any, api_key: str, base_url: str) -> dict[str, Any] | None:
    try:
        response = requests_session().get(
            f"{base_url}/endpoints",
            params={"includeWorkers": "true", "includeTemplate": "true"},
            headers={"authorization": f"Bearer {api_key}", "accept": "application/json"},
            timeout=10,
        )
        if response.status_code not in {200, 201, 202}:
            return None
        payload = response.json()
        rows = payload.get("endpoints") or payload.get("data") or payload
        if not isinstance(rows, list):
            return None
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("id") or row.get("endpointId") or row.get("name") or "") != str(tuple.target):
                continue
            price, field = price_per_second_from_mapping(row)
            if price is None:
                return None
            return {
                "price_per_second": price,
                "source": f"{base_url}/endpoints:{field}",
                "raw": {"field": field, "gpuTypeIds": row.get("gpuTypeIds"), "computeType": row.get("computeType")},
            }
    except Exception:
        return None
    return None


import asyncio
import os
from typing import Any

from gpucall.domain import CompiledPlan, TupleError, TupleResult
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.payloads import openai_chat_completion_result
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class RunpodVllmServerlessAdapter(TupleAdapter):
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
            raise TupleError("RunPod api_key is not configured", retryable=False, status_code=401)
        if not self.endpoint_id:
            raise TupleError("RunPod worker-vLLM endpoint_id is not configured", retryable=False, status_code=400)
        if not self.model:
            raise TupleError("RunPod worker-vLLM model is not configured", retryable=False, status_code=400)
        if self.endpoint_contract != "openai-chat-completions":
            raise TupleError("RunPod worker-vLLM requires endpoint_contract=openai-chat-completions", retryable=False, status_code=400)
        if plan.mode.value == "stream":
            raise TupleError("RunPod worker-vLLM streaming is not supported in v2.0", retryable=False, status_code=400)
        return RemoteHandle(
            tuple=self.name,
            remote_id=f"openai-{plan.plan_id}",
            expires_at=plan.expires_at(),
            account_ref="runpod",
            execution_surface="managed_endpoint",
            resource_kind="endpoint_request",
            cleanup_required=False,
            reaper_eligible=False,
            meta={"official_vllm": True},
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        try:
            return await asyncio.to_thread(self._call_sync, plan)
        except asyncio.TimeoutError as exc:
            raise TupleError("RunPod worker-vLLM timed out", retryable=True, status_code=504) from exc

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        return None

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        raise TupleError("RunPod worker-vLLM streaming is not supported in v2.0", retryable=False, status_code=400)

    def _call_sync(self, plan: CompiledPlan) -> TupleResult:
        if plan.input_refs and plan.task != "vision":
            raise TupleError("RunPod worker-vLLM only accepts DataRef inputs for vision", retryable=True, status_code=502)
        if not plan.messages:
            if not plan.input_refs:
                raise TupleError("RunPod worker-vLLM openai-chat-completions contract requires compiled messages", retryable=True, status_code=502)
        health = self._health_sync()
        rejection_reason = runpod_vllm_health_rejection_reason(health)
        if rejection_reason:
            raise TupleError(
                "RunPod worker-vLLM endpoint is not ready: " + rejection_reason,
                retryable=True,
                status_code=503,
                code=runpod_vllm_health_rejection_code(rejection_reason),
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

    def _messages(self, plan: CompiledPlan) -> list[dict[str, Any]]:
        if plan.input_refs:
            return self._vision_messages(plan)
        if not plan.messages:
            raise TupleError("RunPod worker-vLLM requires compiled chat messages", retryable=False, status_code=400)
        return [{"role": message.role, "content": message.content} for message in plan.messages]

    def _vision_messages(self, plan: CompiledPlan) -> list[dict[str, Any]]:
        if plan.task != "vision":
            raise TupleError("RunPod worker-vLLM only accepts DataRef inputs for vision", retryable=False, status_code=400)
        image_refs = [ref for ref in plan.input_refs if str(ref.content_type or "").lower().startswith("image/")]
        if not image_refs:
            raise TupleError("RunPod worker-vLLM vision route requires image DataRef input", retryable=False, status_code=400)

        messages: list[dict[str, Any]] = []
        prompt_parts: list[str] = []
        for message in plan.messages:
            if message.role == "system":
                messages.append({"role": "system", "content": message.content})
            elif message.content:
                prompt_parts.append(message.content)
        for key in sorted(plan.inline_inputs):
            value = plan.inline_inputs[key]
            if value.value:
                prompt_parts.append(value.value)

        content: list[dict[str, Any]] = []
        prompt = "\n".join(part for part in prompt_parts if part).strip()
        if prompt:
            content.append({"type": "text", "text": prompt})
        for ref in image_refs:
            content.append({"type": "image_url", "image_url": {"url": self._safe_image_ref_url(ref)}})
        if not content:
            raise TupleError("RunPod worker-vLLM vision route requires prompt or image content", retryable=False, status_code=400)
        messages.append({"role": "user", "content": content})
        return messages

    def _safe_image_ref_url(self, ref: Any) -> str:
        uri = str(ref.uri)
        parsed = urlparse(uri)
        if parsed.scheme not in {"http", "https"}:
            raise TupleError("RunPod worker-vLLM vision DataRefs must be gateway-presigned http(s) URLs", retryable=False, status_code=400)
        if ref.gateway_presigned is not True:
            raise TupleError("RunPod worker-vLLM vision DataRefs must be gateway-presigned", retryable=False, status_code=400)
        if not str(ref.content_type or "").lower().startswith("image/"):
            raise TupleError("RunPod worker-vLLM vision DataRefs must have image content_type", retryable=False, status_code=400)
        if ref.sha256 is None:
            raise TupleError("RunPod worker-vLLM vision DataRefs require sha256 metadata", retryable=False, status_code=400)
        max_bytes = int(os.getenv("GPUCALL_RUNPOD_VLLM_MAX_IMAGE_REF_BYTES", os.getenv("GPUCALL_WORKER_MAX_REF_BYTES", "16777216")))
        if ref.bytes is None or int(ref.bytes) <= 0:
            raise TupleError("RunPod worker-vLLM vision DataRefs require positive bytes metadata", retryable=False, status_code=400)
        if int(ref.bytes) > max_bytes:
            raise TupleError("RunPod worker-vLLM vision DataRef exceeds image byte limit", retryable=False, status_code=400)
        if ref.expires_at is not None and ref.expires_at <= datetime.now(timezone.utc):
            raise TupleError("RunPod worker-vLLM vision DataRef is expired", retryable=False, status_code=400)
        return uri

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


def runpod_vllm_health_rejection_code(reason: str | None) -> str:
    if reason is None:
        return "PROVIDER_CAPACITY_UNAVAILABLE"
    lowered = reason.lower()
    if "unhealthy" in lowered:
        return "PROVIDER_UNHEALTHY"
    if "initializing" in lowered:
        return "PROVIDER_WORKER_INITIALIZING"
    if "throttled" in lowered:
        return "PROVIDER_WORKER_THROTTLED"
    return "PROVIDER_CAPACITY_UNAVAILABLE"


def _runpod_terminal_status_code(status: str | None) -> str:
    if status == "TIMED_OUT":
        return "PROVIDER_TIMEOUT"
    if status == "CANCELLED":
        return "PROVIDER_CANCELLED"
    return "PROVIDER_JOB_FAILED"


def _queue_saturation_seconds(timeout_seconds: int | float) -> float:
    raw = os.getenv("GPUCALL_PROVIDER_QUEUE_SATURATION_SECONDS")
    if raw:
        try:
            return max(float(raw), 1.0)
        except ValueError:
            pass
    return min(max(float(timeout_seconds) * 0.1, 10.0), 60.0)


def runpod_vllm_config_findings(tuple: Any) -> list[str]:
    findings: list[str] = []
    if not tuple.target:
        findings.append(f"tuple {tuple.name!r} must declare RunPod endpoint id in target")
    if not tuple.model:
        findings.append(f"tuple {tuple.name!r} must declare deployed worker-vLLM model")
    if not tuple.image:
        findings.append(f"tuple {tuple.name!r} must declare official worker-vLLM image")
    elif not str(tuple.image).startswith("runpod/worker-v1-vllm:"):
        findings.append(f"tuple {tuple.name!r} image must be the official runpod/worker-v1-vllm image")
    input_contracts = set(tuple.input_contracts or [])
    if "data_refs" in input_contracts and "image" not in input_contracts:
        findings.append(f"tuple {tuple.name!r} official worker-vLLM path may declare DataRef support only for image vision inputs")

    worker_env = (tuple.provider_params or {}).get("worker_env")
    if not isinstance(worker_env, dict):
        findings.append(f"tuple {tuple.name!r} must declare provider_params.worker_env for official worker-vLLM deployment")
        return findings

    model_name = str(worker_env.get("MODEL_NAME") or "")
    served_name = str(worker_env.get("OPENAI_SERVED_MODEL_NAME_OVERRIDE") or "")
    declared_model = str(tuple.model or "")
    if declared_model and declared_model not in {model_name, served_name}:
        findings.append(
            f"tuple {tuple.name!r} model must match MODEL_NAME or OPENAI_SERVED_MODEL_NAME_OVERRIDE in worker_env"
        )
    try:
        max_model_len = int(worker_env.get("MAX_MODEL_LEN"))
    except (TypeError, ValueError):
        findings.append(f"tuple {tuple.name!r} worker_env.MAX_MODEL_LEN must be an integer")
    else:
        if max_model_len < int(tuple.max_model_len):
            findings.append(f"tuple {tuple.name!r} worker_env.MAX_MODEL_LEN is below tuple max_model_len")
    if "GPU_MEMORY_UTILIZATION" not in worker_env:
        findings.append(f"tuple {tuple.name!r} worker_env.GPU_MEMORY_UTILIZATION must be declared")
    if "MAX_CONCURRENCY" not in worker_env:
        findings.append(f"tuple {tuple.name!r} worker_env.MAX_CONCURRENCY must be declared")
    storage = (tuple.provider_params or {}).get("model_storage")
    if not isinstance(storage, dict):
        findings.append(f"tuple {tuple.name!r} must declare provider_params.model_storage for official worker-vLLM deployment")
        return findings
    storage_kind = str(storage.get("storage_kind") or "")
    allowed_storage = {"runpod_cached_model", "runpod_network_volume", "container_ephemeral", "baked_image"}
    if storage_kind not in allowed_storage:
        findings.append(f"tuple {tuple.name!r} provider_params.model_storage.storage_kind must be one of {sorted(allowed_storage)}")
    if storage_kind == "runpod_cached_model":
        cached_ref = str(storage.get("cached_model_ref") or "")
        if declared_model and cached_ref != declared_model:
            findings.append(f"tuple {tuple.name!r} cached_model_ref must match deployed model")
    if storage_kind in {"runpod_cached_model", "runpod_network_volume"}:
        mount_path = str(storage.get("mount_path") or "")
        if mount_path != "/runpod-volume":
            findings.append(f"tuple {tuple.name!r} RunPod model storage mount_path must be /runpod-volume")
        if str(worker_env.get("BASE_PATH") or "") != "/runpod-volume":
            findings.append(f"tuple {tuple.name!r} worker_env.BASE_PATH must be /runpod-volume for persistent RunPod model storage")
    return findings


@register_adapter(
    "runpod-vllm-serverless",
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="openai-chat-completions",
        output_contract="openai-chat-completions",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        config_validator=runpod_vllm_config_findings,
        catalog_validator=runpod_endpoint_catalog_findings,
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

from gpucall.domain import CompiledPlan, TupleError, TupleResult
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.payloads import gpucall_tuple_result, plan_payload
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter


class RunpodServerlessAdapter(TupleAdapter):
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
            raise TupleError("RunPod api_key or endpoint_id is not configured", retryable=False, status_code=401)
        if plan.mode.value == "sync":
            run_request = await asyncio.to_thread(self._runsync_sync, plan)
        else:
            run_request = await asyncio.to_thread(self._start_sync, plan)
        return RemoteHandle(
            tuple=self.name,
            remote_id=run_request["job_id"],
            expires_at=plan.expires_at(),
            account_ref="runpod",
            execution_surface="managed_endpoint",
            resource_kind="serverless_job",
            cleanup_required=True,
            reaper_eligible=False,
            meta=run_request,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
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
            raise TupleError("RunPod start response did not include job id", retryable=True, status_code=502)
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
            code = _runpod_terminal_status_code(status)
            raise TupleError(f"RunPod status: {status}", retryable=True, status_code=502, code=code)
        if "output" in data and status in {None, "COMPLETED"}:
            return {"job_id": str(data.get("id") or data.get("job_id") or f"runsync-{plan.plan_id}"), "completed_output": data["output"]}
        job_id = data.get("id") or data.get("job_id")
        if not job_id:
            raise TupleError("RunPod runsync response did not include output or job id", retryable=True, status_code=502)
        return {"job_id": str(job_id)}

    def _wait_sync(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        if "completed_output" in handle.meta:
            return gpucall_tuple_result(handle.meta["completed_output"])
        deadline = time.monotonic() + plan.timeout_seconds
        queue_seen_at: float | None = None
        queue_limit = _queue_saturation_seconds(plan.timeout_seconds)
        while time.monotonic() < deadline:
            response = requests_session().get(
                f"{self.base_url}/{self.endpoint_id}/status/{handle.remote_id}",
                headers=self._headers(),
                timeout=10,
            )
            data = json_or_error(response, "RunPod status failed")
            status = data.get("status")
            if status == "COMPLETED":
                return gpucall_tuple_result(data.get("output"))
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                raise TupleError(
                    f"RunPod status: {status}",
                    retryable=True,
                    status_code=502,
                    code=_runpod_terminal_status_code(status),
                )
            if status == "IN_QUEUE":
                queue_seen_at = queue_seen_at or time.monotonic()
                if time.monotonic() - queue_seen_at >= queue_limit:
                    raise TupleError(
                        "RunPod queue saturated",
                        retryable=True,
                        status_code=503,
                        code="PROVIDER_QUEUE_SATURATED",
                    )
            else:
                queue_seen_at = None
            time.sleep(self.poll_interval_seconds)
        raise TupleError("RunPod polling timed out", retryable=True, status_code=504, code="PROVIDER_POLL_TIMEOUT")

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
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="runpod-serverless",
        output_contract="gpucall-tuple-result",
        production_eligible=False,
        production_rejection_reason=(
            "RunPod generic Serverless queue operations are official, but the gpucall-tuple-result worker "
            "contract is custom and must not be treated as the official worker-vLLM production route"
        ),
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        catalog_validator=runpod_endpoint_catalog_findings,
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
