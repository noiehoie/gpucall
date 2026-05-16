from __future__ import annotations


import asyncio
import inspect
import os
import queue
import sys
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Iterator
from uuid import uuid4

from gpucall.domain import ArtifactManifest, CompiledPlan, ProviderErrorCode, TupleError, TupleResult
from gpucall.execution_surfaces.managed_endpoint import RUNPOD_API_BASE, _queue_saturation_seconds, json_or_error, requests_session
from gpucall.execution.base import TupleAdapter, RemoteHandle
from gpucall.execution.payloads import gpucall_tuple_result, plan_payload, plain_text_result
from gpucall.execution.registry import TupleAdapterDescriptor, register_adapter
from gpucall.live_catalog import live_error, live_info, price_per_second_from_pricing_text

_ephemeral_run_lock = threading.Lock()


def _import_modal():
    try:
        import modal  # type: ignore
    except ImportError as exc:
        raise TupleError("modal SDK is not installed", retryable=False, status_code=501) from exc
    return modal


def _lock_timeout() -> float:
    try:
        raw = os.getenv("GPUCALL_MODAL_EPHEMERAL_LOCK_TIMEOUT_SEC", "600")
        return max(float(raw), 1.0)
    except ValueError:
        return 600.0


def _get_timeout(explicit: float | None) -> float:
    if explicit is not None:
        return float(explicit)
    try:
        raw = os.getenv("GPUCALL_MODAL_TIMEOUT_SEC", "300")
        return max(float(raw), 1.0)
    except ValueError:
        return 300.0


@contextmanager
def _modal_output(modal: Any):
    try:
        params = inspect.signature(modal.enable_output).parameters
        kwargs = {"show_progress": sys.stderr.isatty()} if "show_progress" in params else {}
        ctx = modal.enable_output(**kwargs)
    except Exception:
        ctx = modal.enable_output()
    with ctx:
        yield


@contextmanager
def _ephemeral_guard(timeout: float):
    if not _ephemeral_run_lock.acquire(timeout=min(timeout, _lock_timeout())):
        raise TupleError("modal ephemeral app.run lock timeout", retryable=True, status_code=503)
    try:
        yield
    finally:
        _ephemeral_run_lock.release()


def _unwrap_invocation(invocation: Any, timeout: float) -> Any:
    get = getattr(invocation, "get", None)
    if not callable(get):
        return invocation
    try:
        return get(timeout=timeout)
    except TypeError:
        try:
            return get()
        except Exception as exc:
            mapped = _modal_exception_to_tuple_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise
    except Exception as exc:
        mapped = _modal_exception_to_tuple_error(exc)
        if mapped is not None:
            raise mapped from exc
        raise


def _modal_exception_to_tuple_error(exc: Exception) -> TupleError | None:
    exc_type = f"{exc.__class__.__module__}.{exc.__class__.__name__}"
    if exc_type == "modal.exception.ResourceExhaustedError" or exc.__class__.__name__ == "ResourceExhaustedError":
        return TupleError(
            "Modal GPU resource exhausted",
            retryable=True,
            status_code=503,
            code="PROVIDER_RESOURCE_EXHAUSTED",
        )
    return None


def _runpod_terminal_status_code(status: str | None) -> ProviderErrorCode:
    if status == "TIMED_OUT":
        return ProviderErrorCode.PROVIDER_TIMEOUT
    if status == "CANCELLED":
        return ProviderErrorCode.PROVIDER_CANCELLED
    return ProviderErrorCode.PROVIDER_JOB_FAILED


def _start_modal_call(remote: Any, *args: Any, **kwargs: Any) -> Any:
    spawn = getattr(remote, "spawn", None)
    if callable(spawn):
        return spawn(*args, **kwargs)
    return remote.remote(*args, **kwargs)


def _split_modal_target(target: str | None) -> tuple[str | None, str | None]:
    if not target:
        return None, None
    if ":" not in target:
        return target, None
    app_name, function_name = target.split(":", 1)
    return app_name or None, function_name or None


class ModalAdapter(TupleAdapter):
    """Modal adapter ported from v1, using deployed functions by default."""

    def __init__(
        self,
        name: str = "modal",
        *,
        app_name: str | None = None,
        function_name: str | None = None,
        stream_function_name: str | None = None,
        model: str | None = None,
        max_model_len: int | None = None,
        provider_params: dict[str, Any] | None = None,
        allow_ephemeral: bool = False,
    ) -> None:
        self.name = name
        self.app_name = app_name or os.getenv("GPUCALL_MODAL_APP")
        self.function_name = function_name or os.getenv("GPUCALL_MODAL_FN")
        self.stream_function_name = (
            stream_function_name
            or os.getenv("GPUCALL_MODAL_STREAM_FN")
        )
        self.model = model
        self.max_model_len = max_model_len
        self.provider_params = dict(provider_params or {})
        self.allow_ephemeral = allow_ephemeral
        self._invocations: dict[str, Any] = {}
        self._streams: dict[str, Any] = {}
        self._remote_lock = threading.Lock()

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        if not self.app_name or not self.function_name:
            if not self.allow_ephemeral:
                raise TupleError(
                    "Modal requires deployed GPUCALL_MODAL_APP and GPUCALL_MODAL_FN in v2",
                    retryable=False,
                    status_code=501,
                )
        return RemoteHandle(
            tuple=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="modal",
            execution_surface="function_runtime",
            resource_kind="function_invocation",
            cleanup_required=True,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        try:
            value = await asyncio.to_thread(self._invoke, plan, plan.timeout_seconds, handle.remote_id)
            if plan.artifact_export is not None:
                return TupleResult(kind="artifact_manifest", artifact_manifest=ArtifactManifest.model_validate_json(value))
            return plain_text_result(value)
        finally:
            with self._remote_lock:
                self._invocations.pop(handle.remote_id, None)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        with self._remote_lock:
            invocation = self._invocations.pop(handle.remote_id, None)
            stream = self._streams.pop(handle.remote_id, None)
        for remote in (stream, invocation):
            cancel = getattr(remote, "cancel", None)
            if callable(cancel):
                try:
                    await asyncio.to_thread(cancel)
                except Exception:
                    pass

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        yield ": heartbeat\n\n"
        q: queue.Queue[str | BaseException | None] = queue.Queue()

        def run() -> None:
            try:
                for chunk in self._stream_sync(plan, plan.timeout_seconds, handle.remote_id):
                    q.put(str(chunk))
            except BaseException as exc:
                q.put(exc)
            finally:
                with self._remote_lock:
                    self._streams.pop(handle.remote_id, None)
                q.put(None)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        while True:
            try:
                item = await asyncio.wait_for(asyncio.to_thread(q.get), timeout=5.0)
            except asyncio.TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if item is None:
                break
            if isinstance(item, BaseException):
                raise item
            yield f"data: {item}\n\n"

    def _invoke(self, plan: CompiledPlan, timeout: float | None, remote_id: str) -> Any:
        modal = _import_modal()
        to = _get_timeout(timeout)
        payload = plan_payload(plan)
        try:
            with _modal_output(modal):
                if self.app_name and self.function_name:
                    fn = modal.Function.from_name(self.app_name, self.function_name)
                    invocation = _start_modal_call(
                        fn,
                        payload,
                        plan.task,
                        max_model_len=self.max_model_len,
                        model=self.model,
                        **self.provider_params,
                    )
                    with self._remote_lock:
                        self._invocations[remote_id] = invocation
                    try:
                        return _unwrap_invocation(invocation, to)
                    finally:
                        with self._remote_lock:
                            self._invocations.pop(remote_id, None)
                with _ephemeral_guard(to):
                    from gpucall.worker_contracts.modal import app, vllm_a10g_ref, vllm_t4_ref

                    worker = vllm_a10g_ref if (plan.token_budget or 0) > 8192 else vllm_t4_ref
                    with app.run():
                        invocation = _start_modal_call(
                            worker.run_inference_on_modal,
                            payload,
                            plan.task,
                            max_model_len=self.max_model_len,
                            model=self.model,
                            **self.provider_params,
                        )
                        with self._remote_lock:
                            self._invocations[remote_id] = invocation
                        try:
                            return _unwrap_invocation(invocation, to)
                        finally:
                            with self._remote_lock:
                                self._invocations.pop(remote_id, None)
        except TupleError:
            raise
        except Exception as exc:
            mapped = _modal_exception_to_tuple_error(exc)
            if mapped is not None:
                raise mapped from exc
            raise

    def _stream_sync(self, plan: CompiledPlan, timeout: float | None, remote_id: str) -> Iterator[str]:
        modal = _import_modal()
        to = _get_timeout(timeout)
        deadline = time.monotonic() + to
        with _modal_output(modal):
            if self.app_name:
                if not self.stream_function_name:
                    raise TupleError("Modal stream requires explicit stream_target in tuple config", retryable=False, status_code=400)
                fn = modal.Function.from_name(self.app_name, self.stream_function_name)
                gen = fn.remote_gen(
                    plan_payload(plan),
                    plan.task,
                    max_model_len=self.max_model_len,
                    model=self.model,
                    **self.provider_params,
                )
                with self._remote_lock:
                    self._streams[remote_id] = gen
            else:
                if not self.allow_ephemeral:
                    raise TupleError("Modal stream requires deployed app in v2", retryable=False, status_code=501)
                from gpucall.worker_contracts.modal import app, vllm_a10g_ref, vllm_t4_ref

                worker = vllm_a10g_ref if (plan.token_budget or 0) > 8192 else vllm_t4_ref
                with ExitStack() as stack:
                    stack.enter_context(_ephemeral_guard(to))
                    stack.enter_context(app.run())
                    gen = worker.stream_inference_on_modal.remote_gen(
                        plan_payload(plan),
                        plan.task,
                        max_model_len=self.max_model_len,
                        model=self.model,
                        **self.provider_params,
                    )
                    with self._remote_lock:
                        self._streams[remote_id] = gen
                    for chunk in gen:
                        if time.monotonic() > deadline:
                            raise TupleError("Modal stream timed out", retryable=True, status_code=504)
                        yield str(chunk)
                return
            for chunk in gen:
                if time.monotonic() > deadline:
                    raise TupleError("Modal stream timed out", retryable=True, status_code=504)
                yield str(chunk)


def modal_config_findings(tuple: Any) -> list[str]:
    findings: list[str] = []
    app_name, function_name = _split_modal_target(tuple.target)
    if not app_name or not function_name:
        findings.append(f"tuple {tuple.name!r} target must be '<modal-app>:<function>' for deployed Modal functions")
    stream_contract = str(tuple.stream_contract or "none")
    if stream_contract != "none":
        stream_app_name, stream_function_name = _split_modal_target(tuple.stream_target)
        if not stream_app_name or not stream_function_name:
            findings.append(f"tuple {tuple.name!r} stream_target must be '<modal-app>:<function>' when streaming is declared")
        elif app_name and stream_app_name != app_name:
            findings.append(f"tuple {tuple.name!r} stream_target must use the same Modal app as target")
    elif tuple.stream_target:
        findings.append(f"tuple {tuple.name!r} stream_target must not be set when stream_contract is none")
    if not tuple.model:
        findings.append(f"tuple {tuple.name!r} must declare the model served by the Modal function")
    if tuple.endpoint is not None:
        findings.append(f"tuple {tuple.name!r} must not set endpoint for Modal SDK function invocation")
    return findings


def modal_catalog_findings(tuples: list[Any], credentials: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    del credentials
    findings: list[dict[str, Any]] = []
    try:
        modal = _import_modal()
    except TupleError as exc:
        return [live_error(tuple, dimension="credential", reason=str(exc)) for tuple in tuples]
    for tuple in tuples:
        tuple_errors = 0
        for field, target in (("target", tuple.target), ("stream_target", tuple.stream_target)):
            if field == "stream_target" and str(tuple.stream_contract or "none") == "none":
                continue
            app_name, function_name = _split_modal_target(target)
            if not app_name or not function_name:
                findings.append(live_error(tuple, dimension="endpoint", field=field, reason="Modal function target is not configured"))
                tuple_errors += 1
                continue
            try:
                fn = modal.Function.from_name(app_name, function_name)
                hydrate = getattr(fn, "hydrate", None)
                if callable(hydrate):
                    hydrate()
            except Exception as exc:
                findings.append(live_error(tuple, dimension="endpoint", field=field, reason=f"Modal deployed function lookup failed: {exc}"))
                tuple_errors += 1
        price = _modal_live_price(tuple)
        if price is not None:
            findings.append(live_info(tuple, dimension="price", source=price["source"], live_price_per_second=price["price_per_second"]))
        if tuple.target and tuple_errors == 0:
            findings.append(live_info(tuple, dimension="stock", source="modal.Function.from_name", live_stock_state="available"))
    return findings


def _modal_live_price(tuple: Any) -> dict[str, Any] | None:
    try:
        import requests

        text = requests.get("https://modal.com/pricing", timeout=10).text
    except Exception:
        return None
    gpu = str(getattr(tuple, "gpu", "") or "")
    patterns = [re_escape for re_escape in _modal_gpu_label_patterns(gpu)]
    price = price_per_second_from_pricing_text(text, patterns)
    if price is None:
        return None
    price *= _modal_gpu_count(gpu)
    return {"price_per_second": price, "source": "https://modal.com/pricing"}


def _modal_gpu_label_patterns(gpu: str) -> list[str]:
    compact = gpu.upper().replace(":", " ").replace("-", " ")
    labels = [compact]
    if "A10G" in compact:
        labels.extend(["A10G", r"NVIDIA\s+A10G", r"(?<![A-Z0-9])A10(?![A-Z0-9])", r"NVIDIA\s+A10(?![A-Z0-9])"])
    if "A100" in compact:
        labels.extend([r"(?<![A-Z0-9])A100(?![A-Z0-9])", r"NVIDIA\s+A100(?![A-Z0-9])"])
    if "H100" in compact:
        labels.extend(["H100", "NVIDIA H100"])
    if "H200" in compact:
        labels.extend(["H200", "NVIDIA H200"])
    if "L40S" in compact:
        labels.extend(["L40S", "NVIDIA L40S"])
    if compact == "L4":
        labels.extend(["L4", "NVIDIA L4"])
    if compact == "T4":
        labels.extend(["T4", "NVIDIA T4"])
    if "B200" in compact:
        labels.extend(["B200", "NVIDIA B200"])
    if "RTX PRO 6000" in compact:
        labels.extend(["RTX PRO 6000", "NVIDIA RTX PRO 6000"])
    return [label.replace(" ", r"\s+") for label in labels]


def _modal_gpu_count(gpu: str) -> int:
    if ":" not in gpu:
        return 1
    try:
        return max(1, int(gpu.rsplit(":", 1)[1]))
    except ValueError:
        return 1


@register_adapter(
    "modal",
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="modal-function",
        output_contract="plain-text",
        stream_contract=None,
        required_auto_fields={"target": "Modal function target is not configured"},
        stream_required_fields={"stream_target": "modal stream mode requires explicit stream_target"},
        config_validator=modal_config_findings,
        catalog_validator=modal_catalog_findings,
        official_sources=(
            "https://modal.com/docs/reference/modal.Function#from_name",
            "https://modal.com/docs/reference/modal.Function#remote",
            "https://modal.com/docs/reference/modal.Function#remote_gen",
            "https://modal.com/docs/reference/modal.Function#spawn",
        ),
    ),
)
def build_modal_adapter(spec, _credentials):
    app_name, function_name = _split_modal_target(spec.target)
    stream_app_name, stream_function_name = _split_modal_target(spec.stream_target)
    if stream_app_name and app_name and stream_app_name != app_name:
        raise ValueError("modal stream_target must use the same app as target")
    return ModalAdapter(
        name=spec.name,
        app_name=app_name,
        function_name=function_name,
        stream_function_name=stream_function_name,
        model=spec.model,
        max_model_len=spec.max_model_len,
        provider_params=spec.provider_params,
        allow_ephemeral=False,
    )


class RunpodVllmFlashBootAdapter(TupleAdapter):
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
            raise TupleError("RunPod api_key is not configured", retryable=False, status_code=401)
        if plan.mode.value == "stream":
            raise TupleError("RunPod FlashBoot streaming is not supported in v2.0", retryable=False, status_code=400)
        if not self.model:
            raise TupleError("RunPod FlashBoot model is not configured", retryable=False, status_code=400)
        resource_name = f"gpucall-flash-worker-{plan.plan_id}"
        endpoint_mode = bool(self.endpoint_id)
        return RemoteHandle(
            tuple=self.name,
            remote_id=resource_name,
            expires_at=plan.expires_at(),
            account_ref="runpod",
            execution_surface="function_runtime",
            resource_kind="endpoint_request" if endpoint_mode else "function_runtime",
            cleanup_required=not endpoint_mode,
            reaper_eligible=not endpoint_mode,
            meta={
                "resource_name": resource_name,
                "flash_function": True,
                "owned_resource": not endpoint_mode,
                **({"endpoint_id": self.endpoint_id} if endpoint_mode else {}),
            },
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> TupleResult:
        try:
            value = await asyncio.wait_for(self._run_flash(plan), timeout=max(float(plan.timeout_seconds), 300.0))
        except asyncio.TimeoutError as exc:
            raise TupleError("RunPod FlashBoot timed out", retryable=True, status_code=504) from exc
        return gpucall_tuple_result(value)

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        resource_id = handle.meta.get("resource_id")
        resource_name = handle.meta.get("resource_name")
        if resource_id:
            await asyncio.to_thread(runpod_flash_cleanup_resource_sync, str(resource_id), str(resource_name or ""))

    async def _run_flash(self, plan: CompiledPlan) -> Any:
        previous_env = {key: os.environ.get(key) for key in ("FLASH_SENTINEL_TIMEOUT", "FLASH_IS_LIVE_PROVISIONING", "RUNPOD_API_KEY")}
        os.environ["FLASH_SENTINEL_TIMEOUT"] = str(max(int(plan.timeout_seconds), 300))
        os.environ["FLASH_IS_LIVE_PROVISIONING"] = "true"
        if self.api_key:
            os.environ["RUNPOD_API_KEY"] = self.api_key
        payload = plan_payload(plan)
        payload["resource_name"] = f"gpucall-flash-worker-{plan.plan_id}"
        if self.model:
            payload["model"] = self.model
        if self.max_model_len:
            payload["max_model_len"] = self.max_model_len
        try:
            if self.endpoint_id:
                return await asyncio.to_thread(self._runsync_endpoint_sync, payload, plan)
            try:
                from runpod_flash import Endpoint  # type: ignore
                from runpod_flash.endpoint import EndpointJob  # type: ignore
                from gpucall.worker_contracts.runpod_flash import run_inference_on_flash
            except ImportError as exc:
                raise TupleError("runpod-flash is not installed", retryable=False, status_code=501) from exc
            value = run_inference_on_flash(payload)
            if inspect.isawaitable(value):
                value = await value
            if isinstance(value, dict) and value.get("id") and value.get("status") not in {"COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}:
                endpoint = Endpoint(id=self.endpoint_id) if self.endpoint_id else Endpoint(name="gpucall-flash-worker")
                job = EndpointJob(value, endpoint)
                await job.wait(timeout=max(float(plan.timeout_seconds), 300.0))
                if job.error:
                    raise TupleError("RunPod Flash job failed", retryable=True, status_code=502, code=ProviderErrorCode.PROVIDER_JOB_FAILED)
                return job.output
            if hasattr(value, "wait") and hasattr(value, "output"):
                await value.wait(timeout=max(float(plan.timeout_seconds), 300.0))
                if getattr(value, "error", None):
                    raise TupleError("RunPod Flash job failed", retryable=True, status_code=502, code=ProviderErrorCode.PROVIDER_JOB_FAILED)
                return value.output
            if isinstance(value, dict) and value.get("status") == "COMPLETED" and "output" in value:
                return value["output"]
            return value
        finally:
            for key, old_value in previous_env.items():
                if old_value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old_value

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
            code = _runpod_terminal_status_code(status)
            raise TupleError(f"RunPod Flash job failed: {status}", retryable=True, status_code=502, code=code)
        if status == "COMPLETED" and "output" in data:
            return data["output"]
        if "output" in data and status is None:
            return data["output"]
        job_id = data.get("id") or data.get("job_id")
        if job_id:
            return self._poll_endpoint_job_sync(str(job_id), plan)
        if "error" in data:
            raise TupleError("RunPod Flash job failed", retryable=True, status_code=502, code=ProviderErrorCode.PROVIDER_JOB_FAILED)
        return data

    def _poll_endpoint_job_sync(self, job_id: str, plan: CompiledPlan) -> Any:
        deadline = time.monotonic() + plan.timeout_seconds
        queue_seen_at: float | None = None
        queue_limit = _queue_saturation_seconds(plan.timeout_seconds)
        while time.monotonic() < deadline:
            response = requests_session().get(f"{self.base_url}/{self.endpoint_id}/status/{job_id}", headers=self._headers(), timeout=10)
            data = json_or_error(response, "RunPod Flash status failed")
            status = data.get("status")
            if status == "COMPLETED" and "output" in data:
                return data["output"]
            if status in {"FAILED", "CANCELLED", "TIMED_OUT"}:
                code = _runpod_terminal_status_code(status)
                raise TupleError(f"RunPod Flash job failed: {status}", retryable=True, status_code=502, code=code)
            if status == "IN_QUEUE":
                queue_seen_at = queue_seen_at or time.monotonic()
                if time.monotonic() - queue_seen_at >= queue_limit:
                    raise TupleError(
                        "RunPod Flash queue saturated",
                        retryable=True,
                        status_code=503,
                        code=ProviderErrorCode.PROVIDER_QUEUE_SATURATED,
                    )
            else:
                queue_seen_at = None
            time.sleep(2.0)
        raise TupleError("RunPod Flash polling timed out", retryable=True, status_code=504, code=ProviderErrorCode.PROVIDER_POLL_TIMEOUT)

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
    descriptor=TupleAdapterDescriptor(
        endpoint_contract="runpod-flash-sdk",
        output_contract="gpucall-tuple-result",
        required_auto_fields={"target": "RunPod endpoint target is not configured"},
        official_sources=(
            "https://docs.runpod.io/serverless/endpoints/send-requests",
            "https://github.com/runpod/runpod-python",
        ),
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
