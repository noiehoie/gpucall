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

from gpucall.domain import ArtifactManifest, CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.payloads import plan_payload, plain_text_result
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter

_ephemeral_run_lock = threading.Lock()


def _import_modal():
    try:
        import modal  # type: ignore
    except ImportError as exc:
        raise ProviderError("modal SDK is not installed", retryable=False, status_code=501) from exc
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
        raise ProviderError("modal ephemeral app.run lock timeout", retryable=True, status_code=503)
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
        return get()


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


class ModalAdapter(ProviderAdapter):
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
                raise ProviderError(
                    "Modal requires deployed GPUCALL_MODAL_APP and GPUCALL_MODAL_FN in v2",
                    retryable=False,
                    status_code=501,
                )
        return RemoteHandle(
            provider=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="modal",
            execution_surface="function_runtime",
            resource_kind="function_invocation",
            cleanup_required=True,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        try:
            value = await asyncio.to_thread(self._invoke, plan, plan.timeout_seconds, handle.remote_id)
            if plan.artifact_export is not None:
                return ProviderResult(kind="artifact_manifest", artifact_manifest=ArtifactManifest.model_validate_json(value))
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
                from gpucall.providers.modal_worker import app, vllm_a10g_ref, vllm_t4_ref

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

    def _stream_sync(self, plan: CompiledPlan, timeout: float | None, remote_id: str) -> Iterator[str]:
        modal = _import_modal()
        to = _get_timeout(timeout)
        deadline = time.monotonic() + to
        with _modal_output(modal):
            if self.app_name:
                if not self.stream_function_name:
                    raise ProviderError("Modal stream requires explicit stream_target in provider config", retryable=False, status_code=400)
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
                    raise ProviderError("Modal stream requires deployed app in v2", retryable=False, status_code=501)
                from gpucall.providers.modal_worker import app, vllm_a10g_ref, vllm_t4_ref

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
                            raise ProviderError("Modal stream timed out", retryable=True, status_code=504)
                        yield str(chunk)
                return
            for chunk in gen:
                if time.monotonic() > deadline:
                    raise ProviderError("Modal stream timed out", retryable=True, status_code=504)
                yield str(chunk)


def modal_config_findings(provider: Any) -> list[str]:
    findings: list[str] = []
    app_name, function_name = _split_modal_target(provider.target)
    if not app_name or not function_name:
        findings.append(f"provider {provider.name!r} target must be '<modal-app>:<function>' for deployed Modal functions")
    stream_contract = str(provider.stream_contract or "none")
    if stream_contract != "none":
        stream_app_name, stream_function_name = _split_modal_target(provider.stream_target)
        if not stream_app_name or not stream_function_name:
            findings.append(f"provider {provider.name!r} stream_target must be '<modal-app>:<function>' when streaming is declared")
        elif app_name and stream_app_name != app_name:
            findings.append(f"provider {provider.name!r} stream_target must use the same Modal app as target")
    elif provider.stream_target:
        findings.append(f"provider {provider.name!r} stream_target must not be set when stream_contract is none")
    if not provider.model:
        findings.append(f"provider {provider.name!r} must declare the model served by the Modal function")
    if provider.endpoint is not None:
        findings.append(f"provider {provider.name!r} must not set endpoint for Modal SDK function invocation")
    return findings


@register_adapter(
    "modal",
    descriptor=ProviderAdapterDescriptor(
        endpoint_contract="modal-function",
        output_contract="plain-text",
        required_auto_fields={"target": "Modal function target is not configured"},
        stream_required_fields={"stream_target": "modal stream mode requires explicit stream_target"},
        config_validator=modal_config_findings,
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
