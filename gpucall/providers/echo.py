from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import uuid4

from gpucall.domain import CompiledPlan, ProviderError, ProviderResult
from gpucall.providers.base import ProviderAdapter, RemoteHandle


class EchoProvider(ProviderAdapter):
    """Local deterministic provider for development and contract tests."""

    def __init__(self, name: str = "local-echo", latency_seconds: float = 0.01) -> None:
        self.name = name
        self.latency_seconds = latency_seconds
        self.cancelled: set[str] = set()

    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        return RemoteHandle(provider=self.name, remote_id=uuid4().hex, expires_at=plan.expires_at())

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
