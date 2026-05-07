from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from gpucall.domain import CompiledPlan, ProviderError


@dataclass(frozen=True)
class ResourceLease:
    provider: str
    remote_id: str
    expires_at: datetime
    account_ref: str | None = None
    execution_surface: str | None = None
    resource_kind: str = "remote_execution"
    cleanup_required: bool = True
    reaper_eligible: bool = False
    meta: dict[str, Any] = field(default_factory=dict)


RemoteHandle = ResourceLease


class ProviderAdapter(ABC):
    name: str

    @abstractmethod
    async def start(self, plan: CompiledPlan) -> RemoteHandle:
        raise NotImplementedError

    @abstractmethod
    async def wait(self, handle: RemoteHandle, plan: CompiledPlan):
        raise NotImplementedError

    @abstractmethod
    async def cancel_remote(self, handle: RemoteHandle) -> None:
        raise NotImplementedError

    async def stream(self, handle: RemoteHandle, plan: CompiledPlan):
        raise ProviderError("provider does not support streaming", retryable=False, status_code=400)
