from __future__ import annotations


from datetime import datetime, timezone
import os
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from gpucall.domain import TupleError
from gpucall.live_catalog import live_error, live_info, price_per_second_from_mapping
from gpucall.targeting import is_configured_target

RUNPOD_API_BASE = "https://api.runpod.ai/v2"
RUNPOD_REST_API_BASE = "https://rest.runpod.io/v1"
RUNPOD_SERVERLESS_BILLING_GUARD_CHECK = "runpod_serverless_billing_guard"


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


def _request_get(url: str, *, error_message: str, **kwargs: Any) -> Any:
    try:
        return requests_session().get(url, **kwargs)
    except Exception as exc:
        mapped = _request_exception_to_tuple_error(exc, error_message)
        if mapped is not None:
            raise mapped from exc
        raise


def _request_post(url: str, *, error_message: str, **kwargs: Any) -> Any:
    try:
        return requests_session().post(url, **kwargs)
    except Exception as exc:
        mapped = _request_exception_to_tuple_error(exc, error_message)
        if mapped is not None:
            raise mapped from exc
        raise


def _request_exception_to_tuple_error(exc: Exception, message: str) -> TupleError | None:
    exc_name = exc.__class__.__name__.lower()
    exc_module = exc.__class__.__module__
    if "timeout" in exc_name:
        return TupleError(f"{message}: timeout", retryable=True, status_code=504, code="PROVIDER_TIMEOUT")
    if exc_module.startswith("requests") or exc_module.startswith("urllib3"):
        return TupleError(
            f"{message}: provider request failed",
            retryable=True,
            status_code=503,
            code="PROVIDER_UPSTREAM_UNAVAILABLE",
        )
    return None


def runpod_endpoint_catalog_findings(tuples: list[Any], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    api_key = credentials.get("runpod", {}).get("api_key")
    findings: list[dict[str, Any]] = []
    inventory_rows: list[dict[str, Any]] | None = None
    for tuple in tuples:
        if not api_key:
            findings.append(live_error(tuple, dimension="credential", reason="missing RunPod API key; cannot verify endpoint health"))
            continue
        if not is_configured_target(tuple.target):
            findings.append(live_error(tuple, dimension="endpoint", field="target", reason="RunPod endpoint target is not configured"))
            continue
        base_url = str(tuple.endpoint or RUNPOD_API_BASE).rstrip("/")
        inventory_base_url = RUNPOD_REST_API_BASE
        health: dict[str, Any] | None = None
        if inventory_rows is None:
            try:
                inventory_rows = _runpod_endpoint_live_inventory_rows(api_key, inventory_base_url)
            except TupleError as exc:
                findings.append(
                    live_error(
                        tuple,
                        dimension="endpoint",
                        field="runpod_endpoint_inventory",
                        reason=str(exc),
                        source=f"{inventory_base_url}/endpoints",
                    )
                )
                continue
        endpoint = _runpod_endpoint_live_inventory_row(tuple, api_key, inventory_base_url, rows=inventory_rows)
        if endpoint is None:
            findings.append(
                live_error(
                    tuple,
                    dimension="endpoint",
                    field="runpod_endpoint_inventory",
                    reason="configured RunPod endpoint was not present in live endpoint inventory",
                    source=f"{inventory_base_url}/endpoints",
                )
            )
            continue
        blocked_by_billing_guard = False
        for blocker in runpod_serverless_billing_guard_findings(tuple, endpoint=endpoint):
            blocked_by_billing_guard = True
            findings.append(
                live_error(
                    tuple,
                    dimension="cost",
                    field=str(blocker.get("check") or RUNPOD_SERVERLESS_BILLING_GUARD_CHECK),
                    reason=str(blocker.get("reason") or "RunPod Serverless billing guard blocked this endpoint"),
                    source=f"{inventory_base_url}/endpoints",
                    raw={
                        "endpoint_id": blocker.get("endpoint_id"),
                        "workers_min": blocker.get("workers_min"),
                        "workers_max": blocker.get("workers_max"),
                        "active_workers": blocker.get("active_workers"),
                        "active_pods": blocker.get("active_pods"),
                        "live_reason": blocker.get("live_reason"),
                    },
                )
            )
        if blocked_by_billing_guard:
            price = _runpod_endpoint_live_price(tuple, api_key, inventory_base_url, endpoint=endpoint)
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
            continue
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
        if endpoint is not None:
            for blocker in runpod_serverless_billing_guard_findings(tuple, endpoint=endpoint, health=health):
                findings.append(
                    live_error(
                        tuple,
                        dimension="endpoint",
                        field=str(blocker.get("check") or RUNPOD_SERVERLESS_BILLING_GUARD_CHECK),
                        reason=str(blocker.get("reason") or "RunPod Serverless billing guard blocked this endpoint"),
                        source=f"{inventory_base_url}/endpoints",
                        raw={
                            "endpoint_id": blocker.get("endpoint_id"),
                            "workers_min": blocker.get("workers_min"),
                            "workers_max": blocker.get("workers_max"),
                            "active_workers": blocker.get("active_workers"),
                            "active_pods": blocker.get("active_pods"),
                            "live_reason": blocker.get("live_reason"),
                        },
                    )
                )
        price = _runpod_endpoint_live_price(tuple, api_key, inventory_base_url, endpoint=endpoint)
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


def _runpod_endpoint_live_inventory_rows(api_key: str, base_url: str) -> list[dict[str, Any]]:
    try:
        session = requests_session()
        url = f"{base_url}/endpoints"
        params: dict[str, str] | None = {"includeWorkers": "true", "includeTemplate": "true"}
        rows: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        while url:
            if url in seen_urls:
                raise TupleError("RunPod endpoint inventory pagination loop", retryable=True, status_code=502)
            if len(seen_urls) >= 100:
                raise TupleError("RunPod endpoint inventory pagination exceeded 100 pages", retryable=True, status_code=502)
            seen_urls.add(url)
            response = session.get(
                url,
                params=params,
                headers={"authorization": f"Bearer {api_key}", "accept": "application/json"},
                timeout=10,
            )
            params = None
            if response.status_code not in {200, 201, 202}:
                raise TupleError(f"RunPod endpoint inventory failed: {response.status_code}", retryable=True, status_code=502)
            payload = response.json()
            rows.extend(_runpod_inventory_rows(payload))
            url = _runpod_inventory_next_url(payload, current_url=url)
        return rows
    except TupleError:
        raise
    except Exception as exc:
        raise TupleError(
            f"RunPod endpoint inventory lookup failed: {type(exc).__name__}: {exc}",
            retryable=True,
            status_code=502,
            code="PROVIDER_UPSTREAM_UNAVAILABLE",
        ) from exc


def _runpod_inventory_rows(payload: object) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw = payload.get("endpoints") or payload.get("data") or payload.get("items") or payload.get("results")
    else:
        raw = payload
    if not isinstance(raw, list):
        return []
    return [row for row in raw if isinstance(row, dict)]


def _runpod_inventory_next_url(payload: object, *, current_url: str) -> str:
    if not isinstance(payload, dict):
        return ""
    for key in ("next", "nextUrl", "next_url"):
        raw = payload.get(key)
        if isinstance(raw, str) and raw.strip():
            return urljoin(current_url, raw.strip())
    links = payload.get("links")
    raw = links.get("next") if isinstance(links, dict) else None
    return urljoin(current_url, raw.strip()) if isinstance(raw, str) and raw.strip() else ""


def _append_vision_text_content(parts: list[str], content: Any) -> None:
    if content is None:
        return
    if isinstance(content, str):
        if content:
            parts.append(content)
        return
    if isinstance(content, list):
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "input_text"} and isinstance(item.get("text"), str):
                parts.append(item["text"])
                continue
            raise TupleError("RunPod worker-vLLM vision route only accepts text message parts plus DataRef images", retryable=False, status_code=400)
        return
    raise TupleError("RunPod worker-vLLM vision route received invalid message content", retryable=False, status_code=400)


def _runpod_endpoint_live_inventory_row(tuple: Any, api_key: str, base_url: str, *, rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    try:
        candidate_rows = rows if rows is not None else _runpod_endpoint_live_inventory_rows(api_key, base_url)
        for row in candidate_rows:
            endpoint_id = str(row.get("id") or row.get("endpointId") or row.get("endpoint_id") or "")
            if endpoint_id != str(tuple.target):
                continue
            return row
    except Exception:
        return None
    return None


def _runpod_endpoint_live_price(tuple: Any, api_key: str, base_url: str, *, endpoint: dict[str, Any] | None = None) -> dict[str, Any] | None:
    row = endpoint if endpoint is not None else _runpod_endpoint_live_inventory_row(tuple, api_key, base_url)
    if row is None:
        return None
    price, field = price_per_second_from_mapping(row)
    if price is None:
        return None
    return {
        "price_per_second": price,
        "source": f"{base_url}/endpoints:{field}",
        "raw": {"field": field, "gpuTypeIds": row.get("gpuTypeIds"), "computeType": row.get("computeType")},
    }


import asyncio
import os
from typing import Any

from gpucall.domain import CompiledPlan, TupleError, TupleResult
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.payloads import gpucall_tuple_result, openai_chat_payload_from_plan
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
        self.endpoint_id = endpoint_id or os.getenv("GPUCALL_RUNPOD_ENDPOINT_ID", "")
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
        if not plan.messages and not plan.inline_inputs and not plan.input_refs:
            raise TupleError("RunPod worker-vLLM openai-chat-completions contract requires messages, inline inputs, or input refs", retryable=True, status_code=502)
        health = self._health_sync()
        rejection_reason = runpod_vllm_health_preflight_rejection_reason(health, mode=plan.mode.value)
        if rejection_reason:
            raise TupleError(
                "RunPod worker-vLLM endpoint is not ready: " + rejection_reason,
                retryable=True,
                status_code=503,
                code=runpod_vllm_health_rejection_code(rejection_reason),
            )
        response = _request_post(
            f"{self.base_url}/{self.endpoint_id}/openai/v1/chat/completions",
            error_message="RunPod worker-vLLM OpenAI chat completions failed",
            headers=self._headers(),
            json=self._payload(plan),
            timeout=max(float(plan.timeout_seconds), 1.0),
        )
        return openai_chat_completion_result(json_or_error(response, "RunPod worker-vLLM OpenAI chat completions failed"))

    def _payload(self, plan: CompiledPlan) -> dict[str, Any]:
        messages = self._messages(plan) if plan.input_refs else None
        return openai_chat_payload_from_plan(plan, model=self.model, stream=False, messages=messages)

    def _messages(self, plan: CompiledPlan) -> list[dict[str, Any]]:
        if plan.input_refs:
            return self._vision_messages(plan)
        if not plan.messages:
            raise TupleError("RunPod worker-vLLM requires compiled chat messages", retryable=False, status_code=400)

        messages: list[dict[str, Any]] = []
        for message in plan.messages:
            m: dict[str, Any] = {"role": message.role}
            if message.content is not None:
                m["content"] = message.content
            if message.name is not None:
                m["name"] = message.name
            if message.tool_calls is not None:
                m["tool_calls"] = message.tool_calls
            if message.tool_call_id is not None:
                m["tool_call_id"] = message.tool_call_id
            if message.function_call is not None:
                m["function_call"] = message.function_call
            messages.append(m)
        return messages

    def _vision_messages(self, plan: CompiledPlan) -> list[dict[str, Any]]:
        if plan.task != "vision":
            raise TupleError("RunPod worker-vLLM only accepts DataRef inputs for vision", retryable=False, status_code=400)
        image_refs = [ref for ref in plan.input_refs if str(ref.content_type or "").lower().startswith("image/")]
        non_image_refs = [ref for ref in plan.input_refs if not str(ref.content_type or "").lower().startswith("image/")]
        if non_image_refs:
            raise TupleError("RunPod worker-vLLM vision route only accepts image DataRef inputs", retryable=False, status_code=400)
        if not image_refs:
            raise TupleError("RunPod worker-vLLM vision route requires image DataRef input", retryable=False, status_code=400)

        messages: list[dict[str, Any]] = []
        for message in plan.messages:
            if message.role in {"system", "developer"}:
                messages.append({"role": "system", "content": message.content})
            elif message.role == "user":
                prompt_parts: list[str] = []
                _append_vision_text_content(prompt_parts, message.content)
                if prompt_parts:
                    messages.append({"role": "user", "content": [{"type": "text", "text": part} for part in prompt_parts]})
            else:
                item = message.model_dump(mode="json", exclude_none=True)
                messages.append(item)

        tail_content: list[dict[str, Any]] = []
        for key in sorted(plan.inline_inputs):
            value = plan.inline_inputs[key]
            if value.value:
                tail_content.append({"type": "text", "text": str(value.value)})
        for ref in image_refs:
            tail_content.append({"type": "image_url", "image_url": {"url": self._safe_image_ref_url(ref)}})
        if not tail_content:
            raise TupleError("RunPod worker-vLLM vision route requires prompt or image content", retryable=False, status_code=400)
        if messages and messages[-1].get("role") == "user" and isinstance(messages[-1].get("content"), list):
            messages[-1]["content"].extend(tail_content)
        else:
            messages.append({"role": "user", "content": tail_content})
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
        response = _request_get(
            f"{self.base_url}/{self.endpoint_id}/health",
            error_message="RunPod worker-vLLM health check failed",
            headers=self._headers(),
            timeout=30,
        )
        if response.status_code == 404 and self.endpoint_contract == "openai-chat-completions":
            models_response = _request_get(
                f"{self.base_url}/{self.endpoint_id}/openai/v1/models",
                error_message="RunPod worker-vLLM OpenAI models preflight failed",
                headers=self._headers(),
                timeout=30,
            )
            models = json_or_error(models_response, "RunPod worker-vLLM OpenAI models preflight failed")
            return {
                "workers": {"idle": 1, "initializing": 0, "ready": 1, "running": 0, "throttled": 0, "unhealthy": 0},
                "health_probe": "openai_models_fallback",
                "models": models,
            }
        return json_or_error(response, "RunPod worker-vLLM health check failed")


def runpod_vllm_health_rejection_reason(health: dict[str, Any]) -> str | None:
    workers = health.get("workers") if isinstance(health, dict) else None
    if not isinstance(workers, dict):
        return "health response did not include workers"
    ready = _positive_int_from_mapping(workers, "ready")
    running = _positive_int_from_mapping(workers, "running")
    idle = _positive_int_from_mapping(workers, "idle")
    initializing = _positive_int_from_mapping(workers, "initializing")
    throttled = _positive_int_from_mapping(workers, "throttled")
    unhealthy = _positive_int_from_mapping(workers, "unhealthy")
    if unhealthy > 0:
        return "workers.unhealthy is non-zero"
    if idle + ready + running > 0:
        return None
    if initializing > 0:
        return "workers are still initializing"
    if throttled > 0:
        return "workers are throttled and no ready worker is available"
    return "no ready worker is available"


def runpod_vllm_health_preflight_rejection_reason(health: dict[str, Any], *, mode: str = "sync") -> str | None:
    workers = health.get("workers") if isinstance(health, dict) else None
    if not isinstance(workers, dict):
        return "health response did not include workers"
    unhealthy = _positive_int_from_mapping(workers, "unhealthy")
    if unhealthy > 0:
        return "workers.unhealthy is non-zero"
    if mode == "async":
        return None
    return runpod_vllm_health_rejection_reason(health)


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


def _runpod_vllm_runsync_result(data: dict[str, Any]) -> TupleResult:
    status = data.get("status")
    if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
        raise TupleError(
            f"RunPod worker-vLLM status: {status}",
            retryable=True,
            status_code=502,
            code=_runpod_terminal_status_code(status),
        )
    if status not in {None, "COMPLETED"}:
        raise TupleError(
            f"RunPod worker-vLLM unexpected status: {status}",
            retryable=True,
            status_code=503,
            code="PROVIDER_QUEUE_SATURATED" if status == "IN_QUEUE" else "PROVIDER_JOB_FAILED",
        )
    output = data.get("output")
    if isinstance(output, list) and output and isinstance(output[0], dict):
        first = output[0]
        if "choices" in first:
            choices = first.get("choices")
            if isinstance(choices, list):
                normalized_choices: list[dict[str, Any]] = []
                for choice in choices:
                    if not isinstance(choice, dict):
                        continue
                    if "message" in choice:
                        normalized_choices.append(choice)
                        continue
                    tokens = choice.get("tokens")
                    if isinstance(tokens, list):
                        content = "".join(str(token) for token in tokens)
                    else:
                        content = str(choice.get("text") or choice.get("content") or "")
                    normalized_choices.append({"message": {"role": "assistant", "content": content}})
                if normalized_choices:
                    payload = {"choices": normalized_choices}
                    if isinstance(first.get("usage"), dict):
                        payload["usage"] = first["usage"]
                    return openai_chat_completion_result(payload)
    if isinstance(output, dict) and "choices" in output:
        return openai_chat_completion_result(output)
    return gpucall_tuple_result(output)


def _positive_int_from_mapping(mapping: dict[str, Any], *keys: str) -> int:
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        try:
            return max(int(value), 0)
        except (TypeError, ValueError):
            continue
    return 0


def runpod_serverless_billing_guard_summary(
    tuple: Any | None = None,
    *,
    endpoint: dict[str, Any],
    health: dict[str, Any] | None = None,
) -> dict[str, int | bool]:
    workers_min = _positive_int_from_mapping(endpoint, "workersMin", "workers_min", "minWorkers", "min_workers")
    workers_max = _positive_int_from_mapping(endpoint, "workersMax", "workers_max", "maxWorkers", "max_workers")
    active_workers = max(_runpod_active_worker_count(endpoint), _runpod_active_worker_count(health or {}))
    active_pods = _runpod_active_pod_count(endpoint)
    standing_spend_observed = workers_min > 0
    standing_spend_approved = standing_spend_observed and not _standing_worker_approval_findings(tuple, workers_min=workers_min)
    active_pods_approved = active_pods > 0 and active_pods <= workers_min and standing_spend_approved

    return {
        "workers_min": workers_min,
        "workers_max": workers_max,
        "active_workers": active_workers,
        "active_pods": active_pods,
        "standing_spend_approved": standing_spend_approved,
        "active_pods_approved": active_pods_approved,
        "live_blocked": bool((standing_spend_observed and not standing_spend_approved) or (active_pods > 0 and not active_pods_approved)),
    }


def runpod_serverless_billing_guard_findings(
    tuple: Any,
    *,
    endpoint: dict[str, Any],
    health: dict[str, Any] | None = None,
) -> list[dict[str, object]]:
    summary = runpod_serverless_billing_guard_summary(tuple, endpoint=endpoint, health=health)
    endpoint_id = str(endpoint.get("id") or endpoint.get("endpointId") or endpoint.get("endpoint_id") or getattr(tuple, "target", "") or "")
    base = {
        "check": RUNPOD_SERVERLESS_BILLING_GUARD_CHECK,
        "tuple": getattr(tuple, "name", ""),
        "endpoint_id": endpoint_id,
        "workers_min": summary["workers_min"],
        "workers_max": summary["workers_max"],
        "active_workers": summary["active_workers"],
        "active_pods": summary["active_pods"],
    }
    findings: list[dict[str, object]] = []
    approval_findings = _standing_worker_approval_findings(tuple, workers_min=int(summary["workers_min"]))
    if int(summary["workers_min"]) > 0 and approval_findings:
        findings.append(
            {
                **base,
                "live_reason": "workers_min_positive",
                "reason": "live RunPod Serverless endpoint has workersMin > 0 without explicit standing cost approval: " + "; ".join(approval_findings),
                "severity": "error",
            }
        )
    if int(summary["active_pods"]) > 0 and not bool(summary["active_pods_approved"]):
        active_reason = (
            "live RunPod Serverless endpoint has active pods under an unapproved warm pool: " + "; ".join(approval_findings)
            if approval_findings
            else "live RunPod Serverless endpoint has active pods remaining outside the approved workersMin warm pool"
        )
        findings.append(
            {
                **base,
                "live_reason": "active_pods_present",
                "reason": active_reason,
                "severity": "error",
            }
        )
    return findings


def _runpod_active_worker_count(payload: dict[str, Any]) -> int:
    for key in ("activeWorkers", "active_workers", "workers"):
        value = payload.get(key)
        if isinstance(value, list):
            return sum(1 for item in value if _runpod_worker_entry_active(item))
        if isinstance(value, dict):
            total = 0
            for nested_key in ("running", "ready", "initializing", "throttled", "unhealthy"):
                try:
                    total += int(value.get(nested_key) or 0)
                except (TypeError, ValueError):
                    pass
            return total
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                pass
    return 0


def _runpod_worker_entry_active(worker: object) -> bool:
    if not isinstance(worker, dict):
        return True
    status = str(
        worker.get("desiredStatus")
        or worker.get("desired_status")
        or worker.get("status")
        or worker.get("state")
        or ""
    ).strip().lower()
    if status in {"exited", "exit", "stopped", "terminated", "deleted", "dead"}:
        return False
    return True


def _runpod_active_pod_count(endpoint: dict[str, Any]) -> int:
    for key in ("activePods", "active_pods", "pods"):
        value = endpoint.get(key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, dict):
            total = 0
            for nested_key in ("running", "ready", "initializing", "active"):
                try:
                    total += int(value.get(nested_key) or 0)
                except (TypeError, ValueError):
                    pass
            return total
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                pass
    return 0


def _standing_worker_approval_findings(tuple: Any, *, workers_min: int) -> list[str]:
    if workers_min <= 0:
        return []
    findings: list[str] = []
    tuple_name = getattr(tuple, "name", "<unknown>")
    if getattr(tuple, "standing_cost_per_second", None) is None:
        findings.append(f"tuple {tuple_name!r} warm RunPod workers require standing_cost_per_second")
    if getattr(tuple, "standing_cost_window_seconds", None) is None:
        findings.append(f"tuple {tuple_name!r} warm RunPod workers require standing_cost_window_seconds")
    approval = (getattr(tuple, "provider_params", None) or {}).get("cost_approval")
    if not isinstance(approval, dict) or approval.get("standing_workers_approved") is not True:
        findings.append(f"tuple {tuple_name!r} warm RunPod workers require provider_params.cost_approval.standing_workers_approved=true")
        return findings
    if not str(approval.get("approved_by") or "").strip():
        findings.append(f"tuple {tuple_name!r} warm RunPod worker approval requires provider_params.cost_approval.approved_by")
    if not str(approval.get("approved_at") or "").strip():
        findings.append(f"tuple {tuple_name!r} warm RunPod worker approval requires provider_params.cost_approval.approved_at")
    if not str(approval.get("reason") or "").strip():
        findings.append(f"tuple {tuple_name!r} warm RunPod worker approval requires provider_params.cost_approval.reason")
    return findings


def runpod_warm_worker_config_findings(tuple: Any) -> list[str]:
    endpoint_runtime = (tuple.provider_params or {}).get("endpoint_runtime")
    if not isinstance(endpoint_runtime, dict):
        endpoint_runtime = tuple.provider_params or {}
    workers_min = _positive_int_from_mapping(endpoint_runtime, "workersMin", "workers_min", "minWorkers", "min_workers")
    return _standing_worker_approval_findings(tuple, workers_min=workers_min)


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
    findings.extend(runpod_warm_worker_config_findings(tuple))
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
        response = _request_post(
            f"{self.base_url}/{self.endpoint_id}/run",
            error_message="RunPod start failed",
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
        response = _request_post(
            f"{self.base_url}/{self.endpoint_id}/runsync",
            error_message="RunPod runsync failed",
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
            response = _request_get(
                f"{self.base_url}/{self.endpoint_id}/status/{handle.remote_id}",
                error_message="RunPod status failed",
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
        response = _request_post(
            f"{self.base_url}/{self.endpoint_id}/cancel/{job_id}",
            error_message="RunPod cancel failed",
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
        config_validator=runpod_warm_worker_config_findings,
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
