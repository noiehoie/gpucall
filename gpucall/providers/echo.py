from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle
from gpucall.providers.registry import ProviderAdapterDescriptor, register_adapter


class EchoProvider(ProviderAdapter):
    """Local deterministic provider for development and contract tests."""

    def __init__(self, name: str = "local-echo", latency_seconds: float = 0.01) -> None:
        self.name = name
        self.latency_seconds = latency_seconds
        self.cancelled: set[str] = set()

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(
            provider=self.name,
            remote_id=uuid4().hex,
            expires_at=plan.expires_at(),
            account_ref="local",
            execution_surface="local_runtime",
            resource_kind="local_invocation",
            cleanup_required=False,
            reaper_eligible=False,
        )

    async def wait(self, handle: RemoteHandle, plan: CompiledPlan) -> ProviderResult:
        if datetime.now(timezone.utc) >= handle.expires_at:
            raise ProviderError("lease expired before completion", retryable=True, status_code=504)
        await asyncio.sleep(self.latency_seconds)
        if handle.remote_id in self.cancelled:
            raise ProviderError("remote execution cancelled", retryable=False, status_code=499)
        return ProviderResult(kind="inline", value=f"ok:{plan.task}:{handle.provider}")

    async def cancel_remote(self, handle: RemoteHandle) -> None:
        self.cancelled.add(handle.remote_id)

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        yield ": heartbeat\n\n"
        await asyncio.sleep(self.latency_seconds)
        yield f"data: ok:{plan.task}:{handle.provider}\n\n"


@register_adapter(
    "echo",
    descriptor=ProviderAdapterDescriptor(
        requires_contracts=False,
        stream_contract=None,
        production_eligible=False,
        production_rejection_reason="smoke/fake provider is not eligible for production auto-routing",
        local_execution=True,
        requires_model_for_auto=False,
    ),
)
def build_echo_adapter(spec, _credentials):
    return EchoProvider(name=spec.name)
